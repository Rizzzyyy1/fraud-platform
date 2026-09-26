"""Redis feature state: hand-calculated features, parity with the in-memory state, isolation,
and bootstrap eligibility. Runs against the Compose Redis (database 15)."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest
from redis.asyncio import Redis

from fraudplat.features.compute import FeatureValue, compute_features
from fraudplat.features.offline import bootstrap_state, split_at_cutoff
from fraudplat.features.redis_state import RedisFeatureState, bootstrap_redis
from fraudplat.features.spec import DAY, RETENTION_HORIZON
from fraudplat.features.state import ApplyStatus, InMemoryFeatureState, Snapshot, TxnEvent

from ..unit.features.helpers import event, historical, us
from ..unit.features.test_windows_and_state import EXPECTED_X, HISTORY, X

pytestmark = pytest.mark.integration


def features(ev: TxnEvent, snap: Snapshot) -> dict[str, FeatureValue]:
    return compute_features(
        amount_minor=ev.amount_minor,
        terminal_id=ev.terminal_id,
        event_time_us=ev.event_time_us,
        snapshot=snap,
    )


def comparable(snap: Snapshot) -> tuple[object, ...]:
    """Everything features depend on, plus metadata. Terminal items are compared by time and id
    because the Redis read does not fetch terminal-history amounts (f1 does not use them)."""
    return (
        [
            (i.event_time_us, i.event_id, i.amount_minor, i.terminal_id)
            for i in snap.customer_history
        ],
        [(i.event_time_us, i.event_id) for i in snap.terminal_history],
        snap.customer_meta,
        snap.terminal_meta,
        snap.watermark_us,
        snap.history_complete,
    )


async def test_hand_calculated_features_through_redis(redis_client: Redis) -> None:
    state = RedisFeatureState(redis_client)
    for i, ev in enumerate([*HISTORY, X]):
        assert await state.apply(ev, applied_at_us=i) is ApplyStatus.APPLIED
    snap = await state.read(X.customer_id, X.terminal_id, X.event_time_us, X.event_id)
    assert features(X, snap) == EXPECTED_X


async def test_duplicate_and_conflict_through_redis(redis_client: Redis) -> None:
    state = RedisFeatureState(redis_client)
    for i, ev in enumerate([*HISTORY, X]):
        await state.apply(ev, applied_at_us=i)
    assert await state.apply(HISTORY[2], 99) is ApplyStatus.DUPLICATE
    tampered = event("e3", HISTORY[2].event_time_us, amount=201, terminal="T2")
    assert await state.apply(tampered, 100) is ApplyStatus.PAYLOAD_CONFLICT
    snap = await state.read(X.customer_id, X.terminal_id, X.event_time_us, X.event_id)
    assert features(X, snap) == EXPECTED_X


def random_stream(seed: int) -> list[TxnEvent]:
    """Events over ~45 days (to cross the 31-day horizon) with outbox-style source ids,
    identical duplicates, conflicting payloads, event-id conflicts in both directions, equal
    timestamps and shuffled arrival order."""
    rng = random.Random(seed)
    base = us("2025-03-01T00:00:00")
    events: list[TxnEvent] = []
    for i in range(250):
        t = base + rng.randrange(0, 45 * DAY) // 1_000_000 * 1_000_000  # whole seconds: ties
        ev = event(
            f"s{seed}-{i:03d}",
            t,
            customer=f"C{rng.randrange(4)}",
            terminal=f"T{rng.randrange(5)}",
            amount=rng.randrange(1, 10_000),
        )
        events.append(replace(ev, source_event_id=f"evt-{seed}-{i:03d}"))
    duplicates = rng.sample(events, 30)
    payload_conflicts = [
        replace(e, amount_minor=e.amount_minor + 1, payload_hash=e.payload_hash + "x")
        for e in rng.sample(events, 10)
    ]
    same_txn_new_source = [
        replace(e, source_event_id="evt-other-" + e.event_id) for e in rng.sample(events, 5)
    ]
    stolen_source = [
        replace(event(f"thief-{seed}-{k}", base + k * DAY), source_event_id=e.source_id)
        for k, e in enumerate(rng.sample(events, 5))
    ]
    stream = events + duplicates + payload_conflicts + same_txn_new_source + stolen_source
    rng.shuffle(stream)
    return stream


@pytest.mark.parametrize("seed", range(20))
async def test_redis_matches_in_memory_state(redis_client: Redis, seed: int) -> None:
    stream = random_stream(seed)
    memory = InMemoryFeatureState()
    redis_state = RedisFeatureState(redis_client, namespace=f"replay:parity{seed}")
    for i, ev in enumerate(stream):
        expected = memory.apply(ev, applied_at_us=i)
        got = await redis_state.apply(ev, applied_at_us=i)
        assert got is expected, f"apply #{i} ({ev.event_id}) diverged"

    rng = random.Random(seed + 100)
    probes = [ev for ev in stream if rng.random() < 0.3]
    probes.append(event("probe-cold", us("2025-04-10T12:00:00"), customer="C-new", terminal="T0"))
    for ev in probes:
        mem = memory.read(ev.customer_id, ev.terminal_id, ev.event_time_us, ev.event_id)
        red = await redis_state.read(ev.customer_id, ev.terminal_id, ev.event_time_us, ev.event_id)
        assert comparable(red) == comparable(mem)
        assert features(ev, red) == features(ev, mem)


async def test_replay_namespace_leaves_live_state_byte_identical(redis_client: Redis) -> None:
    live = RedisFeatureState(redis_client, namespace="live")
    for i, ev in enumerate(HISTORY):
        await live.apply(ev, i)
    keys = sorted(await redis_client.keys("live:*"))
    before = {k: await redis_client.dump(k) for k in keys}

    replay = RedisFeatureState(redis_client, namespace="replay:run1")
    for i, ev in enumerate([*random_stream(9), *HISTORY]):
        await replay.apply(ev, 1000 + i)

    assert sorted(await redis_client.keys("live:*")) == keys
    assert {k: await redis_client.dump(k) for k in keys} == before
    assert await redis_client.keys("replay:run1:*")  # the replay did write, elsewhere


async def test_bootstrap_loads_only_history_available_before_cutoff(redis_client: Redis) -> None:
    rng = random.Random(5)
    base = us("2025-01-01T00:00:00")
    txns = []
    for i in range(300):
        t = base + rng.randrange(0, 20 * DAY)
        delay = rng.choice([0, 0, 0, 2 * 3_600_000_000])  # some arrive two hours late
        txns.append(historical(event(f"b{i:03d}", t, customer=f"C{rng.randrange(6)}"), t + delay))
    cutoff = base + 15 * DAY
    split = split_at_cutoff(txns, cutoff)

    state = RedisFeatureState(redis_client)
    statuses = await bootstrap_redis(state, txns, cutoff)
    assert statuses == {ApplyStatus.APPLIED: len(split.bootstrap)}
    stored = {str(k).split(":", 2)[2] for k in await redis_client.keys("live:e:*")}
    assert stored == {t.event.event_id for t in split.bootstrap}
    assert not stored & {t.event.event_id for t in split.scoring}

    # Redis bootstrap equals in-memory bootstrap for every post-cutoff decision.
    memory = bootstrap_state(txns, cutoff)
    for probe in split.scoring:
        e = probe.event
        red = await state.read(e.customer_id, e.terminal_id, e.event_time_us, e.event_id)
        mem = memory.read(e.customer_id, e.terminal_id, e.event_time_us, e.event_id)
        assert comparable(red) == comparable(mem)


async def test_windowed_bootstrap_matches_full_bootstrap(redis_client: Redis) -> None:
    rng = random.Random(8)
    base = us("2025-01-01T00:00:00")
    txns = [
        historical(
            event(f"w{i:03d}", base + rng.randrange(0, 70 * DAY), customer=f"C{rng.randrange(5)}")
        )
        for i in range(400)
    ]
    cutoff = base + 60 * DAY
    full = RedisFeatureState(redis_client, namespace="replay:full")
    windowed = RedisFeatureState(redis_client, namespace="replay:windowed")
    await bootstrap_redis(full, txns, cutoff)
    await bootstrap_redis(windowed, txns, cutoff, history_start_us=cutoff - RETENTION_HORIZON - DAY)
    for probe in split_at_cutoff(txns, cutoff).scoring:
        e = probe.event
        a = await full.read(e.customer_id, e.terminal_id, e.event_time_us, e.event_id)
        b = await windowed.read(e.customer_id, e.terminal_id, e.event_time_us, e.event_id)
        assert features(e, a) == features(e, b)
