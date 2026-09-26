"""One-time held-out evaluation of a frozen release on the test period (day >= 153).

Run: `python -m fraudplat.release.evaluate --release release-1`

Guards (checked before any test row is read):
* the release manifest's integrity digest verifies, and the manifest is committed and unchanged
  in git (`git diff --quiet HEAD -- <manifest>`);
* candidate and baseline model files and policy files match the manifest's SHA-256 values;
* no earlier evaluation result exists (`reports/<release>/test_evaluation.json`); the script
  refuses to overwrite it.

Features for test rows are reconstructed from the full transaction history under the documented
availability semantics; earlier transactions (including earlier test transactions) update
history. Two separately reported conditions: A = immediate processing, B = simulated worker
delay. Nothing is trained, tuned, recalibrated or re-thresholded here. Labels are the eventual
simulated labels.
"""

# ruff: noqa: E501  -- report construction is kept readable

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mlflow
import numpy as np
import polars as pl

from fraudplat.config import Settings
from fraudplat.features.offline import point_in_time_matrix
from fraudplat.features.spec import DAY, FEATURE_NAMES
from fraudplat.policy import DecisionPolicy, load_policy
from fraudplat.release.freeze import artifact_hashes, sha256_file
from fraudplat.simulator.dataset import _code_revision, to_historical
from fraudplat.training.artifacts import ServableModel, load_any_model
from fraudplat.training.diagnostics import average_precision, scenario_metrics, top_k_mask
from fraudplat.training.evaluation import flagged_metrics

SCENARIOS = ("s1", "s2", "s3")


def verify_manifest(path: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(path.read_text())
    core = {k: v for k, v in manifest.items() if k not in ("created_at", "integrity_sha256")}
    digest = hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if digest != manifest["integrity_sha256"]:
        raise SystemExit("release manifest integrity check failed")
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", str(path)], capture_output=True)  # noqa: S603, S607
    unchanged = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", str(path)])  # noqa: S603, S607
    if tracked.returncode != 0 or unchanged.returncode != 0:
        raise SystemExit("the release manifest must be committed and unchanged before evaluation")
    return manifest


def policy_flags(policy: DecisionPolicy, scores: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    declined = (
        scores >= policy.decline_threshold
        if policy.decline_threshold is not None
        else np.zeros(scores.size, bool)
    )
    flagged = scores >= policy.review_threshold
    return flagged, declined


def point_metrics(
    y: np.ndarray, scores: np.ndarray, amounts: np.ndarray, flagged: np.ndarray
) -> dict[str, Any]:
    m = flagged_metrics(y, flagged, amounts, None)
    legit = y == 0
    return {
        "average_precision": average_precision(y, scores),
        "rows": int(y.size),
        "fraud": int(y.sum()),
        "flagged": int(flagged.sum()),
        "flagged_rate": float(flagged.mean()),
        "flagged_precision": m["precision"],
        "flagged_recall": m["recall"],
        "legitimate_sent_to_review": int((flagged & legit).sum()),
        "false_positive_rate": m["false_positive_rate"],
        "fraud_amount_captured": m["fraud_amount_captured"],
    }


def evaluate_model(
    name: str,
    model: ServableModel,
    policy: DecisionPolicy,
    X: np.ndarray,
    y: np.ndarray,
    amounts: np.ndarray,
    decision_us: np.ndarray,
    day: np.ndarray,
    scen: dict[str, np.ndarray],
    budget: float,
    windows: list[tuple[int, int]],
    reps: int,
    seed: int,
) -> dict[str, Any]:
    scores = model.score_matrix(X)
    flagged, declined = policy_flags(policy, scores)
    out: dict[str, Any] = {
        "model_version": model.model_version,
        "policy_version": policy.version,
        "policy": {
            "review_threshold": policy.review_threshold,
            "decline_threshold": policy.decline_threshold,
        },
        "overall": point_metrics(y, scores, amounts, flagged),
        "declined": int(declined.sum()) if policy.decline_threshold is not None else None,
        "decline_precision": flagged_metrics(y, declined, amounts, None)["precision"]
        if policy.decline_threshold is not None
        else None,
    }
    counts = {s: {"positives": int(m.sum())} for s, m in scen.items()}
    per_scenario = scenario_metrics(y, scores, flagged, scen)
    out["scenarios"] = {
        s: {**counts[s], **per_scenario[s], "flagged_positives": int((flagged & scen[s]).sum())}
        for s in SCENARIOS
    }
    out["windows"] = []
    for start, end in windows:
        w = (day >= start) & (day < end)
        out["windows"].append(
            {"days": [start, end], **point_metrics(y[w], scores[w], amounts[w], flagged[w])}
        )
    top = top_k_mask(scores, decision_us, budget)
    t = flagged_metrics(y, top, amounts, None)
    out["ranking_diagnostics_top_k"] = {
        "budget": budget,
        "precision": t["precision"],
        "recall": t["recall"],
        "note": "retrospective; not the deployed policy",
    }
    if reps:
        rng = np.random.default_rng(seed)
        days = np.floor(day).astype(int)
        by_day = {d: np.flatnonzero(days == d) for d in np.unique(days)}
        keys = np.array(sorted(by_day))
        samples: dict[str, list[float]] = {
            "average_precision": [],
            "flagged_rate": [],
            "flagged_precision": [],
            "flagged_recall": [],
        }
        for _ in range(reps):
            idx = np.concatenate([by_day[d] for d in rng.choice(keys, keys.size, replace=True)])
            yb, fb = y[idx], flagged[idx]
            samples["average_precision"].append(average_precision(yb, scores[idx]) or 0.0)
            samples["flagged_rate"].append(float(fb.mean()))
            tp = int((fb & (yb == 1)).sum())
            samples["flagged_precision"].append(tp / max(int(fb.sum()), 1))
            samples["flagged_recall"].append(tp / max(int(yb.sum()), 1))
        out["bootstrap_95ci"] = {
            k: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))]
            for k, v in samples.items()
        }
    print(
        f"{name}: AP {out['overall']['average_precision']:.4f} flagged {out['overall']['flagged_rate']:.4f}",
        flush=True,
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", default="release-1")
    args = parser.parse_args()
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    manifest_path = Path("releases") / args.release / "manifest.json"
    manifest = verify_manifest(manifest_path)
    out_dir = Path("reports") / args.release
    result_path = out_dir / "test_evaluation.json"
    if result_path.exists():
        raise SystemExit(f"{result_path} exists: the held-out evaluation has already been run")
    with Path(f"configs/{args.release}.toml").open("rb") as handle:
        rel = tomllib.load(handle)

    cand_path = Path("artifacts/models") / manifest["model"]["model_version"]
    if artifact_hashes(cand_path) != manifest["model"]["files_sha256"]:
        raise SystemExit("candidate model files do not match the manifest")
    cand_policy_path = Path("artifacts/policies") / f"{manifest['model']['model_version']}.json"
    if sha256_file(cand_policy_path) != manifest["policy"]["file_sha256"]:
        raise SystemExit("candidate policy file does not match the manifest")
    base = manifest["baseline_for_rollback"]
    base_path = Path("artifacts/models") / base["model_version"]
    if artifact_hashes(base_path) != base["files_sha256"]:
        raise SystemExit("baseline model files do not match the manifest")
    candidate, cand_policy = load_any_model(cand_path), load_policy(cand_policy_path)
    baseline, base_policy = load_any_model(base_path), load_policy(Path(rel["baseline_policy"]))
    if (cand_policy.version, base_policy.version) != (
        manifest["policy"]["policy_version"],
        base["policy_version"],
    ):
        raise SystemExit("policy versions do not match the manifest")

    # --- first read of test-period data ---------------------------------------------------
    started = datetime.now(UTC).isoformat(timespec="seconds")
    raw = pl.read_parquet("data/raw/sim-v2/transactions.parquet")
    dataset_sha = json.loads(Path("data/raw/sim-v2/manifest.json").read_text())["tables"][
        "transactions"
    ]["content_sha256"]
    if dataset_sha != manifest["dataset"]["content_sha256"]:
        raise SystemExit("dataset does not match the manifest")
    first = int(raw["event_time"].dt.epoch("us").min())  # type: ignore[arg-type]
    day0 = first - first % DAY
    txns = to_historical(raw)
    ids = raw["transaction_id"].to_numpy()
    labels = {
        c: raw[c].to_numpy()
        for c in ("is_fraud", "fraud_s1", "fraud_s2", "fraud_s3", "amount_minor")
    }
    test_days = rel["test_start_day"], rel["test_end_day"]
    windows = [(153, 160), (160, 167), (167, 174), (174, 183)]
    conditions = {
        "A_immediate_processing": 0,
        "B_simulated_worker_delay_ms": rel["simulated_worker_delay_ms"],
    }
    results: dict[str, Any] = {}
    for cond, delay_ms in conditions.items():
        matrix = point_in_time_matrix(txns, processing_delay_us=int(delay_ms * 1000))
        day = (matrix.decision_time_us - day0) / DAY
        test = (day >= test_days[0]) & (day < test_days[1])
        rows = matrix.txn_index[test]
        X = matrix.values[test]
        y = labels["is_fraud"][rows].astype(int)
        amounts = labels["amount_minor"][rows].astype(float)
        scen = {s: labels[f"fraud_{s}"][rows] for s in SCENARIOS}
        reps = rel["bootstrap_reps"] if cond.startswith("A_") else 0
        common = (
            X,
            y,
            amounts,
            matrix.decision_time_us[test],
            day[test],
            scen,
            rel["review_capacity_target"],
            windows,
            reps,
            rel["bootstrap_seed"],
        )
        results[cond] = {
            "delay_ms": delay_ms,
            "apply_statuses": {k.value: v for k, v in matrix.apply_statuses.items()},
            "test_rows": int(test.sum()),
            "test_fraud": int(y.sum()),
            "first_test_transaction": str(ids[rows[0]]),
            "candidate": evaluate_model(f"{cond} candidate", candidate, cand_policy, *common),
            "baseline": evaluate_model(f"{cond} baseline", baseline, base_policy, *common),
        }

    acc = manifest["acceptance_criteria"]
    a = results["A_immediate_processing"]
    c, b = a["candidate"]["overall"], a["baseline"]["overall"]
    acceptance = {
        "candidate_ap_exceeds_baseline_ap": {
            "candidate": c["average_precision"],
            "baseline": b["average_precision"],
            "pass": c["average_precision"] > b["average_precision"],
        },
        "review_rate_in_range": {
            "observed": c["flagged_rate"],
            "range": [acc["review_rate_min"], acc["review_rate_max"]],
            "pass": acc["review_rate_min"] <= c["flagged_rate"] <= acc["review_rate_max"],
        },
        "flagged_precision_minimum": {
            "observed": c["flagged_precision"],
            "minimum": acc["flagged_precision_min"],
            "pass": c["flagged_precision"] >= acc["flagged_precision_min"],
        },
        "serving": {"status": "evaluated separately by the release benchmark"},
    }
    report = {
        "release": args.release,
        "manifest_sha256": manifest["integrity_sha256"],
        "evaluation_code_revision": _code_revision(),
        "started_at": started,
        "completed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "test_days": list(test_days),
        "labels": "eventual simulated labels",
        "feature_names": list(FEATURE_NAMES),
        "conditions": results,
        "acceptance": acceptance,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(report, indent=2, default=float) + "\n")
    settings = Settings()
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_experiment("fraud-f1-releases")
    with mlflow.start_run(run_name=f"test-evaluation-{args.release}"):
        mlflow.set_tags({"release": args.release, "manifest_sha256": manifest["integrity_sha256"]})
        mlflow.log_metrics(
            {
                "test_ap_candidate": c["average_precision"],
                "test_ap_baseline": b["average_precision"],
                "test_review_rate_candidate": c["flagged_rate"],
                "test_precision_candidate": c["flagged_precision"],
                "test_recall_candidate": c["flagged_recall"],
            }
        )
        mlflow.log_artifact(str(result_path), "evaluation")
    print(json.dumps(acceptance, indent=2, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
