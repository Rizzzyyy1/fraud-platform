"""Start the real processes (API via uvicorn, outbox publisher, feature worker) in an isolated
`replay:<run>` namespace, bootstrap its Redis state, and supply the sim-v2 transactions to send.

Used by the continuous demonstration and the end-to-end benchmark. Everything runs locally;
the final test period (day >= 153) is never read.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import psycopg
from redis.asyncio import Redis

from fraudplat.config import Settings
from fraudplat.features.offline import HistoricalTxn, split_at_cutoff
from fraudplat.features.redis_state import RedisFeatureState, bootstrap_redis
from fraudplat.features.spec import DAY, FEATURE_NAMES, RETENTION_HORIZON
from fraudplat.hashing import format_utc
from fraudplat.pipeline.streams import Stream
from fraudplat.pipeline.topics import delete_consumer_group, delete_topics, ensure_topics
from fraudplat.simulator.dataset import to_historical
from fraudplat.training.derived import load_pre_test_features

DATASET = Path("data/raw/sim-v2")
CUTOFF_DAY = 140
TEST_START_DAY = 153


def run_stream(prefix: str) -> Stream:
    """A fresh replay namespace per run, so topics and consumer groups never collide."""
    return Stream(f"replay:{prefix}-{int(time.time())}")


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")


def single(directory: Path, pattern: str) -> Path:
    found = sorted(directory.glob(pattern))
    if len(found) != 1:
        raise SystemExit(f"expected exactly one {pattern} under {directory}, found {len(found)}")
    return found[0]


@dataclass
class Workload:
    """Transactions decided at or after the cutoff, in decision order, with offline features."""

    txns: list[HistoricalTxn]
    requests: list[dict[str, Any]]
    expected: dict[str, dict[str, float | None]]
    cutoff_us: int
    all_pre_test: list[HistoricalTxn]


def load_workload(limit: int) -> Workload:
    raw = pl.read_parquet(DATASET / "transactions.parquet")
    day0 = int(raw["event_time"].dt.epoch("us").min())  # type: ignore[arg-type]
    day0 -= day0 % DAY
    cutoff = day0 + CUTOFF_DAY * DAY
    test_start = day0 + TEST_START_DAY * DAY
    pre_test = raw.filter(pl.col("received_at").dt.epoch("us") < test_start)
    txns = to_historical(pre_test)
    scoring = sorted(
        split_at_cutoff(txns, cutoff).scoring, key=lambda t: (t.decision_time_us, t.event.event_id)
    )[:limit]
    by_id = {
        r["transaction_id"]: r
        for r in pre_test.filter(
            pl.col("transaction_id").is_in([t.event.event_id for t in scoring])
        ).iter_rows(named=True)
    }
    requests = [
        {
            "transaction_id": t.event.event_id,
            "customer_id": t.event.customer_id,
            "terminal_id": t.event.terminal_id,
            "amount_minor": t.event.amount_minor,
            "currency": by_id[t.event.event_id]["currency"],
            "event_time": format_utc(by_id[t.event.event_id]["event_time"]),
        }
        for t in scoring
    ]
    derived = load_pre_test_features(DATASET, Path("data/derived"), test_start)
    rows = derived.filter(pl.col("transaction_id").is_in([r["transaction_id"] for r in requests]))
    expected = {
        row["transaction_id"]: {
            name: (None if row[name] is None or row[name] != row[name] else row[name])
            for name in FEATURE_NAMES
        }
        for row in rows.iter_rows(named=True)
    }
    return Workload(scoring, requests, expected, cutoff, txns)


@dataclass
class Stack:
    stream: Stream
    port: int
    settings: Settings
    model_path: Path
    policy_path: Path
    processes: dict[str, subprocess.Popen[bytes]] = field(default_factory=dict)
    logs: Path = Path("reports/pipeline/logs")
    model_uri: str | None = None  # registry alias resolved by the API at startup

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _env(self) -> dict[str, str]:
        env = {
            **os.environ,
            "FRAUD_FEATURE_NAMESPACE": self.stream.namespace,
            "MLFLOW_DISABLE_AGENT_HINT": "1",
        }
        if self.model_uri is not None:
            env["FRAUD_MODEL_URI"] = self.model_uri
            env.pop("FRAUD_MODEL_PATH", None)
            env.pop("FRAUD_POLICY_PATH", None)
        else:
            env["FRAUD_MODEL_PATH"] = str(self.model_path)
            env["FRAUD_POLICY_PATH"] = str(self.policy_path)
        return env

    def _spawn(self, name: str, args: list[str]) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        log = (self.logs / f"{name}.log").open("wb")
        self.processes[name] = subprocess.Popen(  # noqa: S603 - fixed module invocations
            [sys.executable, *args], env=self._env(), stdout=log, stderr=subprocess.STDOUT
        )

    def start_api(self) -> None:
        self._spawn(
            "api",
            [
                "-m",
                "uvicorn",
                "fraudplat.api.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
                "--no-access-log",
            ],
        )

    def start_publisher(self) -> None:
        self._spawn(
            "publisher",
            [
                "-m",
                "fraudplat.pipeline.publisher",
                "--namespace",
                self.stream.namespace,
                "--metrics-port",
                "0",
            ],
        )

    def start_worker(self, apply_delay_ms: float = 0.0) -> None:
        self._spawn(
            "worker",
            [
                "-m",
                "fraudplat.pipeline.worker",
                "--namespace",
                self.stream.namespace,
                "--metrics-port",
                "0",
                "--apply-delay-ms",
                str(apply_delay_ms),
            ],
        )

    def stop(self, name: str) -> None:
        proc = self.processes.pop(name, None)
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()

    def stop_all(self) -> None:
        for name in list(self.processes):
            self.stop(name)

    def delete_topics(self) -> None:
        """Remove Kafka resources owned by this run: its topics and its consumer group."""
        delete_topics(self.settings.kafka_bootstrap, self.stream)
        delete_consumer_group(self.settings.kafka_bootstrap, self.stream)

    def delete_owned_rows(self) -> int:
        """Delete decisions and outbox rows written by this run's stream, and nothing else."""
        with psycopg.connect(self.settings.database_url.get_secret_value(), autocommit=True) as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT aggregate_id FROM outbox WHERE stream = %s", (self.stream.namespace,)
                ).fetchall()
            ]
            db.execute("DELETE FROM outbox WHERE stream = %s", (self.stream.namespace,))
            db.execute("DELETE FROM decisions WHERE transaction_id = ANY(%s)", (ids,))
        return len(ids)

    def pids(self) -> dict[str, int]:
        return {name: p.pid for name, p in self.processes.items()}

    async def wait_ready(self, want: str = "ready", timeout_s: float = 60) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        body: dict[str, Any] = {}
        async with httpx.AsyncClient(base_url=self.base_url) as http:
            while time.monotonic() < deadline:
                try:
                    body = (await http.get("/readyz")).json()
                    if body.get("status") == want:
                        return body
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.25)
        raise TimeoutError(f"stack not {want} after {timeout_s}s: {body}")


async def purge_namespace(redis: Redis, namespace: str) -> int:
    """Delete this run's Redis keys (replay namespaces only) so runs do not accumulate."""
    if not namespace.startswith("replay:"):
        raise ValueError("only replay namespaces are purged")
    keys = [k async for k in redis.scan_iter(f"{namespace}:*", count=10_000)]
    for i in range(0, len(keys), 10_000):
        await redis.unlink(*keys[i : i + 10_000])
    return len(keys)


async def prepare(stack: Stack, workload: Workload, redis: Redis) -> dict[str, Any]:
    """Create this run's topics, exclude transactions already decided by another stream, clear
    leftovers of *earlier replay runs* for the remaining ids, and bootstrap Redis at the cutoff.

    Rows owned by `live` (or any non-replay stream) are never deleted: those transactions are
    removed from the workload instead, and the count is reported.
    """
    ensure_topics(stack.settings.kafka_bootstrap, stack.stream)
    ids = [r["transaction_id"] for r in workload.requests]
    with psycopg.connect(stack.settings.database_url.get_secret_value(), autocommit=True) as db:
        foreign = {
            r[0]
            for r in db.execute(
                "SELECT d.transaction_id FROM decisions d LEFT JOIN outbox o "
                "ON o.aggregate_id = d.transaction_id "
                "WHERE d.transaction_id = ANY(%s) "
                "AND (o.stream IS NULL OR o.stream NOT LIKE 'replay:%%')",
                (ids,),
            ).fetchall()
        }
        stale = [
            r[0]
            for r in db.execute(
                "SELECT aggregate_id FROM outbox "
                "WHERE aggregate_id = ANY(%s) AND stream LIKE 'replay:%%'",
                (ids,),
            ).fetchall()
        ]
        db.execute(
            "DELETE FROM outbox WHERE aggregate_id = ANY(%s) AND stream LIKE 'replay:%%'", (stale,)
        )
        db.execute("DELETE FROM decisions WHERE transaction_id = ANY(%s)", (stale,))
    if foreign:
        keep = [i for i, r in enumerate(workload.requests) if r["transaction_id"] not in foreign]
        workload.requests = [workload.requests[i] for i in keep]
        workload.txns = [workload.txns[i] for i in keep]
    state = RedisFeatureState(redis, stack.stream.namespace)
    started = time.perf_counter()
    statuses = await bootstrap_redis(
        state,
        workload.all_pre_test,
        workload.cutoff_us,
        history_start_us=workload.cutoff_us - RETENTION_HORIZON - DAY,
    )
    return {
        "bootstrap_events": sum(statuses.values()),
        "bootstrap_seconds": round(time.perf_counter() - started, 2),
        "excluded_already_decided_by_other_stream": sorted(foreign),
        "stale_replay_rows_removed": len(stale),
    }
