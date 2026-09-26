"""End-to-end benchmark: HTTP decisions through the full stack, plus feature-application delay.

Stack: uvicorn API (1 process) + publisher + worker as separate processes, PostgreSQL / Redis /
Kafka in Compose with their resource limits, all on this laptop (the load generator shares the
machine, which it competes with for CPU). Namespace `replay:bench-<ts>`; Redis bootstrapped with
sim-v2 history before day 140; distinct transactions after the cutoff, in decision order.

Load: open-loop — request i is scheduled at t0 + i / rate, subject to a cap on requests in
flight. For each target rate: `--warmup` seconds (excluded) then `--duration` seconds measured.
Latency is measured by the client from sending a request to receiving the full response.
Delay after each level: Redis `applied_at_us` minus PostgreSQL `persisted_at` for every measured
decision, and outbox `published_at - created_at`.

Usage: python scripts/benchmark_e2e.py --rates 50 100 200 --duration 60 --warmup 10
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import psycopg
from redis.asyncio import Redis

from fraudplat.config import Settings
from fraudplat.demos.stack import (
    Stack,
    load_workload,
    prepare,
    purge_namespace,
    run_stream,
    single,
    write_report,
)
from fraudplat.features.spec import US
from fraudplat.policy import load_policy
from fraudplat.simulator.dataset import _code_revision
from fraudplat.training.artifacts import load_any_model
from fraudplat.training.model import load_model

OUT = Path("reports/benchmarks")


def _pct(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    arr = np.asarray(values)
    return {f"p{q}": round(float(np.percentile(arr, q)), 2) for q in (50, 95, 99)} | {
        "max": round(float(arr.max()), 2),
        "mean": round(float(arr.mean()), 2),
    }


def docker_memory() -> dict[str, str]:
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return {line.split()[0]: " ".join(line.split()[1:]) for line in out.strip().splitlines()}


def process_rss_mib(pids: dict[str, int]) -> dict[str, float]:
    result = {}
    for name, pid in pids.items():
        out = subprocess.run(  # noqa: S603
            ["ps", "-o", "rss=", "-p", str(pid)],  # noqa: S607
            capture_output=True,
            text=True,
        ).stdout.strip()
        if out:
            result[name] = round(int(out) / 1024, 1)
    return result


def machine() -> dict[str, Any]:
    def sysctl(name: str) -> str:
        return subprocess.run(  # noqa: S603
            ["sysctl", "-n", name],  # noqa: S607
            capture_output=True,
            text=True,
        ).stdout.strip()

    return {
        "platform": platform.platform(),
        "cpu": sysctl("machdep.cpu.brand_string"),
        "cores": sysctl("hw.ncpu"),
        "memory_gib": int(sysctl("hw.memsize") or 0) / 2**30,
        "python": platform.python_version(),
        "container_runtime": "Colima VM: 6 CPU, 8 GiB",
        "compose_limits": {
            "postgres": "512 MiB",
            "redis": "512 MiB (maxmemory 384 MiB)",
            "kafka": "1 GiB (heap 512 MiB)",
        },
    }


def scrape_stages(base_url: str) -> dict[str, dict[str, Any]]:
    """Read cumulative stage histograms from the API's /metrics (outside the send path)."""
    from prometheus_client.parser import text_string_to_metric_families

    text = httpx.get(f"{base_url}/metrics", timeout=5).text
    out: dict[str, dict[str, Any]] = {}
    for family in text_string_to_metric_families(text):
        if family.name not in ("fraud_stage_seconds", "fraud_event_loop_lag_seconds"):
            continue
        for sample in family.samples:
            key = sample.labels.get("stage", "event_loop_lag")
            entry = out.setdefault(key, {"buckets": {}, "count": 0.0, "sum": 0.0})
            if sample.name.endswith("_bucket"):
                entry["buckets"][sample.labels["le"]] = sample.value
            elif sample.name.endswith("_count"):
                entry["count"] = sample.value
            elif sample.name.endswith("_sum"):
                entry["sum"] = sample.value
    return out


def stage_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Per-stage count, mean and bucket-bounded quantiles over the window (upper bounds, ms)."""
    result: dict[str, Any] = {}
    for key, a in after.items():
        b = before.get(key, {"buckets": {}, "count": 0.0, "sum": 0.0})
        count = a["count"] - b["count"]
        if count <= 0:
            continue
        cum = sorted(
            ((float(le), a["buckets"][le] - b["buckets"].get(le, 0.0)) for le in a["buckets"]),
            key=lambda x: x[0],
        )

        def upper(
            q: float, cum: list[tuple[float, float]] = cum, count: float = count
        ) -> float | None:
            for le, c in cum:
                if c >= q * count:
                    return None if le == float("inf") else round(le * 1000, 3)
            return None

        result[key] = {
            "count": int(count),
            "mean_ms": round((a["sum"] - b["sum"]) / count * 1000, 3),
            "p50_le_ms": upper(0.5),
            "p95_le_ms": upper(0.95),
            "p99_le_ms": upper(0.99),
        }
    return result


def thread_counts(pids: dict[str, int]) -> dict[str, int]:
    counts = {}
    for name, pid in pids.items():
        out = (
            subprocess.run(  # noqa: S603
                ["ps", "-M", "-p", str(pid)],  # noqa: S607
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .splitlines()
        )
        if len(out) > 1:
            counts[name] = len(out) - 1
    return counts


def environment(stack: Stack) -> dict[str, Any]:
    import importlib.metadata as md

    versions = {}
    for pkg in (
        "xgboost",
        "numpy",
        "scikit-learn",
        "fastapi",
        "uvicorn",
        "redis",
        "psycopg",
        "psycopg-pool",
        "confluent-kafka",
        "prometheus-client",
        "httpx",
    ):
        try:
            versions[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            versions[pkg] = None
    load = subprocess.run(
        ["sysctl", "-n", "vm.loadavg"],  # noqa: S607
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {
        "code_revision": _code_revision(),
        "python": platform.python_version(),
        "packages": versions,
        "host_load_before": load,
        "process_layout": {"api": "1 uvicorn process, 1 event loop", "publisher": 1, "worker": 1},
        "omp_num_threads": os.environ.get("OMP_NUM_THREADS"),
    }


def classify(status: int, body: dict[str, Any] | None) -> str:
    """Separate model-served decisions from degraded ones and from rejections."""
    if status == 503 and body and body.get("error") == "pipeline_backlog_limit":
        return "backlog_rejected"
    if status not in (200, 201) or body is None:
        return f"http_{status}"
    reasons = body.get("reason_codes", [])
    if body.get("score") is None or "FEATURES_UNAVAILABLE" in reasons:
        return "degraded_review_no_score"
    if "PIPELINE_DEGRADED" in reasons:
        return "model_scored_pipeline_degraded"
    return "model_scored"


async def sampler(
    settings: Settings, redis: Redis, stack: Stack, stop: asyncio.Event, t0: float
) -> list[dict[str, Any]]:
    """Once per second: outbox backlog of this run's stream, the run's worker heartbeat lag,
    Redis used memory, and CPU% / RSS of the three processes. Broker calls are not made here."""
    samples: list[dict[str, Any]] = []
    async with await psycopg.AsyncConnection.connect(
        settings.database_url.get_secret_value(), autocommit=True
    ) as db:
        while not stop.is_set():
            cur = await db.execute(
                "SELECT count(*) FROM outbox WHERE published_at IS NULL AND stream = %s",
                (stack.stream.namespace,),
            )
            backlog = (await cur.fetchone() or (0,))[0]
            status = await redis.hgetall(stack.stream.worker_status_key)
            memory = await redis.info("memory")
            samples.append(
                {
                    "t_s": round(time.perf_counter() - t0, 1),
                    "outbox_backlog": backlog,
                    "worker_lag": int(status.get("consumer_lag_events", -1)) if status else None,
                    "redis_used_mb": round(memory["used_memory"] / 2**20, 1),
                    "cpu_percent": await asyncio.to_thread(process_cpu, stack.pids()),
                    "threads": await asyncio.to_thread(thread_counts, stack.pids()),
                }
            )
            try:
                await asyncio.wait_for(stop.wait(), 1.0)
            except TimeoutError:
                pass
    return samples


def process_cpu(pids: dict[str, int]) -> dict[str, float]:
    out = {}
    for name, pid in pids.items():
        value = subprocess.run(  # noqa: S603
            ["ps", "-o", "%cpu=", "-p", str(pid)],  # noqa: S607
            capture_output=True,
            text=True,
        ).stdout.strip()
        if value:
            out[name] = float(value)
    return out


async def run_level(
    http: httpx.AsyncClient,
    bodies: list[dict[str, Any]],
    rate: float,
    warmup_s: float,
    max_in_flight: int,
    pids: dict[str, int],
    sample_memory: bool,
) -> tuple[dict[str, Any], float]:
    """Latency boundary: from immediately before `http.post` (after waiting for an in-flight
    slot) to the fully read response. Scheduling delay: actual start minus scheduled start,
    which includes any wait for a slot. Warm-up requests are excluded from latency statistics
    and outcome counts but included in scheduled/sent totals."""
    records: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(max_in_flight)
    memory: dict[str, Any] = {}
    sent = 0

    async def one(i: int, body: dict[str, Any], scheduled: float) -> None:
        nonlocal sent
        async with semaphore:
            start = time.perf_counter()
            sent += 1
            record: dict[str, Any] = {
                "i": i,
                "scheduled": scheduled - t0,
                "start": start - t0,
                "sched_delay_ms": (start - scheduled) * 1000,
            }
            try:
                response = await http.post("/v1/decisions", json=body)
                record["latency_ms"] = (time.perf_counter() - start) * 1000
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                record["outcome"] = classify(response.status_code, payload)
            except httpx.TimeoutException:
                record["outcome"] = "timed_out"
            except httpx.HTTPError as exc:
                record["outcome"] = f"client_error_{type(exc).__name__}"
            record["done"] = time.perf_counter() - t0
            records.append(record)

    async def sample() -> None:
        memory["docker"] = await asyncio.to_thread(docker_memory)
        memory["processes_rss_mib"] = await asyncio.to_thread(process_rss_mib, pids)

    background: asyncio.Task[None] | None = None
    t0 = time.perf_counter()
    tasks = []
    for i, body in enumerate(bodies):
        scheduled = t0 + i / rate
        delay = scheduled - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        tasks.append(asyncio.create_task(one(i, body, scheduled)))
        if sample_memory and background is None and i == len(bodies) * 2 // 3:
            background = asyncio.create_task(sample())
    await asyncio.gather(*tasks)
    load_end = time.perf_counter()
    if background is not None:
        await background

    measured = [r for r in records if r["scheduled"] >= warmup_s]
    outcomes: dict[str, int] = {}
    for r in measured:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    completed = [r for r in measured if "latency_ms" in r]
    scored = [r for r in completed if r["outcome"] == "model_scored"]
    window = max(r["done"] for r in measured) - min(r["scheduled"] for r in measured)
    failed = sum(
        v
        for k, v in outcomes.items()
        if k.startswith(("http_", "client_error_")) or k == "backlog_rejected"
    )
    return {
        "target_rps": rate,
        "scheduled_total": len(bodies),
        "sent_total": sent,
        "measured_window": {
            "scheduled": len(measured),
            "completed_with_response": len(completed),
            "timed_out": outcomes.get("timed_out", 0),
            "failed_http_or_rejected": failed,
            "achieved_completion_rps": round(len(completed) / window, 1),
            "outcomes": outcomes,
        },
        "scheduling_delay_ms": _pct([r["sched_delay_ms"] for r in measured]),
        "late_starts_over_50ms": sum(1 for r in measured if r["sched_delay_ms"] > 50),
        "latency_ms_all_responses": _pct([r["latency_ms"] for r in completed]),
        "latency_ms_model_scored_only": _pct([r["latency_ms"] for r in scored]),
        "memory_during_load": memory,
    }, load_end


def delays(settings: Settings, ids: list[str], applied: dict[str, int]) -> dict[str, Any]:
    """decision_time (API process, host clock) → applied_at (worker process, host clock): both
    timestamps come from the same host clock. PostgreSQL runs in the Colima VM with its own clock,
    so it is only used for durations measured entirely on that clock (outbox publish delay)."""
    with psycopg.connect(settings.database_url.get_secret_value()) as db:
        rows = db.execute(
            "SELECT d.transaction_id, d.decision_time, o.created_at, o.published_at "
            "FROM decisions d JOIN outbox o ON o.aggregate_id = d.transaction_id "
            "WHERE d.transaction_id = ANY(%s)",
            (ids,),
        ).fetchall()
    to_apply, to_publish = [], []
    for txn, decided, created, published in rows:
        decided_us = int(decided.timestamp()) * US + decided.microsecond
        if txn in applied:
            to_apply.append((applied[txn] - decided_us) / 1000)
        if published is not None:
            to_publish.append((published - created).total_seconds() * 1000)
    return {
        "decision_to_applied_ms_host_clock": _pct(to_apply),
        "outbox_created_to_published_ms_db_clock": _pct(to_publish),
        "applied": len(to_apply),
        "of": len(ids),
    }


def clock_skew_ms(settings: Settings) -> float:
    """PostgreSQL clock minus host clock, from the midpoint of a round trip."""
    with psycopg.connect(settings.database_url.get_secret_value()) as db:
        before = time.time()
        row = db.execute("SELECT extract(epoch FROM clock_timestamp())").fetchone()
        after = time.time()
    assert row is not None
    return round((float(row[0]) - (before + after) / 2) * 1000, 2)


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    if args.model_uri:
        from fraudplat.registry import resolve

        resolved = resolve(settings.mlflow_tracking_uri, args.model_uri, settings.model_cache_dir)
        model_path, policy_file = resolved.model_path, resolved.policy_path
    else:
        model_path = single(Path("artifacts/models"), "*/model.json")
        policy_file = Path("artifacts/policies") / f"{load_model(model_path).model_version}.json"
    model = load_any_model(model_path)
    policy = load_policy(policy_file)
    per_level = [int(rate * (args.warmup + args.duration)) for rate in args.rates]
    workload = load_workload(sum(per_level))
    stack = Stack(
        run_stream("bench"),
        args.port,
        settings,
        model_path,
        policy_file,
        model_uri=args.model_uri,
    )
    assert settings.redis_url is not None
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    report: dict[str, Any] = {
        "machine": machine(),
        "model_version": model.model_version,
        "policy_version": policy.version,
        "dataset": "sim-v2 (synthetic), transactions decided after day 140 in decision order",
        "method": {
            "load": "open loop",
            "warmup_s": args.warmup,
            "duration_s": args.duration,
            "max_in_flight": args.max_in_flight,
            "api_processes": 1,
            "client": "httpx AsyncClient, same machine",
        },
        "redis_used_memory_before_bootstrap": (await redis.info("memory"))["used_memory_human"],
    }
    try:
        report["setup"] = await prepare(stack, workload, redis)
        report["redis_used_memory_after_bootstrap"] = (await redis.info("memory"))[
            "used_memory_human"
        ]
        stack.start_api()
        stack.start_publisher()
        stack.start_worker()
        report["db_clock_minus_host_clock_ms"] = clock_skew_ms(settings)
        report["readiness_at_start"] = await stack.wait_ready()
        report["environment"] = environment(stack)
        report["environment"]["docker_stats_during_load"] = not args.no_docker_stats
        assert report["readiness_at_start"]["pipeline"]["namespace"] == stack.stream.namespace
        report["namespace"] = stack.stream.namespace
        await asyncio.sleep(2)
        report["idle_memory"] = {
            "docker": docker_memory(),
            "processes_rss_mib": process_rss_mib(stack.pids()),
        }
        levels = []
        start = 0
        key = settings.api_key.get_secret_value()
        limits = httpx.Limits(
            max_connections=args.max_in_flight, max_keepalive_connections=args.max_in_flight
        )
        async with httpx.AsyncClient(
            base_url=stack.base_url, headers={"X-API-Key": key}, timeout=10, limits=limits
        ) as http:
            for rate, count in zip(args.rates, per_level, strict=True):
                bodies = workload.requests[start : start + count]
                start += count
                stop = asyncio.Event()
                before = await asyncio.to_thread(scrape_stages, stack.base_url)
                t0 = time.perf_counter()
                sampling = asyncio.create_task(sampler(settings, redis, stack, stop, t0))
                result, load_end = await run_level(
                    http,
                    bodies,
                    rate,
                    args.warmup,
                    args.max_in_flight,
                    stack.pids(),
                    sample_memory=rate == max(args.rates) and not args.no_docker_stats,
                )
                after = await asyncio.to_thread(scrape_stages, stack.base_url)
                result["api_stages"] = stage_delta(before, after)
                result["workload_sha256"] = hashlib.sha256(
                    json.dumps(bodies, sort_keys=True).encode()
                ).hexdigest()
                ids = [b["transaction_id"] for b in bodies[int(rate * args.warmup) :]]
                keys = [f"{stack.stream.namespace}:e:{t}" for t in ids]
                unapplied_at_load_end = len(keys) - await redis.exists(*keys)
                drain_deadline = time.monotonic() + args.drain_timeout
                while time.monotonic() < drain_deadline:
                    if await redis.exists(*keys) == len(keys):
                        break
                    await asyncio.sleep(0.25)
                drain_s = time.perf_counter() - load_end
                unapplied_after_drain = len(keys) - await redis.exists(*keys)
                stop.set()
                samples = await sampling
                applied: dict[str, int] = {}
                async with redis.pipeline(transaction=False) as pipe:
                    for t in ids:
                        pipe.hget(f"{stack.stream.namespace}:e:{t}", "applied_at_us")
                    for t, value in zip(ids, await pipe.execute(), strict=True):
                        if value is not None:
                            applied[t] = int(value)
                during = [x for x in samples if x["t_s"] <= load_end - t0]
                result["pipeline"] = {
                    "delay": delays(settings, ids, applied),
                    "unapplied_at_load_end": unapplied_at_load_end,
                    "drain_seconds_after_load": round(drain_s, 2),
                    "unapplied_after_drain": unapplied_after_drain,
                    "max_outbox_backlog_during_load": max(
                        (x["outbox_backlog"] for x in during), default=None
                    ),
                    "max_worker_lag_during_load": max(
                        (x["worker_lag"] for x in during if x["worker_lag"] is not None),
                        default=None,
                    ),
                    "max_redis_used_mb": max((x["redis_used_mb"] for x in samples), default=None),
                    "timeline": samples,
                }
                levels.append(result)
                print(
                    json.dumps(
                        {
                            "target_rps": rate,
                            "achieved": result["measured_window"]["achieved_completion_rps"],
                            "outcomes": result["measured_window"]["outcomes"],
                            "scored_latency": result["latency_ms_model_scored_only"],
                            "drain_s": result["pipeline"]["drain_seconds_after_load"],
                        }
                    ),
                    flush=True,
                )
        report["levels"] = levels
        with psycopg.connect(settings.database_url.get_secret_value()) as db:
            identities = db.execute(
                "SELECT d.model_version, d.policy_version, d.model_registry_ref, count(*) "
                "FROM decisions d JOIN outbox o ON o.aggregate_id = d.transaction_id "
                "WHERE o.stream = %s GROUP BY 1, 2, 3",
                (stack.stream.namespace,),
            ).fetchall()
        report["decision_identities"] = [
            {"model_version": m, "policy_version": p, "model_registry_ref": r, "decisions": n}
            for m, p, r, n in identities
        ]
        info = await redis.info("memory")
        report["redis_memory"] = {
            "maxmemory_mb": round(int(info["maxmemory"]) / 2**20, 1),
            "before_bootstrap": report.pop("redis_used_memory_before_bootstrap"),
            "after_bootstrap": report.pop("redis_used_memory_after_bootstrap"),
            "after_run": info["used_memory_human"],
            "peak_sampled_mb": max(
                (lv["pipeline"]["max_redis_used_mb"] or 0 for lv in levels), default=None
            ),
            "server_lifetime_peak": info["used_memory_peak_human"],
        }
    finally:
        stack.stop_all()
        stack.delete_topics()
        await purge_namespace(redis, stack.stream.namespace)
        stack.delete_owned_rows()
        await redis.aclose()
    _ = statistics
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rates", type=float, nargs="+", default=[50, 100, 200])
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--warmup", type=float, default=10)
    parser.add_argument("--max-in-flight", type=int, default=64)
    parser.add_argument("--port", type=int, default=8120)
    parser.add_argument("--drain-timeout", type=float, default=300)
    parser.add_argument("--model-uri", help="registry alias, e.g. models:/fraud-risk-f1@candidate")
    parser.add_argument("--out", type=Path, default=OUT / "e2e.json")
    parser.add_argument(
        "--no-docker-stats",
        action="store_true",
        help="skip the in-window `docker stats` memory sample (it queries every container)",
    )
    args = parser.parse_args()
    report = asyncio.run(main_async(args))
    write_report(args.out, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
