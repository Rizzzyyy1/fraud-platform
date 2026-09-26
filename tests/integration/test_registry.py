"""Registry promotion and rollback, startup without the registry, and tamper detection.

Uses a throwaway SQLite-backed MLflow store in a temporary directory (no server, no network).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis

from fraudplat.api.app import create_app
from fraudplat.policy import write_policy
from fraudplat.registry import RegistryUnavailable, publish, resolve, set_alias
from fraudplat.training.model import build_model, save_model

from .conftest import API_KEY, make_settings, redis_test_url
from .test_model_scoring_api import identity_model

pytestmark = pytest.mark.integration
Conn = psycopg.Connection[tuple[object, ...]]
NAME = "fraud-risk-test"


def make_bundle(tmp: Path, intercept: float) -> tuple[Path, Path]:
    base = identity_model()
    model = build_model(
        feature_version=base.feature_version,
        preprocessing=base.preprocessing,
        coefficients=base.coefficients,
        intercept=intercept,
        metadata={"variant": intercept},
    )
    model_path = save_model(model, tmp / "models")
    policy_path = tmp / "policies" / f"{model.model_version}.json"
    write_policy(
        policy_path,
        review_threshold=0.5,
        decline_threshold=None,
        derived_for_model=model.model_version,
        derivation={"data": "test"},
    )
    return model_path, policy_path


@pytest.fixture
def registry(tmp_path: Path) -> dict[str, Any]:
    import mlflow

    uri = f"sqlite:///{tmp_path}/mlflow.db"
    os.environ["MLFLOW_DISABLE_AGENT_HINT"] = "1"
    mlflow.set_tracking_uri(uri)
    experiment = mlflow.create_experiment(
        "registry-test", artifact_location=str(tmp_path / "mlruns")
    )
    with mlflow.start_run(experiment_id=experiment) as run:
        run_id = run.info.run_id
    v1_model, v1_policy = make_bundle(tmp_path, intercept=-3.0)
    v2_model, v2_policy = make_bundle(tmp_path, intercept=-1.0)
    v1 = publish(uri, NAME, v1_model, v1_policy, run_id)
    v2 = publish(uri, NAME, v2_model, v2_policy, run_id)
    return {"uri": uri, "v1": v1, "v2": v2, "cache": tmp_path / "cache", "tmp": tmp_path}


async def decide(settings: Any, txn: str) -> dict[str, Any]:
    app = create_app(settings, monitor_pipeline=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t", headers={"X-API-Key": API_KEY}
        ) as http,
    ):
        response = await http.post(
            "/v1/decisions",
            json={
                "transaction_id": txn,
                "customer_id": "C1",
                "terminal_id": "T1",
                "amount_minor": 100,
                "currency": "USD",
                "event_time": "2025-05-01T12:00:00Z",
            },
        )
        ready = (await http.get("/readyz")).json()
    assert response.status_code == 201
    return {"body": response.json(), "ready": ready}


def settings_for(
    database_url: str, registry: dict[str, Any], tracking_uri: str | None = None
) -> Any:
    return make_settings(
        database_url,
        model_uri=f"models:/{NAME}@production",
        mlflow_tracking_uri=tracking_uri or registry["uri"],
        model_cache_dir=registry["cache"],
        redis_url=SecretStr(redis_test_url()),
    )


async def test_promotion_then_rollback_is_recorded_in_decisions(
    database_url: str, db: Conn, redis_client: Redis, registry: dict[str, Any]
) -> None:
    set_alias(registry["uri"], NAME, "production", registry["v2"])
    promoted = await decide(settings_for(database_url, registry), "reg-1")
    set_alias(registry["uri"], NAME, "production", registry["v1"])  # rollback
    rolled_back = await decide(settings_for(database_url, registry), "reg-2")

    assert promoted["body"]["model_registry_ref"] == f"{NAME}/{registry['v2']}"
    assert rolled_back["body"]["model_registry_ref"] == f"{NAME}/{registry['v1']}"
    assert promoted["body"]["model_version"] != rolled_back["body"]["model_version"]
    assert promoted["ready"]["model"]["source"] == "registry"
    rows = db.execute("SELECT transaction_id, model_registry_ref FROM decisions").fetchall()
    stored = {str(t): str(r) for t, r in rows}
    assert stored == {"reg-1": f"{NAME}/{registry['v2']}", "reg-2": f"{NAME}/{registry['v1']}"}


async def test_registry_down_uses_verified_pin(
    database_url: str, db: Conn, redis_client: Redis, registry: dict[str, Any]
) -> None:
    set_alias(registry["uri"], NAME, "production", registry["v1"])
    resolve(registry["uri"], f"models:/{NAME}@production", registry["cache"])  # writes the pin
    dead = "http://127.0.0.1:1"
    result = await decide(settings_for(database_url, registry, tracking_uri=dead), "reg-3")
    assert result["ready"]["model"]["source"] == "cache"
    assert result["body"]["model_registry_ref"] == f"{NAME}/{registry['v1']}"


def test_registry_down_without_pin_fails_startup(
    database_url: str, registry: dict[str, Any]
) -> None:
    fresh = replace_cache(registry, "empty-cache")
    with pytest.raises(RegistryUnavailable):
        create_app(settings_for(database_url, fresh, tracking_uri="http://127.0.0.1:1"))


def test_tampered_cache_is_rejected_even_with_registry_down(registry: dict[str, Any]) -> None:
    set_alias(registry["uri"], NAME, "production", registry["v1"])
    resolved = resolve(registry["uri"], f"models:/{NAME}@production", registry["cache"])
    policy = resolved.policy_path
    policy.chmod(0o644)
    policy.write_text(
        policy.read_text().replace('"review_threshold": 0.5', '"review_threshold": 0.01')
    )
    with pytest.raises(ValueError, match="integrity"):
        resolve("http://127.0.0.1:1", f"models:/{NAME}@production", registry["cache"])


def replace_cache(registry: dict[str, Any], name: str) -> dict[str, Any]:
    fresh = dict(registry)
    fresh["cache"] = registry["tmp"] / name
    shutil.rmtree(fresh["cache"], ignore_errors=True)
    return fresh
