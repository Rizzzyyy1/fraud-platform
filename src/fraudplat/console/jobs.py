"""Bounded, predefined demo jobs for the dashboard deployment.

Only two job kinds exist, with enumerated parameters; nothing from a request is ever passed to a
shell or used as a command, path or module name:

  traffic        send the next slice of pre-test demo transactions to the deployment's API at a
                 fixed rate for a fixed duration;
  failure_drill  the same traffic for 150 s, with the deployment's feature worker stopped from
                 t = 20 s to t = 110 s (90 s: past the 60 s stale limit, so the API first marks
                 decisions degraded and then persists scoreless reviews), then restarted.

Rules:
  * one active job at a time, enforced by a partial unique index in `console_jobs`;
  * a job owns a contiguous slice of the request list (`start_offset` .. `start_offset + sent`);
    the next job starts where the previous one stopped, so slices never overlap. Resending is
    safe anyway: the API is idempotent on transaction id;
  * the worker is stopped only after its pid is confirmed to be this deployment's worker, and a
    hold file with an expiry keeps the launcher from restarting it early. Removing the hold (on
    completion, cancellation or crash, or when it expires) lets the launcher restart it;
  * cancellation sends SIGTERM to the job process, which stops sending, waits for in-flight
    requests, releases the worker hold and records `cancelled` with what it sent.

Job process entry point (started by the console with a fixed argv):
    python -m fraudplat.console.jobs <job-id>
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from fraudplat.config import Settings
from fraudplat.console.deployment import (
    STATE_DIR,
    Deployment,
    is_worker,
    load_deployment,
    load_requests,
    read_pids,
)

JobKind = Literal["traffic", "failure_drill"]
RATES = (2, 5, 10, 20)
DURATIONS = (30, 60, 180, 300)
DRILL_RATE_MAX = 10
DRILL_DURATION_S = 150
DRILL_STOP_AT_S = 20
DRILL_RESUME_AT_S = 110
MAX_IN_FLIGHT = 32
HOLD_FILE = "worker.hold"


class JobConflict(Exception):
    """Another job is active."""


class JobRejected(ValueError):
    pass


@dataclass(frozen=True)
class JobSpec:
    kind: JobKind
    rate: int
    duration_s: int

    @classmethod
    def validate(cls, kind: str, rate: int, duration_s: int | None) -> JobSpec:
        if kind == "traffic":
            if rate not in RATES or duration_s not in DURATIONS:
                raise JobRejected(f"traffic: rate in {RATES}, duration_s in {DURATIONS}")
            return cls("traffic", rate, duration_s)
        if kind == "failure_drill":
            if rate not in RATES or rate > DRILL_RATE_MAX:
                raise JobRejected(f"failure_drill: rate in {RATES} and <= {DRILL_RATE_MAX}")
            return cls("failure_drill", rate, DRILL_DURATION_S)
        raise JobRejected("kind must be 'traffic' or 'failure_drill'")

    @property
    def requests(self) -> int:
        return self.rate * self.duration_s


# --------------------------------------------------------------------------- console side


class JobManager:
    """Used by the console process. Database calls are synchronous and short; they run in a
    thread from the async endpoints."""

    def __init__(self, database_url: str, deployment: Deployment, state_dir: Path = STATE_DIR):
        self.database_url = database_url
        self.deployment = deployment
        self.state_dir = state_dir
        self._children: dict[int, subprocess.Popen[bytes]] = {}

    def _db(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self.database_url, autocommit=True, row_factory=dict_row)

    def next_offset(self, db: psycopg.Connection[dict[str, Any]]) -> int:
        row = db.execute(
            "SELECT coalesce(max(start_offset + sent), 0) AS o FROM console_jobs "
            "WHERE namespace = %s",
            (self.deployment.namespace,),
        ).fetchone()
        return int(row["o"]) if row else 0

    def start(self, spec: JobSpec, requested_by: str) -> dict[str, Any]:
        self.reconcile()
        with self._db() as db:
            offset = self.next_offset(db)
            remaining = self.deployment.requests - offset
            if remaining < spec.requests:
                raise JobRejected(
                    f"only {remaining} demo transactions remain in this deployment; "
                    "reset the deployment (make dashboard-reset) to start a new one"
                )
            try:
                row = db.execute(
                    "INSERT INTO console_jobs (kind, params, namespace, status, requested_by, "
                    "start_offset) VALUES (%s, %s, %s, 'running', %s, %s) RETURNING *",
                    (
                        spec.kind,
                        Jsonb({"rate": spec.rate, "duration_s": spec.duration_s}),
                        self.deployment.namespace,
                        requested_by,
                        offset,
                    ),
                ).fetchone()
            except psycopg.errors.UniqueViolation as exc:
                raise JobConflict("another job is running") from exc
            assert row is not None
            log = (self.state_dir / "logs").resolve()
            log.mkdir(parents=True, exist_ok=True)
            with (log / f"job-{row['id']}.log").open("wb") as out:
                proc = subprocess.Popen(  # noqa: S603 - fixed argv; the id is an integer
                    [sys.executable, "-m", "fraudplat.console.jobs", str(int(row["id"]))],
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            db.execute("UPDATE console_jobs SET pid = %s WHERE id = %s", (proc.pid, row["id"]))
            self._children[int(row["id"])] = proc
            row["pid"] = proc.pid
            return row

    def cancel(self, job_id: int) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM console_jobs WHERE id = %s", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] != "running":
                return row
            if row["pid"] and _is_job_process(int(row["pid"]), job_id):
                os.kill(int(row["pid"]), signal.SIGTERM)
                row["message"] = "cancellation requested"
            return row

    def reconcile(self) -> None:
        """Mark a `running` job whose process is gone as failed, and release its worker hold."""
        for job_id, proc in list(self._children.items()):
            if proc.poll() is not None:  # reap finished job processes
                del self._children[job_id]
        with self._db() as db:
            rows = db.execute("SELECT * FROM console_jobs WHERE status = 'running'").fetchall()
            for row in rows:
                if row["pid"] and _is_job_process(int(row["pid"]), int(row["id"])):
                    continue
                if row["pid"] is None and time.time() - row["created_at"].timestamp() < 10:
                    continue  # just inserted; its pid is being recorded
                release_hold(self.state_dir, int(row["id"]))
                db.execute(
                    "UPDATE console_jobs SET status = 'failed', finished_at = clock_timestamp(), "
                    "message = coalesce(message, '') || ' job process exited without "
                    "finishing' WHERE id = %s AND status = 'running'",
                    (row["id"],),
                )

    def recent(self, limit: int = 10) -> dict[str, Any]:
        self.reconcile()
        with self._db() as db:
            rows = db.execute(
                "SELECT id, kind, params, status, requested_by, start_offset, sent, outcomes, "
                "message, created_at, finished_at FROM console_jobs WHERE namespace = %s "
                "ORDER BY id DESC LIMIT %s",
                (self.deployment.namespace, limit),
            ).fetchall()
            offset = self.next_offset(db)
        for row in rows:
            row["hold"] = read_hold(self.state_dir) if row["status"] == "running" else None
        return {"remaining_demo_transactions": self.deployment.requests - offset, "jobs": rows}


def _is_job_process(pid: int, job_id: int) -> bool:
    from fraudplat.console.deployment import process_command

    command = process_command(pid)
    return command is not None and command.endswith(f"fraudplat.console.jobs {job_id}")


def read_hold(state_dir: Path) -> dict[str, Any] | None:
    path = state_dir / HOLD_FILE
    try:
        hold: dict[str, Any] = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return hold if hold.get("until", 0) > time.time() else None


def release_hold(state_dir: Path, job_id: int) -> None:
    path = state_dir / HOLD_FILE
    try:
        if json.loads(path.read_text()).get("job_id") == job_id:
            path.unlink()
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------- job process


def classify(status: int, body: dict[str, Any] | None) -> str:
    if status == 200 and body is not None:
        return "idempotent_replay"  # already decided earlier: no new decision or event
    if status == 201 and body is not None:
        if body.get("score") is None:
            return "scoreless_review"
        return str(body.get("action"))
    return f"http_{status}"


async def run_job(job_id: int, settings: Settings, state_dir: Path = STATE_DIR) -> int:
    deployment = load_deployment(state_dir)
    if deployment is None:
        raise SystemExit("no dashboard deployment")
    with psycopg.connect(
        settings.database_url.get_secret_value(), autocommit=True, row_factory=dict_row
    ) as db:
        job = db.execute("SELECT * FROM console_jobs WHERE id = %s", (job_id,)).fetchone()
        if job is None or job["status"] != "running" or job["namespace"] != deployment.namespace:
            raise SystemExit("job is not a running job of this deployment")
        spec = JobSpec.validate(job["kind"], job["params"]["rate"], job["params"]["duration_s"])
        requests = load_requests(state_dir)[job["start_offset"] :][: spec.requests]
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        outcomes: dict[str, int] = {}
        events: list[str] = []
        sent = 0
        in_flight = asyncio.Semaphore(MAX_IN_FLIGHT)
        tasks: set[asyncio.Task[None]] = set()
        worker_stopped = False

        def progress(status: str | None = None, message: str | None = None) -> None:
            db.execute(
                "UPDATE console_jobs SET sent = %s, outcomes = %s, message = %s, "
                "status = coalesce(%s::text, status), "
                "finished_at = CASE WHEN %s::text IS NULL THEN NULL ELSE clock_timestamp() END "
                "WHERE id = %s",
                (
                    sent,
                    Jsonb(outcomes),
                    message or "; ".join(events[-3:]) or None,
                    status,
                    status,
                    job_id,
                ),
            )

        async def one(http: httpx.AsyncClient, body: dict[str, Any]) -> None:
            try:
                resp = await http.post("/v1/decisions", json=body)
                key = classify(resp.status_code, resp.json() if resp.content else None)
            except (httpx.HTTPError, ValueError):
                key = "transport_error"
            outcomes[key] = outcomes.get(key, 0) + 1
            in_flight.release()

        def stop_worker() -> None:
            nonlocal worker_stopped
            pid = read_pids(state_dir).get("worker")
            if pid is None or not is_worker(pid, deployment.namespace):
                events.append("worker not found; drill continued without an outage")
                return
            (state_dir / HOLD_FILE).write_text(
                json.dumps(
                    {"job_id": job_id, "until": time.time() + DRILL_RESUME_AT_S - DRILL_STOP_AT_S}
                )
            )
            os.kill(pid, signal.SIGTERM)
            worker_stopped = True
            events.append(f"worker stopped at t={DRILL_STOP_AT_S}s")

        def resume_worker() -> None:
            nonlocal worker_stopped
            release_hold(state_dir, job_id)
            if worker_stopped:
                events.append("worker hold released; launcher restarts it")
            worker_stopped = False

        port = int(os.environ.get("FRAUD_CONSOLE_API_PORT", "8110"))
        headers = {"X-API-Key": settings.api_key.get_secret_value()}
        status = "succeeded"
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=10
            ) as http:
                t0 = time.monotonic()
                last_update = t0
                for i, body in enumerate(requests):
                    due = t0 + i / spec.rate
                    if spec.kind == "failure_drill":
                        elapsed = time.monotonic() - t0
                        if (
                            elapsed >= DRILL_STOP_AT_S
                            and not worker_stopped
                            and i > 0
                            and (elapsed < DRILL_RESUME_AT_S)
                        ):
                            stop_worker()
                        if worker_stopped and elapsed >= DRILL_RESUME_AT_S:
                            resume_worker()
                    try:
                        await asyncio.wait_for(stop.wait(), max(0.0, due - time.monotonic()))
                        status = "cancelled"
                        break
                    except TimeoutError:
                        pass
                    await in_flight.acquire()
                    task = asyncio.create_task(one(http, body))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                    sent += 1
                    if time.monotonic() - last_update >= 2:
                        progress()
                        last_update = time.monotonic()
                if tasks:
                    await asyncio.gather(*tasks)
        except Exception as exc:
            status = "failed"
            events.append(f"{type(exc).__name__}: {exc}")
        finally:
            resume_worker()
            progress(status, "; ".join(events) or None)
        return 0 if status == "succeeded" else 1


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit("usage: python -m fraudplat.console.jobs <job-id>")
    return asyncio.run(run_job(int(sys.argv[1]), Settings()))


if __name__ == "__main__":
    raise SystemExit(main())
