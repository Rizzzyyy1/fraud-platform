"""Redis-backed behavioural state with the same semantics as `InMemoryFeatureState`.

Key layout (namespace `ns` is `live` or `replay:<run_id>`; namespaces never share keys):

    {ns}:e:{event_id}     hash    payload_hash, customer_id, terminal_id, amount_minor,
                                  event_time_us, source_event_id, applied_at_us
                                  (the event record and its dedup record; event_id is the
                                  transaction id)
    {ns}:x:{source_id}    string  transaction id bound to a source (outbox) event id
    {ns}:c:{customer_id}  zset    member event_id, score event_time_us
    {ns}:t:{terminal_id}  zset    member event_id, score event_time_us
    {ns}:mc:{customer_id} hash    applied_count, last_applied_at_us, last_event_id
    {ns}:mt:{terminal_id} hash    same, per terminal
    {ns}:wm               string  newest applied event_time_us (watermark)

`APPLY` performs the horizon check, dedup, both zset inserts, metadata, watermark and trimming in
one Lua script, so a customer and a terminal are updated atomically. `READ` returns every value one
decision needs in one script, so feature values come from a single consistent snapshot. Scripts
build event keys from a prefix, which is valid on single-node Redis only (not Redis Cluster).

Numbers: microsecond timestamps (< 2^53) are exact as Lua doubles and Redis scores; values are
passed and stored as decimal strings and formatted with %.0f, never tostring().

Difference from the in-memory state: dedup records are deleted when their customer's history is
trimmed (on that customer's next update), while the in-memory state sweeps them periodically.
Both check the horizon before dedup, so this changes no apply status and no feature (tested).
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Sequence
from typing import Any

from redis.asyncio import Redis

from fraudplat.features.offline import HistoricalTxn, available_at_us, split_at_cutoff
from fraudplat.features.spec import MAX_WINDOW, RETENTION_HORIZON
from fraudplat.features.state import (
    ApplyStatus,
    EntityMeta,
    HistoryItem,
    Snapshot,
    TxnEvent,
)
from fraudplat.observability import STAGE

APPLY_LUA = """
local t = tonumber(ARGV[6])
local retention = tonumber(ARGV[8])
local wm = redis.call('GET', KEYS[6])
if wm and t < tonumber(wm) - retention then return 'late_beyond_horizon' end
local horizon_start = nil
if wm then horizon_start = tonumber(wm) - retention end

-- Identity records count only while their transaction is inside the horizon; stale ones are
-- forgotten here, however lazily trimming would have removed them.
local function forget(txn)
  local rec = redis.call('HMGET', ARGV[9] .. txn, 'source_event_id', 'customer_id', 'terminal_id')
  if rec[1] then
    if redis.call('GET', ARGV[11] .. rec[1]) == txn then redis.call('DEL', ARGV[11] .. rec[1]) end
    redis.call('ZREM', ARGV[12] .. rec[2], txn)
    redis.call('ZREM', ARGV[13] .. rec[3], txn)
  end
  redis.call('DEL', ARGV[9] .. txn)
end
local function stale(time_str)
  return horizon_start ~= nil and tonumber(time_str) < horizon_start
end

local existing = redis.call('HMGET', KEYS[1], 'payload_hash', 'source_event_id', 'event_time_us')
if existing[1] and stale(existing[3]) then
  forget(ARGV[1])
  existing = {false, false, false}
end
if existing[1] then
  if existing[1] ~= ARGV[2] then return 'payload_conflict' end
  if existing[2] ~= ARGV[10] then return 'event_id_conflict' end
  return 'duplicate'
end
local owner = redis.call('GET', KEYS[7])
if owner and owner ~= ARGV[1] then
  local owner_time = redis.call('HGET', ARGV[9] .. owner, 'event_time_us')
  if owner_time and not stale(owner_time) then return 'event_id_conflict' end
  if owner_time then forget(owner) end
  redis.call('DEL', KEYS[7])
end

redis.call('HSET', KEYS[1], 'payload_hash', ARGV[2], 'customer_id', ARGV[3],
           'terminal_id', ARGV[4], 'amount_minor', ARGV[5], 'event_time_us', ARGV[6],
           'source_event_id', ARGV[10], 'applied_at_us', ARGV[7])
redis.call('SET', KEYS[7], ARGV[1])
redis.call('ZADD', KEYS[2], ARGV[6], ARGV[1])
redis.call('ZADD', KEYS[3], ARGV[6], ARGV[1])
for _, meta in ipairs({KEYS[4], KEYS[5]}) do
  redis.call('HINCRBY', meta, 'applied_count', 1)
  redis.call('HSET', meta, 'last_applied_at_us', ARGV[7], 'last_event_id', ARGV[1])
end

local newest = t
if wm and tonumber(wm) > t then
  newest = tonumber(wm)
else
  redis.call('SET', KEYS[6], ARGV[6])
end
local horizon = '(' .. string.format('%.0f', newest - retention)
for _, old_id in ipairs(redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', horizon)) do
  local src = redis.call('HGET', ARGV[9] .. old_id, 'source_event_id')
  if src then redis.call('DEL', ARGV[11] .. src) end
  redis.call('DEL', ARGV[9] .. old_id)
end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', horizon)
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', horizon)
return 'applied'
"""

READ_LUA = """
local upper = '(' .. ARGV[2]
local cust = redis.call('ZRANGEBYSCORE', KEYS[1], ARGV[1], upper, 'WITHSCORES')
local details = {}
for i = 1, #cust, 2 do
  local d = redis.call('HMGET', ARGV[3] .. cust[i], 'amount_minor', 'terminal_id')
  if not d[1] then return redis.error_reply('missing event record ' .. cust[i]) end
  details[#details + 1] = d[1]
  details[#details + 1] = d[2]
end
local term = redis.call('ZRANGEBYSCORE', KEYS[2], ARGV[1], upper, 'WITHSCORES')
return {cust, details, term, redis.call('HGETALL', KEYS[3]), redis.call('HGETALL', KEYS[4]),
        redis.call('GET', KEYS[5]) or ''}
"""


def _meta(flat: list[str]) -> EntityMeta | None:
    if not flat:
        return None
    fields = dict(zip(flat[::2], flat[1::2], strict=True))
    return EntityMeta(
        applied_count=int(fields["applied_count"]),
        last_applied_at_us=int(fields["last_applied_at_us"]),
        last_event_id=fields["last_event_id"],
    )


def _score(value: str) -> int:
    return int(float(value))  # exact: integer-valued doubles below 2^53


class RedisFeatureState:
    def __init__(
        self, client: Redis, namespace: str = "live", retention_us: int = RETENTION_HORIZON
    ) -> None:
        if retention_us < MAX_WINDOW:
            raise ValueError("retention must cover the largest feature window")
        if not namespace or ":" in namespace.replace("replay:", "", 1):
            raise ValueError("namespace must be 'live' or 'replay:<run_id>' without extra ':'")
        self._client = client
        self._ns = namespace
        self._retention_us = retention_us
        self._apply = client.register_script(APPLY_LUA)
        self._read = client.register_script(READ_LUA)

    @property
    def namespace(self) -> str:
        return self._ns

    def _apply_keys(self, event: TxnEvent) -> list[str]:
        ns = self._ns
        return [
            f"{ns}:e:{event.event_id}",
            f"{ns}:c:{event.customer_id}",
            f"{ns}:t:{event.terminal_id}",
            f"{ns}:mc:{event.customer_id}",
            f"{ns}:mt:{event.terminal_id}",
            f"{ns}:wm",
            f"{ns}:x:{event.source_id}",
        ]

    def _apply_args(self, event: TxnEvent, applied_at_us: int) -> list[str]:
        return [
            event.event_id,
            event.payload_hash,
            event.customer_id,
            event.terminal_id,
            str(event.amount_minor),
            str(event.event_time_us),
            str(applied_at_us),
            str(self._retention_us),
            f"{self._ns}:e:",
            event.source_id,
            f"{self._ns}:x:",
            f"{self._ns}:c:",
            f"{self._ns}:t:",
        ]

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def apply(self, event: TxnEvent, applied_at_us: int) -> ApplyStatus:
        result = await self._apply(
            keys=self._apply_keys(event), args=self._apply_args(event, applied_at_us)
        )
        return ApplyStatus(result)

    async def apply_many(
        self, events: Sequence[tuple[TxnEvent, int]], batch_size: int = 2000
    ) -> Counter[ApplyStatus]:
        """Apply (event, applied_at_us) pairs in order, pipelined in batches."""
        statuses: Counter[ApplyStatus] = Counter()
        for start in range(0, len(events), batch_size):
            async with self._client.pipeline(transaction=False) as pipe:
                for event, applied_at in events[start : start + batch_size]:
                    await self._apply(
                        keys=self._apply_keys(event),
                        args=self._apply_args(event, applied_at),
                        client=pipe,
                    )
                for result in await pipe.execute():
                    statuses[ApplyStatus(result)] += 1
        return statuses

    async def read(
        self, customer_id: str, terminal_id: str, event_time_us: int, exclude_event_id: str
    ) -> Snapshot:
        ns = self._ns
        start = event_time_us - MAX_WINDOW
        started = time.perf_counter()
        raw: list[Any] = await self._read(
            keys=[
                f"{ns}:c:{customer_id}",
                f"{ns}:t:{terminal_id}",
                f"{ns}:mc:{customer_id}",
                f"{ns}:mt:{terminal_id}",
                f"{ns}:wm",
            ],
            args=[str(start), str(event_time_us), f"{ns}:e:"],
        )
        STAGE.labels("redis_call").observe(time.perf_counter() - started)
        cust, details, term, cust_meta, term_meta, watermark = raw
        customer_history = tuple(
            HistoryItem(
                event_time_us=_score(cust[i + 1]),
                event_id=cust[i],
                amount_minor=int(details[i]),
                terminal_id=details[i + 1],
            )
            for i in range(0, len(cust), 2)
            if cust[i] != exclude_event_id
        )
        # Terminal history needs only times and ids; amounts/terminal are not used by f1.
        terminal_history = tuple(
            HistoryItem(_score(term[i + 1]), term[i], 0, terminal_id)
            for i in range(0, len(term), 2)
            if term[i] != exclude_event_id
        )
        watermark_us = int(watermark) if watermark else None
        horizon = None if watermark_us is None else watermark_us - self._retention_us
        return Snapshot(
            customer_history=customer_history,
            terminal_history=terminal_history,
            customer_meta=_meta(cust_meta),
            terminal_meta=_meta(term_meta),
            watermark_us=watermark_us,
            history_complete=horizon is None or start >= horizon,
        )


async def bootstrap_redis(
    state: RedisFeatureState,
    txns: Sequence[HistoricalTxn],
    cutoff_us: int,
    history_start_us: int | None = None,
) -> Counter[ApplyStatus]:
    """Load history available strictly before `cutoff_us`, in arrival order.

    `history_start_us` skips events available earlier than it. Events older than the retention
    horizon at the cutoff can never be visible to decisions at or after the cutoff, so a start
    of `cutoff - RETENTION_HORIZON - margin` yields identical features (tested in memory);
    per-entity `applied_count` metadata then counts only the loaded window.
    """
    eligible = split_at_cutoff(txns, cutoff_us).bootstrap
    ordered = sorted(
        (
            t
            for t in eligible
            if history_start_us is None or available_at_us(t, 0) >= history_start_us
        ),
        key=lambda t: (available_at_us(t, 0), t.event.event_id),
    )
    return await state.apply_many([(t.event, available_at_us(t, 0)) for t in ordered])
