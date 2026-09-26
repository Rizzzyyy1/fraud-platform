"""In-memory behavioural state with the same update and read semantics Redis will implement.

Update rules (see docs/DESIGN.md §5):
* Each event is stored once per entity, keyed by (event_time, event_id). Aggregates are derived
  at read time, so applying a set of in-horizon events in any order yields the same state.
* A repeated event_id with the same payload hash and source id is a no-op (DUPLICATE); with a
  different payload hash it is a no-op reported as PAYLOAD_CONFLICT; with the same payload but a
  different source id, or a source id already bound to another transaction, EVENT_ID_CONFLICT.
* Events older than `watermark - retention` are rejected (LATE_BEYOND_HORIZON) *before* the
  dedup check, so an out-of-horizon duplicate or conflict is reported as late. The watermark is
  the newest applied event time, so this rule depends on arrival order near the boundary.
* Dedup records are kept at least as long as the retention horizon; because the horizon is
  checked first, when an implementation drops older records cannot change any status.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from enum import StrEnum

from fraudplat.features.spec import MAX_WINDOW, RETENTION_HORIZON


class ApplyStatus(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    PAYLOAD_CONFLICT = "payload_conflict"
    LATE_BEYOND_HORIZON = "late_beyond_horizon"
    EVENT_ID_CONFLICT = "event_id_conflict"


@dataclass(frozen=True, slots=True)
class TxnEvent:
    """One transaction as feature history.

    `event_id` is the transaction id: history, windows and dedup are keyed by it, so a
    transaction enters each window at most once. `source_event_id` is the id of the message that
    carried it (the outbox event id); when absent (historical bootstrap) it equals `event_id`.
    One transaction must always arrive under one source id, and vice versa.
    """

    event_id: str
    customer_id: str
    terminal_id: str
    amount_minor: int
    event_time_us: int
    payload_hash: str
    source_event_id: str | None = None

    @property
    def source_id(self) -> str:
        return self.source_event_id or self.event_id


@dataclass(frozen=True, slots=True)
class HistoryItem:
    event_time_us: int
    event_id: str
    amount_minor: int
    terminal_id: str


@dataclass(frozen=True, slots=True)
class EntityMeta:
    """Feature-state update metadata. Says when state last changed, not whether it is complete."""

    applied_count: int
    last_applied_at_us: int
    last_event_id: str


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything visible for one decision, read at one instant.

    `customer_history` and `terminal_history` hold events with
    t - MAX_WINDOW <= event_time < t, excluding the decision's own event id, sorted by time.
    `history_complete` is False when retention may already have trimmed part of that range.
    """

    customer_history: tuple[HistoryItem, ...]
    terminal_history: tuple[HistoryItem, ...]
    customer_meta: EntityMeta | None
    terminal_meta: EntityMeta | None
    watermark_us: int | None
    history_complete: bool


@dataclass(slots=True)
class _History:
    keys: list[tuple[int, str]] = field(default_factory=list)
    items: dict[str, HistoryItem] = field(default_factory=dict)

    def add(self, item: HistoryItem) -> None:
        bisect.insort(self.keys, (item.event_time_us, item.event_id))
        self.items[item.event_id] = item

    def remove(self, event_time_us: int, event_id: str) -> None:
        key = (event_time_us, event_id)
        index = bisect.bisect_left(self.keys, key)
        if index < len(self.keys) and self.keys[index] == key:
            del self.keys[index]
            del self.items[event_id]

    def trim_before(self, cutoff_us: int) -> None:
        index = bisect.bisect_left(self.keys, (cutoff_us, ""))
        for _, event_id in self.keys[:index]:
            del self.items[event_id]
        del self.keys[:index]

    def window(self, start_us: int, end_us: int, exclude_id: str) -> tuple[HistoryItem, ...]:
        lo = bisect.bisect_left(self.keys, (start_us, ""))
        hi = bisect.bisect_left(self.keys, (end_us, ""))
        return tuple(
            self.items[event_id] for _, event_id in self.keys[lo:hi] if event_id != exclude_id
        )


class InMemoryFeatureState:
    def __init__(self, retention_us: int = RETENTION_HORIZON) -> None:
        if retention_us < MAX_WINDOW:
            raise ValueError("retention must cover the largest feature window")
        self._retention_us = retention_us
        self._customers: dict[str, _History] = {}
        self._terminals: dict[str, _History] = {}
        self._customer_meta: dict[str, EntityMeta] = {}
        self._terminal_meta: dict[str, EntityMeta] = {}
        # event_id -> (payload_hash, event_time, source_id, customer_id, terminal_id)
        # source_id -> event_id
        self._dedup: dict[str, tuple[str, int, str, str, str]] = {}
        self._sources: dict[str, str] = {}
        self._watermark_us: int | None = None
        self._applies_since_sweep = 0

    @property
    def watermark_us(self) -> int | None:
        return self._watermark_us

    def _horizon_start(self) -> int | None:
        return None if self._watermark_us is None else self._watermark_us - self._retention_us

    def apply(self, event: TxnEvent, applied_at_us: int) -> ApplyStatus:
        # Horizon first: an event older than the horizon is LATE whether or not a dedup record
        # still exists, so the status does not depend on when dedup records are dropped.
        horizon = self._horizon_start()
        if horizon is not None and event.event_time_us < horizon:
            return ApplyStatus.LATE_BEYOND_HORIZON
        # Identity records (dedup record, source binding) count only while their transaction is
        # inside the horizon; stale ones are forgotten, however lazily they would be cleaned up.
        seen = self._dedup.get(event.event_id)
        if seen is not None and horizon is not None and seen[1] < horizon:
            self._forget(event.event_id)
            seen = None
        if seen is not None:
            if seen[0] != event.payload_hash:
                return ApplyStatus.PAYLOAD_CONFLICT
            if seen[2] != event.source_id:
                return ApplyStatus.EVENT_ID_CONFLICT
            return ApplyStatus.DUPLICATE
        owner = self._sources.get(event.source_id)
        if owner is not None and owner != event.event_id:
            record = self._dedup.get(owner)
            if record is not None and (horizon is None or record[1] >= horizon):
                return ApplyStatus.EVENT_ID_CONFLICT
            self._forget(owner)
            self._sources.pop(event.source_id, None)

        item = HistoryItem(
            event.event_time_us, event.event_id, event.amount_minor, event.terminal_id
        )
        customer = self._customers.setdefault(event.customer_id, _History())
        terminal = self._terminals.setdefault(event.terminal_id, _History())
        customer.add(item)
        terminal.add(item)
        self._dedup[event.event_id] = (
            event.payload_hash,
            event.event_time_us,
            event.source_id,
            event.customer_id,
            event.terminal_id,
        )
        self._sources[event.source_id] = event.event_id
        for metas, key in (
            (self._customer_meta, event.customer_id),
            (self._terminal_meta, event.terminal_id),
        ):
            previous = metas.get(key)
            metas[key] = EntityMeta(
                applied_count=(previous.applied_count if previous else 0) + 1,
                last_applied_at_us=applied_at_us,
                last_event_id=event.event_id,
            )
        if self._watermark_us is None or event.event_time_us > self._watermark_us:
            self._watermark_us = event.event_time_us

        horizon = self._horizon_start()
        assert horizon is not None
        customer.trim_before(horizon)
        terminal.trim_before(horizon)
        self._applies_since_sweep += 1
        if self._applies_since_sweep >= 10_000:
            self._sweep_dedup(horizon)
        return ApplyStatus.APPLIED

    def _forget(self, event_id: str) -> None:
        record = self._dedup.pop(event_id, None)
        if record is None:
            return
        _, event_time, source, customer, terminal = record
        if self._sources.get(source) == event_id:
            del self._sources[source]
        for histories, key in ((self._customers, customer), (self._terminals, terminal)):
            if key in histories:
                histories[key].remove(event_time, event_id)

    def _sweep_dedup(self, horizon: int) -> None:
        # Keep dedup records for the full horizon; dropping later than required is safe.
        self._dedup = {k: v for k, v in self._dedup.items() if v[1] >= horizon}
        self._sources = {v[2]: k for k, v in self._dedup.items()}
        self._applies_since_sweep = 0

    def read(
        self, customer_id: str, terminal_id: str, event_time_us: int, exclude_event_id: str
    ) -> Snapshot:
        start = event_time_us - MAX_WINDOW
        empty = _History()
        horizon = self._horizon_start()
        return Snapshot(
            customer_history=self._customers.get(customer_id, empty).window(
                start, event_time_us, exclude_event_id
            ),
            terminal_history=self._terminals.get(terminal_id, empty).window(
                start, event_time_us, exclude_event_id
            ),
            customer_meta=self._customer_meta.get(customer_id),
            terminal_meta=self._terminal_meta.get(terminal_id),
            watermark_us=self._watermark_us,
            history_complete=horizon is None or start >= horizon,
        )
