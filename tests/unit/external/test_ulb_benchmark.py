"""ULB benchmark helpers on tiny hand-made inputs (no download)."""

from __future__ import annotations

import numpy as np

from fraudplat.external.benchmark import at_threshold, ece
from fraudplat.external.ulb import parse_arff

ARFF = """@RELATION creditcard
@ATTRIBUTE 'Time' NUMERIC
@ATTRIBUTE 'Amount' NUMERIC
@ATTRIBUTE 'Class' {'0','1'}
@DATA
0,1e+05,'0'
1.5,2.5,'1'
"""


def test_parse_arff_reads_scientific_numbers_and_quoted_labels() -> None:
    frame = parse_arff(ARFF)
    assert frame.columns == ["Time", "Amount", "Class"]
    assert frame["Amount"].to_list() == [100000.0, 2.5]
    assert frame["Class"].to_list() == [0, 1]


def test_fixed_threshold_metrics() -> None:
    y = np.array([0, 0, 1, 1])
    p = np.array([0.1, 0.6, 0.7, 0.2])
    m = at_threshold(y, p, 0.5)
    assert (m["flagged"], m["frauds_caught"], m["precision"], m["recall"]) == (2, 1, 0.5, 0.5)


def test_ece_is_zero_when_scores_match_rates_and_positive_otherwise() -> None:
    y = np.array([0, 1] * 50)
    assert ece(y, np.full(100, 0.5)) == 0.0
    assert ece(y, np.full(100, 0.9)) > 0.3
