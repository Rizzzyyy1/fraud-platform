"""Window boundaries, equal timestamps, cold start, duplicates, conflicts, ordering, horizon.

Expected values are calculated by hand in the comments, not by running the implementation.
"""

from __future__ import annotations

import itertools
import random

import pytest

from fraudplat.features.compute import FeatureValue, compute_features
from fraudplat.features.spec import DAY, FEATURE_NAMES, HOUR, MINUTE, RETENTION_HORIZON, US
from fraudplat.features.state import ApplyStatus, InMemoryFeatureState, TxnEvent

from .helpers import event, us

T0 = us("2025-01-10T12:00:00")  # a Friday

# Customer C1's events relative to the decision at T0 (transaction X, amount 1000, terminal T1):
HISTORY = [
    event("e1", T0 - 30 * DAY, amount=100, terminal="T1"),  # exactly on 30d boundary: IN
    event("e2", T0 - 30 * DAY - 1, amount=999_999, terminal="T8"),  # 1 µs outside 30d: OUT
    event("e3", T0 - 7 * DAY, amount=200, terminal="T2"),  # exactly on 7d boundary: IN 7d
    event("e4", T0 - DAY + 1 * US, amount=300, terminal="T1"),  # inside 1d
    event("e5", T0 - 30 * MINUTE, amount=400, terminal="T3"),  # inside 1h
    event("e6", T0, amount=500, terminal="T1"),  # simultaneous with X: OUT
    event("e7", T0 + 1 * US, amount=600, terminal="T1"),  # future: OUT
    event("o1", T0 - 2 * HOUR, customer="C2", terminal="T1"),  # other customer, same terminal
]
X = event("X", T0, amount=1000, terminal="T1")


def features_for(state: InMemoryFeatureState, ev: TxnEvent) -> dict[str, FeatureValue]:
    snap = state.read(ev.customer_id, ev.terminal_id, ev.event_time_us, ev.event_id)
    return compute_features(
        amount_minor=ev.amount_minor,
        terminal_id=ev.terminal_id,
        event_time_us=ev.event_time_us,
        snapshot=snap,
    )


def loaded_state(events: list[TxnEvent]) -> InMemoryFeatureState:
    state = InMemoryFeatureState()
    for i, ev in enumerate(events):
        assert state.apply(ev, applied_at_us=i) is ApplyStatus.APPLIED
    return state


EXPECTED_X = {
    "amount_minor": 1000,
    "hour_of_day_utc": 12,
    "day_of_week_utc": 4,  # Friday
    "cust_txn_count_1h": 1,  # e5
    "cust_txn_count_1d": 2,  # e4, e5
    "cust_txn_count_7d": 3,  # e3, e4, e5
    "cust_txn_count_30d": 4,  # e1, e3, e4, e5
    "cust_amount_mean_7d": 300.0,  # (200 + 300 + 400) / 3
    "cust_amount_mean_30d": 250.0,  # (100 + 200 + 300 + 400) / 4
    "amount_to_cust_mean_30d": 4.0,  # 1000 / 250
    "secs_since_prev_cust_txn": 1800.0,  # e5 was 30 minutes earlier
    "cust_has_history_30d": 1,
    "cust_terminal_is_new_30d": 0,  # e1 and e4 used T1
    "term_txn_count_1d": 2,  # T1 in [T0-1d, T0): e4, o1  (e1 is 30d ago; e6, e7 not prior)
    "term_txn_count_7d": 2,  # same two
}


def test_window_boundaries_equal_timestamps_and_future_events() -> None:
    state = loaded_state([*HISTORY, X])  # X's own event is present and must be excluded
    assert features_for(state, X) == EXPECTED_X
    assert tuple(EXPECTED_X) == FEATURE_NAMES


def test_simultaneous_transactions_do_not_see_each_other() -> None:
    a = event("a", T0, amount=10)
    b = event("b", T0, amount=20)
    state = loaded_state([a, b])
    assert features_for(state, a)["cust_txn_count_1h"] == 0
    assert features_for(state, b)["cust_txn_count_1h"] == 0


def test_new_terminal_for_customer() -> None:
    state = loaded_state(HISTORY)
    at_new_terminal = event("Y", T0, amount=1000, terminal="T9")
    features = features_for(state, at_new_terminal)
    assert features["cust_terminal_is_new_30d"] == 1
    assert features["term_txn_count_1d"] == 0


def test_cold_start_customer() -> None:
    state = loaded_state(HISTORY)
    newcomer = event("N", T0, customer="C3", amount=700, terminal="T1")
    features = features_for(state, newcomer)
    assert features["cust_txn_count_30d"] == 0
    assert features["cust_amount_mean_30d"] is None
    assert features["amount_to_cust_mean_30d"] is None
    assert features["secs_since_prev_cust_txn"] is None
    assert features["cust_has_history_30d"] == 0
    assert features["cust_terminal_is_new_30d"] == 1
    assert features["term_txn_count_1d"] == 2  # terminal history exists even if customer's doesn't


def test_duplicate_event_does_not_inflate_counts() -> None:
    state = loaded_state([*HISTORY, X])
    duplicate = HISTORY[2]  # e3 again, identical payload
    assert state.apply(duplicate, applied_at_us=999) is ApplyStatus.DUPLICATE
    assert features_for(state, X) == EXPECTED_X


def test_conflicting_payload_for_known_id_is_rejected_without_change() -> None:
    state = loaded_state([*HISTORY, X])
    tampered = event("e3", T0 - 7 * DAY, amount=201, terminal="T2")
    assert state.apply(tampered, applied_at_us=999) is ApplyStatus.PAYLOAD_CONFLICT
    assert features_for(state, X) == EXPECTED_X


@pytest.mark.parametrize("seed", range(20))
def test_out_of_order_application_gives_identical_features(seed: int) -> None:
    events = [*HISTORY, X]
    random.Random(seed).shuffle(events)
    assert features_for(loaded_state(events), X) == EXPECTED_X


def test_every_order_of_a_small_set_commutes() -> None:
    small = [HISTORY[0], HISTORY[3], HISTORY[4], X]
    results = {
        str(features_for(loaded_state(list(order)), X)) for order in itertools.permutations(small)
    }
    assert len(results) == 1


def test_horizon_rejects_only_events_older_than_watermark_minus_retention() -> None:
    state = InMemoryFeatureState()
    newest = event("new", T0)
    assert state.apply(newest, applied_at_us=0) is ApplyStatus.APPLIED
    edge = T0 - RETENTION_HORIZON
    at_edge = event("edge", edge)
    too_old = event("old", edge - 1)
    assert state.apply(too_old, applied_at_us=1) is ApplyStatus.LATE_BEYOND_HORIZON
    assert state.apply(at_edge, applied_at_us=2) is ApplyStatus.APPLIED


def test_horizon_rule_depends_on_arrival_order_at_the_boundary() -> None:
    """Documented limitation: the same two events are accepted in one order, not the other."""
    old = event("old", T0 - RETENTION_HORIZON - 1)
    new = event("new", T0)
    first_old = InMemoryFeatureState()
    assert first_old.apply(old, 0) is ApplyStatus.APPLIED
    assert first_old.apply(new, 1) is ApplyStatus.APPLIED
    first_new = InMemoryFeatureState()
    assert first_new.apply(new, 0) is ApplyStatus.APPLIED
    assert first_new.apply(old, 1) is ApplyStatus.LATE_BEYOND_HORIZON


def test_history_complete_flag_for_decisions_far_behind_the_watermark() -> None:
    state = loaded_state([event("w", T0)])
    recent = state.read("C1", "T1", T0, "none")
    assert recent.history_complete
    # A decision about an event 2 days before the watermark: its 30d window starts 32 days
    # before the watermark, beyond the 31-day retention, so trimming may have removed history.
    old = state.read("C1", "T1", T0 - 2 * DAY, "none")
    assert not old.history_complete


def test_entity_metadata_records_updates_not_completeness() -> None:
    state = loaded_state([*HISTORY, X])
    snap = state.read("C1", "T1", T0, "X")
    assert snap.customer_meta is not None
    assert snap.customer_meta.applied_count == 8  # e1..e7 and X
    assert snap.customer_meta.last_applied_at_us == 8  # index of X in the load order
    assert snap.customer_meta.last_event_id == "X"


def test_out_of_horizon_duplicate_is_reported_late_not_duplicate() -> None:
    """Horizon is checked before dedup, so the status never depends on dedup record retention."""
    state = InMemoryFeatureState()
    old = event("old", T0 - 40 * DAY)
    assert state.apply(old, 0) is ApplyStatus.APPLIED
    assert state.apply(event("new", T0), 1) is ApplyStatus.APPLIED  # watermark moves past old+R
    assert state.apply(old, 2) is ApplyStatus.LATE_BEYOND_HORIZON


def test_event_id_and_transaction_id_must_stay_bound() -> None:
    from dataclasses import replace

    state = InMemoryFeatureState()
    first = replace(event("tx1", T0), source_event_id="evt-A")
    assert state.apply(first, 0) is ApplyStatus.APPLIED
    assert state.apply(first, 1) is ApplyStatus.DUPLICATE  # same txn, same event id, same payload
    # same transaction delivered under another event id
    assert state.apply(replace(first, source_event_id="evt-B"), 2) is ApplyStatus.EVENT_ID_CONFLICT
    # another transaction claiming an event id that is already bound
    other = replace(event("tx2", T0 - HOUR), source_event_id="evt-A")
    assert state.apply(other, 3) is ApplyStatus.EVENT_ID_CONFLICT
    snap = state.read("C1", "T1", T0 + HOUR, "probe")
    assert [i.event_id for i in snap.customer_history] == ["tx1"]  # nothing overwritten or added


def test_identity_records_expire_with_the_horizon() -> None:
    from dataclasses import replace

    state = InMemoryFeatureState()
    old = replace(event("old-tx", T0 - 40 * DAY), source_event_id="evt-1")
    assert state.apply(old, 0) is ApplyStatus.APPLIED
    assert state.apply(event("now", T0), 1) is ApplyStatus.APPLIED  # old-tx now out of horizon
    # evt-1's binding belonged to a transaction outside the horizon: it no longer blocks.
    fresh = replace(event("fresh-tx", T0 - HOUR), source_event_id="evt-1")
    assert state.apply(fresh, 2) is ApplyStatus.APPLIED
