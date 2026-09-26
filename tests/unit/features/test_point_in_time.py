"""Availability semantics: an earlier transaction that arrives after a decision cannot influence it.

Scenario for customer C1 (hand-calculated):

    A  event 10:00  received 10:30   (arrives late)
    B  event 10:10  received 10:10
    C  event 10:40  received 10:40

    decision B (10:10): 1h window [09:10, 10:10). A's event time is inside it, but A is not
    available until 10:30 -> cust_txn_count_1h = 0.
    decision A (10:30): window [09:00, 10:00). B is available but its event time is after A's
    -> 0.
    decision C (10:40): window [09:40, 10:40). A (available 10:30) and B -> 2.
"""

from __future__ import annotations

from fraudplat.features.compute import FeatureValue
from fraudplat.features.offline import (
    Availability,
    HistoricalTxn,
    bootstrap_state,
    point_in_time_features,
    split_at_cutoff,
)
from fraudplat.features.spec import MINUTE
from fraudplat.features.state import ApplyStatus

from .helpers import event, historical, us

A = historical(event("A", us("2025-01-10T10:00:00")), us("2025-01-10T10:30:00"))
B = historical(event("B", us("2025-01-10T10:10:00")))
C = historical(event("C", us("2025-01-10T10:40:00")))


def counts_1h(
    txns: list[HistoricalTxn], processing_delay_us: int = 0, availability: Availability = "arrival"
) -> dict[str, FeatureValue]:
    result = point_in_time_features(txns, processing_delay_us, availability)
    return {row.event_id: row.features["cust_txn_count_1h"] for row in result.rows}


def test_late_arriving_earlier_event_is_excluded_from_earlier_decision() -> None:
    assert counts_1h([A, B, C]) == {"B": 0, "A": 0, "C": 2}


def test_result_does_not_depend_on_input_order() -> None:
    assert counts_1h([C, B, A]) == counts_1h([A, B, C])


def test_event_time_shortcut_leaks_the_late_event() -> None:
    """The leaky reconstruction (filter by event time only) would give B a count of 1."""
    assert counts_1h([A, B, C], availability="event_time") == {"A": 0, "B": 1, "C": 2}


def test_processing_delay_hides_recent_events() -> None:
    # With 15 min worker lag: A available 10:45, B available 10:25.
    # decision C (10:40) sees B only -> 1.
    assert counts_1h([A, B, C], processing_delay_us=15 * MINUTE)["C"] == 1


def test_decision_order_follows_decision_time() -> None:
    result = point_in_time_features([A, B, C])
    assert [row.event_id for row in result.rows] == ["B", "A", "C"]
    assert result.apply_statuses == {ApplyStatus.APPLIED: 3}


def test_duplicate_delivery_in_history_is_counted_once() -> None:
    duplicate_b = historical(B.event, us("2025-01-10T10:20:00"))  # redelivered later
    result = point_in_time_features([A, B, C, duplicate_b])
    c_row = next(
        r for r in result.rows if r.event_id == "C" and r.decision_time_us == C.decision_time_us
    )
    assert c_row.features["cust_txn_count_1h"] == 2
    assert result.apply_statuses[ApplyStatus.DUPLICATE] == 1


def test_cutoff_partitions_history_and_scoring_by_arrival() -> None:
    cutoff = us("2025-01-10T10:20:00")
    split = split_at_cutoff([A, B, C], cutoff)
    # B arrived 10:10 (< cutoff). A arrived 10:30 despite its 10:00 event time, so it is
    # *scored*, not bootstrapped. C arrived 10:40.
    assert [t.event.event_id for t in split.bootstrap] == ["B"]
    assert sorted(t.event.event_id for t in split.scoring) == ["A", "C"]
    ids = [t.event.event_id for t in split.bootstrap + split.scoring]
    assert sorted(ids) == [
        "A",
        "B",
        "C",
    ]  # every transaction appears in exactly one of the two sets


def test_bootstrap_contains_nothing_available_at_or_after_cutoff() -> None:
    cutoff = us("2025-01-10T10:20:00")
    state = bootstrap_state([A, B, C], cutoff)
    snap = state.read("C1", "T1", us("2025-01-10T11:00:00"), "probe")
    assert [item.event_id for item in snap.customer_history] == ["B"]


def test_bootstrap_boundary_is_strict() -> None:
    cutoff = B.received_at_us  # B available exactly at the cutoff -> not bootstrapped
    assert split_at_cutoff([B], cutoff).bootstrap == []
    assert split_at_cutoff([B], cutoff).scoring == [B]


def test_event_available_exactly_at_decision_time_is_visible() -> None:
    # D: event 10:05, received exactly 10:10 = B's decision time. Rule is available_at <=
    # decision_time, so B sees D: cust_txn_count_1h = 1 (window [09:10, 10:10) holds 10:05).
    d = historical(event("D", us("2025-01-10T10:05:00")), us("2025-01-10T10:10:00"))
    assert counts_1h([B, d])["B"] == 1


def test_matrix_matches_row_reconstruction() -> None:
    import math

    from fraudplat.features.offline import point_in_time_matrix
    from fraudplat.features.spec import FEATURE_NAMES

    txns = [A, B, C]
    rows = point_in_time_features(txns).rows
    matrix = point_in_time_matrix(txns)
    for i, row in enumerate(rows):
        assert txns[int(matrix.txn_index[i])].event.event_id == row.event_id
        for j, name in enumerate(FEATURE_NAMES):
            expected = row.features[name]
            got = float(matrix.values[i, j])
            assert math.isnan(got) if expected is None else got == expected


def test_windowed_bootstrap_gives_identical_features_after_cutoff() -> None:
    """Skipping history older than the retention horizon cannot change post-cutoff features."""
    import random

    from fraudplat.features.compute import compute_features
    from fraudplat.features.spec import DAY, RETENTION_HORIZON

    rng = random.Random(3)
    base = us("2025-01-01T00:00:00")
    txns = []
    for i in range(600):
        t = base + rng.randrange(0, 80 * DAY)
        ev = event(
            f"x{i:04d}",
            t,
            customer=f"C{rng.randrange(8)}",
            terminal=f"T{rng.randrange(5)}",
            amount=rng.randrange(1, 500),
        )
        txns.append(historical(ev, t + rng.choice([0, 0, 0, 3_600_000_000])))
    cutoff = base + 70 * DAY
    full = bootstrap_state(txns, cutoff)
    windowed = bootstrap_state(txns, cutoff, history_start_us=cutoff - RETENTION_HORIZON - DAY)
    for probe in split_at_cutoff(txns, cutoff).scoring:
        e = probe.event
        a = full.read(e.customer_id, e.terminal_id, e.event_time_us, e.event_id)
        b = windowed.read(e.customer_id, e.terminal_id, e.event_time_us, e.event_id)
        fa = compute_features(
            amount_minor=e.amount_minor,
            terminal_id=e.terminal_id,
            event_time_us=e.event_time_us,
            snapshot=a,
        )
        fb = compute_features(
            amount_minor=e.amount_minor,
            terminal_id=e.terminal_id,
            event_time_us=e.event_time_us,
            snapshot=b,
        )
        assert fa == fb
