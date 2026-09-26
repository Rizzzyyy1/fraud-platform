"""The pure feature function shared by offline reconstruction and online scoring.

Given a snapshot, the output depends only on the events in it. Missing values are None (a new
customer has no mean amount); the model's preprocessing decides how to encode them.

Snapshot histories are sorted by event time and lie in [t - MAX_WINDOW, t), so each window's
start is found by binary search instead of rescanning the history once per window.
"""

from __future__ import annotations

import bisect
from datetime import UTC, datetime

from fraudplat.features.spec import (
    CUSTOMER_AMOUNT_WINDOWS,
    CUSTOMER_COUNT_WINDOWS,
    FEATURE_NAMES,
    MAX_WINDOW,
    TERMINAL_COUNT_WINDOWS,
    US,
)
from fraudplat.features.state import HistoryItem, Snapshot

FeatureValue = int | float | None


def _times_within_bounds(items: tuple[HistoryItem, ...], t_us: int) -> list[int]:
    times = [item.event_time_us for item in items]
    if times and not (t_us - MAX_WINDOW <= times[0] and times[-1] < t_us):
        raise ValueError("snapshot history outside [t - MAX_WINDOW, t)")
    return times


def compute_features(
    *, amount_minor: int, terminal_id: str, event_time_us: int, snapshot: Snapshot
) -> dict[str, FeatureValue]:
    t = event_time_us
    customer = snapshot.customer_history
    cust_times = _times_within_bounds(customer, t)
    term_times = _times_within_bounds(snapshot.terminal_history, t)
    when = datetime.fromtimestamp(t // US, tz=UTC)
    features: dict[str, FeatureValue] = {
        "amount_minor": amount_minor,
        "hour_of_day_utc": when.hour,
        "day_of_week_utc": when.weekday(),
    }
    n_cust = len(cust_times)
    for name, width in CUSTOMER_COUNT_WINDOWS.items():
        features[f"cust_txn_count_{name}"] = n_cust - bisect.bisect_left(cust_times, t - width)
    for name, width in CUSTOMER_AMOUNT_WINDOWS.items():
        start = bisect.bisect_left(cust_times, t - width)
        count = n_cust - start
        total = sum(item.amount_minor for item in customer[start:])
        features[f"cust_amount_mean_{name}"] = total / count if count else None

    mean_30d = features["cust_amount_mean_30d"]
    features["amount_to_cust_mean_30d"] = (
        amount_minor / mean_30d if isinstance(mean_30d, float) and mean_30d > 0 else None
    )
    features["secs_since_prev_cust_txn"] = (t - cust_times[-1]) / US if cust_times else None
    features["cust_has_history_30d"] = int(n_cust > 0)
    features["cust_terminal_is_new_30d"] = int(
        not any(item.terminal_id == terminal_id for item in customer)
    )
    n_term = len(term_times)
    for name, width in TERMINAL_COUNT_WINDOWS.items():
        features[f"term_txn_count_{name}"] = n_term - bisect.bisect_left(term_times, t - width)

    assert tuple(features) == FEATURE_NAMES, "feature order drifted from spec"
    return features
