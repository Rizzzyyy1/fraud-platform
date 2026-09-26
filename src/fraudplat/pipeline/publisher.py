"""Outbox publisher: unpublished outbox rows → Kafka, marked published only after Kafka acks.

One cycle, inside one PostgreSQL transaction:
  1. lock up to `batch_size` unpublished rows of this stream, oldest first
     (`FOR UPDATE SKIP LOCKED`, so several publishers never publish the same row concurrently);
  2. produce each row (key customer_id; idempotent producer, acks=all) and wait for delivery
     reports, bounded by `delivery_timeout_ms`;
  3. set `published_at` only for rows Kafka acknowledged; commit.
Rows that were not acknowledged stay unpublished and are retried next cycle, with exponential
backoff while cycles keep failing.

Delivery is at-least-once. A crash between step 2 and step 3 leaves acknowledged rows
unpublished, so they are produced again later with the same event_id; the worker treats the
second copy as a DUPLICATE. A partially failed batch can publish a later event before an earlier
one; feature updates commute within the retention horizon, so order is not relied upon.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
from confluent_kafka import KafkaError, Message, Producer
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from fraudplat.config import Settings
from fraudplat.pipeline.events import encode_value, headers_for
from fraudplat.pipeline.streams import Stream

logger = logging.getLogger("fraudplat.publisher")

PUBLISHED = Counter("publisher_events_published_total", "Outbox rows acknowledged by Kafka")
FAILED = Counter("publisher_delivery_failures_total", "Delivery reports with an error")
CYCLES = Counter("publisher_cycles_total", "Publish cycles", ["outcome"])
BACKLOG = Gauge("publisher_outbox_backlog", "Unpublished outbox rows for this stream")
OLDEST_AGE = Gauge("publisher_oldest_unpublished_age_seconds", "Age of oldest unpublished row")
CYCLE_SECONDS = Histogram("publisher_cycle_seconds", "Duration of one publish cycle")


@dataclass(frozen=True)
class CycleResult:
    selected: int
    acknowledged: int
    failed: int


class SimulatedCrash(Exception):
    """Raised by test hooks to stop a component at a precise point."""


class OutboxPublisher:
    def __init__(
        self,
        database_url: str,
        bootstrap: str,
        stream: Stream,
        batch_size: int = 500,
        delivery_timeout_ms: int = 10_000,
        after_ack: Callable[[list[int]], None] | None = None,
    ) -> None:
        self._database_url = database_url
        self._stream = stream
        self._batch_size = batch_size
        self._delivery_timeout_s = delivery_timeout_ms / 1000
        self._after_ack = after_ack
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap,
                "enable.idempotence": True,
                "acks": "all",
                "linger.ms": 5,
                "message.timeout.ms": delivery_timeout_ms,
                "socket.connection.setup.timeout.ms": 2000,
                "reconnect.backoff.max.ms": 1000,
            }
        )
        self._conn: psycopg.Connection[tuple[Any, ...]] | None = None

    def _connection(self) -> psycopg.Connection[tuple[Any, ...]]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._database_url, connect_timeout=2)
        return self._conn

    def publish_once(self) -> CycleResult:
        started = time.perf_counter()
        conn = self._connection()
        with conn.transaction():
            rows = conn.execute(
                "SELECT id, payload FROM outbox WHERE published_at IS NULL AND stream = %s "
                "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
                (self._stream.namespace, self._batch_size),
            ).fetchall()
            acked: list[int] = []
            failed: list[int] = []

            def report(row_id: int) -> Callable[[KafkaError | None, Message], None]:
                def callback(err: KafkaError | None, _msg: Message) -> None:
                    (failed if err is not None else acked).append(row_id)

                return callback

            for row_id, payload in rows:
                self._producer.produce(
                    self._stream.topic,
                    key=payload["customer_id"].encode(),
                    value=encode_value(payload),
                    headers=headers_for(payload),
                    on_delivery=report(row_id),
                )
            if rows:
                self._producer.flush(self._delivery_timeout_s + 1)
            if self._after_ack is not None:
                self._after_ack(sorted(acked))  # test hook: crash after ack, before marking
            if acked:
                conn.execute(
                    "UPDATE outbox SET published_at = clock_timestamp() WHERE id = ANY(%s)",
                    (acked,),
                )
        PUBLISHED.inc(len(acked))
        FAILED.inc(len(failed))
        CYCLE_SECONDS.observe(time.perf_counter() - started)
        return CycleResult(len(rows), len(acked), len(failed))

    def refresh_backlog_metrics(self) -> None:
        count, age = self._connection().execute(
            "SELECT count(*), coalesce(extract(epoch FROM clock_timestamp() - min(created_at)), 0)"
            " FROM outbox WHERE published_at IS NULL AND stream = %s",
            (self._stream.namespace,),
        ).fetchone() or (0, 0)
        self._connection().commit()
        BACKLOG.set(count)
        OLDEST_AGE.set(float(age))

    def run(
        self, stop: threading.Event, idle_sleep_s: float = 0.05, max_backoff_s: float = 5.0
    ) -> None:
        backoff = idle_sleep_s
        while not stop.is_set():
            try:
                result = self.publish_once()
                self.refresh_backlog_metrics()
            except psycopg.OperationalError:
                logger.warning("database unavailable; retrying")
                CYCLES.labels("database_error").inc()
                self._conn = None
                stop.wait(backoff)
                backoff = min(backoff * 2, max_backoff_s)
                continue
            if result.failed:
                CYCLES.labels("delivery_failed").inc()
                logger.warning("%d of %d events not acknowledged", result.failed, result.selected)
                stop.wait(backoff)
                backoff = min(backoff * 2, max_backoff_s)
                continue
            CYCLES.labels("ok" if result.selected else "idle").inc()
            backoff = idle_sleep_s
            if result.selected < self._batch_size:
                stop.wait(idle_sleep_s)

    def close(self) -> None:
        self._producer.flush(1)
        if self._conn is not None:
            self._conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--namespace", default="live")
    parser.add_argument("--metrics-port", type=int, default=9101)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = Settings()
    publisher = OutboxPublisher(
        settings.database_url.get_secret_value(), settings.kafka_bootstrap, Stream(args.namespace)
    )
    if args.metrics_port:
        start_http_server(args.metrics_port, addr="127.0.0.1")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        publisher.run(stop)
    finally:
        publisher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
