"""Reproducibility rerun: portable model parity, comparison, the reference guard, MLflow record."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from fraudplat.external import benchmark as bm
from fraudplat.external.mlflow_import import EXPERIMENT, REPORT, import_run, log_reproduction
from fraudplat.external.reproduce import compare, lr_params, lr_score, reproduce
from fraudplat.external.ulb import FEATURES


def test_lr_json_parameters_reproduce_sklearn_scores() -> None:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(400, len(FEATURES))) * 5 + 2
    y = (x[:, 0] + rng.normal(size=400) > 2).astype(int)
    model = bm.build("lr", {"C": 0.1}).fit(x, y)
    params = json.loads(json.dumps(lr_params(model)))  # through the saved format
    assert params["features"] == FEATURES
    assert np.max(np.abs(lr_score(params, x) - model.predict_proba(x)[:, 1])) < 1e-12


def test_comparison_reports_differences_rather_than_hiding_them() -> None:
    reference = json.loads(REPORT.read_text())
    same = {
        f: {k: v for k, v in m.items() if k != "grid"} for f, m in reference["families"].items()
    }
    exact = compare(reference, same)
    assert exact["all_within_tolerance"] and exact["max_abs_diff"] == 0.0
    changed = json.loads(json.dumps(same))
    changed["xgb"]["test"]["average_precision"] += 0.01
    result = compare(reference, changed)
    assert not result["all_within_tolerance"]
    bad = [r for r in result["rows"] if not r["within_tolerance"]]
    assert [(r["family"], r["metric"]) for r in bad] == [("xgb", "test.average_precision")]


def test_rerun_refuses_to_write_under_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="reports"):
        reproduce(tmp_path / "reports" / "external" / "ulb")


def fake_rerun(directory: Path) -> Path:
    reference = json.loads(REPORT.read_text())
    metrics = {
        f: {k: v for k, v in m.items() if k != "grid"} for f, m in reference["families"].items()
    }
    directory.mkdir()
    for family in metrics:
        (directory / family).mkdir()
        (directory / family / "model.json").write_text("{}")
    for name in ("manifest.json", "config.json", "schema.json"):
        (directory / name).write_text("{}")
    run = {
        "reference": {"created_at": reference["created_at"], "sha256": "0" * 64},
        "started_at": "2026-09-26T13:52:33+00:00",
        "finished_at": "2026-09-26T13:54:06+00:00",
        "environment": {
            "dataset_md5": "178bcf9bb1f31a3dfe12d0e577884add",
            "code_revision": "abc",
            "code_dirty": False,
            "protocol_sha256": "1" * 64,
            "uv_lock_sha256": "2" * 64,
            "python": "3.14.0",
            "platform": "test",
            "packages": {"xgboost": "0"},
        },
        "reload_parity_max_abs_diff": {"lr": 0.0, "xgb": 0.0},
        "metrics": metrics,
        "comparison": compare(reference, metrics),
    }
    (directory / "run.json").write_text(json.dumps(run))
    return directory


def test_reproduction_is_a_separate_run_linked_to_the_import(tmp_path: Path) -> None:
    from mlflow.tracking import MlflowClient

    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    location = (tmp_path / "artifacts").as_uri()
    imported, _ = import_run(uri, artifact_location=location)
    client = MlflowClient(tracking_uri=uri)
    before = client.get_run(imported).data
    run_dir = fake_rerun(tmp_path / "20260926T135233Z")
    rid, created = log_reproduction(uri, run_dir)
    assert created and rid != imported
    assert log_reproduction(uri, run_dir) == (rid, False)  # idempotent

    run = client.get_run(rid)
    tags = run.data.tags
    assert tags["record_kind"] == "reproduction" and tags["reproduces_run_id"] == imported
    assert tags["imported"] == "false" and tags["deployed"] == "false"
    assert tags["registered"] == "false" and "local only" in tags["predictions"]
    assert run.info.start_time == 1790430753000  # the rerun's own time, not the original's
    assert run.data.metrics["xgb_reload_parity_max_abs_diff"] == 0.0
    names = {a.path for a in client.list_artifacts(rid)}
    assert {"run.json", "manifest.json", "lr", "xgb"} <= names
    assert "predictions.parquet" not in names
    after = client.get_run(imported).data
    assert (after.tags, after.params, after.metrics) == (before.tags, before.params, before.metrics)
    exp = client.get_experiment_by_name(EXPERIMENT)
    assert exp is not None
    assert not client.search_registered_models()
