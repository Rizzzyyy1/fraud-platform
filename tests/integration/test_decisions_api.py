"""Checkpoint A acceptance: durable idempotency and outbox atomicity against real PostgreSQL.

The scorer here is the temporary scaffold; these tests make no claim about model quality.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fraudplat.api.app import create_app

from .conftest import API_KEY, make_settings

pytestmark = pytest.mark.integration

Conn = psycopg.Connection[tuple[object, ...]]

PAYLOAD: dict[str, Any] = {
    "transaction_id": "tx-100",
    "customer_id": "c-1",
    "terminal_id": "t-1",
    "amount_minor": 5000,
    "currency": "USD",
    "event_time": "2026-01-02T03:04:05Z",
}

PERSISTED_FIELDS = (
    "transaction_id",
    "action",
    "score",
    "reason_codes",
    "model_version",
    "feature_version",
    "policy_version",
    "event_time",
    "received_at",
    "decision_time",
)


def counts(db: Conn) -> tuple[int, int]:
    decisions = db.execute("SELECT count(*) FROM decisions").fetchone()
    outbox = db.execute("SELECT count(*) FROM outbox").fetchone()
    assert decisions is not None and outbox is not None
    return int(decisions[0]), int(outbox[0])  # type: ignore[call-overload]


def persisted(body: dict[str, Any]) -> dict[str, Any]:
    return {key: body[key] for key in PERSISTED_FIELDS}


async def test_first_request_creates_decision_and_one_outbox_event(
    client: AsyncClient, db: Conn
) -> None:
    response = await client.post("/v1/decisions", json=PAYLOAD)
    assert response.status_code == 201
    body = response.json()
    assert body["idempotent_replay"] is False
    assert body["model_version"] == "scaffold-0"
    assert "SCAFFOLD_NOT_A_MODEL" in body["reason_codes"]
    assert counts(db) == (1, 1)

    row = db.execute(
        "SELECT aggregate_id, event_type, schema_version, payload->>'payload_hash' FROM outbox"
    ).fetchone()
    stored_hash = db.execute("SELECT encode(request_hash, 'hex') FROM decisions").fetchone()
    assert row is not None and stored_hash is not None
    assert row[:3] == ("tx-100", "decision.made", 1)
    assert row[3] == stored_hash[0]


async def test_retry_returns_stored_decision_without_new_rows(
    client: AsyncClient, db: Conn
) -> None:
    first = await client.post("/v1/decisions", json=PAYLOAD)
    # Equivalent payload: different offset spelling and currency case.
    retry = await client.post(
        "/v1/decisions",
        json={**PAYLOAD, "event_time": "2026-01-01T22:04:05-05:00", "currency": "usd"},
    )
    assert (first.status_code, retry.status_code) == (201, 200)
    assert retry.json()["idempotent_replay"] is True
    assert persisted(retry.json()) == persisted(first.json())
    assert counts(db) == (1, 1)

    fetched = await client.get("/v1/decisions/tx-100")
    assert fetched.status_code == 200
    assert persisted(fetched.json()) == persisted(first.json())


async def test_conflicting_payload_returns_409_and_leaves_row_unchanged(
    client: AsyncClient, db: Conn
) -> None:
    await client.post("/v1/decisions", json=PAYLOAD)
    before = db.execute("SELECT * FROM decisions").fetchall()
    conflict = await client.post("/v1/decisions", json={**PAYLOAD, "amount_minor": 5001})
    assert conflict.status_code == 409
    assert conflict.json() == {"error": "idempotency_conflict", "transaction_id": "tx-100"}
    assert db.execute("SELECT * FROM decisions").fetchall() == before
    assert counts(db) == (1, 1)


async def test_concurrent_identical_requests_create_exactly_one_decision(
    client: AsyncClient, db: Conn
) -> None:
    responses = await asyncio.gather(
        *(client.post("/v1/decisions", json=PAYLOAD) for _ in range(20))
    )
    statuses = Counter(r.status_code for r in responses)
    assert statuses == {201: 1, 200: 19}
    assert sum(not r.json()["idempotent_replay"] for r in responses) == 1
    assert len({str(persisted(r.json())) for r in responses}) == 1
    assert counts(db) == (1, 1)


async def test_concurrent_mixed_payloads_have_one_winner(client: AsyncClient, db: Conn) -> None:
    other = {**PAYLOAD, "amount_minor": 9999}
    requests = [PAYLOAD, other] * 10
    responses = await asyncio.gather(*(client.post("/v1/decisions", json=p) for p in requests))
    assert counts(db) == (1, 1)

    winner_amount = db.execute("SELECT amount_minor FROM decisions").fetchone()
    assert winner_amount is not None
    for sent, response in zip(requests, responses, strict=True):
        if sent["amount_minor"] == winner_amount[0]:
            assert response.status_code in (200, 201)
        else:
            assert response.status_code == 409
    assert sum(r.status_code == 201 for r in responses) == 1
    accepted = [persisted(r.json()) for r in responses if r.status_code in (200, 201)]
    assert all(body == accepted[0] for body in accepted)


async def test_outbox_failure_rolls_back_the_decision(client: AsyncClient, db: Conn) -> None:
    db.execute("""
        CREATE FUNCTION fail_outbox() RETURNS trigger LANGUAGE plpgsql AS
        $$ BEGIN RAISE EXCEPTION 'injected outbox failure'; END $$;
        CREATE TRIGGER fail_outbox BEFORE INSERT ON outbox
        FOR EACH ROW EXECUTE FUNCTION fail_outbox();
    """)
    try:
        response = await client.post("/v1/decisions", json=PAYLOAD)
        assert response.status_code == 500
        assert response.json() == {"error": "persistence_failed"}
        assert counts(db) == (0, 0)
    finally:
        db.execute("DROP TRIGGER fail_outbox ON outbox; DROP FUNCTION fail_outbox();")

    # Nothing stale was left behind: the same request now succeeds normally.
    retry = await client.post("/v1/decisions", json=PAYLOAD)
    assert retry.status_code == 201
    assert counts(db) == (1, 1)


async def test_unreachable_database_never_returns_success(database_url: str) -> None:
    # Port 1 on localhost: nothing listens, so every connection attempt fails fast.
    unreachable = "postgresql://nobody:nothing@127.0.0.1:1/none"
    app: FastAPI = create_app(make_settings(unreachable, db_acquire_timeout_s=0.5))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-API-Key": API_KEY},
        ) as http,
    ):
        response = await http.post("/v1/decisions", json=PAYLOAD)
        assert response.status_code == 503
        assert response.json() == {"error": "persistence_unavailable"}
        ready = await http.get("/readyz")
        assert ready.status_code == 503
        assert ready.json()["database"] is False


async def test_requests_without_valid_api_key_are_rejected(client: AsyncClient, db: Conn) -> None:
    response = await client.post("/v1/decisions", json=PAYLOAD, headers={"X-API-Key": "wrong"})
    assert response.status_code == 401
    assert counts(db) == (0, 0)


async def test_race_resolved_by_database_when_fast_path_is_bypassed(
    app: FastAPI, client: AsyncClient, db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every request misses the pre-check, so all 20 contend on INSERT ... ON CONFLICT.

    Correctness must not depend on the pre-check; it is only an optimisation for retries.
    """

    async def always_missing(_: str) -> None:
        return None

    monkeypatch.setattr(app.state.store, "get", always_missing)
    requests = [PAYLOAD] * 10 + [{**PAYLOAD, "amount_minor": 9999}] * 10
    responses = await asyncio.gather(*(client.post("/v1/decisions", json=p) for p in requests))
    assert counts(db) == (1, 1)
    assert sum(r.status_code == 201 for r in responses) == 1
    assert Counter(r.status_code for r in responses)[409] == 10
