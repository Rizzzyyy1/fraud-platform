"""Import the completed ULB benchmark into MLflow as an *imported historical run*.

    python -m fraudplat.external.mlflow_import [--tracking-uri URI]

The benchmark was run without MLflow tracking (`fraudplat.external.benchmark`). This records its
committed results after the fact, without recomputing anything: a parent run tagged
`imported=true` with the original evaluation time as its start time, the protocol and results
revisions, the dataset checksum, the feature list and the protocol; one child run per model family
with its selected configuration, validation threshold and metrics. The report, the JSON and the
protocol source are attached as artifacts. The fitted models and per-transaction predictions were
not saved by the benchmark, so none are logged; runs are tagged `model_saved=false` and
`deployed=false`. Running it again finds the existing import and changes nothing.

`--reproduction DIR` instead logs a reproducibility rerun (`fraudplat.external.reproduce`) as a
separate run in the same experiment, with its actual execution times, `record_kind=reproduction`
and the imported run's id as `reproduces_run_id`. The imported run is not modified. The rerun's
models, schema, configuration, manifest and results are attached as artifacts; its per-transaction
predictions are not (their path is recorded; they stay local). Nothing is registered or promoted.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

REPORT = Path("reports/external/ulb/benchmark.json")
PROTOCOL = Path("src/fraudplat/external/benchmark.py")
EXPERIMENT = "external-ulb-benchmark"
IMPORT_ID = "ulb-benchmark-v1"
NOTE = (
    "Imported historical run: the ULB offline benchmark was evaluated once without MLflow "
    "tracking; this run records its committed results afterwards. Nothing was recomputed. Models "
    "and predictions were not saved. These models do not power the live scoring service."
)


def revision(path: Path, first: bool = False) -> str:
    """Commit that added (first=True) or last changed `path`, or 'unknown' outside Git."""
    args = ["git", "log", "--format=%H", "--", str(path)]
    try:
        out = subprocess.run(args, capture_output=True, text=True, check=True).stdout.split()  # noqa: S603
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    if not out:
        return "unknown"
    return out[-1] if first else out[0]


def import_run(
    tracking_uri: str, report_path: Path = REPORT, artifact_location: str | None = None
) -> tuple[str, bool]:
    """Returns (parent run id, created). Idempotent on the IMPORT_ID tag."""
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking_uri=tracking_uri)
    exp = client.get_experiment_by_name(EXPERIMENT)
    exp_id = (
        exp.experiment_id
        if exp
        else client.create_experiment(EXPERIMENT, artifact_location=artifact_location)
    )
    existing = client.search_runs(
        [exp_id], filter_string=f"tags.import_id = '{IMPORT_ID}' and tags.import_role = 'parent'"
    )
    if existing:
        return existing[0].info.run_id, False

    r: dict[str, Any] = json.loads(report_path.read_text())
    started = int(datetime.fromisoformat(r["created_at"]).timestamp() * 1000)
    common = {
        "imported": "true",
        "import_id": IMPORT_ID,
        "source_report": str(report_path),
        "protocol_revision": revision(PROTOCOL, first=True),
        "results_revision": revision(report_path),
        "dataset": r["dataset"],
        "dataset_md5": r["dataset"].rsplit("MD5 ", 1)[-1],
        "model_saved": "false",
        "deployed": "false",
        "live_scoring": "not used by the live scoring service",
    }
    parent = client.create_run(
        exp_id,
        start_time=started,
        tags={
            **common,
            "import_role": "parent",
            "mlflow.runName": f"{IMPORT_ID} (imported)",
            "mlflow.note.content": NOTE,
        },
    )
    pid = parent.info.run_id
    p = r["protocol"]
    for key, value in {
        "train_hours": p["train_hours"],
        "validation_hours": p["validation_hours"],
        "test_hours": p["test_hours"],
        "review_budget": p["review_budget"],
        "bootstrap": p["bootstrap"],
        "features": p["features"],
    }.items():
        client.log_param(pid, key, str(value))
    for name, w in r["windows"].items():
        client.log_metric(pid, f"{name}_rows", w["rows"], timestamp=started)
        client.log_metric(pid, f"{name}_frauds", w["frauds"], timestamp=started)
    for family, v in r["families"].items():
        child = client.create_run(
            exp_id,
            start_time=started,
            tags={
                **common,
                "import_role": "child",
                "mlflow.parentRunId": pid,
                "mlflow.runName": f"{IMPORT_ID}/{family} (imported)",
                "family": family,
            },
        )
        cid = child.info.run_id
        for key, value in v["selected"].items():
            client.log_param(cid, key, str(value))
        client.log_param(cid, "threshold_from_validation", str(v["threshold_from_validation"]))
        t, a = v["test"], v["test"]["at_fixed_threshold"]
        metrics = {
            "validation_ap": v["validation_ap"],
            "test_ap": t["average_precision"],
            "test_ap_ci_low": t["ci95"]["average_precision"][0],
            "test_ap_ci_high": t["ci95"]["average_precision"][1],
            "test_roc_auc": t["roc_auc"],
            "test_precision_at_threshold": a["precision"],
            "test_recall_at_threshold": a["recall"],
            "test_review_rate_at_threshold": a["review_rate"],
            "test_ece_raw": t["ece_raw"],
            "random_split_contrast_ap": v["random_split_contrast_ap"],
        }
        for key, value in metrics.items():
            client.log_metric(cid, key, float(value), timestamp=started)
        for trial in v["grid"]:
            label = "_".join(f"{k}{val}" for k, val in trial["config"].items())
            client.log_metric(
                cid, f"grid_validation_ap_{label}", trial["validation_ap"], timestamp=started
            )
        client.set_terminated(cid, "FINISHED", end_time=started)
    for artifact in (report_path, report_path.with_suffix(".md"), PROTOCOL):
        client.log_artifact(pid, str(artifact))
    client.set_terminated(pid, "FINISHED", end_time=started)
    return pid, True


def log_reproduction(
    tracking_uri: str, run_dir: Path, artifact_location: str | None = None
) -> tuple[str, bool]:
    """Returns (run id, created). Idempotent on the rerun directory name."""
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking_uri=tracking_uri)
    exp = client.get_experiment_by_name(EXPERIMENT)
    exp_id = (
        exp.experiment_id
        if exp
        else client.create_experiment(EXPERIMENT, artifact_location=artifact_location)
    )
    key = run_dir.name
    existing = client.search_runs([exp_id], filter_string=f"tags.reproduction_id = '{key}'")
    if existing:
        return existing[0].info.run_id, False
    imported = client.search_runs(
        [exp_id], filter_string=f"tags.import_id = '{IMPORT_ID}' and tags.import_role = 'parent'"
    )
    run: dict[str, Any] = json.loads((run_dir / "run.json").read_text())
    env, cmp = run["environment"], run["comparison"]

    def ms(stamp: str) -> int:
        return int(datetime.fromisoformat(stamp).timestamp() * 1000)

    started, finished = ms(run["started_at"]), ms(run["finished_at"])
    note = (
        "Reproducibility rerun of the frozen ULB benchmark (original evaluated "
        f"{run['reference']['created_at']}, metrics only, no saved models; later imported into "
        "MLflow as a historical run). Same committed protocol, windows, seeds and the original "
        "run's selected configurations; no search, no new thresholds. Artifacts from this rerun "
        "only. Not registered, not deployed; not used by the live scoring service."
    )
    created = client.create_run(
        exp_id,
        start_time=started,
        tags={
            "record_kind": "reproduction",
            "reproduction_id": key,
            "reproduces_import_id": IMPORT_ID,
            "reproduces_run_id": imported[0].info.run_id if imported else "not found",
            "reference_created_at": run["reference"]["created_at"],
            "reference_sha256": run["reference"]["sha256"],
            "imported": "false",
            "model_saved": "true (this rerun; local artifacts)",
            "registered": "false",
            "deployed": "false",
            "live_scoring": "not used by the live scoring service",
            "dataset_md5": str(env.get("dataset_md5")),
            "dataset_source": str(env.get("dataset_source", {}).get("source", "openml")),
            "code_revision": env["code_revision"],
            "code_dirty": str(env["code_dirty"]).lower(),
            "protocol_sha256": env["protocol_sha256"],
            "uv_lock_sha256": str(env["uv_lock_sha256"]),
            "python": env["python"],
            "platform": env["platform"],
            "predictions": f"local only: {run_dir / 'predictions.parquet'}",
            "all_metrics_within_tolerance": str(cmp["all_within_tolerance"]).lower(),
            "mlflow.runName": f"{IMPORT_ID} reproduction {key}",
            "mlflow.note.content": note,
        },
    )
    rid = created.info.run_id
    for package, ver in env["packages"].items():
        client.log_param(rid, f"pkg_{package}", ver)
    for family, m in run["metrics"].items():
        for name, value in m["selected"].items():
            client.log_param(rid, f"{family}_{name}", str(value))
        client.log_param(rid, f"{family}_threshold", str(m["threshold_from_validation"]))
        t = m["test"]
        for name, value in {
            "validation_ap": m["validation_ap"],
            "test_ap": t["average_precision"],
            "test_roc_auc": t["roc_auc"],
            "test_precision_at_threshold": t["at_fixed_threshold"]["precision"],
            "test_recall_at_threshold": t["at_fixed_threshold"]["recall"],
            "test_review_rate_at_threshold": t["at_fixed_threshold"]["review_rate"],
            "random_split_contrast_ap": m["random_split_contrast_ap"],
            "reload_parity_max_abs_diff": run["reload_parity_max_abs_diff"][family],
        }.items():
            client.log_metric(rid, f"{family}_{name}", float(value), timestamp=finished)
    client.log_metric(rid, "comparison_max_abs_diff", cmp["max_abs_diff"], timestamp=finished)
    for name in ("run.json", "manifest.json", "config.json", "schema.json"):
        client.log_artifact(rid, str(run_dir / name))
    for family in run["metrics"]:
        client.log_artifact(rid, str(run_dir / family / "model.json"), artifact_path=family)
    client.set_terminated(rid, "FINISHED", end_time=finished)
    return rid, True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tracking-uri",
        default=os.environ.get("FRAUD_MLFLOW_TRACKING_URI", "http://127.0.0.1:5050"),
    )
    parser.add_argument("--reproduction", type=Path, help="log this rerun directory instead")
    args = parser.parse_args()
    if args.reproduction:
        rid, made = log_reproduction(args.tracking_uri, args.reproduction)
        print(f"{'logged' if made else 'already logged'}: reproduction run {rid} in '{EXPERIMENT}'")
        return 0
    run_id, created = import_run(args.tracking_uri)
    print(f"{'imported' if created else 'already imported'}: run {run_id} in '{EXPERIMENT}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
