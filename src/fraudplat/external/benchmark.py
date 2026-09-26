"""Offline benchmark of the project's modelling method on the real ULB credit-card data.

Protocol (fixed and committed before the held-out window was evaluated):

* Chronological split on `Time` (seconds since the first transaction, 48 hours in total):
  train [0 h, 24 h), validation [24 h, 32 h), held-out test [32 h, 48 h).
* Families, as in the platform: logistic regression (standardised features, class weight none)
  and XGBoost. Small grids; the configuration with the best validation average precision (AP) is
  selected. Models are fitted on the training window only; the test window is not used for any
  choice.
* Review budget: 0.5% of transactions (about three times the fraud rate). The score threshold is
  the top-0.5% score on the validation window, applied unchanged to the test window.
* Test metrics, computed once: AP (primary), ROC AUC, precision / recall / realised review rate at
  the fixed threshold, raw expected calibration error (10 equal-count bins). 95% intervals by
  bootstrap over hour blocks of the test window (16 blocks; transactions within an hour are not
  independent).
* Contrast: the same selected configurations fitted on a stratified *random* 70/30 split of all
  48 hours, to show how a random split (common in public notebooks) compares with a time split.
  Reported as measured, whichever way it goes.

Scope: features are the data owner's PCA components plus `Amount`; there are no customer or
merchant identifiers, so the platform's point-in-time entity features cannot be computed and
nothing here feeds the streaming system. Results describe this dataset only.

Run once: python -m fraudplat.external.benchmark   (refuses to overwrite an existing report)
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from xgboost import XGBClassifier

from fraudplat.external.ulb import FEATURES, load

HOUR = 3600
TRAIN, VALID, TEST = (0, 24 * HOUR), (24 * HOUR, 32 * HOUR), (32 * HOUR, 48 * HOUR + 1)
BUDGET = 0.005
BOOTSTRAP = 2000
SEED = 0
OUT = Path("reports/external/ulb/benchmark.json")
GRIDS: dict[str, list[dict[str, Any]]] = {
    "lr": [{"C": c} for c in (0.01, 0.1, 1.0)],
    "xgb": [{"max_depth": d, "n_estimators": 300} for d in (3, 5)],
}


def build(family: str, config: dict[str, Any]) -> Any:
    if family == "lr":
        return make_pipeline(
            StandardScaler(), LogisticRegression(C=config["C"], max_iter=2000, random_state=SEED)
        )
    return XGBClassifier(
        max_depth=config["max_depth"],
        n_estimators=config["n_estimators"],
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        eval_metric="aucpr",
        random_state=SEED,
        n_jobs=4,
    )


def ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    order = np.argsort(p, kind="stable")  # deterministic bins when scores tie
    total = 0.0
    for chunk in np.array_split(order, bins):
        total += len(chunk) / len(p) * abs(float(y[chunk].mean()) - float(p[chunk].mean()))
    return total


def at_threshold(y: np.ndarray, p: np.ndarray, threshold: float) -> dict[str, float]:
    flagged = p >= threshold
    tp = int((flagged & (y == 1)).sum())
    return {
        "review_rate": float(flagged.mean()),
        "precision": tp / max(int(flagged.sum()), 1),
        "recall": tp / max(int(y.sum()), 1),
        "flagged": int(flagged.sum()),
        "frauds_caught": tp,
    }


def window(frame: pl.DataFrame, bounds: tuple[int, int]) -> pl.DataFrame:
    return frame.filter((pl.col("Time") >= bounds[0]) & (pl.col("Time") < bounds[1]))


def xy(frame: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    return frame.select(FEATURES).to_numpy(), frame["Class"].to_numpy().astype(int)


def hour_block_ci(
    y: np.ndarray, p: np.ndarray, hours: np.ndarray, threshold: float
) -> dict[str, list[float]]:
    rng = np.random.default_rng(SEED)
    blocks = [np.flatnonzero(hours == h) for h in np.unique(hours)]
    stats: dict[str, list[float]] = {"average_precision": [], "precision": [], "recall": []}
    for _ in range(BOOTSTRAP):
        idx = np.concatenate([blocks[i] for i in rng.integers(0, len(blocks), len(blocks))])
        if y[idx].sum() == 0:
            continue
        stats["average_precision"].append(float(average_precision_score(y[idx], p[idx])))
        m = at_threshold(y[idx], p[idx], threshold)
        stats["precision"].append(m["precision"])
        stats["recall"].append(m["recall"])
    return {
        k: [float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5))] for k, v in stats.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    if OUT.exists():
        raise SystemExit(f"{OUT} exists: the held-out window is evaluated once")
    data = load()
    train, valid, test = (window(data, b) for b in (TRAIN, VALID, TEST))
    (xt, yt), (xv, yv), (xs, ys) = xy(train), xy(valid), xy(test)
    report: dict[str, Any] = {
        "dataset": "ULB credit-card fraud (OpenML 1597 v1), MD5 178bcf9bb1f31a3dfe12d0e577884add",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "protocol": {
            "train_hours": [0, 24],
            "validation_hours": [24, 32],
            "test_hours": [32, 48],
            "review_budget": BUDGET,
            "bootstrap": f"{BOOTSTRAP} resamples of test hour blocks",
            "features": "V1-V28 (owner PCA) + Amount",
        },
        "windows": {
            name: {"rows": f.height, "frauds": int(f["Class"].sum())}
            for name, f in (("train", train), ("validation", valid), ("test", test))
        },
        "families": {},
    }
    for family, grid in GRIDS.items():
        trials = []
        for config in grid:
            model = build(family, config).fit(xt, yt)
            trials.append(
                (float(average_precision_score(yv, model.predict_proba(xv)[:, 1])), config, model)
            )
        val_ap, config, model = max(trials, key=lambda t: t[0])
        pv = model.predict_proba(xv)[:, 1]
        threshold = float(np.sort(pv)[::-1][math.ceil(BUDGET * len(pv)) - 1])
        ps = model.predict_proba(xs)[:, 1]
        hours = (test["Time"].to_numpy() // HOUR).astype(int)
        # Random-split contrast with the same configuration.
        xa, ya = xy(data)
        xr_tr, xr_te, yr_tr, yr_te = train_test_split(
            xa, ya, test_size=0.3, stratify=ya, random_state=SEED
        )
        random_ap = float(
            average_precision_score(
                yr_te, build(family, config).fit(xr_tr, yr_tr).predict_proba(xr_te)[:, 1]
            )
        )
        report["families"][family] = {
            "grid": [{"config": c, "validation_ap": round(a, 4)} for a, c, _ in trials],
            "selected": config,
            "validation_ap": val_ap,
            "threshold_from_validation": threshold,
            "test": {
                "average_precision": float(average_precision_score(ys, ps)),
                "roc_auc": float(roc_auc_score(ys, ps)),
                "at_fixed_threshold": at_threshold(ys, ps, threshold),
                "ece_raw": ece(ys, ps),
                "mean_score": float(ps.mean()),
                "fraud_rate": float(ys.mean()),
                "ci95": hour_block_ci(ys, ps, hours, threshold),
            },
            "random_split_contrast_ap": random_ap,
        }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({f: v["test"]["average_precision"] for f, v in report["families"].items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
