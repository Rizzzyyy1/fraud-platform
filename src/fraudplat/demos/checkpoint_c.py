"""Checkpoint C demonstration: one genuine ML decision through the API, then retries.

1. Bootstrap the Redis `live` namespace with sim-v2 history available before the demo cutoff
   (day 140, inside the validation period; the final test period is never read).
2. Pick the first transaction decided at or after the cutoff. Nothing is applied between the
   cutoff and its decision, so the live state equals the offline reconstruction for it.
3. POST it to the real app (PostgreSQL + Redis + model artifact), show the stored feature vector
   and versions, compare them with an independent in-memory reconstruction, then send an
   identical retry and a conflicting retry.

Run: `make demo-c` (requires `make up migrate data-v2 train`). `--reset` deletes this demo
transaction's rows from the local development database first so the sequence can be repeated.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import polars as pl
import psycopg
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis

from fraudplat.api.app import create_app
from fraudplat.config import Settings
from fraudplat.features.compute import compute_features
from fraudplat.features.offline import bootstrap_state, split_at_cutoff
from fraudplat.features.redis_state import RedisFeatureState, bootstrap_redis
from fraudplat.features.spec import DAY, RETENTION_HORIZON
from fraudplat.hashing import format_utc
from fraudplat.policy import load_policy
from fraudplat.simulator.dataset import to_historical
from fraudplat.training.model import load_model


def _single(directory: Path, pattern: str) -> Path:
    found = sorted(directory.glob(pattern))
    if len(found) != 1:
        raise SystemExit(f"expected exactly one {pattern} under {directory}, found {len(found)}")
    return found[0]


async def run(args: argparse.Namespace) -> int:
    settings = Settings()  # FRAUD_* from .env
    model_path = args.model or _single(Path("artifacts/models"), "*/model.json")
    model = load_model(model_path)
    policy_path = Path("artifacts/policies") / f"{model.model_version}.json"
    policy = load_policy(policy_path)
    manifest = json.loads((args.dataset / "manifest.json").read_text())
    print(
        f"model  {model.model_version}   policy {policy.version} "
        f"(review >= {policy.review_threshold:.4f}, decline >= {policy.decline_threshold})"
    )

    raw = pl.read_parquet(args.dataset / "transactions.parquet")
    day0 = int(raw["event_time"].dt.epoch("us").min())  # type: ignore[arg-type]
    day0 -= day0 % DAY
    test_start = day0 + args.test_start_day * DAY
    cutoff = day0 + args.cutoff_day * DAY
    pre_test = raw.filter(pl.col("received_at").dt.epoch("us") < test_start)
    txns = to_historical(pre_test)
    print(
        f"data   {manifest['dataset_id']} (synthetic)  rows before test period: {len(txns):,}"
        f"  cutoff = day {args.cutoff_day}"
    )

    assert settings.redis_url is not None, "FRAUD_REDIS_URL must be set"
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    stale = [k async for k in redis.scan_iter("live:*", count=10_000)]
    for start in range(0, len(stale), 10_000):
        await redis.delete(*stale[start : start + 10_000])
    state = RedisFeatureState(redis, "live")
    history_start = cutoff - RETENTION_HORIZON - DAY
    t0 = time.perf_counter()
    statuses = await bootstrap_redis(state, txns, cutoff, history_start_us=history_start)
    print(
        f"redis  bootstrap: {sum(statuses.values()):,} events available in "
        f"[cutoff - 32 d, cutoff) applied in {time.perf_counter() - t0:.1f}s "
        f"{ {k.value: v for k, v in statuses.items()} }"
    )

    subject = min(
        split_at_cutoff(txns, cutoff).scoring, key=lambda t: (t.decision_time_us, t.event.event_id)
    )
    e = subject.event
    row = raw.filter(pl.col("transaction_id") == e.event_id).row(0, named=True)
    payload: dict[str, Any] = {
        "transaction_id": e.event_id,
        "customer_id": e.customer_id,
        "terminal_id": e.terminal_id,
        "amount_minor": e.amount_minor,
        "currency": row["currency"],
        "event_time": format_utc(row["event_time"]),
    }

    memory = bootstrap_state(txns, cutoff, history_start_us=history_start)
    snap = memory.read(e.customer_id, e.terminal_id, e.event_time_us, e.event_id)
    offline = compute_features(
        amount_minor=e.amount_minor,
        terminal_id=e.terminal_id,
        event_time_us=e.event_time_us,
        snapshot=snap,
    )

    with psycopg.connect(settings.database_url.get_secret_value(), autocommit=True) as db:
        if args.reset:
            # Only this demo's own rows (outbox stream `live`, written by this demo). A decision
            # for the same transaction owned by another stream is left alone and reported.
            owner = db.execute(
                "SELECT stream FROM outbox WHERE aggregate_id = %s", (e.event_id,)
            ).fetchone()
            if owner is not None and owner[0] != "live":
                raise SystemExit(f"{e.event_id} is owned by stream {owner[0]}; not deleting it")
            if owner is not None:
                db.execute(
                    "DELETE FROM outbox WHERE aggregate_id = %s AND stream = 'live'", (e.event_id,)
                )
                db.execute("DELETE FROM decisions WHERE transaction_id = %s", (e.event_id,))
                print(f"reset  removed this demo's earlier rows for {e.event_id}")

        # This demo shows one decision without running the publisher/worker, so pipeline
        # monitoring is off here; with it on, the unpublished live backlog would (correctly)
        # mark the pipeline degraded or trigger the backlog limit. See `make demo-pipeline`.
        app = create_app(
            settings.model_copy(update={"model_path": model_path, "policy_path": policy_path}),
            monitor_pipeline=False,
        )
        print("note   pipeline monitoring disabled for this single-decision demo")
        key = settings.api_key.get_secret_value()
        async with (
            app.router.lifespan_context(app),
            AsyncClient(
                transport=ASGITransport(app=app), base_url="http://demo", headers={"X-API-Key": key}
            ) as http,
        ):
            print(f"\nready  {(await http.get('/readyz')).json()}")
            print(f"\nPOST   {json.dumps(payload)}")
            first = await http.post("/v1/decisions", json=payload)
            print(f"-> {first.status_code} {json.dumps(first.json(), indent=2)}")

            stored = db.execute(
                "SELECT features, feature_freshness, model_version, feature_version, "
                "policy_version, (SELECT count(*) FROM outbox WHERE aggregate_id = %s) "
                "FROM decisions WHERE transaction_id = %s",
                (e.event_id, e.event_id),
            ).fetchone()
            assert stored is not None
            features, freshness, mv, fv, pv, outbox = stored
            print("\nstored feature vector (as used):")
            for name in model.preprocessing.feature_names:
                print(f"  {name:28} {features[name]}")
            print(f"stored versions: model={mv} feature={fv} policy={pv}; outbox events={outbox}")
            fresh: dict[str, Any] = freshness
            print(
                f"freshness: watermark_us={fresh['watermark_us']} "
                f"history_complete={fresh['history_complete']} "
                f"customer_meta={fresh['customer_meta']}"
            )
            same = all(features[k] == v for k, v in offline.items())
            direct = model.score(offline)
            print(f"\nstored vector equals independent in-memory reconstruction: {same}")
            print(
                f"model score recomputed from that reconstruction: {direct!r} "
                f"(API returned {first.json()['score']!r})"
            )

            retry = await http.post("/v1/decisions", json=payload)

            def persisted(body: dict[str, Any]) -> dict[str, Any]:
                return {k: v for k, v in body.items() if k != "idempotent_replay"}

            identical = persisted(retry.json()) == persisted(first.json())
            print(
                f"\nidentical retry -> {retry.status_code} idempotent_replay="
                f"{retry.json()['idempotent_replay']} same persisted fields: {identical}"
            )
            conflict = await http.post(
                "/v1/decisions", json={**payload, "amount_minor": e.amount_minor + 1}
            )
            print(f"conflicting retry (amount + 1) -> {conflict.status_code} {conflict.json()}")
            counts = db.execute(
                "SELECT (SELECT count(*) FROM decisions WHERE transaction_id = %s), "
                "(SELECT count(*) FROM outbox WHERE aggregate_id = %s)",
                (e.event_id, e.event_id),
            ).fetchone()
            print(f"rows for {e.event_id}: decisions={counts[0]} outbox={counts[1]}")  # type: ignore[index]
    await redis.aclose()
    ok = (
        first.status_code == 201
        and same
        and retry.status_code == 200
        and conflict.status_code == 409
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=Path("data/raw/sim-v2"))
    parser.add_argument("--model", type=Path)
    parser.add_argument("--cutoff-day", type=int, default=140)
    parser.add_argument("--test-start-day", type=int, default=153)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    if not args.cutoff_day < args.test_start_day:
        raise SystemExit("the demo cutoff must be before the test period")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
