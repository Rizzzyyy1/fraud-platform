"""Render `reports/<release>/test_evaluation.md` from the evaluation JSON (numbers are generated)."""

# ruff: noqa: E501, RUF001  -- markdown rows on one line; en dashes are intended in output

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _pct(x: float) -> str:
    return f"{100 * x:.2f}%"


def _ci(m: dict[str, Any], key: str, pct: bool = False) -> str:
    ci = m.get("bootstrap_95ci", {}).get(key)
    if not ci:
        return ""
    return f" ({_pct(ci[0])}–{_pct(ci[1])})" if pct else f" ({ci[0]:.3f}–{ci[1]:.3f})"


def render(r: dict[str, Any], manifest: dict[str, Any], extras: dict[str, Any]) -> str:
    out: list[str] = []
    w = out.append
    w(f"# Held-out evaluation — {r['release']}\n")
    w(
        f"Frozen manifest `releases/{r['release']}/manifest.json` (sha256 `{r['manifest_sha256'][:16]}…`), "
        f"committed before any test row was read. Evaluation code `{r['evaluation_code_revision'][:7]}`; "
        f"started {r['started_at']}. Test period: days {r['test_days'][0]}–{r['test_days'][1]} "
        f"(synthetic sim-v2), evaluated **once**. Labels: {r['labels']}.\n"
    )
    if "corrections" in extras:
        den = extras["corrections"]["denominator"]
        w(
            f"> **Denominator:** {den['evaluated_rows']:,} transactions were evaluated. {den['explanation']} "
            f"The follow-up delay count below therefore reports {den['delay_check_rows']:,}.\n"
        )
    w(
        "Nothing was trained, tuned, recalibrated or re-thresholded on the test period. Features for test "
        "rows were reconstructed from the full history under the documented availability rules. 95% "
        "intervals: day-block bootstrap (500 replicates) under condition A.\n"
    )
    a = r["conditions"]["A_immediate_processing"]
    w("## Condition A — immediate processing\n")
    w(f"{a['test_rows']:,} transactions, {a['test_fraud']:,} fraud.\n")
    w("| | Candidate (review-only) | Deployed baseline (review + decline) |")
    w("|---|---|---|")
    c, b = a["candidate"], a["baseline"]
    co, bo = c["overall"], b["overall"]
    rows = [
        (
            "Model / policy",
            f"`{c['model_version']}` / `{c['policy_version']}`",
            f"`{b['model_version']}` / `{b['policy_version']}`",
        ),
        (
            "Thresholds",
            f"review ≥ {c['policy']['review_threshold']:.4f}; no decline",
            f"review ≥ {b['policy']['review_threshold']:.4f}; decline ≥ {b['policy']['decline_threshold']:.4f}",
        ),
        (
            "Average precision",
            f"{co['average_precision']:.3f}{_ci(c, 'average_precision')}",
            f"{bo['average_precision']:.3f}{_ci(b, 'average_precision')}",
        ),
        (
            "Flagged (review or decline)",
            f"{co['flagged']:,} = {_pct(co['flagged_rate'])}{_ci(c, 'flagged_rate', True)}",
            f"{bo['flagged']:,} = {_pct(bo['flagged_rate'])}{_ci(b, 'flagged_rate', True)}",
        ),
        (
            "Precision of flagged",
            f"{co['flagged_precision']:.3f}{_ci(c, 'flagged_precision')}",
            f"{bo['flagged_precision']:.3f}{_ci(b, 'flagged_precision')}",
        ),
        (
            "Recall",
            f"{co['flagged_recall']:.3f}{_ci(c, 'flagged_recall')}",
            f"{bo['flagged_recall']:.3f}{_ci(b, 'flagged_recall')}",
        ),
        (
            "Legitimate transactions flagged",
            f"{co['legitimate_sent_to_review']:,} (FPR {co['false_positive_rate']:.4f})",
            f"{bo['legitimate_sent_to_review']:,} (FPR {bo['false_positive_rate']:.4f})",
        ),
        (
            "Automatically declined",
            "— (disabled)",
            f"{b['declined']:,} (precision {b['decline_precision']:.3f})",
        ),
        (
            "Fraud amount captured",
            _pct(co["fraud_amount_captured"]),
            _pct(bo["fraud_amount_captured"]),
        ),
        (
            "Top-1% ranking diagnostic (retrospective)",
            f"precision {c['ranking_diagnostics_top_k']['precision']:.3f}, recall {c['ranking_diagnostics_top_k']['recall']:.3f}",
            f"precision {b['ranking_diagnostics_top_k']['precision']:.3f}, recall {b['ranking_diagnostics_top_k']['recall']:.3f}",
        ),
    ]
    if "corrections" in extras:
        cc = extras["corrections"]["conditions"]["A_immediate_processing"]
        cb, bb = cc["candidate"], cc["baseline"]
        rows = [
            r
            for r in rows
            if r[0] not in ("Legitimate transactions flagged", "Automatically declined")
        ]
        rows[3] = ("Flagged rate (reviewed + declined)", rows[3][1], rows[3][2])
        rows.insert(
            4,
            (
                "Reviewed (sent to analysts)",
                f"{cb['reviewed']:,} = {_pct(cb['review_rate'])}",
                f"{bb['reviewed']:,} = {_pct(bb['review_rate'])}",
            ),
        )
        rows.insert(
            5,
            (
                "Automatically declined",
                "— (disabled)",
                f"{bb['declined']:,} = {_pct(bb['decline_rate'])} ({bb['declined_fraud']:,} fraud, {bb['legitimate_declined']:,} legitimate)",
            ),
        )
        rows.insert(
            8,
            (
                "Legitimate transactions reviewed / declined",
                f"{cb['legitimate_reviewed']:,} / 0",
                f"{bb['legitimate_reviewed']:,} / {bb['legitimate_declined']:,}",
            ),
        )
    for label, cv, bv in rows:
        w(f"| {label} | {cv} | {bv} |")
    w(
        '\nThe capacity target was 1.00% reviewed. "Flagged" = reviewed + automatically declined; for '
        "the review-only candidate the two are the same. Review/decline splits for the baseline are "
        "derived arithmetically from the frozen results (`test_evaluation_corrections.json`); the "
        "frozen JSON field `legitimate_sent_to_review` counts all flagged legitimate transactions.\n"
    )
    w("### Scenarios\n")
    w(
        "Population for scenario s: its fraud plus all legitimate rows; a transaction in several scenarios counts in each.\n"
    )
    w(
        "| Scenario | Positives | Candidate flagged (recall) | Candidate AP vs legit | Baseline flagged (recall) | Baseline AP vs legit |"
    )
    w("|---|---|---|---|---|---|")
    for s in ("s1", "s2", "s3"):
        cs, bs = c["scenarios"][s], b["scenarios"][s]
        w(
            f"| {s} | {cs['positives']:,} | {cs['flagged_positives']:,} ({cs['recall_in_flagged']:.3f}) | {cs['average_precision_vs_legit']:.3f} | {bs['flagged_positives']:,} ({bs['recall_in_flagged']:.3f}) | {bs['average_precision_vs_legit']:.3f} |"
        )
    w(
        "\nScenario 2 (compromised terminals) is essentially undetected by both models. Missing terminal "
        "fraud-history features remains a hypothesis, not a tested cause.\n"
    )
    w("### Weekly windows (fixed policy)\n")
    w(
        "| Days | Rows | Fraud | Candidate flagged rate / precision / recall / AP | Baseline flagged rate / precision / recall / AP |"
    )
    w("|---|---|---|---|---|")
    for cw, bw in zip(c["windows"], b["windows"], strict=True):
        w(
            f"| {cw['days'][0]}–{cw['days'][1]} | {cw['rows']:,} | {cw['fraud']:,} | {_pct(cw['flagged_rate'])} / {cw['flagged_precision']:.3f} / {cw['flagged_recall']:.3f} / {cw['average_precision']:.3f} | {_pct(bw['flagged_rate'])} / {bw['flagged_precision']:.3f} / {bw['flagged_recall']:.3f} / {bw['average_precision']:.3f} |"
        )
    bcond = r["conditions"]["B_simulated_worker_delay_ms"]
    w(f"\n## Condition B — simulated worker delay {bcond['delay_ms']} ms\n")
    w(
        f"Candidate AP {bcond['candidate']['overall']['average_precision']:.4f}, flagged {_pct(bcond['candidate']['overall']['flagged_rate'])}, "
        f"precision {bcond['candidate']['overall']['flagged_precision']:.3f}, recall {bcond['candidate']['overall']['flagged_recall']:.3f}; "
        f"baseline AP {bcond['baseline']['overall']['average_precision']:.4f} — identical to condition A at the reported precision."
    )
    if "delay_check" in extras:
        d = extras["delay_check"]
        w(
            " A follow-up count (features only, no labels): of "
            f"{d['delay_1000ms']['test_rows']:,} test rows, {d['delay_1000ms']['test_rows_with_different_features']:,} had a different feature "
            f"vector with a 1 s processing delay and {d['delay_60000ms']['test_rows_with_different_features']:,} with a 60 s delay "
            f"({d.get('note', '')}).\n"
        )
    w("## Acceptance criteria (declared in the manifest before the test)\n")
    w("| Criterion | Observed | Result |")
    w("|---|---|---|")
    acc = r["acceptance"]
    w(
        f"| Candidate AP > baseline AP (A) | {acc['candidate_ap_exceeds_baseline_ap']['candidate']:.3f} vs {acc['candidate_ap_exceeds_baseline_ap']['baseline']:.3f} | {'pass' if acc['candidate_ap_exceeds_baseline_ap']['pass'] else 'fail'} |"
    )
    w(
        f"| Review rate in [0.5%, 2.0%] | {_pct(acc['review_rate_in_range']['observed'])} | {'pass' if acc['review_rate_in_range']['pass'] else 'fail'} |"
    )
    w(
        f"| Flagged precision ≥ 0.20 | {acc['flagged_precision_minimum']['observed']:.3f} | {'pass' if acc['flagged_precision_minimum']['pass'] else 'fail'} |"
    )
    if "serving" in extras:
        s = extras["serving"]
        w(
            f"| Serving at 100 rps: p95 ≤ 100 ms, p99 ≤ 200 ms, error rate ≤ 0.1% | {s['summary']} | {'pass' if s['pass'] else 'fail'} |"
        )
        w(
            '\nThe release-session "baseline control" was a single 60 s run. The later bounded '
            "diagnosis (`reports/serving_diagnosis/summary.md`) found sustained performance "
            "inconsistent for both models (4/6 runs passed each); the baseline is the retained "
            "default, not a consistently passing control."
        )
    else:
        w("| Serving at 100 rps | see release benchmark | pending |")
    return "\n".join(out) + "\n"


def main() -> int:
    release = sys.argv[1] if len(sys.argv) > 1 else "release-1"
    base = Path("reports") / release
    r = json.loads((base / "test_evaluation.json").read_text())
    manifest = json.loads((Path("releases") / release / "manifest.json").read_text())
    extras: dict[str, Any] = {}
    for name in ("delay_check", "serving", "corrections"):
        path = base / (
            "test_evaluation_corrections.json" if name == "corrections" else f"{name}.json"
        )
        if path.exists():
            extras[name] = json.loads(path.read_text())
    (base / "test_evaluation.md").write_text(render(r, manifest, extras))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
