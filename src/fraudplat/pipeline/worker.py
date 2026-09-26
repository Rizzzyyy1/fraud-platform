"""Feature worker: Kafka `decision.made` events → Redis feature state.

Offsets are committed manually, one message at a time, and only after that message is durably
handled:
  * valid event → applied to Redis (APPLIED or DUPLICATE) → commit;
  * PAYLOAD_CONFLICT / EVENT_ID_CONFLICT / LATE_BEYOND_HORIZON → nothing written to history;
    the original message plus a reason is written to the DLQ and acknowledged → commit;
  * invalid message (see `events.parse_message`) → DLQ → commit.
Messages are handled sequentially, so an offset is never committed past an earlier unfinished
message of the same partition.

Failures: Redis errors are retried with exponential backoff up to `max_attempts`; then the worker
raises `FeatureStoreUnavailable` and stops without committing, so the message is redelivered
after restart. A DLQ write that is not acknowledged also stops the worker without committing.
A crash after the Redis update but before the commit causes redelivery; the second application
is a DUPLICATE and changes no feature (tested).

Heartbeat: every `heartbeat_s` the processing loop writes `{ns}:pipeline:worker` in Redis:
heartbeat time, lag = broker end offset - committed progress (or -1 when unknown or when the
broker view is stale), the view's age, fetch position and committed progress. The API reads it
to detect an unhealthy pipeline even while Redis answers: a loop stuck on an update stops
writing the heartbeat, and fetched-but-unapplied messages still count as lag.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from confluent_kafka import (
    Consumer,
    ConsumerGroupTopicPartitions,
    KafkaError,
    Message,
    Producer,
    TopicPartition,
)
from confluent_kafka.admin import (  # type: ignore[attr-defined]  # not re-exported in stubs
    AdminClient,
    OffsetSpec,
)
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from redis.asyncio import Redis
from redis.exceptions import RedisError

from fraudplat.config import Settings
from fraudplat.features.redis_state import RedisFeatureState
from fraudplat.features.spec import US
from fraudplat.features.state import ApplyStatus
from fraudplat.pipeline.events import (
    Headers,
    InvalidEvent,
    decision_time_us,
    parse_message,
    to_txn_event,
)
from fraudplat.pipeline.streams import Stream

logger = logging.getLogger("fraudplat.worker")

EVENTS = Counter("worker_events_total", "Messages handled", ["outcome"])
REDIS_RETRIES = Counter("worker_redis_retries_total", "Redis apply retries")
LAG = Gauge("worker_consumer_lag_events", "Sum over assigned partitions of end offset - position")
DELAY = Histogram(
    "worker_decision_to_apply_seconds",
    "applied_at (worker clock) - decision_time (API clock) of applied events",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300),
)

DLQ_STATUSES = {
    ApplyStatus.PAYLOAD_CONFLICT,
    ApplyStatus.EVENT_ID_CONFLICT,
    ApplyStatus.LATE_BEYOND_HORIZON,
}


class FeatureStoreUnavailable(Exception):
    pass


class DeadLetterFailed(Exception):
    pass


@dataclass
class WorkerCounts:
    by_outcome: dict[str, int] = field(default_factory=dict)

    def add(self, outcome: str) -> None:
        self.by_outcome[outcome] = self.by_outcome.get(outcome, 0) + 1
        EVENTS.labels(outcome).inc()


def _now_us() -> int:
    return time.time_ns() // 1000


class BrokerView:
    """Committed offsets and end offsets for the worker's partitions, refreshed by a background
    thread with its own admin client, so no broker round trip happens in the processing loop.

    Each entry records when it was fetched; `snapshot()` returns (end_offset, committed_offset,
    fetched_at_monotonic) per partition. A failed refresh leaves the old entries in place, and
    their age grows until the worker reports lag as unknown.
    """

    def __init__(self, bootstrap: str, group: str, refresh_s: float = 1.0, timeout_s: float = 5.0):
        self._admin = AdminClient({"bootstrap.servers": bootstrap})
        self._group = group
        self._refresh_s = refresh_s
        self._timeout_s = timeout_s
        self._partitions: list[tuple[str, int]] = []
        self._view: dict[tuple[str, int], tuple[int, int, float]] = {}
        self.last_refresh_seconds: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="broker-view", daemon=True)

    def watch(self, partitions: list[tuple[str, int]]) -> None:
        self._partitions = partitions  # replaced atomically; read by the refresh thread

    def snapshot(self) -> dict[tuple[str, int], tuple[int, int, float]]:
        return dict(self._view)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(self._timeout_s + 1)

    def refresh(self) -> None:
        parts = self._partitions
        if not parts:
            return
        started = time.monotonic()
        tps = [TopicPartition(t, p) for t, p in parts]
        ends = self._admin.list_offsets(
            {tp: OffsetSpec.latest() for tp in tps},  # type: ignore[no-untyped-call]
            request_timeout=self._timeout_s,
        )
        committed_future = self._admin.list_consumer_group_offsets(
            [ConsumerGroupTopicPartitions(self._group, tps)], request_timeout=self._timeout_s
        )[self._group]
        committed = {
            (tp.topic, tp.partition): tp.offset
            for tp in committed_future.result(self._timeout_s).topic_partitions
        }
        fetched_at = time.monotonic()
        for tp, future in ends.items():
            key = (tp.topic, tp.partition)
            self._view[key] = (
                future.result(self._timeout_s).offset,
                committed.get(key, -1),
                fetched_at,
            )
        self.last_refresh_seconds = fetched_at - started

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as exc:
                logger.warning("broker view refresh failed: %s", exc)
            self._stop.wait(self._refresh_s)


class FeatureWorker:
    def __init__(
        self,
        bootstrap: str,
        stream: Stream,
        redis: Redis,
        *,
        max_attempts: int = 8,
        base_backoff_s: float = 0.1,
        heartbeat_s: float = 1.0,
        apply_delay_s: float = 0.0,
        after_apply: Callable[[Message, ApplyStatus], None] | None = None,
        client_id: str | None = None,
        lag_stale_after_s: float = 10.0,
    ) -> None:
        self._stream = stream
        self._redis = redis
        self._state = RedisFeatureState(redis, stream.namespace)
        self._max_attempts = max_attempts
        self._base_backoff_s = base_backoff_s
        self._heartbeat_s = heartbeat_s
        self._apply_delay_s = apply_delay_s  # demonstration only: deliberate consumer delay
        self._after_apply = after_apply
        self.counts = WorkerCounts()
        self._consumer = Consumer(
            {
                "bootstrap.servers": bootstrap,
                "group.id": stream.consumer_group,
                "client.id": client_id or f"worker-{os.getpid()}",
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
                "auto.offset.reset": "earliest",
                "isolation.level": "read_committed",
                "session.timeout.ms": 10_000,
                "max.poll.interval.ms": 300_000,
            }
        )
        self._dlq = Producer(
            {
                "bootstrap.servers": bootstrap,
                "enable.idempotence": True,
                "acks": "all",
                "message.timeout.ms": 10_000,
            }
        )
        self._consumer.subscribe([stream.topic])
        self._last_heartbeat = 0.0
        self._lag_stale_after_s = lag_stale_after_s
        # Next offset after the last message fully handled *and* committed, per partition.
        self._committed_next: dict[tuple[str, int], int] = {}
        self._broker = BrokerView(bootstrap, stream.consumer_group)
        self._broker.start()

    # --- handling -------------------------------------------------------------------------

    def _dead_letter(self, msg: Message, reason: str, detail: str, event_id: str | None) -> None:
        errors: list[KafkaError] = []
        headers: Headers = [
            ("dlq_reason", reason.encode()),
            ("dlq_detail", detail.encode()[:1000]),
            ("source_topic", (msg.topic() or "").encode()),
            ("source_partition", str(msg.partition()).encode()),
            ("source_offset", str(msg.offset()).encode()),
        ]
        if event_id:
            headers.append(("event_id", event_id.encode()))
        self._dlq.produce(
            self._stream.dlq_topic,
            key=msg.key(),
            value=msg.value(),
            headers=headers,
            on_delivery=lambda err, _m: errors.append(err) if err is not None else None,
        )
        remaining = self._dlq.flush(15)
        if remaining or errors:
            raise DeadLetterFailed(f"DLQ write not acknowledged for offset {msg.offset()}")

    async def _apply_with_retry(self, event: Any) -> ApplyStatus:
        delay = self._base_backoff_s
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await self._state.apply(event, applied_at_us=_now_us())
            except (RedisError, OSError) as exc:
                if attempt == self._max_attempts:
                    raise FeatureStoreUnavailable(str(exc)) from exc
                REDIS_RETRIES.inc()
                logger.warning("redis apply failed (attempt %d): %s", attempt, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
        raise AssertionError("unreachable")

    async def handle(self, msg: Message) -> str:
        parsed = parse_message(msg.key(), msg.value(), msg.headers())
        if isinstance(parsed, InvalidEvent):
            await asyncio.to_thread(
                self._dead_letter, msg, parsed.reason, parsed.detail, parsed.event_id
            )
            return f"invalid:{parsed.reason}"
        if self._apply_delay_s:
            await asyncio.sleep(self._apply_delay_s)
        event = to_txn_event(parsed)
        status = await self._apply_with_retry(event)
        if self._after_apply is not None:
            self._after_apply(msg, status)  # test hook: crash after apply, before commit
        if status in DLQ_STATUSES:
            await asyncio.to_thread(
                self._dead_letter, msg, status.value.upper(), event.event_id, str(parsed.event_id)
            )
        elif status is ApplyStatus.APPLIED:
            # decision_time comes from the API's host clock, as does applied_at; persisted_at comes
            # from the database clock, which may be skewed (it runs in a VM locally).
            DELAY.observe(max(_now_us() - decision_time_us(parsed), 0) / US)
        return status.value

    # --- loop -----------------------------------------------------------------------------

    def progress(self) -> dict[str, Any]:
        """Three different positions, summed over assigned partitions:

        * fetch position — next offset the consumer will *read* (advances on poll);
        * committed progress — next offset after the last message fully handled and committed
          (Redis updated or DLQ acknowledged, then offset committed);
        * end offset — from the broker view, with its age.
        Lag = end offset - committed progress. It is None (unknown) if nothing is assigned, a
        partition has no broker entry, or the broker view is older than `lag_stale_after_s`.
        A message that has been fetched but whose Redis update is stuck still counts as lag.
        """
        assignment = self._consumer.assignment()
        keys = [(tp.topic, tp.partition) for tp in assignment]
        self._broker.watch(keys)
        view = self._broker.snapshot()
        now = time.monotonic()
        positions = self._consumer.position(assignment) if assignment else []
        fetch_total = sum(max(p.offset, 0) for p in positions)
        committed_total, lag, ages = 0, 0, []
        known = bool(keys)
        for key in keys:
            entry = view.get(key)
            if entry is None:
                known = False
                continue
            end, broker_committed, fetched_at = entry
            ages.append(now - fetched_at)
            progress = max(self._committed_next.get(key, -1), broker_committed, 0)
            committed_total += progress
            lag += max(end - progress, 0)
        age = max(ages) if ages else None
        if age is None or age > self._lag_stale_after_s:
            known = False
        return {
            "assigned_partitions": len(keys),
            "fetch_position_total": fetch_total,
            "committed_progress_total": committed_total,
            "consumer_lag_events": lag if known else None,
            "broker_view_age_s": None if age is None else round(age, 3),
        }

    async def _heartbeat(self, force: bool = False) -> None:
        """Written only from the processing loop (after a commit or an empty poll), so a loop
        stuck on a Redis update stops refreshing `heartbeat_at_us`."""
        now = time.monotonic()
        if not force and now - self._last_heartbeat < self._heartbeat_s:
            return
        self._last_heartbeat = now
        progress = self.progress()
        lag = progress["consumer_lag_events"]
        if lag is not None:
            LAG.set(lag)
        age = progress["broker_view_age_s"]
        try:
            await self._redis.hset(
                self._stream.worker_status_key,
                mapping={
                    "namespace": self._stream.namespace,
                    "heartbeat_at_us": _now_us(),
                    "consumer_lag_events": -1 if lag is None else lag,
                    "lag_basis": "end offset - committed progress",
                    "broker_view_age_ms": -1 if age is None else int(age * 1000),
                    "fetch_position_total": progress["fetch_position_total"],
                    "committed_progress_total": progress["committed_progress_total"],
                    "assigned_partitions": progress["assigned_partitions"],
                    "pid": os.getpid(),
                    **{f"count_{k}": v for k, v in self.counts.by_outcome.items()},
                },
            )
        except (RedisError, OSError):
            logger.warning("heartbeat not written: redis unavailable")

    async def run(self, stop: asyncio.Event, idle_exit_s: float | None = None) -> None:
        """Consume until `stop` is set (or, if `idle_exit_s` is given, until idle that long)."""
        idle_since = time.monotonic()
        while not stop.is_set():
            msg = await asyncio.to_thread(self._consumer.poll, 0.2)
            if msg is None:
                if not self._consumer.assignment():
                    idle_since = time.monotonic()  # still joining the group: not idle yet
                elif idle_exit_s is not None and time.monotonic() - idle_since > idle_exit_s:
                    break
                await self._heartbeat()
                continue
            error = msg.error()
            if error is not None:
                if error.code() != KafkaError._PARTITION_EOF:
                    logger.warning("consumer error: %s", error)
                continue
            idle_since = time.monotonic()
            outcome = await self.handle(msg)
            await asyncio.to_thread(self._consumer.commit, message=msg, asynchronous=False)
            self._committed_next[(msg.topic() or "", msg.partition() or 0)] = (
                msg.offset() or 0
            ) + 1
            self.counts.add(outcome)
            await self._heartbeat()
        await self._heartbeat(force=True)

    async def lag(self, wait_s: float = 5.0) -> int | None:
        """Lag once the broker view is fresher than the last commit (for tests and tools)."""
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            self._broker.refresh()
            lag = self.progress()["consumer_lag_events"]
            if lag == 0:
                return 0
            await asyncio.sleep(0.2)
        final: int | None = self.progress()["consumer_lag_events"]
        return final

    def close(self) -> None:
        """Leave the group without committing anything further."""
        self._broker.close()
        self._consumer.close()
        self._dlq.flush(5)


async def _main_async(args: argparse.Namespace) -> int:
    settings = Settings()
    assert settings.redis_url is not None, "FRAUD_REDIS_URL must be set"
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    worker = FeatureWorker(
        settings.kafka_bootstrap,
        Stream(args.namespace),
        redis,
        apply_delay_s=args.apply_delay_ms / 1000,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await worker.run(stop)
    finally:
        worker.close()
        await redis.aclose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="live")
    parser.add_argument("--metrics-port", type=int, default=9102)
    parser.add_argument(
        "--apply-delay-ms",
        type=float,
        default=0.0,
        help="deliberate per-event delay, for the skew demonstration only",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.metrics_port:
        start_http_server(args.metrics_port, addr="127.0.0.1")
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
