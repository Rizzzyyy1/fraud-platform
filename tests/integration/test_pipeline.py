"""Event pipeline against real PostgreSQL, Kafka and Redis.

Each test uses its own `replay:<id>` namespace (own outbox stream, topics, consumer group and
Redis prefix), so tests are isolated from each other and from live state.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from confluent_kafka import Consumer, Producer, TopicPartition
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from fraudplat.api.app import create_app
from fraudplat.features.compute import compute_features
from fraudplat.features.redis_state import RedisFeatureState
from fraudplat.features.spec import DAY, US
from fraudplat.features.state import ApplyStatus, InMemoryFeatureState
from fraudplat.hashing import canonical_fields_json, format_utc, sha256_hex
from fraudplat.pipeline.events import encode_value, headers_for, parse_message, to_txn_event
from fraudplat.pipeline.publisher import OutboxPublisher, SimulatedCrash
from fraudplat.pipeline.streams import Stream
from fraudplat.pipeline.topics import delete_consumer_group, delete_topics, ensure_topics
from fraudplat.pipeline.worker import FeatureStoreUnavailable, FeatureWorker

from ..safety import assert_outage_container
from .conftest import (
    API_KEY,
    _optional_setting,
    kafka_test_bootstrap,
    make_settings,
    redis_test_url,
)

pytestmark = pytest.mark.integration
Conn = psycopg.Connection[tuple[object, ...]]
BASE = datetime(2025, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def bootstrap() -> str:
    return kafka_test_bootstrap()


@pytest.fixture
def stream(bootstrap: str) -> Iterator[Stream]:
    s = Stream(f"replay:t{uuid.uuid4().hex[:10]}")
    ensure_topics(bootstrap, s)
    yield s
    delete_topics(bootstrap, s)
    delete_consumer_group(bootstrap, s)


def payload(
    txn: str,
    customer: str = "C1",
    terminal: str = "T1",
    amount: int = 1000,
    when: datetime = BASE,
    event_id: str | None = None,
) -> dict[str, Any]:
    fields = {
        "transaction_id": txn,
        "customer_id": customer,
        "terminal_id": terminal,
        "amount_minor": amount,
        "currency": "USD",
        "event_time": when,
    }
    stamp = format_utc(when + timedelta(seconds=1))
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": "decision.made",
        "schema_version": 1,
        "transaction_id": txn,
        "payload_hash": sha256_hex(canonical_fields_json(**fields)),  # type: ignore[arg-type]
        "customer_id": customer,
        "terminal_id": terminal,
        "amount_minor": amount,
        "currency": "USD",
        "event_time": format_utc(when),
        "received_at": stamp,
        "decision_time": stamp,
        "persisted_at": stamp,
        "action": "approve",
        "score": 0.1,
        "model_version": "test",
        "feature_version": "f1",
        "policy_version": "test",
    }


def produce(
    bootstrap: str, topic: str, messages: Sequence[tuple[bytes | None, bytes, Any]]
) -> None:
    producer = Producer({"bootstrap.servers": bootstrap, "acks": "all"})
    for key, value, headers in messages:
        producer.produce(topic, key=key, value=value, headers=headers)
    assert producer.flush(10) == 0


def valid(p: dict[str, Any]) -> tuple[bytes, bytes, Any]:
    return p["customer_id"].encode(), encode_value(p), headers_for(p)


def topic_size(bootstrap: str, topic: str) -> int:
    consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": "size-probe"})
    meta = consumer.list_topics(topic, timeout=5).topics[topic]
    total = 0
    for pid in meta.partitions:
        low, high = consumer.get_watermark_offsets(TopicPartition(topic, pid), timeout=5)
        total += high - low
    consumer.close()
    return total


def _delete_probe_group(bootstrap: str, group: str) -> None:
    """The group becomes deletable a moment after its consumer leaves; retry briefly."""
    from confluent_kafka import KafkaError, KafkaException
    from confluent_kafka.admin import AdminClient

    admin = AdminClient({"bootstrap.servers": bootstrap})
    for _ in range(20):
        try:
            admin.delete_consumer_groups([group])[group].result(10)
            return
        except KafkaException as exc:
            if exc.args[0].code() == KafkaError.GROUP_ID_NOT_FOUND:
                return
            time.sleep(0.25)
    raise AssertionError(f"probe consumer group {group} could not be deleted")


def dlq_reasons(bootstrap: str, stream: Stream) -> list[str]:
    group = f"dlq-probe-{uuid.uuid4()}"
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": group,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    consumer.subscribe([stream.dlq_topic])
    reasons: list[str] = []
    idle = 0
    while idle < 8:
        msg = consumer.poll(0.5)
        if msg is None or msg.error():
            idle += 1
            continue
        idle = 0
        reasons.append(dict(msg.headers() or [])["dlq_reason"].decode())  # type: ignore[union-attr]
    consumer.close()
    _delete_probe_group(bootstrap, group)
    return sorted(reasons)


async def drain(worker: FeatureWorker, idle_s: float = 3.0) -> None:
    await worker.run(asyncio.Event(), idle_exit_s=idle_s)


def make_worker(bootstrap: str, stream: Stream, redis: Redis, **kwargs: Any) -> FeatureWorker:
    return FeatureWorker(bootstrap, stream, redis, heartbeat_s=0.2, **kwargs)


@pytest.fixture
async def api(database_url: str, db: Conn, stream: Stream) -> AsyncIterator[AsyncClient]:
    """The real API (scaffold scorer) writing outbox rows into this test's stream."""
    app: FastAPI = create_app(make_settings(database_url, feature_namespace=stream.namespace))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t", headers={"X-API-Key": API_KEY}
        ) as http,
    ):
        yield http


async def post_decisions(api: AsyncClient, n: int, customers: int = 3) -> list[dict[str, Any]]:
    bodies = []
    for i in range(n):
        body = {
            "transaction_id": f"p{i:03d}",
            "customer_id": f"C{i % customers}",
            "terminal_id": f"T{i % 4}",
            "amount_minor": 100 + i,
            "currency": "USD",
            "event_time": format_utc(BASE + timedelta(minutes=i)),
        }
        assert (await api.post("/v1/decisions", json=body)).status_code == 201
        bodies.append(body)
    return bodies


def unpublished(db: Conn, stream: Stream) -> int:
    row = db.execute(
        "SELECT count(*) FROM outbox WHERE published_at IS NULL AND stream = %s",
        (stream.namespace,),
    ).fetchone()
    assert row is not None
    count: int = row[0]  # type: ignore[assignment]
    return count


async def customer_sizes(redis: Redis, stream: Stream, customers: int = 3) -> list[int]:
    return [int(await redis.zcard(f"{stream.namespace}:c:C{i}")) for i in range(customers)]


# --- happy path -------------------------------------------------------------------------------


async def test_each_decision_is_applied_once(
    api: AsyncClient,
    db: Conn,
    stream: Stream,
    bootstrap: str,
    database_url: str,
    redis_client: Redis,
) -> None:
    await post_decisions(api, 30)
    publisher = OutboxPublisher(database_url, bootstrap, stream)
    assert publisher.publish_once().acknowledged == 30
    assert unpublished(db, stream) == 0
    worker = make_worker(bootstrap, stream, redis_client)
    await drain(worker)
    assert worker.counts.by_outcome == {"applied": 30}
    assert await customer_sizes(redis_client, stream) == [10, 10, 10]
    assert await worker.lag() == 0
    status = await redis_client.hgetall(stream.worker_status_key)
    assert int(status["consumer_lag_events"]) == 0
    worker.close()
    publisher.close()
    # nothing was written to the live namespace
    assert not await redis_client.keys("live:*")


# --- publisher failures -----------------------------------------------------------------------


async def test_kafka_unavailable_decisions_accumulate_then_publish(
    api: AsyncClient, db: Conn, stream: Stream, bootstrap: str, database_url: str
) -> None:
    """Broker unreachable (a closed port): nothing is acknowledged, nothing is marked published,
    and the API keeps accepting decisions into the outbox."""
    dead = OutboxPublisher(database_url, "127.0.0.1:1", stream, delivery_timeout_ms=1500)
    await post_decisions(api, 5)
    result = dead.publish_once()
    assert (result.selected, result.acknowledged, result.failed) == (5, 0, 5)
    assert unpublished(db, stream) == 5
    more = {
        "transaction_id": "late-1",
        "customer_id": "C9",
        "terminal_id": "T1",
        "amount_minor": 5,
        "currency": "USD",
        "event_time": format_utc(BASE),
    }
    assert (await api.post("/v1/decisions", json=more)).status_code == 201
    assert unpublished(db, stream) == 6
    healthy = OutboxPublisher(database_url, bootstrap, stream)
    assert healthy.publish_once().acknowledged == 6
    assert unpublished(db, stream) == 0
    assert topic_size(bootstrap, stream.topic) == 6


async def test_crash_after_ack_before_marking_causes_harmless_duplicate(
    api: AsyncClient,
    db: Conn,
    stream: Stream,
    bootstrap: str,
    database_url: str,
    redis_client: Redis,
) -> None:
    await post_decisions(api, 12)

    def crash(acked: list[int]) -> None:
        assert len(acked) == 12  # Kafka accepted every event...
        raise SimulatedCrash  # ...but the publisher dies before PostgreSQL records it

    with pytest.raises(SimulatedCrash):
        OutboxPublisher(database_url, bootstrap, stream, after_ack=crash).publish_once()
    assert unpublished(db, stream) == 12  # rolled back: still unpublished
    assert OutboxPublisher(database_url, bootstrap, stream).publish_once().acknowledged == 12
    assert topic_size(bootstrap, stream.topic) == 24  # every event is in Kafka twice

    worker = make_worker(bootstrap, stream, redis_client)
    await drain(worker)
    assert worker.counts.by_outcome == {"applied": 12, "duplicate": 12}
    assert await customer_sizes(redis_client, stream) == [4, 4, 4]  # not inflated
    worker.close()


# --- worker failures --------------------------------------------------------------------------


async def test_worker_crash_after_redis_update_before_commit(
    stream: Stream, bootstrap: str, redis_client: Redis
) -> None:
    events = [
        payload(f"w{i:02d}", customer=f"C{i % 2}", when=BASE + timedelta(minutes=i))
        for i in range(10)
    ]
    produce(bootstrap, stream.topic, [valid(p) for p in events])
    seen: list[str] = []

    def crash_on_sixth(msg: Any, status: ApplyStatus) -> None:
        seen.append(status.value)
        if len(seen) == 6:
            raise SimulatedCrash  # Redis already updated; offset not committed

    first = make_worker(bootstrap, stream, redis_client, after_apply=crash_on_sixth)
    with pytest.raises(SimulatedCrash):
        await drain(first)
    first.close()
    applied_before_crash = sum(await customer_sizes(redis_client, stream, 2))
    assert applied_before_crash == 6

    second = make_worker(bootstrap, stream, redis_client)
    await drain(second)
    second.close()
    # The sixth event is redelivered and recognised; totals are exact.
    assert second.counts.by_outcome.get("duplicate", 0) >= 1
    assert await customer_sizes(redis_client, stream, 2) == [5, 5]

    # Features equal those from applying each event once (the redelivery changed nothing).
    memory = InMemoryFeatureState()
    for i, p in enumerate(events):
        parsed = parse_message(*valid(p))
        assert not isinstance(parsed, type(None))
        memory.apply(to_txn_event(parsed), i)  # type: ignore[arg-type]
    state = RedisFeatureState(redis_client, stream.namespace)
    t = int((BASE + timedelta(hours=1)).timestamp()) * US
    for customer in ("C0", "C1"):
        red = await state.read(customer, "T1", t, "probe")
        mem = memory.read(customer, "T1", t, "probe")
        kwargs: dict[str, Any] = {"amount_minor": 1, "terminal_id": "T1", "event_time_us": t}
        assert compute_features(snapshot=red, **kwargs) == compute_features(snapshot=mem, **kwargs)


def _redis_container() -> str:
    """The disposable test Redis container, named explicitly and checked before it is paused."""
    name = os.environ.get("FRAUD_TEST_REDIS_CONTAINER")
    published = ""
    if name:
        found = subprocess.run(  # noqa: S603
            ["docker", "port", name, "6379/tcp"],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
        )
        published = found.stdout
    assert_outage_container(name, published, redis_test_url(), _optional_setting("FRAUD_REDIS_URL"))
    assert name is not None
    return name


def _docker(action: str) -> None:
    """Pause/unpause the Redis container to simulate an outage (fixed arguments, local only)."""
    subprocess.run(  # noqa: S603
        ["docker", action, _redis_container()],  # noqa: S607
        check=True,
        capture_output=True,
    )


async def test_redis_outage_within_retry_budget_recovers(stream: Stream, bootstrap: str) -> None:
    redis = Redis.from_url(
        redis_test_url(),
        decode_responses=True,
        socket_timeout=0.5,
        socket_connect_timeout=0.5,
    )
    await redis.flushdb()
    produce(
        bootstrap,
        stream.topic,
        [valid(payload(f"r{i}", when=BASE + timedelta(minutes=i))) for i in range(5)],
    )
    worker = make_worker(bootstrap, stream, redis, max_attempts=12, base_backoff_s=0.2)
    _docker("pause")
    try:
        task = asyncio.create_task(drain(worker, idle_s=2.0))
        await asyncio.sleep(3)
    finally:
        _docker("unpause")
    await task
    worker.close()
    assert worker.counts.by_outcome == {"applied": 5}
    assert await redis.zcard(f"{stream.namespace}:c:C1") == 5
    await redis.aclose()


async def test_redis_outage_beyond_budget_stops_without_commit(
    stream: Stream, bootstrap: str
) -> None:
    redis = Redis.from_url(
        redis_test_url(),
        decode_responses=True,
        socket_timeout=0.3,
        socket_connect_timeout=0.3,
    )
    await redis.flushdb()
    produce(
        bootstrap,
        stream.topic,
        [valid(payload(f"b{i}", when=BASE + timedelta(minutes=i))) for i in range(4)],
    )
    worker = make_worker(bootstrap, stream, redis, max_attempts=2, base_backoff_s=0.1)
    _docker("pause")
    try:
        with pytest.raises(FeatureStoreUnavailable):
            await drain(worker)
    finally:
        _docker("unpause")
    worker.close()
    # Nothing was committed, so a restarted worker applies each of the four once.
    restarted = make_worker(bootstrap, stream, redis)
    await drain(restarted)
    restarted.close()
    assert restarted.counts.by_outcome == {"applied": 4}
    assert await redis.zcard(f"{stream.namespace}:c:C1") == 4
    await redis.aclose()


# --- invalid, conflicting, out-of-order and late events ---------------------------------------


async def test_invalid_events_are_dead_lettered_and_committed(
    stream: Stream, bootstrap: str, redis_client: Redis
) -> None:
    good = payload("ok-1")
    tampered = {**payload("bad-hash"), "amount_minor": 999_999}  # hash no longer matches fields
    header_lie = payload("bad-header")
    messages = [
        (b"C1", b"{not json", None),
        (b"C1", encode_value({"event_type": "decision.made"}), None),  # schema violation
        (b"C-other", encode_value(payload("bad-key")), None),  # key is not the customer
        valid(tampered),
        (b"C1", encode_value(header_lie), [("event_id", b"not-the-same")]),
        valid(good),
    ]
    produce(bootstrap, stream.topic, messages)
    worker = make_worker(bootstrap, stream, redis_client)
    await drain(worker)
    assert worker.counts.by_outcome["applied"] == 1
    assert sum(v for k, v in worker.counts.by_outcome.items() if k.startswith("invalid:")) == 5
    assert await worker.lag() == 0  # every message, valid or not, was committed after handling
    worker.close()
    assert dlq_reasons(bootstrap, stream) == sorted(
        [
            "MALFORMED_JSON",
            "SCHEMA_VIOLATION",
            "KEY_MISMATCH",
            "PAYLOAD_HASH_MISMATCH",
            "HEADER_MISMATCH",
        ]
    )
    assert await redis_client.zrange(f"{stream.namespace}:c:C1", 0, -1) == ["ok-1"]


async def test_conflicting_duplicates_are_observable_and_do_not_overwrite(
    stream: Stream, bootstrap: str, redis_client: Redis
) -> None:
    original = payload("tx-1", amount=1000)
    same_again = original  # identical duplicate: harmless
    other_amount = payload("tx-1", amount=2000)  # valid on its own, conflicts with history
    other_event_id = {**original, "event_id": str(uuid.uuid4())}  # same txn, new event id
    stolen_id = payload("tx-2", when=BASE - timedelta(hours=1), event_id=original["event_id"])
    produce(
        bootstrap,
        stream.topic,
        [valid(p) for p in (original, same_again, other_amount, other_event_id, stolen_id)],
    )
    worker = make_worker(bootstrap, stream, redis_client)
    await drain(worker)
    worker.close()
    assert worker.counts.by_outcome == {
        "applied": 1,
        "duplicate": 1,
        "payload_conflict": 1,
        "event_id_conflict": 2,
    }
    assert dlq_reasons(bootstrap, stream) == [
        "EVENT_ID_CONFLICT",
        "EVENT_ID_CONFLICT",
        "PAYLOAD_CONFLICT",
    ]
    record = await redis_client.hgetall(f"{stream.namespace}:e:tx-1")
    assert record["amount_minor"] == "1000"  # history kept the first payload
    assert record["source_event_id"] == original["event_id"]
    assert await redis_client.zrange(f"{stream.namespace}:c:C1", 0, -1) == ["tx-1"]


async def test_out_of_order_and_too_late_events(
    stream: Stream, bootstrap: str, redis_client: Redis
) -> None:
    rng = random.Random(4)
    events = [
        payload(
            f"o{i:02d}",
            amount=100 + i,
            terminal=f"T{i % 3}",
            when=BASE + timedelta(hours=rng.randrange(0, 72)),
        )
        for i in range(20)
    ]
    newest = max(e["event_time"] for e in events)
    too_late = payload("too-late", when=datetime.fromisoformat(newest) - timedelta(days=32))
    shuffled = events[:]
    rng.shuffle(shuffled)
    produce(bootstrap, stream.topic, [valid(p) for p in [*shuffled, too_late]])
    worker = make_worker(bootstrap, stream, redis_client)
    await drain(worker)
    worker.close()
    assert worker.counts.by_outcome == {"applied": 20, "late_beyond_horizon": 1}
    assert dlq_reasons(bootstrap, stream) == ["LATE_BEYOND_HORIZON"]

    memory = InMemoryFeatureState()
    for i, p in enumerate(sorted(events, key=lambda e: e["event_time"])):  # in event-time order
        memory.apply(to_txn_event(parse_message(*valid(p))), i)  # type: ignore[arg-type]
    state = RedisFeatureState(redis_client, stream.namespace)
    for probe_hours in (6, 30, 80):
        t = int((BASE + timedelta(hours=probe_hours)).timestamp()) * US
        red = await state.read("C1", "T0", t, "probe")
        mem = memory.read("C1", "T0", t, "probe")
        kwargs: dict[str, Any] = {"amount_minor": 1, "terminal_id": "T0", "event_time_us": t}
        assert compute_features(snapshot=red, **kwargs) == compute_features(snapshot=mem, **kwargs)
    _ = DAY


# --- pipeline health seen by the API ----------------------------------------------------------


async def test_api_detects_stalled_pipeline_while_redis_is_reachable(
    database_url: str, db: Conn, stream: Stream, redis_client: Redis
) -> None:
    settings = make_settings(
        database_url, feature_namespace=stream.namespace, pipeline_monitor_interval_s=0.1
    )
    app = create_app(settings, pipeline_redis=redis_client, monitor_pipeline=True)
    now_us = int(datetime.now(UTC).timestamp()) * US
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t", headers={"X-API-Key": API_KEY}
        ) as http,
    ):
        # A fresh heartbeat, no lag: healthy.
        await redis_client.hset(
            stream.worker_status_key, mapping={"heartbeat_at_us": now_us, "consumer_lag_events": 0}
        )
        await asyncio.sleep(0.3)
        healthy = await http.post(
            "/v1/decisions",
            json={
                "transaction_id": "h1",
                "customer_id": "C1",
                "terminal_id": "T1",
                "amount_minor": 1,
                "currency": "USD",
                "event_time": format_utc(BASE),
            },
        )
        assert "PIPELINE_DEGRADED" not in healthy.json()["reason_codes"]
        # Heartbeat 60 s old (worker stalled) while Redis answers: degraded, still decided.
        await redis_client.hset(
            stream.worker_status_key, mapping={"heartbeat_at_us": now_us - 60 * US}
        )
        await asyncio.sleep(0.3)
        stale = await http.post(
            "/v1/decisions",
            json={
                "transaction_id": "h2",
                "customer_id": "C1",
                "terminal_id": "T1",
                "amount_minor": 1,
                "currency": "USD",
                "event_time": format_utc(BASE),
            },
        )
        assert stale.status_code == 201
        assert {"PIPELINE_DEGRADED", "WORKER_HEARTBEAT_STALE"} <= set(stale.json()["reason_codes"])
        ready = await http.get("/readyz")
        assert ready.status_code == 200 and ready.json()["status"] == "degraded"


async def test_backlog_limit_rejects_new_decisions_but_answers_retries(
    database_url: str, db: Conn, stream: Stream, redis_client: Redis
) -> None:
    settings = make_settings(
        database_url,
        feature_namespace=stream.namespace,
        pipeline_monitor_interval_s=0.1,
        outbox_backlog_reject_rows=3,
    )
    app = create_app(settings, pipeline_redis=redis_client, monitor_pipeline=True)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t", headers={"X-API-Key": API_KEY}
        ) as http,
    ):
        bodies = await post_decisions(http, 4)  # nothing publishes: backlog grows to 4 > 3
        await asyncio.sleep(0.3)
        rejected = await http.post("/v1/decisions", json={**bodies[0], "transaction_id": "new"})
        assert rejected.status_code == 503
        assert rejected.json() == {"error": "pipeline_backlog_limit"}
        retry = await http.post("/v1/decisions", json=bodies[0])
        assert retry.status_code == 200 and retry.json()["idempotent_replay"] is True
        ready = await http.get("/readyz")
        assert ready.status_code == 503 and ready.json()["pipeline"]["reject"] is not None
    assert unpublished(db, stream) == 4  # bounded: the rejected request added nothing
    _ = json


async def test_stalled_update_is_not_reported_healthy(
    database_url: str, db: Conn, stream: Stream, bootstrap: str, redis_client: Redis
) -> None:
    """The worker fetches a message but its Redis update does not finish (a 6 s stall stands in
    for a hung update). Redis itself stays reachable. The API must see a stale heartbeat, and
    the last reported lag must not claim the fetched-but-unapplied message is done."""
    from fraudplat.config import Settings
    from fraudplat.pipeline.status import PipelineMonitor
    from fraudplat.storage.decisions import DecisionStore

    produce(bootstrap, stream.topic, [valid(payload("stall-1"))])
    worker = make_worker(bootstrap, stream, redis_client, apply_delay_s=6.0)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        # Wait for the first heartbeat, i.e. the worker is assigned and polling.
        for _ in range(100):
            if await redis_client.exists(stream.worker_status_key):
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(3.0)  # the message is fetched and stuck in its update
        status = await redis_client.hgetall(stream.worker_status_key)
        assert int(status["consumer_lag_events"]) != 0  # 1 (end - committed) or -1 (unknown)
        assert not await redis_client.exists(f"{stream.namespace}:e:stall-1")

        from psycopg_pool import AsyncConnectionPool

        async with AsyncConnectionPool(database_url, open=False) as pool:
            settings: Settings = make_settings(database_url, worker_heartbeat_max_age_s=1.5)
            monitor = PipelineMonitor(
                DecisionStore(pool, stream.namespace), redis_client, stream, settings
            )
            health = await monitor.refresh()
        assert "WORKER_HEARTBEAT_STALE" in health.reasons
    finally:
        stop.set()
        await task
        worker.close()
    assert await redis_client.exists(f"{stream.namespace}:e:stall-1")  # finished after the stall


async def test_live_state_does_not_influence_replay_health(
    database_url: str, db: Conn, stream: Stream, redis_client: Redis
) -> None:
    from psycopg_pool import AsyncConnectionPool

    from fraudplat.pipeline.status import PipelineMonitor
    from fraudplat.storage.decisions import DecisionStore

    now_us = int(datetime.now(UTC).timestamp()) * US
    # Live: stale heartbeat and a two-hour-old unpublished outbox row.
    await redis_client.hset(
        "live:pipeline:worker",
        mapping={
            "namespace": "live",
            "heartbeat_at_us": now_us - 3600 * US,
            "consumer_lag_events": 99_999,
        },
    )
    db.execute("""INSERT INTO outbox (event_id, aggregate_id, event_type, schema_version, payload,
                  created_at, stream) VALUES (gen_random_uuid(), 'live-row', 'decision.made', 1,
                  '{}', now() - interval '2 hours', 'live')""")
    # Replay: healthy heartbeat, empty backlog.
    await redis_client.hset(
        stream.worker_status_key,
        mapping={
            "namespace": stream.namespace,
            "heartbeat_at_us": now_us,
            "consumer_lag_events": 0,
        },
    )
    # A heartbeat copied under the replay key but written by another run is rejected.
    other = Stream("replay:someone-else")
    await redis_client.hset(
        other.worker_status_key,
        mapping={
            "namespace": stream.namespace,
            "heartbeat_at_us": now_us,
            "consumer_lag_events": 0,
        },
    )

    async with AsyncConnectionPool(database_url, open=False) as pool:
        settings = make_settings(database_url)
        replay = await PipelineMonitor(
            DecisionStore(pool, stream.namespace), redis_client, stream, settings
        ).refresh()
        live = await PipelineMonitor(
            DecisionStore(pool, "live"), redis_client, Stream("live"), settings
        ).refresh()
        spoofed = await PipelineMonitor(
            DecisionStore(pool, other.namespace), redis_client, other, settings
        ).refresh()
    assert replay.reasons == () and replay.detail["outbox_backlog"] == 0
    assert "WORKER_HEARTBEAT_STALE" in live.reasons and "OUTBOX_BACKLOG_AGED" in live.reasons
    assert spoofed.detail["worker_status"] == "namespace_mismatch"
    assert "WORKER_HEARTBEAT_MISSING" in spoofed.reasons


def test_deleting_an_absent_consumer_group_is_a_no_op(bootstrap: str) -> None:
    """Retiring a namespace after the broker was recreated must not fail on its missing group."""
    delete_consumer_group(bootstrap, Stream(f"replay:absent-{uuid.uuid4().hex[:8]}"))
