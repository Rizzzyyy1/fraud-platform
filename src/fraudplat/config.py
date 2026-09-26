"""Runtime settings, read from FRAUD_* environment variables or a local .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FRAUD_", env_file=".env", extra="ignore", protected_namespaces=()
    )

    database_url: SecretStr
    api_key: SecretStr
    db_pool_min: int = 1
    db_pool_max: int = 10
    db_acquire_timeout_s: float = 2.0
    db_statement_timeout_ms: int = 2000

    # Model scoring. When model_path is unset the temporary scaffold scorer is used.
    model_path: Path | None = None
    policy_path: Path | None = None
    # Alternatively, a registry alias resolved once at startup (never per request).
    model_uri: str | None = None  # e.g. models:/fraud-risk-f1@production
    mlflow_tracking_uri: str = "http://127.0.0.1:5050"
    model_cache_dir: Path = Path("artifacts/registry-cache")
    model_cache_fallback: bool = True
    redis_url: SecretStr | None = None
    feature_namespace: str = "live"
    feature_read_timeout_ms: int = 50

    # Event pipeline.
    kafka_bootstrap: str = "127.0.0.1:9094"
    # Pipeline-health policy (see docs/DESIGN.md §8): above the degraded limits decisions carry
    # PIPELINE_DEGRADED; above the hard limits new decisions are rejected with 503.
    pipeline_monitor_interval_s: float = 1.0
    worker_heartbeat_max_age_s: float = 10.0
    outbox_max_age_degraded_s: float = 30.0
    consumer_lag_degraded_events: int = 1000
    outbox_backlog_reject_rows: int = 50_000
    outbox_age_reject_s: float = 900.0
    # Beyond these limits the service stops scoring on possibly stale features and persists a
    # scoreless review instead (see docs/DESIGN.md §9).
    stale_scoring_limit_s: float = 60.0
    unknown_state_limit_s: float = 60.0
