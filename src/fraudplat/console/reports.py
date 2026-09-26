"""Historical results shown by the console, read from committed report files (never typed in).

Each entry names the data split it was measured on and the exact artifact and policy identities,
so development, held-out and live numbers are never mixed. Only files listed in `REPORTS` can be
served; a request names a report by key, never by path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPORTS: dict[str, tuple[str, str]] = {
    "comparison": ("Model comparison (development folds)", "reports/model_comparison/report.md"),
    "held_out": ("Release-1 held-out evaluation", "reports/release-1/test_evaluation.md"),
    "diagnosis": ("Serving diagnosis (12 runs)", "reports/serving_diagnosis/summary.md"),
    "benchmark": ("End-to-end benchmark", "reports/benchmarks/e2e.md"),
    "model_card": ("Model card", "docs/MODEL_CARD.md"),
    "ulb": ("Real-data offline benchmark (ULB)", "reports/external/ulb/benchmark.md"),
}


def _load(path: str) -> dict[str, Any] | None:
    p = Path(path)
    if not p.exists():
        return None
    data: dict[str, Any] = json.loads(p.read_text())
    return data


def report_text(key: str) -> tuple[str, str] | None:
    entry = REPORTS.get(key)
    if entry is None or not Path(entry[1]).exists():
        return None
    return entry[0], Path(entry[1]).read_text()


def historical_results() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    comparison = _load("reports/model_comparison/report.json")
    if comparison is not None:
        best = comparison["best_per_family"]
        serving = comparison["serving"]
        out.append(
            {
                "key": "comparison",
                "title": "Model comparison",
                "split": "development",
                "split_detail": "chronological folds on pre-test days; no test data",
                "source": "reports/model_comparison/report.json",
                "rows": [
                    {
                        "label": "Rules baseline",
                        "identity": "rule configuration (no artifact)",
                        "metrics": {
                            "mean_average_precision": best["rules"]["mean_average_precision"]
                        },
                    },
                    {
                        "label": "Logistic regression",
                        "identity": serving["lr"]["model_version"],
                        "metrics": {"mean_average_precision": best["lr"]["mean_average_precision"]},
                    },
                    {
                        "label": "XGBoost",
                        "identity": serving["xgb"]["model_version"],
                        "metrics": {
                            "mean_average_precision": best["xgb"]["mean_average_precision"]
                        },
                    },
                ],
                "note": "Comparison-stage artifacts, not the deployed model or the release "
                "candidate (both were refit later).",
            }
        )
    held = _load("reports/release-1/test_evaluation.json")
    corrections = _load("reports/release-1/test_evaluation_corrections.json")
    if held is not None:
        cond = held["conditions"]["A_immediate_processing"]
        fixed = (corrections or {}).get("conditions", {}).get("A_immediate_processing", {})
        rows = []
        for role in ("candidate", "baseline"):
            m = cond[role]
            o = m["overall"]
            split = fixed.get(role, {})
            rows.append(
                {
                    "label": "Candidate (XGBoost)" if role == "candidate" else "Baseline (LR)",
                    "identity": f"{m['model_version']} + {m['policy_version']}",
                    "metrics": {
                        "average_precision": o["average_precision"],
                        "average_precision_95ci": m["bootstrap_95ci"].get("average_precision"),
                        "reviewed": split.get("reviewed"),
                        "declined": split.get("declined"),
                        "flagged_precision": o["flagged_precision"],
                        "flagged_recall": o["flagged_recall"],
                        "legitimate_reviewed": split.get("legitimate_reviewed"),
                    },
                }
            )
        out.append(
            {
                "key": "held_out",
                "title": "Release-1 held-out evaluation (run once)",
                "split": "held-out",
                "split_detail": f"decision days [{held['test_days'][0]}, "
                f"{held['test_days'][1]}) of sim-v2, {cond['test_rows']:,} transactions; "
                "frozen manifest "
                f"{held['manifest_sha256'][:12]}",
                "source": "reports/release-1/test_evaluation.json",
                "rows": rows,
                "note": "Frozen policies as evaluated. The baseline's frozen policy "
                "pol-82d9e16668ae includes automatic declines; it is not the policy serving now.",
            }
        )
    policy = _load("reports/serving_diagnosis/baseline_review_only_policy.json")
    if policy is not None:
        out.append(
            {
                "key": "active_policy",
                "title": "Active review-only policy",
                "split": "development",
                "split_detail": "threshold chosen on pre-test days 123-146 without labels; "
                "days 146-153 are the forward check",
                "source": "reports/serving_diagnosis/baseline_review_only_policy.json",
                "rows": [
                    {
                        "label": f"Days {w['days'][0]}-{w['days'][1]}",
                        "identity": f"lr-f1-6f0ebad8fcc7 + {policy['policy_version']}",
                        "metrics": {"review_rate": w["review_rate"]},
                    }
                    for w in policy["development_review_rates"]
                ],
                "note": "No held-out result exists for this policy; the test period is not reused.",
            }
        )
    ulb = _load("reports/external/ulb/benchmark.json")
    if ulb is not None:
        test = ulb["windows"]["test"]
        names = {"lr": "Logistic regression", "xgb": "XGBoost"}
        out.append(
            {
                "key": "ulb_benchmark",
                "title": "Real-data offline benchmark (ULB)",
                "split": "external",
                "split_detail": (
                    f"real, anonymised card transactions ({ulb['dataset']}); held out: elapsed "
                    f"hours 32\u201348, {test['rows']:,} transactions, {test['frauds']} frauds; "
                    "evaluated once"
                ),
                "source": "reports/external/ulb/benchmark.json",
                "rows": [
                    {
                        "label": names[family],
                        "identity": "original fit, not saved: "
                        + ", ".join(f"{k}={val}" for k, val in v["selected"].items()),
                        "metrics": {
                            "validation_ap": v["validation_ap"],
                            "average_precision": v["test"]["average_precision"],
                            "average_precision_95ci": v["test"]["ci95"]["average_precision"],
                            "roc_auc": v["test"]["roc_auc"],
                            "precision_at_threshold": v["test"]["at_fixed_threshold"]["precision"],
                            "recall_at_threshold": v["test"]["at_fixed_threshold"]["recall"],
                            "review_rate": v["test"]["at_fixed_threshold"]["review_rate"],
                        },
                    }
                    for family, v in ulb["families"].items()
                ],
                "note": (
                    "These models do not power the live scoring service, and no model is selected "
                    "from these held-out results. Features are PCA components computed upstream by "
                    "the data owner (fitting scope unverifiable); no customer or merchant IDs; "
                    "label-arrival times unavailable. AP here is not comparable with the synthetic "
                    "results above. A later rerun of the frozen protocol reproduced these values "
                    "identically in its recorded environment (the original environment was not "
                    "recorded); its models and predictions are kept locally, not published. "
                    "Contains information from Credit Card Fraud Detection (Kaggle "
                    "mlg-ulb/creditcardfraud), made available under the Open Database License "
                    "(ODbL)."
                ),
            }
        )
    return out


def _serving_tally() -> dict[str, dict[str, int]]:
    """Pass counts per model from the recorded diagnosis runs, with the criterion used there."""
    tally: dict[str, dict[str, int]] = {}
    for path in sorted(Path("reports/serving_diagnosis").glob("run*_*.json")):
        run = json.loads(path.read_text())
        level = run["levels"][0]
        m, lat = level["measured_window"], level["latency_ms_all_responses"]
        ok = (
            lat["p95"] <= 100
            and lat["p99"] <= 200
            and m["achieved_completion_rps"] >= 99
            and m["failed_http_or_rejected"] / m["scheduled"] <= 0.001
        )
        model = run["readiness_at_start"]["model"]["model_version"]
        entry = tally.setdefault(model, {"passed": 0, "runs": 0})
        entry["runs"] += 1
        entry["passed"] += int(ok)
    return tally


def release_status() -> dict[str, Any] | None:
    held = _load("reports/release-1/test_evaluation.json")
    rollout = _load("reports/release-1/rollout.json")
    if held is None:
        return None
    acc = held["acceptance"]
    candidate = held["conditions"]["A_immediate_processing"]["candidate"]
    serving = _load("reports/release-1/serving.json") or {}
    return {
        "release": held["release"],
        "candidate": {
            "model_version": candidate["model_version"],
            "policy_version": candidate["policy_version"],
            "registry_ref": (
                f"fraud-risk-f1/{rollout['registry_versions']['candidate']}" if rollout else None
            ),
            "status": "not promoted",
        },
        "criteria": [
            {
                "name": "Candidate AP > baseline AP (held-out)",
                "passed": acc["candidate_ap_exceeds_baseline_ap"]["pass"],
            },
            {
                "name": "Review rate in [0.5%, 2.0%] (held-out)",
                "passed": acc["review_rate_in_range"]["pass"],
            },
            {
                "name": "Flagged precision >= 0.20 (held-out)",
                "passed": acc["flagged_precision_minimum"]["pass"],
            },
            {
                "name": "Serving at 100 rps: p95 <= 100 ms, p99 <= 200 ms, errors <= 0.1%",
                "passed": bool(serving.get("pass", False)),
            },
        ],
        "serving_diagnosis": _serving_tally(),
        "limitation": "Sustained 100 rps latency is inconsistent for both models on this "
        "machine; the cause is not established. The baseline is the retained default, not a "
        "consistently passing control.",
    }


_POLICY_SOURCES = (
    "releases/active/policy.json",
    "artifacts/policies/*.json",
    "artifacts/registry-cache/*/*/policy.json",
)
_policies: dict[str, dict[str, Any]] | None = None


def policy_rules() -> dict[str, dict[str, Any]]:
    """Thresholds of every integrity-verified policy file available locally, by version.

    Used to state which rule produced a stored action. A policy whose file is not available
    locally is simply absent (the UI then shows the stored reason codes only).
    """
    global _policies
    if _policies is None:
        from fraudplat.policy import load_policy

        found: dict[str, dict[str, Any]] = {}
        for pattern in _POLICY_SOURCES:
            for path in sorted(Path().glob(pattern)):
                try:
                    policy = load_policy(path)
                except (OSError, ValueError, KeyError):
                    continue  # unreadable or failing integrity: not used
                found[policy.version] = {
                    "version": policy.version,
                    "review_threshold": policy.review_threshold,
                    "decline_threshold": policy.decline_threshold,
                    "derived_for_model": policy.derived_for_model,
                }
        _policies = found
    return _policies
