"""The four impaired states the API distinguishes, and what each does to a new decision.

* Unknown lag (worker starting): 201; model score; policy action; adds PIPELINE_DEGRADED,
  CONSUMER_LAG_UNKNOWN; persisted.
* Confirmed stale worker within the 60 s grace: 201; model score; policy action; adds
  PIPELINE_DEGRADED, WORKER_HEARTBEAT_STALE; persisted.
* Stale beyond 60 s, or unknown for more than 60 s: 201; no score; action review;
  PIPELINE_STALE_BEYOND_LIMIT / PIPELINE_UNKNOWN_BEYOND_LIMIT ... NO_SCORE; persisted.
* Redis (feature store) unreachable: 201; no score; action review; FEATURES_UNAVAILABLE ... NO_SCORE
  (plus any pipeline reasons); persisted.
* Outbox backlog over the hard limit: 503 pipeline_backlog_limit; nothing persisted.

Only an unreadable feature store produces a scoreless review; pipeline degradation alone never
suppresses the model. `/readyz` is 200 (`degraded`) in the first three states and 503 in the last.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from fraudplat.api.app import create_app
from fraudplat.features.redis_state import RedisFeatureState
from fraudplat.features.spec import US
from fraudplat.pipeline.streams import Stream
from fraudplat.scoring.model_scorer import ModelScorer

from .conftest import API_KEY, make_settings
from .test_model_scoring_api import POLICY, identity_model

pytestmark = pytest.mark.integration
Conn = psycopg.Connection[tuple[object, ...]]
STREAM = Stream("replay:degraded-modes")


def request(txn: str) -> dict[str, Any]:
    return {
        "transaction_id": txn,
        "customer_id": "C1",
        "terminal_id": "T1",
        "amount_minor": 2000,
        "currency": "USD",
        "event_time": "2025-05-01T12:00:00Z",
    }


@asynccontextmanager
async def api(
    database_url: str, feature_redis: Redis, status_redis: Redis, **overrides: Any
) -> AsyncIterator[AsyncClient]:
    settings = make_settings(
        database_url,
        feature_namespace=STREAM.namespace,
        pipeline_monitor_interval_s=0.1,
        **overrides,
    )
    scorer = ModelScorer(
        identity_model(), RedisFeatureState(feature_redis, STREAM.namespace), read_timeout_s=0.5
    )
    app = create_app(
        settings, scorer=scorer, policy=POLICY, pipeline_redis=status_redis, monitor_pipeline=True
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t", headers={"X-API-Key": API_KEY}
        ) as http,
    ):
        await asyncio.sleep(0.3)  # let the monitor refresh once
        yield http


async def heartbeat(redis: Redis, age_s: float, lag: int) -> None:
    now_us = int(datetime.now(UTC).timestamp()) * US
    await redis.hset(
        STREAM.worker_status_key,
        mapping={
            "namespace": STREAM.namespace,
            "heartbeat_at_us": now_us - int(age_s * US),
            "consumer_lag_events": lag,
        },
    )


def stored(db: Conn, txn: str) -> tuple[Any, ...] | None:
    return db.execute(
        "SELECT action, score, reason_codes FROM decisions WHERE transaction_id = %s", (txn,)
    ).fetchone()


async def test_unknown_lag_keeps_model_scoring(
    database_url: str, db: Conn, redis_client: Redis
) -> None:
    await heartbeat(redis_client, age_s=0, lag=-1)  # worker alive, lag not yet known
    async with api(database_url, redis_client, redis_client) as http:
        body = (await http.post("/v1/decisions", json=request("u1"))).json()
        ready = await http.get("/readyz")
    assert body["score"] is not None
    assert {"PIPELINE_DEGRADED", "CONSUMER_LAG_UNKNOWN"} <= set(body["reason_codes"])
    assert "WORKER_HEARTBEAT_STALE" not in body["reason_codes"]
    assert ready.status_code == 200 and ready.json()["status"] == "degraded"
    assert stored(db, "u1") is not None


async def test_confirmed_stale_worker_keeps_model_scoring(
    database_url: str, db: Conn, redis_client: Redis
) -> None:
    await heartbeat(redis_client, age_s=30, lag=0)  # stale (> 10 s) but within the 60 s grace
    async with api(database_url, redis_client, redis_client) as http:
        body = (await http.post("/v1/decisions", json=request("s1"))).json()
    assert body["score"] is not None
    assert {"PIPELINE_DEGRADED", "WORKER_HEARTBEAT_STALE"} <= set(body["reason_codes"])
    assert "CONSUMER_LAG_UNKNOWN" not in body["reason_codes"]


async def test_feature_store_down_persists_scoreless_review(database_url: str, db: Conn) -> None:
    dead = Redis.from_url(
        "redis://127.0.0.1:1/0",
        decode_responses=True,
        socket_connect_timeout=0.2,
        socket_timeout=0.2,
    )
    async with api(database_url, dead, dead) as http:
        response = await http.post("/v1/decisions", json=request("r1"))
        ready = await http.get("/readyz")
    await dead.aclose()
    body = response.json()
    assert response.status_code == 201
    assert body["score"] is None and body["action"] == "review"
    assert (
        body["reason_codes"][0] == "FEATURES_UNAVAILABLE" and body["reason_codes"][-1] == "NO_SCORE"
    )
    row = stored(db, "r1")
    assert row is not None and row[0] == "review" and row[1] is None
    assert ready.status_code == 200 and "FEATURES_UNAVAILABLE" in ready.json()["degraded"]


async def test_backlog_limit_rejects_without_persisting(
    database_url: str, db: Conn, redis_client: Redis
) -> None:
    await heartbeat(redis_client, age_s=0, lag=0)
    async with api(database_url, redis_client, redis_client, outbox_backlog_reject_rows=1) as http:
        for txn in ("b1", "b2"):  # nothing publishes, so the backlog reaches 2 > 1
            assert (await http.post("/v1/decisions", json=request(txn))).status_code == 201
        await asyncio.sleep(0.3)
        rejected = await http.post("/v1/decisions", json=request("b3"))
        ready = await http.get("/readyz")
    assert rejected.status_code == 503 and rejected.json() == {"error": "pipeline_backlog_limit"}
    assert stored(db, "b3") is None
    assert ready.status_code == 503 and ready.json()["status"] == "not_ready"


async def test_stale_beyond_limit_persists_scoreless_review(
    database_url: str, db: Conn, redis_client: Redis
) -> None:
    await heartbeat(redis_client, age_s=90, lag=0)
    async with api(database_url, redis_client, redis_client) as http:
        response = await http.post("/v1/decisions", json=request("s2"))
        ready = (await http.get("/readyz")).json()
    body = response.json()
    assert ready["pipeline"]["suppress_scoring"] == "PIPELINE_STALE_BEYOND_LIMIT"
    assert response.status_code == 201
    assert body["score"] is None and body["action"] == "review"
    assert body["reason_codes"][0] == "PIPELINE_STALE_BEYOND_LIMIT"
    assert (
        "WORKER_HEARTBEAT_STALE" in body["reason_codes"] and body["reason_codes"][-1] == "NO_SCORE"
    )
    row = stored(db, "s2")
    assert row is not None and row[1] is None


async def test_unknown_state_escalates_after_its_limit(
    database_url: str, db: Conn, redis_client: Redis
) -> None:
    await heartbeat(redis_client, age_s=0, lag=-1)
    async with api(database_url, redis_client, redis_client, unknown_state_limit_s=0.6) as http:
        early = (await http.post("/v1/decisions", json=request("k1"))).json()
        for _ in range(8):  # keep the worker alive but its lag unknown
            await heartbeat(redis_client, age_s=0, lag=-1)
            await asyncio.sleep(0.15)
        late = (await http.post("/v1/decisions", json=request("k2"))).json()
    assert early["score"] is not None and "CONSUMER_LAG_UNKNOWN" in early["reason_codes"]
    assert late["score"] is None and late["reason_codes"][0] == "PIPELINE_UNKNOWN_BEYOND_LIMIT"


async def test_backlog_rejection_takes_precedence_over_scoreless_review(
    database_url: str, db: Conn, redis_client: Redis
) -> None:
    await heartbeat(redis_client, age_s=90, lag=0)
    async with api(database_url, redis_client, redis_client, outbox_backlog_reject_rows=0) as http:
        await http.post("/v1/decisions", json=request("p1"))  # backlog becomes 1 > 0
        await asyncio.sleep(0.3)
        rejected = await http.post("/v1/decisions", json=request("p2"))
    assert rejected.status_code == 503
    assert stored(db, "p2") is None
