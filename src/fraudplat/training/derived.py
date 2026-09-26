"""Derived point-in-time feature tables, excluding the final test period.

Only transactions decided before `test_start_us` are reconstructed. Their features depend only on
events available before their own decision time, so truncating the input at `test_start_us` gives
exactly the same values as reconstructing the full dataset, and no test-period row is ever
materialised. Tables are write-once and record the source dataset's content hash.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

from fraudplat.features.offline import point_in_time_matrix
from fraudplat.features.spec import FEATURE_NAMES, FEATURE_VERSION
from fraudplat.simulator.dataset import _code_revision, content_hash, to_historical


class DerivedTableMismatch(Exception):
    pass


def _source_hash(dataset_dir: Path) -> str:
    manifest = json.loads((dataset_dir / "manifest.json").read_text())
    return str(manifest["tables"]["transactions"]["content_sha256"])


def derived_dir(root: Path, dataset_id: str, test_start_us: int) -> Path:
    return root / dataset_id / f"{FEATURE_VERSION}-pre-test-{test_start_us}"


def build_pre_test_features(dataset_dir: Path, root: Path, test_start_us: int) -> Path:
    raw = pl.read_parquet(dataset_dir / "transactions.parquet")
    target = derived_dir(root, dataset_dir.name, test_start_us)
    if target.exists():
        raise FileExistsError(f"{target} exists; derived tables are write-once")
    pre_test = raw.filter(pl.col("received_at").dt.epoch("us") < test_start_us)
    txns = to_historical(pre_test)
    matrix = point_in_time_matrix(txns)
    ids = [txns[int(i)].event.event_id for i in matrix.txn_index]
    table = pl.DataFrame(
        {
            "transaction_id": ids,
            "decision_time_us": matrix.decision_time_us,
            "history_complete": matrix.history_complete,
            **{name: matrix.values[:, j] for j, name in enumerate(FEATURE_NAMES)},
        }
    )
    target.mkdir(parents=True)
    table.write_parquet(target / "features.parquet")
    manifest: dict[str, Any] = {
        "feature_version": FEATURE_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "source_dataset": dataset_dir.name,
        "source_content_sha256": _source_hash(dataset_dir),
        "test_start_us": test_start_us,
        "availability": "arrival, processing delay 0 (immediate-processing assumption)",
        "rows": table.height,
        "apply_statuses": {k.value: v for k, v in matrix.apply_statuses.items()},
        "content_sha256": content_hash(table, "transaction_id"),
        "code_revision": _code_revision(),
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    for path in target.iterdir():
        path.chmod(0o444)
    return target


def load_pre_test_features(dataset_dir: Path, root: Path, test_start_us: int) -> pl.DataFrame:
    target = derived_dir(root, dataset_dir.name, test_start_us)
    manifest = json.loads((target / "manifest.json").read_text())
    if manifest["source_content_sha256"] != _source_hash(dataset_dir):
        raise DerivedTableMismatch("derived table was built from different source data")
    if manifest["feature_version"] != FEATURE_VERSION:
        raise DerivedTableMismatch("derived table has a different feature version")
    table = pl.read_parquet(target / "features.parquet")
    if content_hash(table, "transaction_id") != manifest["content_sha256"]:
        raise DerivedTableMismatch("derived table content hash mismatch")
    return table
