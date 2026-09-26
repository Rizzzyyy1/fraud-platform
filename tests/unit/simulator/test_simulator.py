"""Simulator determinism, invariants and dataset immutability on a tiny configuration."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from fraudplat.contracts import DecisionRequest
from fraudplat.hashing import request_hash
from fraudplat.simulator.config import SimulatorConfig, load_config
from fraudplat.simulator.dataset import (
    DatasetExists,
    content_hash,
    to_historical,
    verify_dataset,
    write_dataset,
)
from fraudplat.simulator.generate import generate

ROOT = Path(__file__).resolve().parents[3]


def tiny(seed: int = 7) -> SimulatorConfig:
    base = load_config(ROOT / "configs" / "sim-small-v1.toml")
    return base.model_copy(
        update={
            "dataset_id": "tiny",
            "seed": seed,
            "n_customers": 40,
            "n_terminals": 80,
            "n_days": 20,
        }
    )


def test_same_seed_same_content_different_seed_different_content() -> None:
    a = content_hash(generate(tiny()).transactions, "transaction_id")
    b = content_hash(generate(tiny()).transactions, "transaction_id")
    c = content_hash(generate(tiny(seed=8)).transactions, "transaction_id")
    assert a == b
    assert a != c


def test_invariants() -> None:
    config = tiny()
    tx = generate(config).transactions
    assert tx.height > 0
    assert tx["transaction_id"].is_unique().all()
    assert tx["event_time"].is_sorted()
    assert (tx["received_at"] >= tx["event_time"]).all()
    assert (tx["label_available_at"] > tx["event_time"]).all()
    assert (tx["amount_minor"] > 0).all()
    assert tx["is_fraud"].to_list() == (tx["fraud_s1"] | tx["fraud_s2"] | tx["fraud_s3"]).to_list()
    threshold = config.scenarios.s1_amount_threshold_minor
    # Scenario 1 is judged on the pre-scenario-3 amount, so every s1 row is above the threshold,
    # and every row above it that scenario 3 did not inflate is s1.
    assert (tx.filter(pl.col("fraud_s1"))["amount_minor"] > threshold).all()
    unscaled_high = tx.filter(~pl.col("fraud_s3") & (pl.col("amount_minor") > threshold))
    assert unscaled_high["fraud_s1"].all()


def test_arrival_delay_is_independent_of_transactions() -> None:
    base = tiny()
    other = base.model_copy(
        update={"arrival": base.arrival.model_copy(update={"delayed_fraction": 0.5})}
    )
    cols = [
        "transaction_id",
        "customer_id",
        "terminal_id",
        "amount_minor",
        "event_time",
        "is_fraud",
    ]
    assert (
        generate(base).transactions.select(cols).equals(generate(other).transactions.select(cols))
    )


def test_dataset_is_written_once_verified_and_read_only(tmp_path: Path) -> None:
    config = tiny()
    directory = write_dataset(generate(config), config, tmp_path)
    assert verify_dataset(directory) == []
    assert all(not (p.stat().st_mode & 0o222) for p in directory.iterdir())
    with pytest.raises(DatasetExists):
        write_dataset(generate(config), config, tmp_path)


def test_event_payload_hash_matches_api_request_hash() -> None:
    tx = generate(tiny()).transactions.head(3)
    for row, hist in zip(tx.iter_rows(named=True), to_historical(tx), strict=True):
        request = DecisionRequest(
            transaction_id=row["transaction_id"],
            customer_id=row["customer_id"],
            terminal_id=row["terminal_id"],
            amount_minor=row["amount_minor"],
            currency=row["currency"],
            event_time=row["event_time"],
        )
        assert hist.event.payload_hash == request_hash(request).hex()
