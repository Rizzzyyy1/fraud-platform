"""Reproducible comparison of rules, logistic regression and XGBoost on identical data.

Run: `python -m fraudplat.training.compare --config configs/compare-v1.toml`
(needs `make up`, `make mlflow`, sim-v2 and its pre-test feature table).

Every model family sees the same eligible rows, the same feature matrix (feature version f1),
the same temporal folds and the same metric code. Every attempted configuration is logged to
MLflow, including failures. The selection rule is read from the config and applied mechanically.
Outputs: validated artifacts for each family's representative fitted on the last fold's fit
window, a policy for the selected model derived on that fold's calibration window, and
`reports/model_comparison/report.{json,md}`.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import time
import tomllib
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mlflow
import numpy as np
import polars as pl
import xgboost as xgb
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

from fraudplat.config import Settings
from fraudplat.features.spec import DAY, FEATURE_NAMES, FEATURE_VERSION
from fraudplat.policy import write_policy
from fraudplat.simulator.dataset import _code_revision
from fraudplat.training.artifacts import load_any_model
from fraudplat.training.compare_report import render
from fraudplat.training.derived import derived_dir, load_pre_test_features
from fraudplat.training.diagnostics import (
    PlattCalibrator,
    average_precision,
    calibration,
    fixed_threshold_results,
    scenario_metrics,
    thresholds_from_calibration_window,
    top_k_mask,
)
from fraudplat.training.evaluation import flagged_metrics
from fraudplat.training.model import Preprocessing, build_model, save_model
from fraudplat.training.xgb_model import save_xgb_model

SCENARIOS = ("s1", "s2", "s3")


# --- data ---------------------------------------------------------------------------------


@dataclass
class Data:
    X: np.ndarray
    y: np.ndarray
    amounts: np.ndarray
    decision_us: np.ndarray
    label_us: np.ndarray
    day: np.ndarray
    scenarios: dict[str, np.ndarray]
    day0_us: int
    dataset: dict[str, str]
    derived_sha: str


def load_data(cfg: dict[str, Any]) -> Data:
    dataset_dir = Path(cfg["dataset_dir"])
    manifest = json.loads((dataset_dir / "manifest.json").read_text())
    raw = pl.read_parquet(dataset_dir / "transactions.parquet")
    first = int(raw["event_time"].dt.epoch("us").min())  # type: ignore[arg-type]
    day0 = first - first % DAY
    test_start_us = day0 + cfg["test_start_day"] * DAY
    root = Path(cfg["derived_root"])
    feats = load_pre_test_features(dataset_dir, root, test_start_us)
    derived_manifest = json.loads(
        (derived_dir(root, dataset_dir.name, test_start_us) / "manifest.json").read_text()
    )
    labels = raw.select(
        "transaction_id",
        "is_fraud",
        "fraud_s1",
        "fraud_s2",
        "fraud_s3",
        pl.col("amount_minor").alias("amount_label"),
        pl.col("label_available_at").dt.epoch("us").alias("label_us"),
    )
    df = feats.join(labels, on="transaction_id", how="inner", validate="1:1").sort(
        ["decision_time_us", "transaction_id"]
    )
    assert df.height == feats.height
    decision = df["decision_time_us"].to_numpy()
    if (decision >= test_start_us).any():
        raise AssertionError("test-period rows reached the comparison")
    return Data(
        X=df.select(FEATURE_NAMES).to_numpy().astype(np.float64),
        y=df["is_fraud"].to_numpy().astype(np.int64),
        amounts=df["amount_label"].to_numpy().astype(np.float64),
        decision_us=decision,
        label_us=df["label_us"].to_numpy(),
        day=(decision - day0) / DAY,
        scenarios={s: df[f"fraud_{s}"].to_numpy() for s in SCENARIOS},
        day0_us=day0,
        dataset={
            "dataset_id": manifest["dataset_id"],
            "content_sha256": manifest["tables"]["transactions"]["content_sha256"],
        },
        derived_sha=derived_manifest["content_sha256"],
    )


@dataclass(frozen=True)
class Fold:
    cutoff: int
    fit: tuple[int, int]
    cal: tuple[int, int]
    eval: tuple[int, int]

    @property
    def name(self) -> str:
        return f"cutoff{self.cutoff:03d}"


def make_folds(cfg: dict[str, Any]) -> list[Fold]:
    folds = []
    for c in cfg["fold_cutoff_days"]:
        cal_end = c - cfg["label_gap_days"]
        cal_start = cal_end - cfg["calibration_days"]
        fold = Fold(
            c, (cfg["burn_in_days"], cal_start), (cal_start, cal_end), (c, c + cfg["eval_days"])
        )
        if fold.eval[1] > cfg["test_start_day"] or fold.fit[1] <= fold.fit[0]:
            raise ValueError(f"invalid fold {fold}")
        folds.append(fold)
    return folds


def window(data: Data, days: tuple[int, int]) -> np.ndarray:
    mask: np.ndarray = (data.day >= days[0]) & (data.day < days[1])
    return mask


def labelled_at(data: Data, fold: Fold) -> np.ndarray:
    known: np.ndarray = data.label_us <= data.day0_us + fold.cutoff * DAY
    return known


# --- model families -------------------------------------------------------------------------

Scorer = Callable[[np.ndarray], np.ndarray]


def rules(name: str) -> Scorer:
    idx = FEATURE_NAMES.index("amount_minor" if name == "amount" else "amount_to_cust_mean_30d")
    return lambda X: np.nan_to_num(X[:, idx], nan=0.0)


def fit_lr(
    cfg: dict[str, Any], params: dict[str, Any], X: np.ndarray, y: np.ndarray
) -> tuple[Scorer, Any]:
    pre = Preprocessing.fit(
        X, FEATURE_NAMES, tuple(cfg["lr"]["nullable"]), tuple(cfg["lr"]["log1p"])
    )
    clf = LogisticRegression(
        C=params["C"],
        class_weight=None if params["class_weight"] == "none" else "balanced",
        max_iter=5000,
        random_state=cfg["seed"],
    ).fit(pre.transform(X), y)
    return (lambda Z: clf.predict_proba(pre.transform(Z))[:, 1]), (pre, clf)


def xgb_params(cfg: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    x = cfg["xgb"]
    return {
        "objective": "binary:logistic",
        "eval_metric": "aucpr",
        "tree_method": "hist",
        "max_depth": params["max_depth"],
        "eta": x["learning_rate"],
        "subsample": x["subsample"],
        "colsample_bytree": x["colsample_bytree"],
        "min_child_weight": x["min_child_weight"],
        "scale_pos_weight": params["scale_pos_weight"],
        "nthread": x["nthread"],
        "seed": cfg["seed"],
    }


def fit_xgb(
    cfg: dict[str, Any], params: dict[str, Any], X: np.ndarray, y: np.ndarray
) -> tuple[Scorer, Any]:
    dtrain = xgb.DMatrix(X, label=y, missing=np.nan, feature_names=list(FEATURE_NAMES))
    booster = xgb.train(xgb_params(cfg, params), dtrain, num_boost_round=params["n_estimators"])
    return (lambda Z: booster.inplace_predict(Z, missing=np.nan).astype(np.float64)), booster


def configurations(cfg: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = [
        ("rules", {"rule": "amount"}),
        ("rules", {"rule": "amount_to_cust_mean_30d"}),
    ]
    for weight, c in itertools.product(cfg["lr"]["class_weights"], cfg["lr"]["c_grid"]):
        out.append(("lr", {"C": c, "class_weight": weight}))
    x = cfg["xgb"]
    for depth, n, spw in itertools.product(
        x["max_depth"], x["n_estimators"], x["scale_pos_weight"]
    ):
        out.append(("xgb", {"max_depth": depth, "n_estimators": n, "scale_pos_weight": spw}))
    return out


def label(family: str, params: dict[str, Any]) -> str:
    return family + ":" + ",".join(f"{k}={v}" for k, v in sorted(params.items()))


# --- evaluation ------------------------------------------------------------------------------


def evaluate(
    data: Data, fold: Fold, scores_eval: np.ndarray, scores_cal: np.ndarray, cfg: dict[str, Any]
) -> dict[str, Any]:
    ev = window(data, fold.eval)
    cal = window(data, fold.cal) & labelled_at(data, fold)
    y, amounts, t = data.y[ev], data.amounts[ev], data.decision_us[ev]
    scen = {s: m[ev] for s, m in data.scenarios.items()}
    budget = cfg["review_budget"]
    top = top_k_mask(scores_eval, t, budget)
    review, decline = thresholds_from_calibration_window(
        data.y[cal], scores_cal, budget, cfg["decline_min_precision"], cfg["decline_min_count"]
    )
    weeks = []
    days = data.day[ev]
    for start in range(fold.eval[0], fold.eval[1], 7):
        end = min(start + 7, fold.eval[1])
        if fold.eval[1] - end < 7:  # merge a short tail into the last window
            end = fold.eval[1]
        w = (days >= start) & (days < end)
        weeks.append(
            {
                "days": [start, end],
                "rows": int(w.sum()),
                "fraud": int(y[w].sum()),
                "average_precision": average_precision(y[w], scores_eval[w]),
                **fixed_threshold_results(
                    y[w],
                    scores_eval[w],
                    amounts[w],
                    review,
                    decline,
                    {s: m[w] for s, m in scen.items()},
                ),
            }
        )
        if end == fold.eval[1]:
            break
    platt = PlattCalibrator.fit(data.y[cal], scores_cal)
    return {
        "rows": int(ev.sum()),
        "fraud": int(y.sum()),
        "average_precision": average_precision(y, scores_eval),
        "roc_auc": float(roc_auc_score(y, scores_eval)),
        "retrospective_top_k": {
            "budget": budget,
            **flagged_metrics(y, top, amounts, None),
            "scenarios": scenario_metrics(y, scores_eval, top, scen),
        },
        "fixed_thresholds": {
            "review": review,
            "decline": decline,
            "derived_on_days": list(fold.cal),
            **fixed_threshold_results(y, scores_eval, amounts, review, decline, scen),
            "scenarios": scenario_metrics(y, scores_eval, scores_eval >= review, scen),
            "by_window": weeks,
        },
        "calibration_raw": calibration(y, scores_eval),
        "calibration_platt_from_calibration_window": {
            "a": platt.a,
            "b": platt.b,
            **calibration(y, platt.apply(scores_eval)),
        },
    }


# --- run --------------------------------------------------------------------------------------


def run(cfg: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    data = load_data(cfg)
    folds = make_folds(cfg)
    code = _code_revision()
    mlflow.set_experiment(cfg["mlflow"]["experiment"])
    common_tags = {
        "dataset_id": data.dataset["dataset_id"],
        "dataset_sha256": data.dataset["content_sha256"],
        "derived_features_sha256": data.derived_sha,
        "feature_version": FEATURE_VERSION,
        "code_revision": code,
    }
    fold_info = []
    for f in folds:
        fit, cal, ev = window(data, f.fit), window(data, f.cal), window(data, f.eval)
        known = labelled_at(data, f)
        fold_info.append(
            {
                "fold": f.name,
                "fit_days": list(f.fit),
                "calibration_days": list(f.cal),
                "eval_days": list(f.eval),
                "fit_rows": int((fit & known).sum()),
                "fit_fraud": int(data.y[fit & known].sum()),
                "fit_excluded_unlabelled": int((fit & ~known).sum()),
                "calibration_rows": int((cal & known).sum()),
                "eval_rows": int(ev.sum()),
                "eval_fraud": int(data.y[ev].sum()),
            }
        )

    attempts: list[dict[str, Any]] = []
    kept: dict[str, dict[str, tuple[np.ndarray, np.ndarray, Any]]] = {}
    with mlflow.start_run(run_name=f"comparison-{datetime.now(UTC):%Y%m%dT%H%M%S}") as parent:
        mlflow.set_tags({**common_tags, "kind": "comparison"})
        mlflow.log_params(
            {
                "folds": json.dumps(fold_info),
                "selection_rule": json.dumps(
                    {
                        k: cfg[k]
                        for k in (
                            "primary_metric",
                            "xgboost_min_ap_gain",
                            "xgboost_min_fold_wins",
                            "max_single_row_p99_us",
                            "max_artifact_bytes",
                        )
                    }
                ),
                "label_eligibility": "label_available_at <= fold cutoff (fit and calibration rows)",
            }
        )
        for family, params in configurations(cfg):
            name = label(family, params)
            record: dict[str, Any] = {"family": family, "params": params, "folds": {}}
            with mlflow.start_run(run_name=name, nested=True):
                mlflow.set_tags({**common_tags, "family": family})
                mlflow.log_params(params)
                try:
                    per_fold = {}
                    for f in folds:
                        fit = window(data, f.fit) & labelled_at(data, f)
                        ev = window(data, f.eval)
                        cal = window(data, f.cal) & labelled_at(data, f)
                        t0 = time.perf_counter()
                        if family == "rules":
                            scorer, obj = rules(params["rule"]), None
                        elif family == "lr":
                            scorer, obj = fit_lr(cfg, params, data.X[fit], data.y[fit])
                        else:
                            scorer, obj = fit_xgb(cfg, params, data.X[fit], data.y[fit])
                        fit_s = time.perf_counter() - t0
                        s_eval, s_cal = scorer(data.X[ev]), scorer(data.X[cal])
                        top = top_k_mask(s_eval, data.decision_us[ev], cfg["review_budget"])
                        m = flagged_metrics(data.y[ev], top, data.amounts[ev], None)
                        ap = average_precision(data.y[ev], s_eval)
                        per_fold[f.name] = {
                            "average_precision": ap,
                            "fit_seconds": round(fit_s, 2),
                            "top_k_precision": m["precision"],
                            "top_k_recall": m["recall"],
                        }
                        mlflow.log_metrics(
                            {
                                f"{f.name}_average_precision": ap or 0.0,
                                f"{f.name}_fit_seconds": fit_s,
                                f"{f.name}_top_k_precision": m["precision"],
                                f"{f.name}_top_k_recall": m["recall"],
                            }
                        )
                        kept.setdefault(name, {})[f.name] = (s_eval, s_cal, obj)
                    mean_ap = float(np.mean([v["average_precision"] for v in per_fold.values()]))
                    record.update(folds=per_fold, mean_average_precision=mean_ap, status="ok")
                    mlflow.log_metric("mean_eval_average_precision", mean_ap)
                except Exception as exc:
                    record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                    mlflow.set_tag("failure", traceback.format_exc()[-2000:])
                    mlflow.end_run(status="FAILED")
                    kept.pop(name, None)
            attempts.append(record)
            print(
                json.dumps(
                    {
                        k: record.get(k)
                        for k in ("family", "params", "status", "mean_average_precision")
                    }
                ),
                flush=True,
            )

        best: dict[str, dict[str, Any]] = {}
        for rec in attempts:
            if rec["status"] != "ok":
                continue
            fam = rec["family"]
            if (
                fam not in best
                or rec["mean_average_precision"] > best[fam]["mean_average_precision"]
            ):
                best[fam] = rec
        detailed = {}
        for fam in ("rules", "lr", "xgb"):
            if fam not in best:
                continue
            name = label(fam, best[fam]["params"])
            detailed[fam] = {
                "config": best[fam]["params"],
                "mean_average_precision": best[fam]["mean_average_precision"],
                "folds": {
                    f.name: evaluate(data, f, kept[name][f.name][0], kept[name][f.name][1], cfg)
                    for f in folds
                },
            }

        # Serving checks on each learned family's representative, fitted on the last fold.
        last = folds[-1]
        artifacts = Path(cfg["artifacts_root"])
        serving: dict[str, dict[str, Any]] = {}
        ev_last = window(data, last.eval)
        row = {
            n: (None if np.isnan(v) else float(v))
            for n, v in zip(FEATURE_NAMES, data.X[ev_last][0], strict=True)
        }
        base_meta = {
            "dataset": data.dataset,
            "derived_features_sha256": data.derived_sha,
            "fit_days": list(last.fit),
            "calibration_days": list(last.cal),
            "label_eligibility": f"label_available_at <= day {last.cutoff}",
            "code_revision": code,
            "protocol": "configs/compare-v1.toml",
            "score_semantics": "risk score; see calibration diagnostics",
        }
        for fam in ("lr", "xgb"):
            if fam not in best:
                continue
            obj = kept[label(fam, best[fam]["params"])][last.name][2]
            meta = {**base_meta, "config": best[fam]["params"]}
            if fam == "lr":
                pre, clf = obj
                model = build_model(
                    feature_version=FEATURE_VERSION,
                    preprocessing=pre,
                    coefficients=tuple(float(v) for v in clf.coef_[0]),
                    intercept=float(clf.intercept_[0]),
                    metadata=meta,
                )
                path = save_model(model, artifacts / "models")
                in_memory = kept[label(fam, best[fam]["params"])][last.name][0]
            else:
                path = save_xgb_model(
                    obj,
                    feature_version=FEATURE_VERSION,
                    feature_names=FEATURE_NAMES,
                    params=xgb_params(cfg, best[fam]["params"])
                    | {"n_estimators": best[fam]["params"]["n_estimators"]},
                    metadata=meta,
                    directory=artifacts / "models",
                )
                in_memory = kept[label(fam, best[fam]["params"])][last.name][0]
            loaded = load_any_model(path)
            parity = float(np.max(np.abs(loaded.score_matrix(data.X[ev_last]) - in_memory)))
            timings = []
            for _ in range(2000):
                t0 = time.perf_counter_ns()
                loaded.score(row)
                timings.append(time.perf_counter_ns() - t0)
            size = (
                sum(p.stat().st_size for p in path.parent.iterdir())
                if path.name == "manifest.json"
                else path.stat().st_size
            )
            serving[fam] = {
                "model_version": loaded.model_version,
                "path": str(path),
                "artifact_bytes": size,
                "artifact_vs_training_max_abs_diff": parity,
                "single_row_us": {
                    f"p{q}": float(np.percentile(timings, q) / 1000) for q in (50, 95, 99)
                },
            }

        # Selection rule.
        lr_folds = detailed["lr"]["folds"]
        xgb_ok = "xgb" in detailed
        decision: dict[str, Any] = {
            "rule": {
                k: cfg[k]
                for k in (
                    "primary_metric",
                    "xgboost_min_ap_gain",
                    "xgboost_min_fold_wins",
                    "max_single_row_p99_us",
                    "max_artifact_bytes",
                )
            }
        }
        if xgb_ok:
            gain = (
                detailed["xgb"]["mean_average_precision"] - detailed["lr"]["mean_average_precision"]
            )
            wins = sum(
                detailed["xgb"]["folds"][f]["average_precision"] > lr_folds[f]["average_precision"]
                for f in lr_folds
            )
            s = serving["xgb"]
            constraints = (
                s["single_row_us"]["p99"] <= cfg["max_single_row_p99_us"]
                and s["artifact_bytes"] <= cfg["max_artifact_bytes"]
            )
            choose_xgb = (
                gain >= cfg["xgboost_min_ap_gain"]
                and wins >= cfg["xgboost_min_fold_wins"]
                and constraints
            )
            decision.update(
                mean_ap_gain_xgb_minus_lr=gain,
                xgb_fold_wins=wins,
                xgb_serving_constraints_met=constraints,
            )
        else:
            choose_xgb = False
        selected = "xgb" if choose_xgb else "lr"
        decision["selected_family"] = selected
        decision["selected_model_version"] = serving[selected]["model_version"]

        # Policy for the selected model: thresholds from the last fold's calibration window.
        cal_last = window(data, last.cal) & labelled_at(data, last)
        sel_scores_cal = kept[label(selected, best[selected]["params"])][last.name][1]
        review, decline = thresholds_from_calibration_window(
            data.y[cal_last],
            sel_scores_cal,
            cfg["review_budget"],
            cfg["decline_min_precision"],
            cfg["decline_min_count"],
        )
        policy = write_policy(
            artifacts / "policies" / f"{serving[selected]['model_version']}.json",
            review_threshold=review,
            decline_threshold=decline,
            derived_for_model=serving[selected]["model_version"],
            derivation={
                "data": (
                    f"calibration window days {list(last.cal)}, labels known at day {last.cutoff}"
                ),
                "review_budget": cfg["review_budget"],
                "decline_min_precision": cfg["decline_min_precision"],
                "decline_min_count": cfg["decline_min_count"],
            },
        )
        decision["policy_version"] = policy.version

        report = {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "code_revision": code,
            "dataset": data.dataset,
            "derived_features_sha256": data.derived_sha,
            "feature_version": FEATURE_VERSION,
            "feature_names": list(FEATURE_NAMES),
            "folds": fold_info,
            "attempts": attempts,
            "best_per_family": detailed,
            "serving": serving,
            "selection": decision,
            "mlflow": {
                "experiment": cfg["mlflow"]["experiment"],
                "parent_run_id": parent.info.run_id,
                "tracking_uri": mlflow.get_tracking_uri(),
            },
            "elapsed_s": round(time.perf_counter() - started, 1),
        }
        out = Path(cfg["report_dir"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(report, indent=2, default=float) + "\n")
        (out / "report.md").write_text(render(json.loads((out / "report.json").read_text())))
        mlflow.log_artifact(str(out / "report.json"), "evaluation")
        mlflow.log_artifact(str(out / "report.md"), "evaluation")
        mlflow.log_metrics({"selected_is_xgb": float(choose_xgb)})
        mlflow.set_tag("selected_model_version", decision["selected_model_version"])
        for s in serving.values():
            mlflow.log_artifacts(str(Path(s["path"]).parent), f"models/{s['model_version']}")
        mlflow.log_artifact(
            str(artifacts / "policies" / f"{serving[selected]['model_version']}.json"), "policies"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    # Always log to the configured tracking server; never fall back to MLflow's local default.
    tracking_uri = Settings().mlflow_tracking_uri
    mlflow.set_tracking_uri(tracking_uri)
    try:
        mlflow.search_experiments(max_results=1)
    except Exception as exc:
        raise SystemExit(f"MLflow tracking server {tracking_uri} is not reachable: {exc}") from exc
    with args.config.open("rb") as handle:
        report = run(tomllib.load(handle))
    print(json.dumps(report["selection"], indent=2, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
