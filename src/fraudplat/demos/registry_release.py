"""Compatible model promotion and rollback through the MLflow registry (local server).

1. Register the current baseline (the Checkpoint C model and its policy) and the candidate
   selected by the comparison (with its calibration-window policy) as versions of
   `fraud-risk-f1`, skipping any bundle already registered.
2. Promote: alias `production` → candidate; start the API (it resolves the alias once, at
   startup); score one transaction; the decision records the registry version.
3. Roll back: alias `production` → baseline; restart; score another transaction.
The alias is left on the baseline; promoting the candidate for real is a separate decision.

Transactions come from sim-v2 just after the day-140 cutoff (validation period), scored against
the `live` namespace state bootstrapped by `make demo-c`. Rows of these transactions are removed
first only if they belong to the `live` stream.

Run: `make demo-registry` (needs `make up mlflow demo-c` and a completed `make compare`).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from httpx import ASGITransport, AsyncClient

from fraudplat.api.app import create_app
from fraudplat.config import Settings
from fraudplat.demos.stack import load_workload
from fraudplat.registry import publish, set_alias

NAME = "fraud-risk-f1"
BASELINE = "lr-f1-6f0ebad8fcc7"


def _registered(tracking_uri: str) -> dict[str, str]:
    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)
    found: dict[str, str] = {}
    for mv in client.search_model_versions(f"name='{NAME}'"):
        found[mv.tags.get("model_version", "")] = str(mv.version)
    return found


def _model_file(version: str) -> Path:
    base = Path("artifacts/models") / version
    return base / ("manifest.json" if (base / "manifest.json").exists() else "model.json")


async def _decide(settings: Settings, body: dict[str, Any]) -> dict[str, Any]:
    app = create_app(settings, monitor_pipeline=False)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://demo",
            headers={"X-API-Key": settings.api_key.get_secret_value()},
        ) as http,
    ):
        ready = (await http.get("/readyz")).json()
        response = await http.post("/v1/decisions", json=body)
    return {"status": response.status_code, "decision": response.json(), "model": ready["model"]}


def main() -> int:
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    import mlflow

    settings = Settings()
    uri = settings.mlflow_tracking_uri
    report = json.loads(Path("reports/model_comparison/report.json").read_text())
    candidate = report["selection"]["selected_model_version"]
    registered = _registered(uri)
    mlflow.set_tracking_uri(uri)
    mlflow.set_experiment("fraud-f1-releases")
    with mlflow.start_run(run_name=f"release-{candidate}") as run:
        mlflow.set_tags(
            {
                "candidate": candidate,
                "baseline": BASELINE,
                "comparison_run": report["mlflow"]["parent_run_id"],
            }
        )
        versions = {}
        for model_version in (BASELINE, candidate):
            if model_version in registered:
                versions[model_version] = registered[model_version]
                continue
            versions[model_version] = publish(
                uri,
                NAME,
                _model_file(model_version),
                Path("artifacts/policies") / f"{model_version}.json",
                run.info.run_id,
                description="baseline (Checkpoint C)"
                if model_version == BASELINE
                else "selected by configs/compare-v1.toml",
            )

    requests = load_workload(3).requests[1:3]
    ids = [r["transaction_id"] for r in requests]
    with psycopg.connect(settings.database_url.get_secret_value(), autocommit=True) as db:
        found = db.execute(
            "SELECT aggregate_id, stream FROM outbox WHERE aggregate_id = ANY(%s)", (ids,)
        ).fetchall()
        owners: dict[str, str] = {str(t): str(s) for t, s in found}
        if any(stream != "live" for stream in owners.values()):
            raise SystemExit(f"transactions owned by another stream: {owners}")
        db.execute("DELETE FROM outbox WHERE aggregate_id = ANY(%s) AND stream = 'live'", (ids,))
        db.execute("DELETE FROM decisions WHERE transaction_id = ANY(%s)", (list(owners),))

    registry_settings = settings.model_copy(
        update={"model_uri": f"models:/{NAME}@production", "model_path": None, "policy_path": None}
    )
    set_alias(uri, NAME, "production", versions[candidate])
    promoted = asyncio.run(_decide(registry_settings, requests[0]))
    set_alias(uri, NAME, "production", versions[BASELINE])
    rolled_back = asyncio.run(_decide(registry_settings, requests[1]))

    result = {
        "registered_versions": versions,
        "promoted": promoted,
        "rolled_back": rolled_back,
        "final_alias": {"production": versions[BASELINE]},
    }
    out = Path("reports/model_comparison/release_demo.json")
    out.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps(result, indent=2, default=str))
    ok = (
        promoted["decision"]["model_registry_ref"] == f"{NAME}/{versions[candidate]}"
        and rolled_back["decision"]["model_registry_ref"] == f"{NAME}/{versions[BASELINE]}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
