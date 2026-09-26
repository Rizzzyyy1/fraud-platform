"""Model scoring through the API: real Redis features, hand-calculated score, persisted feature
vector and versions, idempotent retry, conflict, concurrency, cold start, unavailable features,
and an incompatible model."""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from fraudplat.api.app import create_app
from fraudplat.features.redis_state import RedisFeatureState
from fraudplat.features.spec import FEATURE_NAMES, FEATURE_VERSION
from fraudplat.policy import DecisionPolicy
from fraudplat.scoring.model_scorer import ModelScorer
from fraudplat.training.model import LinearModel, Preprocessing, build_model

from ..unit.features.helpers import event, us
from .conftest import API_KEY, make_settings

pytestmark = pytest.mark.integration
Conn = psycopg.Connection[tuple[object, ...]]

NULLABLE = (
    "cust_amount_mean_7d",
    "cust_amount_mean_30d",
    "amount_to_cust_mean_30d",
    "secs_since_prev_cust_txn",
)


def identity_model(feature_version: str = FEATURE_VERSION) -> LinearModel:
    """No log, zero means, unit scales: score = sigmoid(0.001 * amount + 0.5 * count_1d - 3)."""
    n_out = len(FEATURE_NAMES) + len(NULLABLE)
    pre = Preprocessing(
        feature_names=FEATURE_NAMES,
        nullable=NULLABLE,
        log1p=(),
        medians=tuple(0.0 for _ in FEATURE_NAMES),
        means=tuple(0.0 for _ in range(n_out)),
        scales=tuple(1.0 for _ in range(n_out)),
    )
    coef = [0.0] * n_out
    coef[FEATURE_NAMES.index("amount_minor")] = 0.001
    coef[FEATURE_NAMES.index("cust_txn_count_1d")] = 0.5
    return build_model(
        feature_version=feature_version,
        preprocessing=pre,
        coefficients=tuple(coef),
        intercept=-3.0,
        metadata={"purpose": "integration test"},
    )


def expected_score(amount: int, count_1d: int) -> float:
    return 1.0 / (1.0 + math.exp(-(0.001 * amount + 0.5 * count_1d - 3.0)))


POLICY = DecisionPolicy(version="test-policy", review_threshold=0.5, decline_threshold=0.95)
T = us("2025-05-01T12:00:00")
REQUEST: dict[str, Any] = {
    "transaction_id": "tx-model-1",
    "customer_id": "C1",
    "terminal_id": "T1",
    "amount_minor": 2000,
    "currency": "USD",
    "event_time": "2025-05-01T12:00:00Z",
}


async def seed_history(redis_client: Redis) -> RedisFeatureState:
    state = RedisFeatureState(redis_client)
    await state.apply(event("h1", T - 2 * 3_600_000_000, amount=100), applied_at_us=1)  # in 1d
    await state.apply(event("h2", T - 5 * 3_600_000_000, amount=300), applied_at_us=2)  # in 1d
    await state.apply(event("h3", T - 3 * 86_400_000_000, amount=200), applied_at_us=3)  # 3 days
    return state


@pytest.fixture
async def model_client(
    database_url: str, db: Conn, redis_client: Redis
) -> AsyncIterator[AsyncClient]:
    state = await seed_history(redis_client)
    scorer = ModelScorer(identity_model(), state, read_timeout_s=1.0)
    app = create_app(make_settings(database_url), scorer=scorer, policy=POLICY)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t", headers={"X-API-Key": API_KEY}
        ) as http,
    ):
        yield http


async def test_model_decision_persists_features_and_versions(
    model_client: AsyncClient, db: Conn
) -> None:
    response = await model_client.post("/v1/decisions", json=REQUEST)
    assert response.status_code == 201
    body = response.json()
    # count_1d = 2 (h1, h2); score = sigmoid(0.001*2000 + 0.5*2 - 3) = sigmoid(0) = 0.5
    assert body["score"] == pytest.approx(expected_score(2000, 2)) == pytest.approx(0.5)
    assert body["action"] == "review"  # 0.5 >= review threshold 0.5
    assert body["model_version"] == identity_model().model_version
    assert body["feature_version"] == FEATURE_VERSION
    assert body["policy_version"] == "test-policy"

    row = db.execute("SELECT features, feature_freshness, reason_codes FROM decisions").fetchone()
    assert row is not None
    features, freshness, reasons = row
    assert features["cust_txn_count_1d"] == 2  # type: ignore[index]
    assert features["cust_txn_count_7d"] == 3  # type: ignore[index]
    assert features["cust_amount_mean_7d"] == 200.0  # type: ignore[index]  # (100+300+200)/3
    # jsonb does not preserve key order; the artifact's feature_names define the model input order.
    assert set(features) == set(FEATURE_NAMES)  # type: ignore[call-overload]
    assert freshness["state"] == "read"  # type: ignore[index]
    assert freshness["customer_meta"]["applied_count"] == 3  # type: ignore[index]
    assert "COLD_START_CUSTOMER" not in reasons  # type: ignore[operator]


async def test_retry_and_conflict_with_model(model_client: AsyncClient, db: Conn) -> None:
    first = await model_client.post("/v1/decisions", json=REQUEST)
    retry = await model_client.post("/v1/decisions", json=REQUEST)
    conflict = await model_client.post("/v1/decisions", json={**REQUEST, "amount_minor": 2001})
    assert (first.status_code, retry.status_code, conflict.status_code) == (201, 200, 409)
    same = {k: v for k, v in retry.json().items() if k != "idempotent_replay"}
    assert same == {k: v for k, v in first.json().items() if k != "idempotent_replay"}
    counts = db.execute("SELECT (SELECT count(*) FROM decisions), (SELECT count(*) FROM outbox)")
    assert counts.fetchone() == (1, 1)


async def test_concurrent_duplicates_with_model(model_client: AsyncClient, db: Conn) -> None:
    responses = await asyncio.gather(
        *(model_client.post("/v1/decisions", json=REQUEST) for _ in range(20))
    )
    assert Counter(r.status_code for r in responses) == {201: 1, 200: 19}
    counts = db.execute("SELECT (SELECT count(*) FROM decisions), (SELECT count(*) FROM outbox)")
    assert counts.fetchone() == (1, 1)


async def test_cold_start_customer_is_scored_and_flagged(model_client: AsyncClient) -> None:
    response = await model_client.post(
        "/v1/decisions", json={**REQUEST, "transaction_id": "tx-new", "customer_id": "C-new"}
    )
    body = response.json()
    assert response.status_code == 201
    assert "COLD_START_CUSTOMER" in body["reason_codes"]
    assert body["score"] == pytest.approx(expected_score(2000, 0))  # nulls imputed to 0 here


async def test_unavailable_feature_store_gives_persisted_review(
    database_url: str, db: Conn
) -> None:
    dead = Redis.from_url(
        "redis://127.0.0.1:1/0", decode_responses=True, socket_connect_timeout=0.2
    )
    scorer = ModelScorer(identity_model(), RedisFeatureState(dead), read_timeout_s=0.5)
    app = create_app(make_settings(database_url), scorer=scorer, policy=POLICY)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t", headers={"X-API-Key": API_KEY}
        ) as http,
    ):
        response = await http.post("/v1/decisions", json=REQUEST)
        ready = await http.get("/readyz")
    await dead.aclose()
    body = response.json()
    assert response.status_code == 201
    assert body["action"] == "review"
    assert body["score"] is None
    assert body["reason_codes"] == ["FEATURES_UNAVAILABLE", "NO_SCORE"]
    stored = db.execute("SELECT features, feature_freshness FROM decisions").fetchone()
    assert stored is not None
    assert stored[0] == {}
    assert stored[1]["state"] == "unavailable"  # type: ignore[index]
    # Degraded, not unready: the review path must stay reachable while Redis is down.
    assert ready.status_code == 200
    assert ready.json()["feature_store"] is False
    assert ready.json()["status"] == "degraded"
    assert "FEATURES_UNAVAILABLE" in ready.json()["degraded"]


async def test_incompatible_model_fails_readiness_and_refuses_new_decisions(
    database_url: str, db: Conn, redis_client: Redis
) -> None:
    scorer = ModelScorer(identity_model(feature_version="f0"), RedisFeatureState(redis_client))
    app: FastAPI = create_app(make_settings(database_url), scorer=scorer, policy=POLICY)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t", headers={"X-API-Key": API_KEY}
        ) as http,
    ):
        ready = await http.get("/readyz")
        response = await http.post("/v1/decisions", json=REQUEST)
    assert ready.status_code == 503 and ready.json()["scorer"] is False
    assert response.status_code == 503
    assert response.json() == {"error": "scorer_not_ready"}
    assert db.execute("SELECT count(*) FROM decisions").fetchone() == (0,)


def test_policy_derived_for_another_model_stops_startup(database_url: str, tmp_path: Path) -> None:
    other = DecisionPolicy("p", 0.5, None, derived_for_model="lr-f1-000000000000")
    scorer = ModelScorer(identity_model(), RedisFeatureState(Redis()))
    with pytest.raises(RuntimeError, match="derived for"):
        create_app(make_settings(database_url), scorer=scorer, policy=other)
