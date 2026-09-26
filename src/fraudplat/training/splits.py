"""Chronological training/validation/test timeline and label-based training eligibility.

Day numbers count from the dataset's first day (00:00 UTC). With the sim-v2 defaults:

    [0, 30)      burn-in: features lack a full 30-day history; not used as examples
    [30, 100)    training examples (decision time)
    day 130      training cutoff T: the model is built with what was known at T
    [130, 153)   validation examples: decisions a model trained at T would score
    [153, end)   final test period: held out; no features or labels are computed for it here

A training example is eligible only if its label was available at T (`label_available_at <= T`).
Ending training examples 30 days before T (the longest label delay and the legitimate maturity
period) means eligible examples are not skewed towards quickly-reported fraud; any example still
unlabelled at T is excluded and counted rather than assumed legitimate.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from fraudplat.features.spec import DAY


@dataclass(frozen=True)
class Timeline:
    day0_us: int
    burn_in_days: int
    train_end_day: int
    training_cutoff_day: int
    test_start_day: int

    def __post_init__(self) -> None:
        days = (
            self.burn_in_days,
            self.train_end_day,
            self.training_cutoff_day,
            self.test_start_day,
        )
        if not (0 <= days[0] < days[1] <= days[2] < days[3]):
            raise ValueError("timeline must satisfy burn_in < train_end <= cutoff < test_start")

    def at(self, day: int) -> int:
        return self.day0_us + day * DAY

    @property
    def training_cutoff_us(self) -> int:
        return self.at(self.training_cutoff_day)

    @property
    def test_start_us(self) -> int:
        return self.at(self.test_start_day)


@dataclass(frozen=True)
class SplitMasks:
    train: np.ndarray
    validation: np.ndarray
    train_candidates: int
    excluded_unlabelled_at_cutoff: int


def split_masks(
    timeline: Timeline, decision_time_us: np.ndarray, label_available_at_us: np.ndarray
) -> SplitMasks:
    if (decision_time_us >= timeline.test_start_us).any():
        raise ValueError("rows from the final test period must not reach training code")
    candidates = (decision_time_us >= timeline.at(timeline.burn_in_days)) & (
        decision_time_us < timeline.at(timeline.train_end_day)
    )
    labelled = label_available_at_us <= timeline.training_cutoff_us
    validation = (decision_time_us >= timeline.training_cutoff_us) & (
        decision_time_us < timeline.test_start_us
    )
    train = candidates & labelled
    return SplitMasks(
        train=train,
        validation=validation,
        train_candidates=int(candidates.sum()),
        excluded_unlabelled_at_cutoff=int((candidates & ~labelled).sum()),
    )
