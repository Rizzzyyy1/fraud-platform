"""Lightweight serving instrumentation (Prometheus), used to locate where decision time goes.

Stage histograms (seconds) for one `POST /v1/decisions`:
    request_total      ASGI time from request start to response end (middleware)
    handler            the endpoint coroutine (validation of the body happens before it)
    db_pool_wait       waiting for a PostgreSQL connection from the pool (per acquisition)
    db_precheck        idempotency pre-check query (including pool wait)
    feature_read       Redis snapshot read as awaited by the scorer (includes pool/loop delay)
    redis_call         the Redis script call itself, measured inside the feature-state read
    feature_compute    pure feature computation from the snapshot
    model_input        converting the feature dict into the model's input array
    inference          model prediction (wall); `inference_cpu` is thread CPU time
    policy             policy decision
    db_insert          decision + outbox transaction (including pool wait)
`request_total - handler` covers body parsing/validation, response serialisation and framework
overhead. `event_loop_lag` samples how late a 50 ms timer fires; it measures scheduling delay
that every awaited step (including the 50 ms feature-read timeout) is subject to.
`fraud_decision_responses_total{status}` counts decision responses by HTTP status (a request
that fails before a response starts is counted as 500).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

_BUCKETS = (
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

STAGE = Histogram("fraud_stage_seconds", "Decision stage duration", ["stage"], buckets=_BUCKETS)
LOOP_LAG = Histogram(
    "fraud_event_loop_lag_seconds", "Timer lateness of a 50 ms sleep", buckets=_BUCKETS
)
LOOP_LAG_MAX = Gauge("fraud_event_loop_lag_max_seconds", "Largest lag since the last scrape reset")
FEATURE_TIMEOUTS = Counter("fraud_feature_read_timeouts_total", "Feature reads that timed out")
RESPONSES = Counter(
    "fraud_decision_responses_total", "POST /v1/decisions responses by HTTP status", ["status"]
)


@contextmanager
def stage(name: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        STAGE.labels(name).observe(time.perf_counter() - start)


@contextmanager
def cpu_stage(name: str) -> Iterator[None]:
    """Wall time as `name` and this thread's CPU time as `name_cpu`."""
    wall, cpu = time.perf_counter(), time.thread_time()
    try:
        yield
    finally:
        STAGE.labels(name).observe(time.perf_counter() - wall)
        STAGE.labels(f"{name}_cpu").observe(time.thread_time() - cpu)


async def monitor_event_loop(stop: asyncio.Event, interval_s: float = 0.05) -> None:
    worst = 0.0
    while not stop.is_set():
        expected = time.perf_counter() + interval_s
        await asyncio.sleep(interval_s)
        lag = max(time.perf_counter() - expected, 0.0)
        LOOP_LAG.observe(lag)
        worst = max(worst, lag)
        LOOP_LAG_MAX.set(worst)


Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class RequestTimer:
    """Pure-ASGI middleware timing decision requests end to end (no body buffering)."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/v1/decisions":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500

        async def capture(message: MutableMapping[str, Any]) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            STAGE.labels("request_total").observe(time.perf_counter() - start)
            RESPONSES.labels(str(status)).inc()
