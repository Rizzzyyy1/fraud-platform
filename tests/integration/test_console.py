"""Console service against a real PostgreSQL: authentication, namespace scoping, pagination,
append-only reviews that never modify decisions, roles, and the one-active-job constraint."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fraudplat.console.app import COOKIE, create_console
from fraudplat.console.auth import Accounts
from fraudplat.console.deployment import Deployment
from fraudplat.console.jobs import JobManager, JobSpec
from fraudplat.console.telemetry import Telemetry

from ..unit.console.test_console_units import BAD_DECISION_CURSORS, BAD_QUEUE_CURSORS
from .conftest import make_settings

pytestmark = pytest.mark.integration
Conn = psycopg.Connection[tuple[object, ...]]
NS = "replay:console-test"
PASSWORD = "integration password"


def decide(
    db: Conn,
    txn: str,
    stream: str = NS,
    *,
    score: float | None = 0.01,
    action: str = "approve",
    codes: tuple[str, ...] = ("SCORE_BELOW_REVIEW_THRESHOLD",),
    seconds_ago: float = 10,
) -> None:
    db.execute(
        "INSERT INTO decisions (transaction_id, request_hash, hash_version, customer_id, "
        "terminal_id, amount_minor, currency, event_time, received_at, decision_time, score, "
        "action, reason_codes, features, feature_freshness, model_version, feature_version, "
        "policy_version, model_registry_ref) VALUES (%s, decode(repeat('00', 32), 'hex'), 1, "
        "'C1', 'T1', 1234, 'USD', now(), now(), now() - make_interval(secs => %s), %s, %s, %s, "
        "'{\"cust_txn_count_1h\": 2}', '{}', 'lr-test', 'f1', 'pol-test', 'fraud-risk-f1/5')",
        (txn, seconds_ago, score, action, list(codes)),
    )
    db.execute(
        "INSERT INTO outbox (event_id, aggregate_id, event_type, schema_version, payload, stream) "
        "VALUES (%s, %s, 'decision.made', 1, '{}', %s)",
        (uuid.uuid4(), txn, stream),
    )


class NoSpawnJobs(JobManager):
    """Records requests instead of starting job processes (no traffic in tests)."""

    def __init__(self, database_url: str, deployment: Deployment, state_dir: Path) -> None:
        super().__init__(database_url, deployment, state_dir)
        self.started: list[JobSpec] = []

    def start(self, spec: JobSpec, requested_by: str) -> dict[str, Any]:
        self.started.append(spec)
        return {"id": len(self.started), "kind": spec.kind, "status": "running"}


@pytest.fixture
def deployment() -> Deployment:
    return Deployment(namespace=NS, id_prefix="X-", created_at="2026-01-01T00:00:00Z", requests=10)


@pytest.fixture
async def console(
    database_url: str, db: Conn, tmp_path: Path, deployment: Deployment
) -> AsyncIterator[FastAPI]:
    accounts = Accounts(tmp_path / "analysts.json")
    accounts.add("ana", PASSWORD, "analyst")
    accounts.add("root", PASSWORD, "admin")
    jobs = NoSpawnJobs(database_url, deployment, tmp_path)
    app = create_console(
        make_settings(database_url),
        deployment=deployment,
        state_dir=tmp_path,
        telemetry=Telemetry("http://127.0.0.1:9", "http://127.0.0.1:9", None, min_interval_s=0),
        jobs=jobs,
    )
    app.state.test_jobs = jobs
    async with app.router.lifespan_context(app):
        yield app


async def signed_in(app: FastAPI, name: str) -> tuple[AsyncClient, str]:
    http = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    r = await http.post("/api/session", json={"username": name, "password": PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    return http, r.json()["csrf_token"]


async def test_everything_but_login_requires_a_session(console: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=console), base_url="http://test") as http:
        for path in ("/api/decisions", "/api/queue", "/api/health", "/api/results", "/api/jobs"):
            assert (await http.get(path)).status_code == 401, path
        bad = await http.post("/api/session", json={"username": "ana", "password": "nope"})
        assert bad.status_code == 401
        http.cookies.set(COOKIE, "forged-token")
        assert (await http.get("/api/decisions")).status_code == 401


async def test_decisions_are_scoped_paginated_and_never_score_null_as_zero(
    console: FastAPI, db: Conn
) -> None:
    for i in range(7):
        decide(db, f"N{i}", seconds_ago=10 + i)
    decide(db, "S1", score=None, action="review", codes=("PIPELINE_STALE_BEYOND_LIMIT", "NO_SCORE"))
    decide(db, "OTHER", stream="replay:someone-else")
    decide(db, "LIVE1", stream="live")
    decide(db, "OLD", seconds_ago=3 * 3600)  # outside the 15-minute range
    http, _ = await signed_in(console, "ana")
    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"range": "15m", "limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = (await http.get("/api/decisions", params=params)).json()
        seen += [d["transaction_id"] for d in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted([f"N{i}" for i in range(7)] + ["S1"])
    assert len(seen) == len(set(seen))  # keyset pages never repeat a row
    scoreless = (await http.get("/api/decisions", params={"scoring": "scoreless"})).json()
    [row] = scoreless["items"]
    assert row["score"] is None and row["scoreless"] and row["degraded"]
    hours = (await http.get("/api/decisions", params={"range": "6h"})).json()
    assert "OLD" in [d["transaction_id"] for d in hours["items"]]
    assert (await http.get("/api/decisions", params={"cursor": "garbage"})).status_code == 400
    assert (await http.get("/api/decisions", params={"limit": 500})).status_code == 422
    assert (await http.get("/api/decisions/OTHER")).status_code == 404
    activity = (await http.get("/api/activity", params={"range": "15m"})).json()
    assert activity["totals"]["total"] == 8 and activity["totals"]["scoreless"] == 1
    await http.aclose()


async def test_reviews_are_appended_and_never_change_the_decision(
    console: FastAPI, db: Conn
) -> None:
    decide(db, "R1", score=0.9, action="review", codes=("SCORE_AT_OR_ABOVE_REVIEW_THRESHOLD",))
    decide(db, "R2", score=0.5, action="review", codes=("SCORE_AT_OR_ABOVE_REVIEW_THRESHOLD",))
    decide(db, "R3", score=None, action="review", codes=("FEATURES_UNAVAILABLE", "NO_SCORE"))
    decide(db, "R4", stream="replay:someone-else", action="review")
    before = db.execute(
        "SELECT to_jsonb(d) FROM decisions d WHERE transaction_id = 'R1'"
    ).fetchone()
    http, csrf = await signed_in(console, "ana")

    queue = (await http.get("/api/queue")).json()
    assert [i["transaction_id"] for i in queue["items"]] == ["R1", "R2", "R3"]  # scoreless last
    assert queue["counts"] == {"open": 3, "in_review": 0, "closed": 0}

    body = {"status": "in_review", "note": "checking terminal history"}
    assert (await http.post("/api/decisions/R1/reviews", json=body)).status_code == 403  # no CSRF
    headers = {"X-CSRF-Token": csrf}
    assert (
        await http.post("/api/decisions/R1/reviews", json=body, headers={"X-CSRF-Token": "x"})
    ).status_code == 403
    assert (
        await http.post("/api/decisions/R1/reviews", json=body, headers=headers)
    ).status_code == 201
    close = {"status": "closed"}
    assert (
        await http.post("/api/decisions/R1/reviews", json=close, headers=headers)
    ).status_code == 422
    close["disposition"] = "suspected_fraud"
    done = await http.post("/api/decisions/R1/reviews", json=close, headers=headers)
    assert done.status_code == 201 and done.json()["analyst"] == "ana"
    extra = {"status": "open", "score": 0.0}
    assert (
        await http.post("/api/decisions/R1/reviews", json=extra, headers=headers)
    ).status_code == 422
    assert (
        await http.post("/api/decisions/R4/reviews", json=body, headers=headers)
    ).status_code == 404  # other namespace

    after = db.execute("SELECT to_jsonb(d) FROM decisions d WHERE transaction_id = 'R1'").fetchone()
    assert before == after  # the decision row is byte-for-byte unchanged
    detail = (await http.get("/api/decisions/R1")).json()
    assert [r["status"] for r in detail["reviews"]] == ["in_review", "closed"]
    assert detail["decision"]["score"] == 0.9 and detail["decision"]["action"] == "review"
    assert "request_hash" not in detail["decision"]
    assert detail["policy"] is None  # `pol-test` has no local policy file
    queue = (await http.get("/api/queue", params={"status": "closed"})).json()
    assert [i["transaction_id"] for i in queue["items"]] == ["R1"]
    assert queue["items"][0]["disposition"] == "suspected_fraud"

    # Reviews are append-only at the database level too.
    db.execute("UPDATE reviews SET note = 'rewritten'")
    db.execute("DELETE FROM reviews")
    notes = db.execute("SELECT note FROM reviews ORDER BY id").fetchall()
    assert notes == [("checking terminal history",), (None,)]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute("DELETE FROM decisions WHERE transaction_id = 'R1'")
    await http.aclose()


async def test_jobs_require_admin_and_enumerated_parameters(console: FastAPI) -> None:
    analyst, a_csrf = await signed_in(console, "ana")
    job = {"kind": "traffic", "rate": 5, "duration_s": 60}
    denied = await analyst.post("/api/jobs", json=job, headers={"X-CSRF-Token": a_csrf})
    assert denied.status_code == 403
    admin, csrf = await signed_in(console, "root")
    headers = {"X-CSRF-Token": csrf}
    assert (await admin.post("/api/jobs", json=job, headers=headers)).status_code == 201
    for bad in (
        {"kind": "traffic", "rate": 1000, "duration_s": 60},
        {"kind": "shell", "rate": 5, "duration_s": 60},
        {**job, "command": "rm -rf /"},
    ):
        assert (await admin.post("/api/jobs", json=bad, headers=headers)).status_code == 422
    jobs: NoSpawnJobs = console.state.test_jobs
    assert jobs.started == [JobSpec("traffic", 5, 60)]
    await analyst.aclose()
    await admin.aclose()


def test_only_one_job_can_be_active(database_url: str, db: Conn, deployment: Deployment) -> None:
    manager = JobManager(database_url, deployment, Path("/nonexistent"))
    insert = (
        "INSERT INTO console_jobs (kind, params, namespace, status, requested_by, start_offset) "
        "VALUES ('traffic', %s, %s, 'running', 'root', 0)"
    )
    db.execute(insert, (json.dumps({"rate": 2, "duration_s": 30}), NS))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(insert, (json.dumps({"rate": 2, "duration_s": 30}), NS))
    # The running job has no live process, so reconciliation marks it failed.
    db.execute("UPDATE console_jobs SET pid = 999999, created_at = now() - interval '1 minute'")
    manager.reconcile()
    status = db.execute("SELECT status FROM console_jobs").fetchone()
    assert status == ("failed",)
    db.execute(insert, (json.dumps({"rate": 2, "duration_s": 30}), NS))  # a new job may start


async def test_health_reports_unavailable_telemetry_instead_of_values(console: FastAPI) -> None:
    http, _ = await signed_in(console, "ana")
    health = (await http.get("/api/health")).json()
    assert health["running"]["state"] == "unavailable" and health["running"]["value"] is None
    assert health["live_window"] is None
    assert health["deployment"]["namespace"] == NS
    assert health["console"]["database"] is True
    results = (await http.get("/api/results")).json()
    assert {r["split"] for r in results["results"]} <= {"development", "held-out"}
    assert (await http.get("/api/reports/held_out")).status_code == 200
    assert (await http.get("/api/reports/..%2F.env")).status_code == 404
    await http.aclose()


async def test_activity_pagination_and_malformed_cursors(console: FastAPI, db: Conn) -> None:
    for i in range(9):
        decide(db, f"P{i}", seconds_ago=5 + i)
    http, _ = await signed_in(console, "ana")
    pages: list[list[str]] = []
    cursor = None
    for _ in range(10):
        params: dict[str, Any] = {"range": "15m", "limit": 4}
        if cursor:
            params["cursor"] = cursor
        r = await http.get("/api/decisions", params=params)
        assert r.status_code == 200
        body = r.json()
        pages.append([d["transaction_id"] for d in body["items"]])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == [["P0", "P1", "P2", "P3"], ["P4", "P5", "P6", "P7"], ["P8"]]
    for bad in BAD_DECISION_CURSORS:
        r = await http.get("/api/decisions", params={"cursor": bad})
        assert r.status_code == 400, (bad, r.status_code, r.text)
        assert r.json() == {"detail": "invalid cursor"}
    await http.aclose()


async def test_investigation_pagination_and_malformed_cursors(console: FastAPI, db: Conn) -> None:
    scores = [0.9, 0.8, 0.7, 0.6, 0.5]
    for i, score in enumerate(scores):
        decide(db, f"Q{i}", score=score, action="review", seconds_ago=5 + i)
    decide(db, "QS1", score=None, action="review", codes=("FEATURES_UNAVAILABLE", "NO_SCORE"))
    decide(db, "QS2", score=None, action="review", codes=("FEATURES_UNAVAILABLE", "NO_SCORE"))
    http, _ = await signed_in(console, "ana")
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params: dict[str, Any] = {"status": "open", "limit": 3}
        if cursor:
            params["cursor"] = cursor
        r = await http.get("/api/queue", params=params)
        assert r.status_code == 200
        body = r.json()
        seen += [i["transaction_id"] for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen[:5] == ["Q0", "Q1", "Q2", "Q3", "Q4"]  # highest score first
    assert sorted(seen[5:]) == ["QS1", "QS2"]  # scoreless last, across a page boundary
    assert len(seen) == len(set(seen)) == 7
    for bad in BAD_QUEUE_CURSORS + BAD_DECISION_CURSORS:
        r = await http.get("/api/queue", params={"cursor": bad})
        assert r.status_code == 400, (bad, r.status_code, r.text)
    await http.aclose()
