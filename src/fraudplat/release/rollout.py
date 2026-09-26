"""Local release rollout: promotion and rollback with explicit API restarts.

This is a local release exercise on simulated data, not a production deployment.

The API resolves `models:/fraud-risk-f1@production` once at startup, so an alias change takes
effect only after a restart. Steps (each decision's stored identities are read back from
PostgreSQL):
  1. alias → baseline; start the stack (API, publisher, worker) in a fresh replay namespace;
     decide transaction 1                                        → baseline
  2. alias → candidate, *no restart*; decide transaction 2       → still baseline
  3. restart the API; decide transaction 3                       → candidate
  4. alias → baseline (rollback); restart the API; decide 4      → baseline
  5. leave the alias on `--final` (baseline or candidate).

Run: `python -m fraudplat.release.rollout --release release-1 --final baseline`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx
import psycopg
from redis.asyncio import Redis

from fraudplat.config import Settings
from fraudplat.demos.stack import Stack, load_workload, prepare, purge_namespace, run_stream
from fraudplat.registry import set_alias

NAME = "fraud-risk-f1"


def registry_versions(uri: str, baseline: str, candidate: str) -> dict[str, str]:
    from mlflow.tracking import MlflowClient

    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    found: dict[str, str] = {}
    for mv in client.search_model_versions(f"name='{NAME}'"):
        if "superseded" not in mv.tags:
            found[mv.tags.get("model_version", "")] = str(mv.version)
    return {"baseline": found[baseline], "candidate": found[candidate]}


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    manifest = json.loads((Path("releases") / args.release / "manifest.json").read_text())
    candidate = manifest["model"]["model_version"]
    baseline = manifest["baseline_for_rollback"]["model_version"]
    versions = registry_versions(settings.mlflow_tracking_uri, baseline, candidate)
    uri = settings.mlflow_tracking_uri
    workload = load_workload(8)
    stack = Stack(
        run_stream("rollout"),
        args.port,
        settings,
        Path("unused"),
        Path("unused"),
        model_uri=f"models:/{NAME}@production",
    )
    assert settings.redis_url is not None
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    steps: list[dict[str, Any]] = []
    key = settings.api_key.get_secret_value()

    async def decide(label: str, expected: str) -> None:
        body = workload.requests[len(steps)]
        async with httpx.AsyncClient(base_url=stack.base_url, headers={"X-API-Key": key}) as http:
            ready = (await http.get("/readyz")).json()
            response = await http.post("/v1/decisions", json=body)
        with psycopg.connect(settings.database_url.get_secret_value()) as db:
            row = db.execute(
                "SELECT model_version, policy_version, model_registry_ref, action, score "
                "FROM decisions WHERE transaction_id = %s",
                (body["transaction_id"],),
            ).fetchone()
        assert row is not None
        steps.append(
            {
                "step": label,
                "transaction_id": body["transaction_id"],
                "http": response.status_code,
                "api_model_at_startup": ready["model"],
                "stored": {
                    "model_version": row[0],
                    "policy_version": row[1],
                    "model_registry_ref": row[2],
                    "action": row[3],
                    "score": row[4],
                },
                "expected_model": expected,
                "as_expected": row[0] == expected,
            }
        )
        print(f"{label:48} -> {row[0]} / {row[1]} / {row[2]}", flush=True)

    try:
        await prepare(stack, workload, redis)
        set_alias(uri, NAME, "production", versions["baseline"])
        stack.start_publisher()
        stack.start_worker()
        stack.start_api()
        await stack.wait_ready()
        await decide("1 start on baseline", baseline)
        set_alias(uri, NAME, "production", versions["candidate"])
        await decide("2 alias -> candidate, no restart", baseline)
        stack.stop("api")
        stack.start_api()
        await stack.wait_ready()
        await decide("3 after restart", candidate)
        set_alias(uri, NAME, "production", versions["baseline"])
        stack.stop("api")
        stack.start_api()
        await stack.wait_ready()
        await decide("4 rollback: alias -> baseline, restart", baseline)
    finally:
        stack.stop_all()
        stack.delete_topics()
        await purge_namespace(redis, stack.stream.namespace)
        stack.delete_owned_rows()
        await redis.aclose()
    final = versions[args.final]
    set_alias(uri, NAME, "production", final)
    return {
        "release": args.release,
        "registry_versions": versions,
        "steps": steps,
        "final_production_alias": {"version": final, "role": args.final},
        "note": "local release on simulated data; not a production deployment",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", default="release-1")
    parser.add_argument("--final", choices=["baseline", "candidate"], required=True)
    parser.add_argument("--port", type=int, default=8130)
    args = parser.parse_args()
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    result = asyncio.run(run(args))
    out = Path("reports") / args.release / "rollout.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str) + "\n")
    return 0 if all(s["as_expected"] and s["http"] == 201 for s in result["steps"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
