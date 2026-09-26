"""Render `reports/external/ulb/benchmark.md` from `benchmark.json` (no recomputation).

Run: python -m fraudplat.external.report
"""

# ruff: noqa: E501, RUF001  -- Markdown table rows and en-dash ranges

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SRC = Path("reports/external/ulb/benchmark.json")


def f3(x: float) -> str:
    return f"{x:.3f}"


def render(r: dict[str, Any]) -> str:
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
    fam = r["families"]
    budget = r["protocol"]["review_budget"] * 100
    return f"""# ULB credit-card benchmark (real data, offline)

Source: `{SRC}` ({r["created_at"]}). Dataset: {r["dataset"]}. Protocol fixed and committed before
the held-out window was evaluated (`src/fraudplat/external/benchmark.py`); evaluated once. These
models do not power the live scoring service, and their AP is not comparable with the synthetic
platform's AP: the datasets, features and tasks differ.

**Data provenance and limits.** Real card transactions over 48 hours (2013), released by the data
owner with features V1–V28 already transformed by PCA. The PCA was performed upstream; whether it
was fitted on all 48 hours or on a subset cannot be verified from the released features, so some
information from the held-out hours may be embedded in the features of every window. `Time` is
elapsed seconds since the first transaction; it does not give time of day, so windows are
described in elapsed hours. Actual label-arrival times are unavailable, so labels are treated as
known. There are no customer or merchant identifiers. Licensing metadata is recorded separately in
`THIRD_PARTY_NOTICES.md`; the raw data is not redistributed.

| Window | Elapsed hours | Rows | Frauds |
|---|---|---|---|
| Train | 0–24 | {w["train"]["rows"]:,} | {w["train"]["frauds"]} |
| Validation (model and threshold choice) | 24–32 | {w["validation"]["rows"]:,} | {w["validation"]["frauds"]} |
| Held-out test | 32–48 | {w["test"]["rows"]:,} | {w["test"]["frauds"]} |

| Model | Selected | Validation AP | Test AP (95% CI) | Test ROC AUC | Review rate at threshold | Precision | Recall (caught) | ECE raw | Random-split AP |
|---|---|---|---|---|---|---|---|---|---|
{chr(10).join(rows)}

Threshold: top {budget:.1f}% of validation scores, applied unchanged. Intervals: {r["protocol"]["bootstrap"]}
(per model, not paired).

**Findings**

* Logistic regression had the higher validation AP ({f3(fam["lr"]["validation_ap"])} vs
  {f3(fam["xgb"]["validation_ap"])}); XGBoost had the higher held-out AP
  ({f3(fam["xgb"]["test"]["average_precision"])} vs {f3(fam["lr"]["test"]["average_precision"])}).
  No model is selected for deployment from these results; choosing by held-out AP would use the
  test window for a decision.
* The confidence intervals are computed per model. No paired comparison or significance test was
  run, so no claim is made about whether the held-out difference is statistically significant.
* Validation AP exceeded test AP for both models. The validation window (elapsed hours 24–32,
  {w["validation"]["frauds"]} frauds) is small, and its composition differs from the test window.
* The threshold chosen for a {budget:.1f}% review budget produced review rates of
  {fam["xgb"]["test"]["at_fixed_threshold"]["review_rate"] * 100:.2f}% (XGBoost) and
  {fam["lr"]["test"]["at_fixed_threshold"]["review_rate"] * 100:.2f}% (logistic regression) on the
  test window: a fixed threshold did not hold the intended workload when the score distribution
  shifted.
* Random-split column: the same configurations fitted on a stratified random 70/30 split of all 48
  hours scored higher AP. This is a descriptive comparison only: the training sets differ in size
  (70% of 48 hours vs the first 24 hours) and the test populations differ, so it neither proves
  leakage nor quantifies optimism from ignoring time.
* Raw ECE is small, but so is the fraud rate; scores are treated as risk scores.

**Not saved.** The fitted models and per-transaction predictions were not saved by the protocol run;
only the aggregate metrics above exist. They are not recomputed here.
"""


def main() -> int:
    SRC.with_suffix(".md").write_text(render(json.loads(SRC.read_text())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
