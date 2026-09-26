"""Pipeline health as seen by the scoring API.

Signals (none of them involve customer activity):
  * outbox backlog for this stream — row count and age of the oldest unpublished row (PostgreSQL);
  * worker heartbeat age and reported consumer lag — `{ns}:pipeline:worker` in Redis.
A worker that has stalled, crashed or lost Kafka stops refreshing its heartbeat, so an unhealthy
pipeline is detected even while Redis answers normally; a publisher that cannot reach Kafka shows
up as a growing, ageing outbox backlog.

Policy (thresholds in `Settings`):
  * degraded (heartbeat missing or older than 10 s, oldest unpublished row older than 30 s, lag
    above 1,000 or unknown): decisions are still scored and persisted; reason codes
    `PIPELINE_DEGRADED` plus the specific signal are added; the action is not changed.
  * scoreless review (heartbeat or oldest unpublished row older than 60 s, or an unknown state
    lasting more than 60 s): features may be stale, so the model is not called; the decision is
    persisted as `review` with `PIPELINE_STALE_BEYOND_LIMIT` / `PIPELINE_UNKNOWN_BEYOND_LIMIT`.
  * reject (backlog above 50,000 rows or oldest unpublished row older than 15 min): new decisions
    return 503 `pipeline_backlog_limit`, which bounds outbox growth when Kafka stays down.
    Idempotent retries of already-stored decisions are still answered.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from fraudplat.config import Settings
from fraudplat.features.freshness import PipelineStatus, PipelineThresholds, assess_pipeline
from fraudplat.features.spec import US
from fraudplat.pipeline.streams import Stream
from fraudplat.storage.decisions import DecisionStore, PersistenceUnavailable

logger = logging.getLogger("fraudplat.pipeline.status")


@dataclass(frozen=True)
class PipelineHealth:
    reasons: tuple[str, ...] = ()
    reject: str | None = None
    suppress_scoring: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def staleness_escalation(
    heartbeat_age_s: float | None,
    oldest_unpublished_age_s: float | None,
    unknown_for_s: float | None,
    stale_limit_s: float,
    unknown_limit_s: float,
) -> str | None:
    """Decide whether features are too stale to score on. Boundaries are inclusive of the
    limit: an age of exactly `stale_limit_s` still scores; anything larger does not.

    * confirmed staleness: the worker's last heartbeat, or the oldest unpublished outbox row, is
      older than `stale_limit_s` -> PIPELINE_STALE_BEYOND_LIMIT;
    * unknown state (no heartbeat, or lag not yet known) that has lasted longer than
      `unknown_limit_s` -> PIPELINE_UNKNOWN_BEYOND_LIMIT (a starting worker gets that long).
    """
    if heartbeat_age_s is not None and heartbeat_age_s > stale_limit_s:
        return "PIPELINE_STALE_BEYOND_LIMIT"
    if oldest_unpublished_age_s is not None and oldest_unpublished_age_s > stale_limit_s:
        return "PIPELINE_STALE_BEYOND_LIMIT"
    if unknown_for_s is not None and unknown_for_s > unknown_limit_s:
        return "PIPELINE_UNKNOWN_BEYOND_LIMIT"
    return None


class PipelineMonitor:
    def __init__(
        self, store: DecisionStore, redis: Redis | None, stream: Stream, settings: Settings
    ) -> None:
        self._store = store
        self._redis = redis
        self._stream = stream
        self._settings = settings
        self._thresholds = PipelineThresholds(
            heartbeat_max_age_us=int(settings.worker_heartbeat_max_age_s * US),
            outbox_max_age_us=int(settings.outbox_max_age_degraded_s * US),
            consumer_lag_max_events=settings.consumer_lag_degraded_events,
        )
        self.current = PipelineHealth(reasons=("PIPELINE_STATUS_UNKNOWN",))
        self._unknown_since: float | None = None  # monotonic time the unknown state began

    async def refresh(self) -> PipelineHealth:
        now_us = time.time_ns() // 1000
        detail: dict[str, Any] = {"namespace": self._stream.namespace}
        try:
            backlog, oldest = await self._store.outbox_backlog()
        except PersistenceUnavailable:
            backlog, oldest = -1, None
        oldest_us = None if oldest is None else int(oldest.timestamp() * US)
        detail["outbox_backlog"] = backlog
        detail["oldest_unpublished_age_s"] = (
            None if oldest_us is None else (now_us - oldest_us) / US
        )

        heartbeat_us: int | None = None
        lag: int | None = None
        if self._redis is not None:
            try:
                raw = await self._redis.hgetall(self._stream.worker_status_key)
                if raw and raw.get("namespace", self._stream.namespace) != self._stream.namespace:
                    raw = {}  # a heartbeat from another run must never count as this one's
                    detail["worker_status"] = "namespace_mismatch"
                if raw:
                    heartbeat_us = int(raw["heartbeat_at_us"])
                    detail["broker_view_age_ms"] = int(raw.get("broker_view_age_ms", -1))
                    reported = int(raw["consumer_lag_events"])
                    lag = reported if reported >= 0 else None  # -1: not yet known
            except (RedisError, OSError, KeyError, ValueError):
                detail["worker_status"] = "unreadable"
        detail["worker_heartbeat_age_s"] = (
            None if heartbeat_us is None else (now_us - heartbeat_us) / US
        )
        detail["consumer_lag_events"] = lag

        reasons = assess_pipeline(
            PipelineStatus(heartbeat_us, oldest_us, lag), now_us, self._thresholds
        )
        unknown = heartbeat_us is None or lag is None
        if not unknown:
            self._unknown_since = None
        elif self._unknown_since is None:
            self._unknown_since = time.monotonic()
        unknown_for = (
            None if self._unknown_since is None else time.monotonic() - self._unknown_since
        )
        detail["unknown_state_for_s"] = unknown_for
        suppress = staleness_escalation(
            detail["worker_heartbeat_age_s"],
            detail["oldest_unpublished_age_s"],
            unknown_for,
            self._settings.stale_scoring_limit_s,
            self._settings.unknown_state_limit_s,
        )
        reject = None
        if backlog > self._settings.outbox_backlog_reject_rows or (
            oldest_us is not None and now_us - oldest_us > self._settings.outbox_age_reject_s * US
        ):
            reject = "pipeline_backlog_limit"
        self.current = PipelineHealth(
            reasons=reasons, reject=reject, suppress_scoring=suppress, detail=detail
        )
        return self.current

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.refresh()
            except Exception:  # never let the monitor kill the API
                logger.exception("pipeline status refresh failed")
            try:
                await asyncio.wait_for(stop.wait(), self._settings.pipeline_monitor_interval_s)
            except TimeoutError:
                pass
