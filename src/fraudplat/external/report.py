"""Render `reports/external/ulb/benchmark.md` from `benchmark.json` (no recomputation).

Run: python -m fraudplat.external.report
"""

# ruff: noqa: E501, RUF001  -- Markdown table rows and en-dash ranges

from __future__ import annotations

import json
from pathlib import Path

SRC = Path("reports/external/ulb/benchmark.json")


def f3(x: float) -> str:
    return f"{x:.3f}"


def main() -> int:
    r = json.loads(SRC.read_text())
    w = r["windows"]
    names = {"lr": "Logistic regression", "xgb": "XGBoost"}
    rows = []
    for fam, v in r["families"].items():
        t, a = v["test"], v["test"]["at_fixed_threshold"]
        ap_lo, ap_hi = t["ci95"]["average_precision"]
        rows.append(
            f"| {names[fam]} | `{v['selected']}` | {f3(v['validation_ap'])} | "
            f"{f3(t['average_precision'])} ({f3(ap_lo)}–{f3(ap_hi)}) | {t['roc_auc']:.4f} | "
            f"{a['review_rate'] * 100:.2f}% | {f3(a['precision'])} | {f3(a['recall'])} "
            f"({a['frauds_caught']}/{w['test']['frauds']}) | {t['ece_raw']:.5f} | "
            f"{f3(v['random_split_contrast_ap'])} |"
        )
    text = f"""# ULB credit-card benchmark (real data, offline)

Source: `{SRC}` ({r["created_at"]}). Dataset: {r["dataset"]}. Protocol fixed and committed before
the held-out window was evaluated (`src/fraudplat/external/benchmark.py`); evaluated once.

**Scope.** Real card transactions (two days, 2013), anonymised by the data owner into PCA
components. No customer or merchant identifiers, so the platform's streaming features cannot be
computed; this checks the modelling and evaluation method on real data and does not change the
platform, its models or its results.

| Window | Hours | Rows | Frauds |
|---|---|---|---|
| Train | 0–24 | {w["train"]["rows"]:,} | {w["train"]["frauds"]} |
| Validation (model and threshold choice) | 24–32 | {w["validation"]["rows"]:,} | {w["validation"]["frauds"]} |
| Held-out test | 32–48 | {w["test"]["rows"]:,} | {w["test"]["frauds"]} |

| Model | Selected | Validation AP | Test AP (95% CI) | Test ROC AUC | Review rate at threshold | Precision | Recall (caught) | ECE raw | Random-split AP |
|---|---|---|---|---|---|---|---|---|---|
{chr(10).join(rows)}

Threshold: top {r["protocol"]["review_budget"] * 100:.1f}% of validation scores, applied unchanged.
Intervals: {r["protocol"]["bootstrap"]}.

**Findings**

* XGBoost ranks better than logistic regression on the held-out window, as on the simulator; the
  intervals overlap, so the size of the gap is uncertain with 136 test frauds.
* Validation AP was higher than test AP for both models; the validation window (8 night hours,
  {w["validation"]["frauds"]} frauds) is small and differs from the test period.
* The threshold chosen for a {r["protocol"]["review_budget"] * 100:.1f}% review budget produced a
  lower review rate on the test window: a fixed threshold does not fix the workload when the
  score distribution shifts, the same lesson as on the simulator.
* A stratified random 70/30 split gave higher AP for both models. The two test sets differ, so
  this is indicative of optimism from ignoring time, not a controlled estimate.
* Raw ECE is small, but so is the fraud rate; scores are still treated as risk scores.
"""
    SRC.with_suffix(".md").write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
