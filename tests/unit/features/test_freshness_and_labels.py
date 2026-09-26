"""Customer inactivity vs pipeline staleness, and label availability."""

from __future__ import annotations

import inspect

from fraudplat.features.compute import compute_features
from fraudplat.features.freshness import PipelineStatus, assess_pipeline
from fraudplat.features.spec import DAY, US
from fraudplat.features.state import InMemoryFeatureState
from fraudplat.labels import label_as_of

from .helpers import event, us

NOW = us("2025-02-01T12:00:00")
HEALTHY = PipelineStatus(
    worker_heartbeat_at_us=NOW - 2 * US, oldest_unpublished_outbox_at_us=None, consumer_lag_events=3
)


def test_healthy_pipeline_has_no_reasons() -> None:
    assert assess_pipeline(HEALTHY, NOW) == ()


def test_each_pipeline_signal_is_reported_separately() -> None:
    assert assess_pipeline(PipelineStatus(NOW - 11 * US, NOW - 31 * US, 1001), NOW) == (
        "WORKER_HEARTBEAT_STALE",
        "OUTBOX_BACKLOG_AGED",
        "CONSUMER_LAG_HIGH",
    )
    assert assess_pipeline(PipelineStatus(None, None, None), NOW) == (
        "WORKER_HEARTBEAT_MISSING",
        "CONSUMER_LAG_UNKNOWN",
    )
    # Boundaries are inclusive of the limit: exactly 10 s / 30 s / 1000 events is healthy.
    assert assess_pipeline(PipelineStatus(NOW - 10 * US, NOW - 30 * US, 1000), NOW) == ()


def test_pipeline_assessment_cannot_see_customer_data() -> None:
    params = set(inspect.signature(assess_pipeline).parameters)
    assert params == {"status", "now_us", "thresholds"}


def test_inactive_customer_is_a_feature_value_not_staleness() -> None:
    state = InMemoryFeatureState()
    state.apply(event("old", NOW - 25 * DAY), applied_at_us=NOW - 25 * DAY)
    snap = state.read("C1", "T1", NOW, "now")
    features = compute_features(
        amount_minor=100, terminal_id="T1", event_time_us=NOW, snapshot=snap
    )
    assert features["secs_since_prev_cust_txn"] == 25 * 86_400.0
    assert features["cust_txn_count_1d"] == 0
    assert assess_pipeline(HEALTHY, NOW) == ()  # inactivity does not degrade the pipeline


def test_labels_are_unknown_until_available() -> None:
    available = us("2025-01-20T00:00:00")
    assert (
        label_as_of(is_fraud=True, label_available_at_us=available, as_of_us=available - 1)
        == "unknown"
    )
    assert (
        label_as_of(is_fraud=True, label_available_at_us=available, as_of_us=available) == "fraud"
    )
    assert (
        label_as_of(is_fraud=False, label_available_at_us=available, as_of_us=available - 1)
        == "unknown"
    )
    assert (
        label_as_of(is_fraud=False, label_available_at_us=available, as_of_us=available) == "legit"
    )
