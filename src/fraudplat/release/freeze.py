"""Freeze release 1: final refit, review-only policy, and a hashed release manifest.

Run: `python -m fraudplat.release.freeze --config configs/release-1.toml`
Refuses to run from a dirty working tree. Reads only pre-test data (day < 153).

1. Refit the comparison's selected XGBoost configuration on `final_fit_days` with labels known by
   `labels_known_by_day` (no new tuning).
2. Score `policy_selection_days` with that final model; the review threshold is the score of the
   ceil(target * N)-th highest transaction there. No labels are used to choose it.
3. Report how the fixed threshold transfers: review rate per week in the selection window and
   in the later `policy_transfer_check_days`. Eventual simulated labels are shown for
   description only; they would not all be known at day 153.
4. Write artifacts, the policy (automatic decline disabled) and `releases/<id>/manifest.json`
   with identities, hashes, evaluation plan and acceptance criteria; log them to MLflow and
   register the bundle.
"""

# ruff: noqa: E501  -- manifest construction is kept readable as long literal dicts

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mlflow
import numpy as np
import xgboost as xgb

from fraudplat.config import Settings
from fraudplat.features.spec import FEATURE_NAMES, FEATURE_VERSION
from fraudplat.policy import load_policy, write_policy
from fraudplat.registry import publish
from fraudplat.simulator.dataset import _code_revision
from fraudplat.training.artifacts import artifact_file, load_any_model
from fraudplat.training.compare import load_data, window, xgb_params
from fraudplat.training.diagnostics import average_precision, top_k_mask
from fraudplat.training.evaluation import flagged_metrics
from fraudplat.training.xgb_model import save_xgb_model

REGISTERED_NAME = "fraud-risk-f1"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_hashes(model_path: Path) -> dict[str, str]:
    file = artifact_file(model_path)
    files = sorted(file.parent.iterdir()) if file.name == "manifest.json" else [file]
    return {p.name: sha256_file(p) for p in files}


def _digest(core: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    code = _code_revision()
    if code.endswith("+dirty") or code == "unknown":
        raise SystemExit(f"refusing to freeze from a dirty or unknown code revision: {code}")
    with args.config.open("rb") as handle:
        rel = tomllib.load(handle)
    with Path(rel["comparison_config"]).open("rb") as handle:
        cmp_cfg = tomllib.load(handle)
    report_path = Path(rel["comparison_report"])
    report = json.loads(report_path.read_text())
    if report["selection"]["selected_family"] != "xgb":
        raise SystemExit("this freeze procedure expects the comparison to have selected XGBoost")
    params = report["best_per_family"]["xgb"]["config"]

    data = load_data(cmp_cfg)  # pre-test rows only (raises if test rows appear)
    day0_us = data.day0_us
    known_by = data.label_us <= day0_us + rel["labels_known_by_day"] * 86_400_000_000
    fit = window(data, tuple(rel["final_fit_days"])) & known_by
    booster = xgb.train(
        xgb_params(cmp_cfg, params),
        xgb.DMatrix(
            data.X[fit], label=data.y[fit], missing=np.nan, feature_names=list(FEATURE_NAMES)
        ),
        num_boost_round=params["n_estimators"],
    )
    fit_info = {
        "days": rel["final_fit_days"],
        "rows": int(fit.sum()),
        "fraud": int(data.y[fit].sum()),
        "excluded_unlabelled": int((window(data, tuple(rel["final_fit_days"])) & ~known_by).sum()),
        "label_rule": f"label_available_at <= day {rel['labels_known_by_day']}",
    }
    model_path = save_xgb_model(
        booster,
        feature_version=FEATURE_VERSION,
        feature_names=FEATURE_NAMES,
        params=xgb_params(cmp_cfg, params) | {"n_estimators": params["n_estimators"]},
        metadata={
            "release": rel["release_id"],
            "fit": fit_info,
            "dataset": data.dataset,
            "derived_features_sha256": data.derived_sha,
            "code_revision": code,
            "selected_by": str(report_path),
            "score_semantics": "risk score",
        },
        directory=Path("artifacts/models"),
    )
    model = load_any_model(model_path)

    # Review threshold from the selection window (no labels used).
    sel = window(data, tuple(rel["policy_selection_days"]))
    scores_sel = model.score_matrix(data.X[sel])
    k = math.ceil(rel["review_capacity_target"] * scores_sel.size)
    threshold = float(np.sort(scores_sel)[::-1][k - 1])

    def describe(mask: np.ndarray) -> dict[str, Any]:
        s = model.score_matrix(data.X[mask])
        flagged = s >= threshold
        y = data.y[mask]
        m = flagged_metrics(y, flagged, data.amounts[mask], None)
        top = top_k_mask(s, data.decision_us[mask], rel["review_capacity_target"])
        return {
            "rows": int(mask.sum()),
            "fraud_eventual": int(y.sum()),
            "fixed_threshold_review_rate": float(flagged.mean()),
            "reviewed": int(flagged.sum()),
            "precision_eventual_labels": m["precision"],
            "recall_eventual_labels": m["recall"],
            "ranking_diagnostics": {
                "average_precision": average_precision(y, s),
                "top_k_precision": flagged_metrics(y, top, data.amounts[mask], None)["precision"],
            },
        }

    transfer: dict[str, Any] = {"selection_window": describe(sel), "weeks": []}
    start, end = rel["policy_selection_days"][0], rel["policy_transfer_check_days"][1]
    for d in range(start, end, 7):
        e = min(d + 7, end)
        transfer["weeks"].append({"days": [d, e], **describe(window(data, (d, e)))})
    transfer["forward_check_window"] = describe(
        window(data, tuple(rel["policy_transfer_check_days"]))
    )

    policy_path = Path("artifacts/policies") / f"{model.model_version}.json"
    policy = write_policy(
        policy_path,
        review_threshold=threshold,
        decline_threshold=None,
        derived_for_model=model.model_version,
        derivation={
            "release": rel["release_id"],
            "type": "review-only (automatic decline disabled)",
            "review_capacity_target": rel["review_capacity_target"],
            "selection_days": rel["policy_selection_days"],
            "rule": "score of the ceil(target*N)-th highest transaction in the selection window",
            "labels_used": False,
        },
    )

    baseline_model = load_any_model(Path("artifacts/models") / rel["baseline_model"])
    baseline_policy = load_policy(Path(rel["baseline_policy"]))
    comparison_lr = report["serving"]["lr"]
    core = {
        "schema": "fraudplat.release-manifest/v1",
        "release_id": rel["release_id"],
        "code_revision": code,
        "dataset": data.dataset,
        "derived_features_sha256_pre_test": data.derived_sha,
        "feature_version": FEATURE_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "model": {
            "model_version": model.model_version,
            "family": "xgboost",
            "hyperparameters": params,
            "fixed_parameters": xgb_params(cmp_cfg, params),
            "xgboost_version": xgb.__version__,
            "fit": fit_info,
            "refit_procedure": "comparison-selected configuration refitted once on final_fit_days; "
            "no further tuning",
            "files_sha256": artifact_hashes(model_path),
        },
        "policy": {
            "policy_version": policy.version,
            "file_sha256": sha256_file(policy_path),
            "review_threshold": threshold,
            "automatic_decline": False,
            "review_capacity_target": rel["review_capacity_target"],
            "selection_days": rel["policy_selection_days"],
            "development_transfer": transfer,
        },
        "comparison": {
            "report_sha256": sha256_file(report_path),
            "mlflow_parent_run_id": report["mlflow"]["parent_run_id"],
            "selection": report["selection"],
            "models": {
                "deployed_baseline_lr": {
                    "model_version": rel["baseline_model"],
                    "fit_days": [30, 100],
                    "C": 0.001,
                    "class_weight": "none",
                    "policy_version": baseline_policy.version,
                    "policy": "review 0.0378 / decline 0.0745, chosen on days 130-153",
                },
                "comparison_best_lr": {
                    "model_version": comparison_lr["model_version"],
                    "fit_days": [30, 90],
                    **report["best_per_family"]["lr"]["config"],
                },
                "comparison_selected_xgb": {
                    "model_version": report["serving"]["xgb"]["model_version"],
                    "fit_days": [30, 90],
                    **params,
                },
                "release_xgb": {
                    "model_version": model.model_version,
                    "fit_days": rel["final_fit_days"],
                    **params,
                },
            },
        },
        "baseline_for_rollback": {
            "model_version": baseline_model.model_version,
            "policy_version": baseline_policy.version,
            "files_sha256": artifact_hashes(Path("artifacts/models") / rel["baseline_model"]),
        },
        "evaluation_plan": {
            "test_days": [rel["test_start_day"], rel["test_end_day"]],
            "labels": "eventual simulated labels (is_fraud)",
            "feature_conditions": {
                "A": "arrival-time availability, immediate processing (delay 0)",
                "B": f"arrival-time availability, simulated worker delay {rel['simulated_worker_delay_ms']} ms",
            },
            "history": "all earlier transactions update features under the documented availability rules",
            "forbidden": "training, tuning, recalibration or threshold changes using test outcomes",
            "uncertainty": f"day-block bootstrap, {rel['bootstrap_reps']} replicates, seed {rel['bootstrap_seed']}",
            "ranking_diagnostics": "retrospective top-1% (ties: earlier decision time, then order)",
        },
        "acceptance_criteria": rel["acceptance"],
    }
    digest = _digest(core)
    manifest = {
        **core,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "integrity_sha256": digest,
    }
    out = Path("releases") / rel["release_id"]
    out.mkdir(parents=True, exist_ok=False)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=float) + "\n")

    settings = Settings()
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_experiment("fraud-f1-releases")
    with mlflow.start_run(run_name=f"freeze-{rel['release_id']}") as run:
        mlflow.set_tags(
            {
                "release": rel["release_id"],
                "code_revision": code,
                "model_version": model.model_version,
                "policy_version": policy.version,
                "dataset_sha256": data.dataset["content_sha256"],
            }
        )
        mlflow.log_params(
            {
                "final_fit_days": rel["final_fit_days"],
                **params,
                "review_threshold": threshold,
                "automatic_decline": False,
            }
        )
        mlflow.log_artifact(str(out / "manifest.json"), "release")
        version = publish(
            settings.mlflow_tracking_uri,
            REGISTERED_NAME,
            model_path,
            policy_path,
            run.info.run_id,
            description=f"{rel['release_id']} frozen candidate",
        )
        from mlflow.tracking import MlflowClient

        client = MlflowClient(settings.mlflow_tracking_uri)
        client.set_model_version_tag(REGISTERED_NAME, version, "release", rel["release_id"])
        client.set_model_version_tag(REGISTERED_NAME, version, "manifest_sha256", digest)
    print(
        json.dumps(
            {
                "release": rel["release_id"],
                "model_version": model.model_version,
                "policy_version": policy.version,
                "review_threshold": threshold,
                "registry_version": version,
                "manifest_sha256": digest,
                "selection_review_rate": transfer["selection_window"][
                    "fixed_threshold_review_rate"
                ],
                "forward_review_rate": transfer["forward_check_window"][
                    "fixed_threshold_review_rate"
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
