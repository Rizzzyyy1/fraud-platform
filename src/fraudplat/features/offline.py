"""Point-in-time reconstruction of what the scoring service would have observed.

Timeline for each historical transaction:

    event_time      when it happened
    received_at     when the API received it (synthetic arrival delay in simulated data)
    decision_time   = received_at  (assumption: scoring happens on receipt)
    available_at    = received_at + processing_delay   (when it becomes feature history)

A prior event e is visible to decision d iff e.available_at <= d.decision_time AND e is inside
d's event-time window. At equal instants, availability is processed before decisions, which is
exactly what `<=` means. With processing_delay = 0 the result is labelled the
*immediate-processing assumption*; other delays model worker lag.

`availability="event_time"` reproduces the common leaky shortcut (every event with an earlier
event time is visible, however late it arrived). It exists only to measure the leak.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np

from fraudplat.features.compute import FeatureValue, compute_features
from fraudplat.features.spec import FEATURE_NAMES
from fraudplat.features.state import ApplyStatus, InMemoryFeatureState, TxnEvent

Availability = Literal["arrival", "event_time"]


@dataclass(frozen=True, slots=True)
class HistoricalTxn:
    event: TxnEvent
    received_at_us: int

    @property
    def decision_time_us(self) -> int:
        return self.received_at_us


@dataclass(frozen=True, slots=True)
class FeatureRow:
    event_id: str
    decision_time_us: int
    features: dict[str, FeatureValue]
    history_complete: bool


@dataclass(frozen=True, slots=True)
class Reconstruction:
    rows: list[FeatureRow]
    apply_statuses: Counter[ApplyStatus]
    processing_delay_us: int
    availability: Availability


def available_at_us(
    txn: HistoricalTxn, processing_delay_us: int, availability: Availability = "arrival"
) -> int:
    if availability == "event_time":
        return txn.event.event_time_us
    return txn.received_at_us + processing_delay_us


def _replay(
    txns: Sequence[HistoricalTxn],
    processing_delay_us: int,
    availability: Availability,
    state: InMemoryFeatureState,
    statuses: Counter[ApplyStatus],
) -> Iterator[tuple[int, int, dict[str, FeatureValue], bool]]:
    """Yield (decision instant, txn index, features, history_complete) in decision order."""
    if processing_delay_us < 0:
        raise ValueError("processing delay cannot be negative")
    # (time, kind, index): kind 0 = becomes available, 1 = decision. Availability first on ties.
    timeline: list[tuple[int, int, int]] = []
    for index, txn in enumerate(txns):
        timeline.append((available_at_us(txn, processing_delay_us, availability), 0, index))
        timeline.append((txn.decision_time_us, 1, index))
    timeline.sort()

    for instant, kind, index in timeline:
        event = txns[index].event
        if kind == 0:
            statuses[state.apply(event, applied_at_us=instant)] += 1
            continue
        snapshot = state.read(
            event.customer_id, event.terminal_id, event.event_time_us, event.event_id
        )
        features = compute_features(
            amount_minor=event.amount_minor,
            terminal_id=event.terminal_id,
            event_time_us=event.event_time_us,
            snapshot=snapshot,
        )
        yield instant, index, features, snapshot.history_complete


def point_in_time_features(
    txns: Sequence[HistoricalTxn],
    processing_delay_us: int = 0,
    availability: Availability = "arrival",
    state: InMemoryFeatureState | None = None,
) -> Reconstruction:
    statuses: Counter[ApplyStatus] = Counter()
    rows = [
        FeatureRow(txns[index].event.event_id, instant, features, complete)
        for instant, index, features, complete in _replay(
            txns, processing_delay_us, availability, state or InMemoryFeatureState(), statuses
        )
    ]
    return Reconstruction(rows, statuses, processing_delay_us, availability)


@dataclass(frozen=True)
class FeatureMatrix:
    """Columnar reconstruction for training: one row per decision, None encoded as NaN.

    `txn_index[i]` is the position of row i's transaction in the input sequence.
    """

    txn_index: np.ndarray  # int64
    decision_time_us: np.ndarray  # int64
    values: np.ndarray  # float64, shape (rows, len(FEATURE_NAMES))
    history_complete: np.ndarray  # bool
    apply_statuses: Counter[ApplyStatus]


def point_in_time_matrix(
    txns: Sequence[HistoricalTxn],
    processing_delay_us: int = 0,
    availability: Availability = "arrival",
) -> FeatureMatrix:
    n = len(txns)
    txn_index = np.empty(n, dtype=np.int64)
    decision_time = np.empty(n, dtype=np.int64)
    values = np.empty((n, len(FEATURE_NAMES)), dtype=np.float64)
    complete = np.empty(n, dtype=bool)
    statuses: Counter[ApplyStatus] = Counter()
    row = 0
    for instant, index, features, is_complete in _replay(
        txns, processing_delay_us, availability, InMemoryFeatureState(), statuses
    ):
        txn_index[row] = index
        decision_time[row] = instant
        values[row] = [np.nan if v is None else v for v in features.values()]
        complete[row] = is_complete
        row += 1
    assert row == n, "every transaction must produce exactly one decision row"
    return FeatureMatrix(txn_index, decision_time, values, complete, statuses)


@dataclass(frozen=True, slots=True)
class CutoffSplit:
    """Partition around a demonstration cutoff C (processing delay assumed zero).

    bootstrap: available before C, loaded into state before the demo starts.
    scoring:   decided at or after C, replayed through the live path.
    With zero processing delay available_at == decision_time, so these partition the data.
    """

    bootstrap: list[HistoricalTxn]
    scoring: list[HistoricalTxn]


def split_at_cutoff(txns: Sequence[HistoricalTxn], cutoff_us: int) -> CutoffSplit:
    bootstrap = [t for t in txns if available_at_us(t, 0) < cutoff_us]
    scoring = [t for t in txns if t.decision_time_us >= cutoff_us]
    return CutoffSplit(bootstrap=bootstrap, scoring=scoring)


def bootstrap_state(
    txns: Sequence[HistoricalTxn],
    cutoff_us: int,
    state: InMemoryFeatureState | None = None,
    history_start_us: int | None = None,
) -> InMemoryFeatureState:
    """Apply only history available strictly before the cutoff, in arrival order.

    `history_start_us` optionally skips events available before it (see `bootstrap_redis`).
    """
    state = state or InMemoryFeatureState()
    eligible = [
        t
        for t in split_at_cutoff(txns, cutoff_us).bootstrap
        if history_start_us is None or available_at_us(t, 0) >= history_start_us
    ]
    for txn in sorted(eligible, key=lambda t: (available_at_us(t, 0), t.event.event_id)):
        state.apply(txn.event, applied_at_us=available_at_us(txn, 0))
    return state
