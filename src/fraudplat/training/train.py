"""Train and select a logistic regression; derive a policy on validation; write artifacts.

Run: `python -m fraudplat.training.train --config configs/train-lr-v1.toml`

Steps: build or load the pre-test feature table → chronological masks with label eligibility at
the training cutoff → fit preprocessing on training rows only → fit one model per C → choose C by
validation average precision → rules baselines on the same validation rows → policy thresholds
from validation scores → write model, policy and report. The final test period is not loaded.
Validation is used both to choose C and thresholds, so validation metrics are optimistic.
"""

from __future__ import annotations

import argparse
import json
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression

from fraudplat.features.spec import FEATURE_NAMES, FEATURE_VERSION
from fraudplat.policy import write_policy
from fraudplat.simulator.dataset import _code_revision
from fraudplat.training.derived import build_pre_test_features, derived_dir, load_pre_test_features
from fraudplat.training.evaluation import (
    budget_metrics,
    choose_thresholds,
    flagged_metrics,
    ranking_metrics,
)
from fraudplat.training.model import Preprocessing, build_model, save_model
from fraudplat.training.splits import Timeline, split_masks


def _day0_us(raw: pl.DataFrame) -> int:
    first = int(raw["event_time"].dt.epoch("us").min())  # type: ignore[arg-type]
    return first - first % (86_400 * 1_000_000)


def load_examples(config: dict[str, Any]) -> tuple[pl.DataFrame, Timeline, dict[str, Any]]:
    dataset_dir = Path(config["dataset_dir"])
    manifest = json.loads((dataset_dir / "manifest.json").read_text())
    raw = pl.read_parquet(dataset_dir / "transactions.parquet")
    timeline = Timeline(
        day0_us=_day0_us(raw),
        burn_in_days=config["burn_in_days"],
        train_end_day=config["train_end_day"],
        training_cutoff_day=config["training_cutoff_day"],
        test_start_day=config["test_start_day"],
    )
    root = Path(config["derived_root"])
    if not derived_dir(root, dataset_dir.name, timeline.test_start_us).exists():
        build_pre_test_features(dataset_dir, root, timeline.test_start_us)
    features = load_pre_test_features(dataset_dir, root, timeline.test_start_us)
    labels = raw.select(
        "transaction_id",
        "amount_minor",
        "is_fraud",
        "fraud_s1",
        "fraud_s2",
        "fraud_s3",
        pl.col("label_available_at").dt.epoch("us").alias("label_available_at_us"),
    ).rename({"amount_minor": "amount_minor_label"})
    examples = features.join(labels, on="transaction_id", how="inner", validate="1:1")
    assert examples.height == features.height
    dataset = {
        "dataset_id": manifest["dataset_id"],
        "content_sha256": manifest["tables"]["transactions"]["content_sha256"],
    }
    return examples, timeline, dataset


def _summaries(
    y: np.ndarray,
    scores: np.ndarray,
    amounts: np.ndarray,
    scen: dict[str, np.ndarray],
    budget: float,
) -> dict[str, Any]:
    return ranking_metrics(y, scores) | {
        "at_budget": budget_metrics(y, scores, amounts, budget, scen)
    }


def train(config: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    examples, timeline, dataset = load_examples(config)
    masks = split_masks(
        timeline,
        examples["decision_time_us"].to_numpy(),
        examples["label_available_at_us"].to_numpy(),
    )
    X = examples.select(FEATURE_NAMES).to_numpy().astype(np.float64)
    y = examples["is_fraud"].to_numpy().astype(np.int64)
    amounts = examples["amount_minor_label"].to_numpy().astype(np.float64)
    X_train, y_train = X[masks.train], y[masks.train]
    X_val, y_val, amt_val = X[masks.validation], y[masks.validation], amounts[masks.validation]
    scen_val = {s: examples[f"fraud_{s}"].to_numpy()[masks.validation] for s in ("s1", "s2", "s3")}

    pre = Preprocessing.fit(
        X_train, FEATURE_NAMES, tuple(config["nullable"]), tuple(config["log1p"])
    )
    Z_train, Z_val = pre.transform(X_train), pre.transform(X_val)

    candidates = []
    for weight in config["class_weights"]:
        for c in config["c_grid"]:
            clf = LogisticRegression(
                C=c,
                class_weight=None if weight == "none" else weight,
                max_iter=5000,
                random_state=config["seed"],
            )
            clf.fit(Z_train, y_train)
            val_scores = clf.predict_proba(Z_val)[:, 1]
            ap = ranking_metrics(y_val, val_scores)["average_precision"]
            candidates.append((ap, c, weight, clf))
    _best_ap, best_c, best_weight, best = max(candidates, key=lambda item: item[0])
    at_edge = best_c in (min(config["c_grid"]), max(config["c_grid"]))

    budget = config["review_budget"]
    metadata = {
        "dataset": dataset,
        "derived_features": {
            "availability": "arrival, processing delay 0 (immediate-processing assumption)",
            "test_start_us": timeline.test_start_us,
        },
        "timeline_days": {
            "burn_in": timeline.burn_in_days,
            "train_end": timeline.train_end_day,
            "training_cutoff": timeline.training_cutoff_day,
            "test_start": timeline.test_start_day,
        },
        "training": {
            "rows": int(masks.train.sum()),
            "fraud_rows": int(y_train.sum()),
            "candidates_in_window": masks.train_candidates,
            "excluded_unlabelled_at_cutoff": masks.excluded_unlabelled_at_cutoff,
            "estimator": "sklearn.linear_model.LogisticRegression(lbfgs)",
            "class_weights": config["class_weights"],
            "c_grid": config["c_grid"],
            "selected_c": best_c,
            "selected_class_weight": best_weight,
            "selected_c_at_grid_edge": at_edge,
            "selection_metric": "validation average_precision",
            "seed": config["seed"],
        },
        "score_semantics": (
            "logistic output used as a ranking score; calibration has not been evaluated"
        ),
        "code_revision": _code_revision(),
    }
    model = build_model(
        feature_version=FEATURE_VERSION,
        preprocessing=pre,
        coefficients=tuple(float(v) for v in best.coef_[0]),
        intercept=float(best.intercept_[0]),
        metadata=metadata,
    )
    val_scores = model.score_matrix(X_val)
    sk_scores = best.predict_proba(Z_val)[:, 1]
    parity = float(np.max(np.abs(val_scores - sk_scores)))
    if parity > 1e-12:
        raise AssertionError(f"artifact inference differs from scikit-learn by {parity}")

    thresholds = choose_thresholds(
        y_val, val_scores, budget, config["decline_min_precision"], config["decline_min_count"]
    )
    decline = thresholds.decline
    declined = val_scores >= decline if decline is not None else np.zeros_like(y_val, dtype=bool)
    reviewed = (val_scores >= thresholds.review) & ~declined
    policy_rates = {
        "approve_rate": float((~reviewed & ~declined).mean()),
        "review_rate": float(reviewed.mean()),
        "decline_rate": float(declined.mean()),
        "review_or_decline": flagged_metrics(y_val, reviewed | declined, amt_val, scen_val),
        "decline_only": flagged_metrics(y_val, declined, amt_val, scen_val) if decline else None,
    }

    artifacts = Path(config["artifacts_root"])
    model_path = save_model(model, artifacts / "models")
    policy = write_policy(
        artifacts / "policies" / f"{model.model_version}.json",
        review_threshold=thresholds.review,
        decline_threshold=decline,
        derived_for_model=model.model_version,
        derivation={
            "data": "validation period only",
            "review_budget": budget,
            "decline_min_precision": config["decline_min_precision"],
            "decline_min_count": config["decline_min_count"],
            "validation_rates": {
                k: policy_rates[k] for k in ("approve_rate", "review_rate", "decline_rate")
            },
        },
    )

    # Single-row inference latency of the artifact (in-process, no I/O).
    row = dict(
        zip(FEATURE_NAMES, [None if np.isnan(v) else float(v) for v in X_val[0]], strict=True)
    )
    timings = []
    for _ in range(2000):
        t0 = time.perf_counter_ns()
        model.score(row)
        timings.append(time.perf_counter_ns() - t0)
    latency = {f"p{q}": float(np.percentile(timings, q) / 1000) for q in (50, 95, 99)}

    rules = {
        "rule_amount": np.nan_to_num(X_val[:, FEATURE_NAMES.index("amount_minor")]),
        "rule_amount_to_customer_mean": np.nan_to_num(
            X_val[:, FEATURE_NAMES.index("amount_to_cust_mean_30d")], nan=0.0
        ),
    }
    report = {
        "model_version": model.model_version,
        "policy_version": policy.version,
        "model_path": str(model_path),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "code_revision": metadata["code_revision"],
        "dataset": dataset,
        "timeline_days": metadata["timeline_days"],
        "rows": {
            "train": int(masks.train.sum()),
            "train_fraud": int(y_train.sum()),
            "validation": int(masks.validation.sum()),
            "validation_fraud": int(y_val.sum()),
            "excluded_unlabelled_at_cutoff": masks.excluded_unlabelled_at_cutoff,
        },
        "search": [{"c": c, "class_weight": w, "validation_ap": ap} for ap, c, w, _ in candidates],
        "selected": {"c": best_c, "class_weight": best_weight, "at_grid_edge": at_edge},
        "validation": {
            "logistic_regression": _summaries(y_val, val_scores, amt_val, scen_val, budget),
            **{name: _summaries(y_val, s, amt_val, scen_val, budget) for name, s in rules.items()},
        },
        "policy": {
            "review_threshold": thresholds.review,
            "decline_threshold": decline,
            **policy_rates,
        },
        "artifact_vs_sklearn_max_abs_diff": parity,
        "single_row_inference_us": latency,
        "elapsed_s": time.perf_counter() - started,
        "notes": [
            "Validation chose C and the thresholds, so validation metrics are optimistic.",
            "The final test period was not loaded.",
            "Transaction-level metrics only; eventual (simulated) labels used for validation.",
        ],
    }
    out = Path(config["reports_root"]) / model.model_version
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    with args.config.open("rb") as handle:
        report = train(tomllib.load(handle))
    print(json.dumps({k: report[k] for k in ("model_version", "policy_version", "rows")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
