"""Simulator configuration, loaded from TOML. Every field is recorded in the dataset manifest."""

from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ArrivalConfig(_Frozen):
    """Synthetic API arrival delay (received_at - event_time). Not from the handbook."""

    typical_median_ms: float = Field(gt=0)
    typical_sigma: float = Field(gt=0)  # log-normal shape
    delayed_fraction: float = Field(ge=0, le=1)
    delayed_min_s: float = Field(gt=0)
    delayed_max_s: float = Field(gt=0)


class LabelConfig(_Frozen):
    """Synthetic label availability. Not from the handbook."""

    fraud_delay_min_days: float = Field(gt=0)
    fraud_delay_max_days: float = Field(gt=0)
    legit_maturity_days: float = Field(gt=0)


class ScenarioConfig(_Frozen):
    s1_amount_threshold_minor: int = Field(gt=0)
    s2_terminals_per_day: int = Field(ge=0)
    s2_duration_days: int = Field(gt=0)
    s3_customers_per_day: int = Field(ge=0)
    s3_duration_days: int = Field(gt=0)
    s3_fraction: float = Field(gt=0, le=1)
    s3_multiplier: int = Field(gt=1)


class SimulatorConfig(_Frozen):
    dataset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]*$")
    seed: int = Field(ge=0)
    n_customers: int = Field(gt=0)
    n_terminals: int = Field(gt=0)
    n_days: int = Field(gt=0)
    start_date: date
    radius: float = Field(gt=0)
    time_of_day_mean_s: float
    time_of_day_std_s: float = Field(gt=0)
    currency: str = "USD"
    arrival: ArrivalConfig
    labels: LabelConfig
    scenarios: ScenarioConfig


def load_config(path: Path) -> SimulatorConfig:
    with path.open("rb") as handle:
        return SimulatorConfig.model_validate(tomllib.load(handle))
