"""The ULB import records committed results as an imported run, without recomputation."""

from __future__ import annotations

import json
from pathlib import Path

from fraudplat.external.mlflow_import import EXPERIMENT, REPORT, import_run


def test_import_is_labelled_complete_and_idempotent(tmp_path: Path) -> None:
    from mlflow.tracking import MlflowClient

    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"
    run_id, created = import_run(uri, artifact_location=(tmp_path / "artifacts").as_uri())
    assert created
    again, created_again = import_run(uri)
    assert (again, created_again) == (run_id, False)  # nothing duplicated

    client = MlflowClient(tracking_uri=uri)
    parent = client.get_run(run_id)
    tags = parent.data.tags
    assert tags["imported"] == "true" and tags["deployed"] == "false"
    assert (
        tags["model_saved"] == "false" and "Imported historical run" in tags["mlflow.note.content"]
    )
    assert tags["dataset_md5"] == "178bcf9bb1f31a3dfe12d0e577884add"
    assert "V1-V28" in parent.data.params["features"]
    report = json.loads(REPORT.read_text())
    assert parent.info.start_time // 1000 == int(
        __import__("datetime").datetime.fromisoformat(report["created_at"]).timestamp()
    )
    exp = client.get_experiment_by_name(EXPERIMENT)
    assert exp is not None
    children = client.search_runs([exp.experiment_id], f"tags.mlflow.parentRunId = '{run_id}'")
    assert {c.data.tags["family"] for c in children} == {"lr", "xgb"}
    for child in children:
        source = report["families"][child.data.tags["family"]]
        assert child.data.metrics["test_ap"] == source["test"]["average_precision"]
        assert child.data.metrics["validation_ap"] == source["validation_ap"]
        assert (
            float(child.data.params["threshold_from_validation"])
            == source["threshold_from_validation"]
        )
    names = {a.path for a in client.list_artifacts(run_id)}
    assert {"benchmark.json", "benchmark.md", "benchmark.py"} <= names
