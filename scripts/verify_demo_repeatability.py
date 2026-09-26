"""Check that the dashboard's demo jobs repeat cleanly on a running deployment.

Signs in to the console (credentials from E2E_USER / E2E_PASSWORD), then:
  1. runs the standard traffic job twice and checks each run decides *new* transactions
     (no idempotent replays, disjoint consecutive slices, one decision per request sent);
  2. cancels a traffic job mid-run and checks the next job starts where it stopped;
  3. cancels a failure drill while the worker is held, and checks the worker is restarted;
  4. kills the worker (SIGKILL) during traffic and checks the launcher restarts it;
and after each step that the pipeline drains (no backlog, lag 0, heartbeat fresh) and that the
deployment's dead-letter topic stays empty (no late, conflicting or invalid events).

Run from the checkout whose `.env` and `run/dashboard/` belong to the deployment:
    E2E_USER=... E2E_PASSWORD=... uv run python scripts/verify_demo_repeatability.py
Writes reports/dashboard/repeatability.json.
"""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psycopg
from confluent_kafka import Consumer, TopicPartition

from fraudplat.config import Settings
from fraudplat.console.deployment import is_worker, load_deployment, read_pids

OUT = Path("reports/dashboard/repeatability.json")


class Console:
    def __init__(self, base: str, user: str, password: str) -> None:
        self.http = httpx.Client(base_url=base, timeout=10)
        r = self.http.post("/api/session", json={"username": user, "password": password})
        r.raise_for_status()
        self.csrf = r.json()["csrf_token"]

    def get(self, path: str) -> Any:
        r = self.http.get(path)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        r = self.http.post(path, json=body or {}, headers={"X-CSRF-Token": self.csrf})
        r.raise_for_status()
        return r.json()

    def job(self, job_id: int) -> dict[str, Any]:
        jobs: list[dict[str, Any]] = self.get("/api/jobs")["jobs"]
        return next(j for j in jobs if j["id"] == job_id)

    def wait_job(self, job_id: int, timeout_s: float = 400) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            job = self.job(job_id)
            if job["status"] != "running":
                return job
            time.sleep(2)
        raise TimeoutError(f"job {job_id} still running")


def dlq_messages(settings: Settings, topic: str) -> int:
    consumer = Consumer(
        {"bootstrap.servers": settings.kafka_bootstrap, "group.id": "repeatability-probe"}
    )
    try:
        partitions = consumer.list_topics(topic, timeout=10).topics[topic].partitions
        total = 0
        for p in partitions:
            low, high = consumer.get_watermark_offsets(TopicPartition(topic, p), timeout=10)
            total += high - low
        return total
    finally:
        consumer.close()


def wait_drained(api: str, timeout_s: float = 120) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = httpx.get(f"{api}/readyz", timeout=5).json()
        p = body.get("pipeline", {})
        if (
            body.get("status") == "ready"
            and p.get("outbox_backlog") == 0
            and p.get("consumer_lag_events") == 0
            and (p.get("worker_heartbeat_age_s") or 99) < 5
        ):
            return {
                "status": body["status"],
                "outbox_backlog": 0,
                "consumer_lag_events": 0,
                "worker_heartbeat_age_s": p["worker_heartbeat_age_s"],
            }
        time.sleep(1)
    raise TimeoutError(f"pipeline not drained: {body}")


def decisions_in(settings: Settings, namespace: str, prefix: str) -> int:
    with psycopg.connect(settings.database_url.get_secret_value()) as db:
        row = db.execute(
            "SELECT count(*) FROM outbox WHERE stream = %s AND aggregate_id LIKE %s",
            (namespace, prefix + "%"),
        ).fetchone()
    return int(row[0]) if row else 0


def main() -> int:
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    settings = Settings()
    deployment = load_deployment()
    if deployment is None:
        raise SystemExit("no dashboard deployment in run/dashboard")
    api = f"http://127.0.0.1:{os.environ.get('FRAUD_CONSOLE_API_PORT', '8110')}"
    console = Console(
        f"http://127.0.0.1:{os.environ.get('FRAUD_CONSOLE_PORT', '8200')}",
        os.environ["E2E_USER"],
        os.environ["E2E_PASSWORD"],
    )
    ns, dlq = deployment.namespace, deployment.stream.dlq_topic
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "namespace": ns,
        "steps": [],
    }
    before = decisions_in(settings, ns, deployment.id_prefix)
    ok = True

    def record(name: str, **detail: Any) -> None:
        report["steps"].append({"step": name, **detail})
        print(name, json.dumps(detail, default=str))

    # 1. The standard traffic demonstration, twice.
    runs = []
    for i in (1, 2):
        job = console.post("/api/jobs", {"kind": "traffic", "rate": 10, "duration_s": 60})
        done = console.wait_job(job["id"])
        drained = wait_drained(api)
        runs.append(done)
        replays = done["outcomes"].get("idempotent_replay", 0)
        errors = sum(v for k, v in done["outcomes"].items() if k.startswith(("http_", "transport")))
        passed = done["status"] == "succeeded" and replays == 0 and errors == 0
        ok &= passed
        record(
            f"traffic run {i}",
            job=done["id"],
            status=done["status"],
            slice=[done["start_offset"], done["start_offset"] + done["sent"]],
            outcomes=done["outcomes"],
            drained=drained,
            passed=passed,
        )
    contiguous = runs[1]["start_offset"] == runs[0]["start_offset"] + runs[0]["sent"]
    ok &= contiguous
    record("consecutive runs use disjoint, contiguous slices", passed=contiguous)

    # 2. Cancel a traffic job mid-run.
    job = console.post("/api/jobs", {"kind": "traffic", "rate": 5, "duration_s": 180})
    time.sleep(20)
    console.post(f"/api/jobs/{job['id']}/cancel")
    cancelled = console.wait_job(job["id"], 60)
    wait_drained(api)
    nxt = console.post("/api/jobs", {"kind": "traffic", "rate": 5, "duration_s": 30})
    after_cancel = console.wait_job(nxt["id"])
    resumes = after_cancel["start_offset"] == cancelled["start_offset"] + cancelled["sent"]
    passed = cancelled["status"] == "cancelled" and resumes
    ok &= passed
    record(
        "cancel traffic mid-run",
        cancelled={k: cancelled[k] for k in ("id", "status", "sent")},
        next_job_start_offset=after_cancel["start_offset"],
        passed=passed,
    )
    wait_drained(api)

    # 3. Cancel a failure drill while the worker is held.
    worker_before = read_pids().get("worker")
    drill = console.post("/api/jobs", {"kind": "failure_drill", "rate": 5})
    time.sleep(35)
    held = read_pids().get("worker") is None
    console.post(f"/api/jobs/{drill['id']}/cancel")
    drill_done = console.wait_job(drill["id"], 60)
    t0 = time.monotonic()
    drained = wait_drained(api)
    worker_after = read_pids().get("worker")
    passed = (
        held
        and drill_done["status"] == "cancelled"
        and worker_after is not None
        and worker_after != worker_before
        and is_worker(worker_after, ns)
    )
    ok &= passed
    record(
        "cancel failure drill while worker held",
        worker_held_before_cancel=held,
        job_status=drill_done["status"],
        outcomes=drill_done["outcomes"],
        worker_restarted=worker_after != worker_before,
        seconds_to_drained_after_cancel=round(time.monotonic() - t0, 1),
        passed=passed,
    )

    # 4. Kill the worker during traffic; the launcher restarts it.
    job = console.post("/api/jobs", {"kind": "traffic", "rate": 10, "duration_s": 60})
    time.sleep(15)
    victim = read_pids()["worker"]
    if not is_worker(victim, ns):
        raise SystemExit("pid is not this deployment's worker")
    os.kill(victim, signal.SIGKILL)
    done = console.wait_job(job["id"])
    drained = wait_drained(api)
    restarted = read_pids().get("worker")
    errors = sum(v for k, v in done["outcomes"].items() if k.startswith(("http_", "transport")))
    passed = done["status"] == "succeeded" and errors == 0 and restarted not in (None, victim)
    ok &= passed
    record(
        "worker killed during traffic",
        job=done["id"],
        outcomes=done["outcomes"],
        worker_restarted=restarted not in (None, victim),
        drained=drained,
        passed=passed,
    )

    # Whole-run invariants.
    jobs = console.get("/api/jobs")["jobs"]
    sent = sum(j["sent"] for j in jobs if j["id"] >= runs[0]["id"])
    created = decisions_in(settings, ns, deployment.id_prefix) - before
    dead = dlq_messages(settings, dlq)
    invariants = created == sent and dead == 0
    ok &= invariants
    record(
        "invariants",
        requests_sent=sent,
        new_decisions=created,
        dead_letter_messages=dead,
        passed=invariants,
    )
    report["passed"] = ok
    report["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
