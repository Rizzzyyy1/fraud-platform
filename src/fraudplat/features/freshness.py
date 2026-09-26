"""Pipeline staleness, kept separate from customer inactivity.

Customer recency (`secs_since_prev_cust_txn`) is a model feature; a customer silent for weeks is
normal and never makes state "stale". Pipeline health depends only on pipeline signals: worker
heartbeat, age of the oldest unpublished outbox row, and Kafka consumer lag. `assess_pipeline`
takes no customer data by construction.

Policy v1: a degraded pipeline adds reason codes and alerts but does not change the action;
only unreadable state (Redis down) forces `review`. Completeness of one customer's history
cannot be proven from these signals and is not claimed.
"""

from __future__ import annotations

from dataclasses import dataclass

from fraudplat.features.spec import US


@dataclass(frozen=True, slots=True)
class PipelineStatus:
    worker_heartbeat_at_us: int | None
    oldest_unpublished_outbox_at_us: int | None  # None = outbox empty
    consumer_lag_events: int | None  # None = unknown


@dataclass(frozen=True, slots=True)
class PipelineThresholds:
    heartbeat_max_age_us: int = 10 * US
    outbox_max_age_us: int = 30 * US
    consumer_lag_max_events: int = 1000


def assess_pipeline(
    status: PipelineStatus, now_us: int, thresholds: PipelineThresholds | None = None
) -> tuple[str, ...]:
    """Return degradation reason codes; an empty tuple means the pipeline looks healthy."""
    limits = thresholds or PipelineThresholds()
    reasons: list[str] = []
    if status.worker_heartbeat_at_us is None:
        reasons.append("WORKER_HEARTBEAT_MISSING")
    elif now_us - status.worker_heartbeat_at_us > limits.heartbeat_max_age_us:
        reasons.append("WORKER_HEARTBEAT_STALE")
    if (
        status.oldest_unpublished_outbox_at_us is not None
        and now_us - status.oldest_unpublished_outbox_at_us > limits.outbox_max_age_us
    ):
        reasons.append("OUTBOX_BACKLOG_AGED")
    if status.consumer_lag_events is None:
        reasons.append("CONSUMER_LAG_UNKNOWN")
    elif status.consumer_lag_events > limits.consumer_lag_max_events:
        reasons.append("CONSUMER_LAG_HIGH")
    return tuple(reasons)
