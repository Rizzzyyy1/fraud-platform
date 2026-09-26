"""Render `reports/model_comparison/report.md` from `report.json` (numbers are never typed)."""
# ruff: noqa: E501  -- markdown table rows are kept on one line for readability

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _f(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value: Any) -> str:
    return "—" if value is None else f"{100 * value:.2f}%"


def render(r: dict[str, Any]) -> str:
    fams = r["best_per_family"]
    folds = [f["fold"] for f in r["folds"]]
    out: list[str] = []
    w = out.append
    w("# Model comparison (development evidence)\n")
    w(
        f"Generated from `report.json` at commit `{r['code_revision'][:12]}`; dataset "
        f"`{r['dataset']['dataset_id']}` (`{r['dataset']['content_sha256'][:16]}…`, synthetic); "
        f"feature version `{r['feature_version']}`. MLflow experiment `{r['mlflow']['experiment']}`, "
        f"parent run `{r['mlflow']['parent_run_id']}`.\n"
    )
    w(
        "**The final test period (day ≥ 153) was not used.** All windows below are pre-test and have "
        "been used for development; the day-130 evaluation window is the former validation period, "
        "which already guided earlier choices. These are development estimates, not untouched ones.\n"
    )

    w("## Folds\n")
    w(
        "| Fold | Fit days | Calibration days | Evaluation days | Fit rows (fraud) | Excluded unlabelled | Eval rows (fraud) |"
    )
    w("|---|---|---|---|---|---|---|")
    for f in r["folds"]:
        w(
            f"| {f['fold']} | {f['fit_days']} | {f['calibration_days']} | {f['eval_days']} | "
            f"{f['fit_rows']:,} ({f['fit_fraud']:,}) | {f['fit_excluded_unlabelled']} | "
            f"{f['eval_rows']:,} ({f['eval_fraud']:,}) |"
        )
    w(
        "\nEligibility: fit and calibration rows need `label_available_at <= cutoff`. The 30-day gap "
        "between calibration end and cutoff means every such label is known.\n"
    )

    w("## All attempted configurations\n")
    w(
        "| Family | Configuration | Status | "
        + " | ".join(f"AP {f}" for f in folds)
        + " | Mean AP |"
    )
    w("|---|---|---|" + "---|" * len(folds) + "---|")
    for a in r["attempts"]:
        cfg = ", ".join(f"{k}={v}" for k, v in sorted(a["params"].items()))
        cells = (
            [_f(a["folds"].get(f, {}).get("average_precision")) for f in folds]
            if a["status"] == "ok"
            else ["—"] * len(folds)
        )
        w(
            f"| {a['family']} | {cfg} | {a['status']}{': ' + a['error'] if a.get('error') else ''} | "
            + " | ".join(cells)
            + f" | {_f(a.get('mean_average_precision'))} |"
        )

    w("\n## Representative per family (highest mean AP)\n")
    w(
        "Retrospective top-k: the top 1% of each evaluation window by score, ties broken by earlier "
        "decision time then row position. This uses the whole window in hindsight.\n"
    )
    w(
        "| Family | Fold | AP | ROC AUC | Top-1% precision | Top-1% recall | FPR | Fraud amount captured |"
    )
    w("|---|---|---|---|---|---|---|---|")
    for fam, d in fams.items():
        for f in folds:
            e = d["folds"][f]
            t = e["retrospective_top_k"]
            w(
                f"| {fam} | {f} | {_f(e['average_precision'])} | {_f(e['roc_auc'])} | "
                f"{_f(t['precision'])} | {_f(t['recall'])} | {_f(t['false_positive_rate'], 4)} | "
                f"{_f(t['fraud_amount_captured'])} |"
            )

    w("\n## Fixed-threshold policy (what the online service can do)\n")
    w(
        "Thresholds chosen on each fold's calibration window (review: top 1% of calibration scores; "
        "decline: lowest threshold with precision ≥ 0.90 on ≥ 20 calibration rows), then applied "
        "unchanged to the following evaluation window.\n"
    )
    w(
        "| Family | Fold | Review rate | Decline rate | Flagged precision | Flagged recall | Decline precision | Weekly review+decline rates |"
    )
    w("|---|---|---|---|---|---|---|---|")
    for fam, d in fams.items():
        for f in folds:
            x = d["folds"][f]["fixed_thresholds"]
            weekly = ", ".join(
                _pct(wk["review_rate"] + wk["decline_rate"]) for wk in x["by_window"]
            )
            dec = x["decline_only"]["precision"] if x["decline_only"] else None
            w(
                f"| {fam} | {f} | {_pct(x['review_rate'])} | {_pct(x['decline_rate'])} | "
                f"{_f(x['review_or_decline']['precision'])} | {_f(x['review_or_decline']['recall'])} | "
                f"{_f(dec)} | {weekly} |"
            )

    w("\n## Scenarios\n")
    w(
        "Population for scenario s: fraud under s plus all legitimate rows; rows that are fraud only "
        "under other scenarios are excluded; a row in several scenarios is positive in each. Recall "
        "and precision refer to the retrospective top-1% set.\n"
    )
    w("| Family | Fold | s1 AP / recall | s2 AP / recall | s3 AP / recall |")
    w("|---|---|---|---|---|")
    for fam, d in fams.items():
        for f in folds:
            sc = d["folds"][f]["retrospective_top_k"]["scenarios"]
            w(
                f"| {fam} | {f} | "
                + " | ".join(
                    f"{_f(sc[s]['average_precision_vs_legit'])} / {_f(sc[s]['recall_in_flagged'])}"
                    for s in ("s1", "s2", "s3")
                )
                + " |"
            )
    w(
        "\nScenario 2 (compromised terminals) remains weakly detected. Missing terminal "
        "fraud-history features is a hypothesis for this, not a tested cause.\n"
    )

    w("## Calibration (evaluation windows)\n")
    w(
        "Raw scores, and scores after Platt scaling fitted on the calibration window (separate from "
        "the fit window). ECE over 10 equal-count bins.\n"
    )
    w(
        "| Family | Fold | Fraud rate | Mean raw score | Brier raw | ECE raw | Brier Platt | ECE Platt |"
    )
    w("|---|---|---|---|---|---|---|---|")
    for fam, d in fams.items():
        if fam == "rules":
            continue
        for f in folds:
            c, p = (
                d["folds"][f]["calibration_raw"],
                d["folds"][f]["calibration_platt_from_calibration_window"],
            )
            w(
                f"| {fam} | {f} | {_f(c['fraud_rate'], 4)} | {_f(c['mean_score'], 4)} | "
                f"{_f(c['brier'], 5)} | {_f(c['ece_10_equal_count'], 5)} | {_f(p['brier'], 5)} | "
                f"{_f(p['ece_10_equal_count'], 5)} |"
            )

    w("\n## Serving checks (models fitted on the last fold)\n")
    w(
        "| Family | Model version | Artifact size | Single-row p50 / p95 / p99 | Artifact vs training max abs diff |"
    )
    w("|---|---|---|---|---|")
    for fam, s in r["serving"].items():
        t = s["single_row_us"]
        w(
            f"| {fam} | `{s['model_version']}` | {s['artifact_bytes']:,} B | "
            f"{t['p50']:.1f} / {t['p95']:.1f} / {t['p99']:.1f} µs | {s['artifact_vs_training_max_abs_diff']:.2e} |"
        )

    sel = r["selection"]
    w("\n## Selection (rule declared in `configs/compare-v1.toml` before running)\n")
    for k, v in sel["rule"].items():
        w(f"* {k} = {v}")
    if "mean_ap_gain_xgb_minus_lr" in sel:
        w(
            f"\nXGBoost minus logistic regression, mean AP: **{sel['mean_ap_gain_xgb_minus_lr']:+.4f}**; "
            f"folds won by XGBoost: **{sel['xgb_fold_wins']}/{len(folds)}**; serving constraints met: "
            f"**{sel['xgb_serving_constraints_met']}**."
        )
    w(
        f"\n**Selected: {sel['selected_family']}** — `{sel['selected_model_version']}` with policy "
        f"`{sel['policy_version']}`.\n"
    )
    return "\n".join(out) + "\n"


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "reports/model_comparison/report.json")
    (path.parent / "report.md").write_text(render(json.loads(path.read_text())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
