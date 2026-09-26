"""Immutable raw dataset storage: write once, hash, verify, load.

A dataset directory holds Parquet tables and `manifest.json` (config, seed, generator version,
code revision, library versions, row counts, prevalence, hashes). Writing refuses to overwrite,
and files are made read-only. The *content hash* is computed over a canonical CSV rendering
sorted by primary key, so it identifies the data rather than Parquet encoding details.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from fraudplat.features.offline import HistoricalTxn
from fraudplat.features.state import TxnEvent
from fraudplat.hashing import canonical_fields_json, sha256_hex
from fraudplat.simulator.config import SimulatorConfig
from fraudplat.simulator.generate import GENERATOR_VERSION, SimulatedDataset

TABLE_KEYS = {
    "transactions": "transaction_id",
    "customers": "customer_id",
    "terminals": "terminal_id",
}


class DatasetExists(Exception):
    pass


def content_hash(frame: pl.DataFrame, key: str) -> str:
    return hashlib.sha256(frame.sort(key).write_csv().encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _code_revision() -> str:
    """Current git commit, suffixed +dirty when the working tree has uncommitted changes."""
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - git on PATH is intended
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{rev}{'+dirty' if dirty else ''}"


def write_dataset(dataset: SimulatedDataset, config: SimulatorConfig, root: Path) -> Path:
    target = root / config.dataset_id
    if target.exists():
        raise DatasetExists(f"{target} already exists; raw datasets are immutable")
    target.mkdir(parents=True)
    tables = {
        "transactions": dataset.transactions,
        "customers": dataset.customers,
        "terminals": dataset.terminals,
    }
    tx = dataset.transactions
    manifest: dict[str, Any] = {
        "dataset_id": config.dataset_id,
        "generator_version": GENERATOR_VERSION,
        "code_revision": _code_revision(),
        "libraries": {"polars": pl.__version__, "numpy": np.__version__},
        "config": config.model_dump(mode="json"),
        "synthetic": True,
        "rows": {name: frame.height for name, frame in tables.items()},
        "fraud_prevalence": {
            "any": tx["is_fraud"].mean(),
            "s1": tx["fraud_s1"].mean(),
            "s2": tx["fraud_s2"].mean(),
            "s3": tx["fraud_s3"].mean(),
        },
        "arrival_delayed_fraction": tx["arrival_delayed"].mean(),
        "tables": {},
    }
    for name, frame in tables.items():
        path = target / f"{name}.parquet"
        frame.write_parquet(path)
        manifest["tables"][name] = {
            "file": path.name,
            "content_sha256": content_hash(frame, TABLE_KEYS[name]),
            "file_sha256": _file_sha256(path),
        }
    manifest_path = target / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    for path in target.iterdir():
        path.chmod(0o444)
    return target


def verify_dataset(directory: Path) -> list[str]:
    """Return a list of problems; empty means every table matches its manifest hashes."""
    manifest = json.loads((directory / "manifest.json").read_text())
    problems: list[str] = []
    for name, entry in manifest["tables"].items():
        path = directory / entry["file"]
        if _file_sha256(path) != entry["file_sha256"]:
            problems.append(f"{name}: file hash mismatch")
        if content_hash(pl.read_parquet(path), TABLE_KEYS[name]) != entry["content_sha256"]:
            problems.append(f"{name}: content hash mismatch")
    return problems


def load_transactions(directory: Path) -> pl.DataFrame:
    return pl.read_parquet(directory / "transactions.parquet")


def to_historical(transactions: pl.DataFrame) -> list[HistoricalTxn]:
    """Convert raw rows to events. The payload hash matches what the API would compute."""
    frame = transactions.with_columns(
        pl.col("event_time").dt.epoch("us").alias("event_us"),
        pl.col("received_at").dt.epoch("us").alias("received_us"),
    )
    out: list[HistoricalTxn] = []
    for row in frame.iter_rows(named=True):
        payload = canonical_fields_json(
            transaction_id=row["transaction_id"],
            customer_id=row["customer_id"],
            terminal_id=row["terminal_id"],
            amount_minor=row["amount_minor"],
            currency=row["currency"],
            event_time=row["event_time"],
        )
        event = TxnEvent(
            event_id=row["transaction_id"],
            customer_id=row["customer_id"],
            terminal_id=row["terminal_id"],
            amount_minor=row["amount_minor"],
            event_time_us=row["event_us"],
            payload_hash=sha256_hex(payload),
        )
        out.append(HistoricalTxn(event=event, received_at_us=row["received_us"]))
    return out
