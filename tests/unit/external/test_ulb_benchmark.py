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


def test_committed_report_is_rendered_from_its_json() -> None:
    import json

    from fraudplat.external.report import SRC, render

    assert SRC.with_suffix(".md").read_text() == render(json.loads(SRC.read_text()))


def test_kaggle_csv_and_openml_arff_parse_to_the_same_frame() -> None:
    from fraudplat.external.ulb import FEATURES, parse_arff, parse_csv

    names = ["Time", *FEATURES, "Class"]
    rows = [
        ["0", *["-1.3598071336738"] * 28, "149.62", "0"],
        ["406", *["1e+05"] * 28, "0", "1"],
    ]
    arff = "@relation creditcard\n"
    arff += "".join(f"@attribute {n} numeric\n" for n in names[:-1])
    arff += "@attribute Class {'0','1'}\n@data\n"
    arff += "".join(",".join([*r[:-1], f"'{r[-1]}'"]) + "\n" for r in rows)
    csv = ",".join(f'"{n}"' for n in names) + "\n"
    csv += "".join(",".join([*r[:-1], f'"{r[-1]}"']) + "\n" for r in rows)
    a, b = parse_arff(arff), parse_csv(csv.encode())
    assert a.equals(b) and a.schema == b.schema
    assert b["Class"].to_list() == [0, 1] and b["V1"][1] == 100000.0
