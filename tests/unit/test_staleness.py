"""Boundaries of the staleness escalation rule (limits are inclusive: == limit still scores)."""

from __future__ import annotations

import pytest

from fraudplat.pipeline.status import staleness_escalation


@pytest.mark.parametrize(
    ("heartbeat", "outbox", "unknown", "expected"),
    [
        (5.0, None, None, None),  # healthy
        (60.0, None, None, None),  # exactly at the limit: still scores
        (60.001, None, None, "PIPELINE_STALE_BEYOND_LIMIT"),
        (1.0, 60.0, None, None),
        (1.0, 60.001, None, "PIPELINE_STALE_BEYOND_LIMIT"),  # outbox not draining
        (None, None, 60.0, None),  # unknown for exactly the limit: still scores
        (None, None, 60.001, "PIPELINE_UNKNOWN_BEYOND_LIMIT"),
        (61.0, None, 61.0, "PIPELINE_STALE_BEYOND_LIMIT"),  # confirmed staleness wins
    ],
)
def test_boundaries(
    heartbeat: float | None, outbox: float | None, unknown: float | None, expected: str | None
) -> None:
    assert staleness_escalation(heartbeat, outbox, unknown, 60.0, 60.0) == expected
