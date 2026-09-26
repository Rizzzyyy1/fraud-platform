"""Decision policy threshold boundaries."""

from __future__ import annotations

import pytest

from fraudplat.policy import DecisionPolicy

POLICY = DecisionPolicy(version="test", review_threshold=0.5, decline_threshold=0.9)


@pytest.mark.parametrize(
    ("score", "action"),
    [
        (0.0, "approve"),
        (0.4999, "approve"),
        (0.5, "review"),  # boundary is inclusive
        (0.8999, "review"),
        (0.9, "decline"),
        (1.0, "decline"),
        (None, "review"),  # a missing score is never approved
    ],
)
def test_score_to_action(score: float | None, action: str) -> None:
    assert POLICY.decide(score)[0] == action


def test_rejects_inverted_thresholds() -> None:
    with pytest.raises(ValueError):
        DecisionPolicy(version="bad", review_threshold=0.9, decline_threshold=0.5)
