"""Check that summary numbers in the documentation match the committed evidence they cite.

    python scripts/check_evidence.py

Each claim below is computed from its source report and must appear (whitespace-insensitive) in
the listed documents. Nothing is re-measured: if a report changes, the documents must be updated
to match, and this check fails until they are. Exit status 1 on any mismatch.
"""

from __future__ import annotations

import glob
import json
import re
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

README = "README.md"
MODEL_CARD = "docs/MODEL_CARD.md"
NOTES = "docs/RELEASE_NOTES.md"
DEMO = "docs/DEMO_SCRIPT.md"
CHECKLIST = "docs/RELEASE_CHECKLIST.md"
DASH = "\u2013"  # the en dash used for ranges in the documents


def load(path: str) -> Any:
    return json.loads(Path(path).read_text(), parse_float=Decimal)


def r(value: Decimal | float | int, places: int) -> str:
    """Round half up, as a reader would, from the exact decimal in the report."""
    return str(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def pct(value: Decimal, places: int = 2) -> str:
    return r(Decimal(value) * 100, places)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("*", ""))


def claims() -> list[tuple[str, list[str], str]]:
    """(description, documents, expected text)."""
    out: list[tuple[str, list[str], str]] = []

    walk = load("reports/dashboard/walkthrough.json")
    checks = {c["name"].split(":")[0]: c for c in walk["checks"]}
    drill = next(c for n, c in checks.items() if n.startswith("drill phase 3"))["observed"]["drill"]
    sent, scoreless = drill["sent"], drill["outcomes"]["scoreless_review"]
    out.append(("failure drill (walkthrough.json)", [README], f"{scoreless} of {sent}"))
    out.append(
        ("failure drill (walkthrough.json)", [README], f"{sent} decisions: {scoreless} scoreless")
    )
    out.append(
        (
            "failure drill (walkthrough.json)",
            [DEMO],
            f"{sent} decisions at 5/s, {scoreless} scoreless",
        )
    )
    polling = next(c for n, c in checks.items() if n.startswith("polling"))["observed"]
    means = [Decimal(str(v["mean_ms"])) for v in polling["by_path"].values()]
    out.append(
        (
            "dashboard polling (walkthrough.json)",
            [README],
            f"{polling['requests_per_minute']} console requests/min; mean console response "
            f"{r(min(means), 1)}{DASH}{r(max(means), 1)} ms",
        )
    )

    rep = load("reports/dashboard/repeatability.json")
    inv = next(s for s in rep["steps"] if s["step"] == "invariants")
    out.append(
        (
            "demo repeatability (repeatability.json)",
            [README],
            f"{inv['requests_sent']:,} demo requests → {inv['new_decisions']:,} new decisions, "
            f"{inv['dead_letter_messages']} dead-lettered events",
        )
    )

    held = load("reports/release-1/test_evaluation.json")["conditions"]["A_immediate_processing"]
    cand, base = held["candidate"], held["baseline"]

    def ap(m: dict[str, Any]) -> str:
        lo, hi = m["bootstrap_95ci"]["average_precision"]
        return f"{r(m['overall']['average_precision'], 3)} ({r(lo, 3)}{DASH}{r(hi, 3)})"

    out.append(
        (
            "held-out AP (test_evaluation.json)",
            [MODEL_CARD],
            f"| Average precision | {ap(cand)} | {ap(base)} |",
        )
    )
    out.append(
        (
            "held-out AP (test_evaluation.json)",
            [README],
            f"AP {ap(cand).replace(' (', ' (95% CI ')}",
        )
    )
    out.append(
        (
            "held-out AP (test_evaluation.json)",
            [README, NOTES],
            f"{r(cand['overall']['average_precision'], 3)}",
        )
    )
    out.append(("held-out rows (test_evaluation.json)", [README], f"{held['test_rows']:,}"))
    out.append(
        (
            "candidate review rate / precision / recall (test_evaluation.json)",
            [README],
            f"candidate review rate {pct(cand['overall']['flagged_rate'])}%, precision "
            f"{r(cand['overall']['flagged_precision'], 3)}, "
            f"recall {r(cand['overall']['flagged_recall'], 3)}",
        )
    )

    policy = load("reports/serving_diagnosis/baseline_review_only_policy.json")
    rates = [w["review_rate"] for w in policy["development_review_rates"]]
    rng = f"{pct(min(rates))}{DASH}{pct(max(rates))}%"
    out.append(("active policy development review rates", [README, NOTES], rng))

    tally: dict[str, list[bool]] = {}
    for path in glob.glob("reports/serving_diagnosis/run*_*.json"):
        run = load(path)
        lvl = run["levels"][0]
        m, lat = lvl["measured_window"], lvl["latency_ms_all_responses"]
        ok = (
            lat["p95"] <= 100
            and lat["p99"] <= 200
            and m["achieved_completion_rps"] >= 99
            and Decimal(m["failed_http_or_rejected"]) / Decimal(m["scheduled"]) <= Decimal("0.001")
        )
        tally.setdefault(run["readiness_at_start"]["model"]["model_version"], []).append(ok)
    counts = {f"{sum(v)}/{len(v)}" for v in tally.values()}
    if len(counts) == 1:
        (both,) = counts
        out.append(("serving diagnosis pass counts (run*.json)", [README, CHECKLIST], both))
        passed, runs = both.split("/")
        out.append(
            ("serving diagnosis pass counts (run*.json)", [README], f"only {passed} of {runs}")
        )

    e2e = load("reports/benchmarks/e2e.json")
    level = next(lv for lv in e2e["levels"] if lv["target_rps"] == 100)
    lat = level["latency_ms_all_responses"]
    out.append(("e2e benchmark 100 rps (e2e.json)", [README], f"p95 {r(lat['p95'], 1)} ms"))

    comp = load("reports/model_comparison/report.json")["best_per_family"]

    def ece(family: str) -> str:
        v = [f["calibration_raw"]["ece_10_equal_count"] for f in comp[family]["folds"].values()]
        return f"{r(min(v), 4)}{DASH}{r(max(v), 4)}"

    text = f"raw ECE: logistic regression {ece('lr')}, XGBoost {ece('xgb')}"
    out.append(("comparison-stage calibration (report.json)", [README, MODEL_CARD, NOTES], text))
    ulb = load("reports/external/ulb/benchmark.json")["families"]

    def ulb_ap(family: str) -> str:
        t = ulb[family]["test"]
        lo, hi = t["ci95"]["average_precision"]
        return f"{r(t['average_precision'], 3)} (95% CI {r(lo, 3)}{DASH}{r(hi, 3)})"

    out.append(("ULB benchmark (benchmark.json)", [README, NOTES], f"XGBoost AP {ulb_ap('xgb')}"))
    out.append(
        (
            "ULB benchmark (benchmark.json)",
            [README, NOTES],
            f"logistic regression {r(ulb['lr']['test']['average_precision'], 3)}",
        )
    )
    return out


def main() -> int:
    docs: dict[str, str] = {}
    failures = 0
    items = claims()
    for description, files, expected in items:
        for f in files:
            docs.setdefault(f, norm(Path(f).read_text()))
            if norm(expected) not in docs[f]:
                failures += 1
                print(f"MISMATCH {f}: expected {expected!r}  [{description}]")
    checked = sum(len(files) for _, files, _ in items)
    print(f"checked {checked} evidence claims; {failures} mismatch(es)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
