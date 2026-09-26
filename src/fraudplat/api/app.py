"""Scoring service HTTP application.

Status codes: 201 new decision, 200 idempotent replay, 409 conflicting payload for an existing
transaction id, 503 database unreachable (durability not confirmed; retrying with the same
payload is safe) or scorer not ready (model incompatible with the feature code), 500 database
rejected the write (rolled back). Request payloads are never logged.

With `FRAUD_MODEL_PATH` set, the app loads the integrity-checked model and policy artifacts and
reads features from Redis; a policy derived for a different model version stops startup.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from fraudplat.config import Settings
from fraudplat.contracts import DecisionRecord, DecisionRequest, DecisionResponse
from fraudplat.features.redis_state import RedisFeatureState
from fraudplat.observability import RequestTimer, monitor_event_loop, stage
from fraudplat.pipeline.status import PipelineMonitor
from fraudplat.pipeline.streams import Stream
from fraudplat.policy import SCAFFOLD_POLICY, DecisionPolicy, load_policy
from fraudplat.registry import resolve
from fraudplat.scoring import Scorer
from fraudplat.scoring.model_scorer import ModelScorer
from fraudplat.scoring.scaffold import ScaffoldScorer
from fraudplat.service import (
    DecisionService,
    IdempotencyConflict,
    PipelineRejecting,
    ScorerNotReady,
    to_record,
    utc_now,
)
from fraudplat.storage.decisions import DecisionStore, PersistenceFailed, PersistenceUnavailable
from fraudplat.training.artifacts import load_any_model

logger = logging.getLogger("fraudplat.api")


def create_app(
    settings: Settings | None = None,
    scorer: Scorer | None = None,
    policy: DecisionPolicy | None = None,
    pipeline_redis: Redis | None = None,
    monitor_pipeline: bool | None = None,
) -> FastAPI:
    """`monitor_pipeline` defaults to True when a model is loaded from settings (a deployment)
    and False otherwise; tests pass it explicitly with `pipeline_redis`."""
    settings = settings or Settings()  # populated from FRAUD_* env / .env
    redis_client: Redis | None = None
    registry_ref: str | None = None
    model_source: dict[str, Any] = {}
    if scorer is None and settings.model_uri is not None:
        if settings.model_path is not None:
            raise RuntimeError("set either FRAUD_MODEL_URI or FRAUD_MODEL_PATH, not both")
        resolved = resolve(
            settings.mlflow_tracking_uri,
            settings.model_uri,
            settings.model_cache_dir,
            allow_cache=settings.model_cache_fallback,
        )
        settings = settings.model_copy(
            update={"model_path": resolved.model_path, "policy_path": resolved.policy_path}
        )
        registry_ref = resolved.registry_ref
        model_source = {
            "uri": settings.model_uri,
            "registry_ref": resolved.registry_ref,
            "source": resolved.source,
        }
    if scorer is None and settings.model_path is not None:
        if settings.policy_path is None or settings.redis_url is None:
            raise RuntimeError("FRAUD_MODEL_PATH requires FRAUD_POLICY_PATH and FRAUD_REDIS_URL")
        redis_client = Redis.from_url(
            settings.redis_url.get_secret_value(),
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        scorer = ModelScorer(
            load_any_model(settings.model_path),
            RedisFeatureState(redis_client, settings.feature_namespace),
            read_timeout_s=settings.feature_read_timeout_ms / 1000,
        )
        policy = policy or load_policy(settings.policy_path)
    monitoring = monitor_pipeline if monitor_pipeline is not None else redis_client is not None
    status_redis = pipeline_redis or redis_client
    scorer = scorer or ScaffoldScorer()
    policy = policy or SCAFFOLD_POLICY
    if policy.derived_for_model is not None and policy.derived_for_model != scorer.model_version:
        raise RuntimeError(
            f"policy {policy.version} was derived for {policy.derived_for_model}, "
            f"not {scorer.model_version}"
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
            settings.database_url.get_secret_value(),
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            timeout=settings.db_acquire_timeout_s,
            kwargs={
                "connect_timeout": 2,
                "options": (
                    f"-c timezone=UTC -c statement_timeout={settings.db_statement_timeout_ms}"
                ),
            },
            open=False,
        )
        # wait=False: the service starts even if Postgres is down; /readyz reports it.
        await pool.open(wait=False)
        store = DecisionStore(pool, stream=settings.feature_namespace)
        app.state.store = store
        monitor: PipelineMonitor | None = None
        stop = asyncio.Event()
        task: asyncio.Task[None] | None = None
        if monitoring:
            monitor = PipelineMonitor(
                store, status_redis, Stream(settings.feature_namespace), settings
            )
            await monitor.refresh()
            task = asyncio.create_task(monitor.run(stop))
        app.state.pipeline = monitor
        lag_task = asyncio.create_task(monitor_event_loop(stop))
        app.state.service = DecisionService(
            store, scorer, policy, pipeline=monitor, model_registry_ref=registry_ref
        )
        try:
            yield
        finally:
            stop.set()
            await lag_task
            if task is not None:
                await task
            await pool.close()
            if redis_client is not None:
                await redis_client.aclose()

    app = FastAPI(title="fraud-platform scoring", version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestTimer)

    async def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
        expected = settings.api_key.get_secret_value().encode()
        if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), expected):
            raise HTTPException(status_code=401, detail="invalid or missing API key")

    @app.exception_handler(IdempotencyConflict)
    async def _conflict(_: Request, exc: IdempotencyConflict) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": "idempotency_conflict", "transaction_id": exc.transaction_id},
        )

    @app.exception_handler(PersistenceUnavailable)
    async def _unavailable(_: Request, __: PersistenceUnavailable) -> JSONResponse:
        logger.warning("decision durability not confirmed: database unavailable")
        return JSONResponse(status_code=503, content={"error": "persistence_unavailable"})

    @app.exception_handler(ScorerNotReady)
    async def _not_ready(_: Request, __: ScorerNotReady) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error": "scorer_not_ready"})

    @app.exception_handler(PipelineRejecting)
    async def _rejecting(_: Request, exc: PipelineRejecting) -> JSONResponse:
        return JSONResponse(status_code=503, content={"error": exc.reason})

    @app.exception_handler(PersistenceFailed)
    async def _failed(_: Request, __: PersistenceFailed) -> JSONResponse:
        logger.error("decision not persisted: database rejected the write")
        return JSONResponse(status_code=500, content={"error": "persistence_failed"})

    @app.post(
        "/v1/decisions",
        response_model=DecisionResponse,
        dependencies=[Depends(require_api_key)],
        responses={200: {"description": "Idempotent replay of the stored decision"}},
        status_code=201,
    )
    async def create_decision(
        body: DecisionRequest, request: Request, response: Response
    ) -> DecisionResponse:
        received_at = utc_now()
        service: DecisionService = request.app.state.service
        with stage("handler"):
            outcome = await service.decide(body, received_at)
        if not outcome.created:
            response.status_code = 200
        return outcome.response

    @app.get(
        "/v1/decisions/{transaction_id}",
        response_model=DecisionRecord,
        dependencies=[Depends(require_api_key)],
    )
    async def get_decision(transaction_id: str, request: Request) -> DecisionRecord:
        store: DecisionStore = request.app.state.store
        stored = await store.get(transaction_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="decision not found")
        return to_record(stored)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        """Ready = new decisions can be accepted and durably stored.

        Not ready (503): database unreachable, model incompatible, or pipeline past its hard
        backlog limit. A feature-store outage or a degraded pipeline is reported as `degraded`
        with status 200, because the API still persists decisions (as reviews when features
        cannot be read); routing traffic away would make that path unreachable.
        """
        store: DecisionStore = request.app.state.store
        monitor: PipelineMonitor | None = request.app.state.pipeline
        body: dict[str, Any] = {"database": await store.ping(), "scorer": scorer.ready()}
        body["model"] = {
            "model_version": scorer.model_version,
            "policy_version": policy.version,
            **model_source,
        }
        degraded: list[str] = []
        if isinstance(scorer, ModelScorer):
            body["feature_store"] = await scorer.feature_store_ok()
            if not body["feature_store"]:
                degraded.append("FEATURES_UNAVAILABLE")
        rejecting = None
        if monitor is not None:
            degraded.extend(monitor.current.reasons)
            rejecting = monitor.current.reject
            body["pipeline"] = {
                **monitor.current.detail,
                "reject": rejecting,
                "suppress_scoring": monitor.current.suppress_scoring,
            }
        body["degraded"] = degraded
        ready = body["database"] and body["scorer"] and rejecting is None
        body["status"] = "not_ready" if not ready else ("degraded" if degraded else "ready")
        return JSONResponse(status_code=200 if ready else 503, content=body)

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
