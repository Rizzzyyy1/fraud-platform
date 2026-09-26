"""Demo/benchmark cleanup touches only resources owned by its own run.

Transactions T1..T4 are all in the next run's workload:
  T1 decided by `live`                     → must be excluded from the workload, never deleted
  T2 decided by an earlier replay run      → stale replay leftover: deleted before the run
  T3 decided by a *different* current run  → replay-owned too: treated as stale leftover
  T4 undecided                             → sent normally
After the run, `delete_owned_rows` removes only rows of the run's own stream.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest
from redis.asyncio import Redis

from fraudplat.demos.stack import Stack, Workload, prepare
from fraudplat.features.spec import DAY
from fraudplat.pipeline.streams import Stream
from fraudplat.pipeline.topics import delete_consumer_group, delete_topics

from ..unit.features.helpers import event, historical, us
from .conftest import make_settings

pytestmark = pytest.mark.integration
Conn = psycopg.Connection[tuple[object, ...]]


def decide(db: Conn, txn: str, stream: str) -> None:
    db.execute(
        "INSERT INTO decisions (transaction_id, request_hash, hash_version, customer_id, "
        "terminal_id, amount_minor, currency, event_time, received_at, decision_time, action, "
        "reason_codes, features, feature_freshness, feature_version, policy_version) VALUES "
        "(%s, decode(repeat('00', 32), 'hex'), 1, 'C1', 'T1', 1, 'USD', now(), now(), now(), "
        "'approve', '{}', '{}', '{}', 'f1', 'p')",
        (txn,),
    )
    db.execute(
        "INSERT INTO outbox (event_id, aggregate_id, event_type, schema_version, payload, stream) "
        "VALUES (%s, %s, 'decision.made', 1, '{}', %s)",
        (uuid.uuid4(), txn, stream),
    )


def rows(db: Conn) -> dict[str, str]:
    result = db.execute(
        "SELECT d.transaction_id, o.stream FROM decisions d "
        "JOIN outbox o ON o.aggregate_id = d.transaction_id ORDER BY 1"
    ).fetchall()
    return {str(t): str(s) for t, s in result}


async def test_cleanup_touches_only_owned_rows(
    database_url: str, db: Conn, redis_client: Redis
) -> None:
    run = Stream(f"replay:own-{uuid.uuid4().hex[:6]}")
    decide(db, "T1", "live")
    decide(db, "T2", "replay:old-run")
    decide(db, "T3", "replay:other-run")
    decide(db, "OTHER", "replay:other-run")  # not in the workload: must survive everything

    base = us("2025-05-01T12:00:00")
    txns = [
        historical(event(t, base + i * 60_000_000)) for i, t in enumerate(["T1", "T2", "T3", "T4"])
    ]
    workload = Workload(
        txns=list(txns),
        requests=[{"transaction_id": t.event.event_id} for t in txns],
        expected={},
        cutoff_us=base - DAY,
        all_pre_test=[],
    )
    settings = make_settings(database_url)
    stack = Stack(run, 0, settings, Path("unused"), Path("unused"))
    try:
        setup = await prepare(stack, workload, redis_client)
    finally:
        delete_topics(settings.kafka_bootstrap, run)
        delete_consumer_group(settings.kafka_bootstrap, run)

    assert setup["excluded_already_decided_by_other_stream"] == ["T1"]
    assert [r["transaction_id"] for r in workload.requests] == ["T2", "T3", "T4"]
    assert rows(db) == {"T1": "live", "OTHER": "replay:other-run"}  # live row kept

    decide(db, "T4", run.namespace)  # the run's own decision
    assert stack.delete_owned_rows() == 1
    assert rows(db) == {"T1": "live", "OTHER": "replay:other-run"}
