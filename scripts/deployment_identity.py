"""Record which model the running dashboard deployment serves, next to the registry's target.

Reads (does not change) the running API's `/readyz`, the registry alias target, the start time of
the API process, and the most recent persisted decision of the deployment namespace, and writes
`reports/dashboard/deployment_identity.json`. The registry target and the running deployment are
reported separately: an alias change takes effect only when the API restarts.

Usage: python scripts/deployment_identity.py
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

from fraudplat.config import Settings
from fraudplat.console.deployment import load_deployment, read_pids
from fraudplat.registry import _client, alias_target

MODEL_URI = "models:/fraud-risk-f1@production"
OUT = Path("reports/dashboard/deployment_identity.json")


def process_started(pid: int) -> str | None:
    out = subprocess.run(  # noqa: S603 - fixed argv
        ["/bin/ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not out:
        return None
    local = datetime.strptime(out, "%a %b %d %H:%M:%S %Y").astimezone()
    return local.astimezone(UTC).isoformat(timespec="seconds")


def main() -> int:
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    settings = Settings()
    deployment = load_deployment()
    if deployment is None:
        raise SystemExit("no dashboard deployment")
    ready: dict[str, Any] = httpx.get("http://127.0.0.1:8110/readyz", timeout=5).json()
    target = alias_target(settings.mlflow_tracking_uri, MODEL_URI)
    name, version = target.registry_ref.split("/")
    mv = _client(settings.mlflow_tracking_uri).get_model_version(name, version)
    with psycopg.connect(settings.database_url.get_secret_value(), row_factory=dict_row) as db:
        row = db.execute(
            "SELECT d.transaction_id, d.decision_time, d.persisted_at, d.score, d.action, "
            "d.model_version, d.policy_version, d.feature_version, d.model_registry_ref "
            "FROM decisions d JOIN outbox o ON o.aggregate_id = d.transaction_id "
            "AND o.event_type = 'decision.made' WHERE o.stream = %s "
            "ORDER BY d.decision_time DESC LIMIT 1",
            (deployment.namespace,),
        ).fetchone()
        counts = db.execute(
            "SELECT d.model_version, d.policy_version, d.model_registry_ref, count(*) AS n "
            "FROM decisions d JOIN outbox o ON o.aggregate_id = d.transaction_id "
            "AND o.event_type = 'decision.made' WHERE o.stream = %s GROUP BY 1, 2, 3",
            (deployment.namespace,),
        ).fetchall()
    api_pid = read_pids().get("api")
    report = {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "namespace": deployment.namespace,
        "registry_target": {
            "uri": MODEL_URI,
            "registry_ref": target.registry_ref,
            "model_version": target.model_version,
            "policy_version": target.policy_version,
            "version_created_at": datetime.fromtimestamp(
                mv.creation_timestamp / 1000, UTC
            ).isoformat(timespec="seconds"),
        },
        "running_deployment": {
            "api_pid": api_pid,
            "api_started_at": process_started(api_pid) if api_pid else None,
            "readyz_model": ready.get("model"),
            "readyz_status": ready.get("status"),
        },
        "latest_persisted_decision": row,
        "decisions_by_identity": counts,
        "consistent": bool(
            row
            and ready.get("model", {}).get("registry_ref") == target.registry_ref
            and row["model_registry_ref"] == target.registry_ref
            and row["model_version"] == target.model_version
            and row["policy_version"] == target.policy_version
            and all(c["model_registry_ref"] == target.registry_ref for c in counts)
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
