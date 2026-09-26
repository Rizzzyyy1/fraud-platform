"""Continuous scoring through the full pipeline, and the skew it can and cannot remove.

Stack: uvicorn API (model lr-f1, policy) → PostgreSQL outbox → publisher → Kafka → worker →
Redis, all as separate processes in namespace `replay:demo`; Redis bootstrapped with sim-v2
history available before day 140 (validation period; the test period is never read).

Part A — deterministic sequence. The first N transactions decided at or after the cutoff are
sent one at a time; before sending the next, the demo waits until the worker has applied the
previous one to Redis. Under that discipline the live service observes exactly what the offline
point-in-time reconstruction assumes (immediate processing), so every stored feature vector must
equal the offline table value, which counts each earlier transaction once in each later window.

Part B — burst with deliberate consumer delay. The next M transactions are sent concurrently
while the worker sleeps `--delay-ms` per event. Decisions are made before earlier transactions
reach Redis, so stored features can lag the offline values. The demo reports how many decisions
differ, which features, and how scores and actions change in *this* experiment; the result does
not establish that lag is harmless under other workloads. Kafka does not remove this skew.

Delivery is at-least-once with idempotent feature application; the checks here show that
duplicate-safe effects hold in these scenarios, not a general exactly-once guarantee.

Run: `make demo-pipeline` (needs `make up migrate data-v2 train`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import psycopg
from redis.asyncio import Redis

from fraudplat.config import Settings
from fraudplat.demos.stack import (
    Stack,
    Workload,
    load_workload,
    prepare,
    purge_namespace,
    run_stream,
    single,
    write_report,
)
from fraudplat.policy import load_policy
from fraudplat.training.model import load_model

OUT = Path("reports/pipeline")


async def wait_applied(redis: Redis, ns: str, txn: str, timeout_s: float = 30) -> float:
    started = time.perf_counter()
    while not await redis.exists(f"{ns}:e:{txn}"):
        if time.perf_counter() - started > timeout_s:
            raise TimeoutError(f"{txn} not applied within {timeout_s}s")
        await asyncio.sleep(0.002)
    return time.perf_counter() - started


def stored_rows(settings: Settings, ids: list[str]) -> dict[str, dict[str, Any]]:
    with psycopg.connect(settings.database_url.get_secret_value()) as db:
        rows = db.execute(
            "SELECT transaction_id, features, score, action, reason_codes FROM decisions "
            "WHERE transaction_id = ANY(%s)",
            (ids,),
        ).fetchall()
    return {r[0]: {"features": r[1], "score": r[2], "action": r[3], "reasons": r[4]} for r in rows}


def compare(
    workload: Workload, ids: list[str], stored: dict[str, dict[str, Any]], model: Any, policy: Any
) -> dict[str, Any]:
    differing_rows = 0
    by_feature: Counter[str] = Counter()
    score_deltas: list[float] = []
    action_changes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for txn in ids:
        expected = workload.expected[txn]
        got = stored[txn]
        diffs = [n for n, v in expected.items() if got["features"].get(n) != v]
        if diffs:
            differing_rows += 1
            by_feature.update(diffs)
        expected_score = model.score(expected)
        score_deltas.append(abs(got["score"] - expected_score))
        expected_action = policy.decide(expected_score)[0]
        if expected_action != got["action"]:
            action_changes[f"{expected_action}->{got['action']}"] += 1
        reasons.update(got["reasons"])
    nonzero = [d for d in score_deltas if d > 0]
    return {
        "decisions": len(ids),
        "feature_vectors_equal_offline": len(ids) - differing_rows,
        "feature_vectors_different": differing_rows,
        "differences_by_feature": dict(by_feature.most_common()),
        "scores_changed": len(nonzero),
        "max_abs_score_change": max(score_deltas) if score_deltas else 0.0,
        "median_abs_score_change_when_changed": statistics.median(nonzero) if nonzero else 0.0,
        "action_changes": dict(action_changes),
        "reason_codes": dict(reasons.most_common()),
    }


async def membership_check(
    redis: Redis, ns: str, ids: list[str], workload: Workload
) -> dict[str, int]:
    by_id = {t.event.event_id: t.event for t in workload.txns}
    missing = 0
    for txn in ids:
        e = by_id[txn]
        in_customer = await redis.zscore(f"{ns}:c:{e.customer_id}", txn)
        in_terminal = await redis.zscore(f"{ns}:t:{e.terminal_id}", txn)
        missing += int(in_customer is None) + int(in_terminal is None)
    repeat = Counter(by_id[t].customer_id for t in ids)
    return {
        "sequence_members_missing_from_zsets": missing,
        "customers_with_repeat_transactions": sum(1 for c in repeat.values() if c > 1),
        "max_transactions_per_customer": max(repeat.values()),
    }


def repeat_trace(
    workload: Workload, ids: list[str], stored: dict[str, dict[str, Any]]
) -> list[Any]:
    """For customers seen more than once in the sequence: stored vs expected 1-day count at each
    of their decisions. Each earlier sequence transaction must add exactly one."""
    by_id = {t.event.event_id: t.event for t in workload.txns}
    per_customer: dict[str, list[str]] = {}
    for txn in ids:
        per_customer.setdefault(by_id[txn].customer_id, []).append(txn)
    trace = []
    for customer, txns in per_customer.items():
        if len(txns) > 1:
            trace.append(
                {
                    "customer": customer,
                    "decisions": [
                        {
                            "transaction_id": t,
                            "stored_cust_txn_count_1d": stored[t]["features"]["cust_txn_count_1d"],
                            "expected_cust_txn_count_1d": workload.expected[t]["cust_txn_count_1d"],
                        }
                        for t in txns
                    ],
                }
            )
    return trace


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    model_path = single(Path("artifacts/models"), "*/model.json")
    model = load_model(model_path)
    policy_path = Path("artifacts/policies") / f"{model.model_version}.json"
    policy = load_policy(policy_path)
    workload = load_workload(args.sequence + args.burst)
    seq_ids = [r["transaction_id"] for r in workload.requests[: args.sequence]]
    burst_ids = [r["transaction_id"] for r in workload.requests[args.sequence :]]
    stack = Stack(run_stream("demo"), args.port, settings, model_path, policy_path)
    assert settings.redis_url is not None
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    report: dict[str, Any] = {
        "model_version": model.model_version,
        "policy_version": policy.version,
        "namespace": stack.stream.namespace,
        "cutoff_day": 140,
    }
    try:
        report["setup"] = await prepare(stack, workload, redis)
        stack.start_api()
        stack.start_publisher()
        stack.start_worker()
        report["readiness_at_start"] = await stack.wait_ready()
        ns = stack.stream.namespace
        key = settings.api_key.get_secret_value()
        async with httpx.AsyncClient(
            base_url=stack.base_url, headers={"X-API-Key": key}, timeout=10
        ) as http:
            # Part A
            waits, t0 = [], time.perf_counter()
            for body in workload.requests[: args.sequence]:
                response = await http.post("/v1/decisions", json=body)
                assert response.status_code == 201, response.text
                waits.append(await wait_applied(redis, ns, body["transaction_id"]))
            report["sequence"] = {
                "elapsed_s": round(time.perf_counter() - t0, 2),
                "decision_to_applied_observed_ms": {
                    "p50": round(statistics.median(waits) * 1000, 1),
                    "max": round(max(waits) * 1000, 1),
                },
                **compare(
                    workload, seq_ids, (seq_rows := stored_rows(settings, seq_ids)), model, policy
                ),
                **await membership_check(redis, ns, seq_ids, workload),
                "repeat_customer_trace": repeat_trace(workload, seq_ids, seq_rows),
            }
            # Part B
            stack.stop("worker")
            stack.start_worker(apply_delay_ms=args.delay_ms)
            semaphore = asyncio.Semaphore(args.concurrency)

            async def send(body: dict[str, Any]) -> int:
                async with semaphore:
                    return (await http.post("/v1/decisions", json=body)).status_code

            t0 = time.perf_counter()
            statuses = await asyncio.gather(*(send(b) for b in workload.requests[args.sequence :]))
            sent_s = time.perf_counter() - t0
            for txn in burst_ids:
                await wait_applied(redis, ns, txn, timeout_s=args.burst * args.delay_ms / 1000 + 60)
            report["burst"] = {
                "worker_delay_ms_per_event": args.delay_ms,
                "concurrency": args.concurrency,
                "send_elapsed_s": round(sent_s, 2),
                "drain_elapsed_s": round(time.perf_counter() - t0, 2),
                "http_status": dict(Counter(statuses)),
                **compare(workload, burst_ids, stored_rows(settings, burst_ids), model, policy),
            }
    finally:
        stack.stop_all()
        stack.delete_topics()
        await purge_namespace(redis, stack.stream.namespace)
        stack.delete_owned_rows()
        await redis.aclose()
    return report


def main_report(args: argparse.Namespace) -> int:
    report = asyncio.run(run(args))
    write_report(OUT / "demo.json", report)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["sequence"]["feature_vectors_different"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sequence", type=int, default=300)
    parser.add_argument("--burst", type=int, default=1000)
    parser.add_argument("--delay-ms", type=float, default=20.0)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--port", type=int, default=8110)
    return main_report(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
