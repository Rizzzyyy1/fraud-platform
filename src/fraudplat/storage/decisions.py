"""Durable decision storage: the decision row and its outbox event commit in one transaction.

Idempotency is enforced by the database, not by application locks: the primary key on
`decisions.transaction_id` plus `INSERT ... ON CONFLICT DO NOTHING` guarantees one durable
decision per transaction id, and `UNIQUE (aggregate_id, event_type)` on the outbox guarantees
one `decision.made` event per decision.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from fraudplat.contracts import Action
from fraudplat.hashing import format_utc
from fraudplat.observability import STAGE

DECISION_EVENT_TYPE = "decision.made"
DECISION_EVENT_SCHEMA_VERSION = 1

_COLUMNS = """
    transaction_id, request_hash, hash_version, customer_id, terminal_id, amount_minor,
    currency, event_time, received_at, decision_time, persisted_at, score, action,
    reason_codes, features, feature_freshness, model_version, feature_version, policy_version,
    model_registry_ref
"""


class PersistenceUnavailable(Exception):
    """The database could not be reached, so durability is not confirmed.

    If the connection failed during COMMIT the decision may in fact be stored. Clients retry
    with the same payload; idempotency then returns the stored decision or creates it once.
    """


class PersistenceFailed(Exception):
    """The database rejected the write; the transaction was rolled back."""


@dataclass(frozen=True)
class NewDecision:
    transaction_id: str
    request_hash: bytes
    hash_version: int
    customer_id: str
    terminal_id: str
    amount_minor: int
    currency: str
    event_time: datetime
    received_at: datetime
    decision_time: datetime
    score: float | None
    action: Action
    reason_codes: tuple[str, ...]
    features: dict[str, Any]
    feature_freshness: dict[str, Any]
    model_version: str | None
    feature_version: str
    policy_version: str
    model_registry_ref: str | None = None


@dataclass(frozen=True)
class StoredDecision:
    transaction_id: str
    request_hash: bytes
    hash_version: int
    customer_id: str
    terminal_id: str
    amount_minor: int
    currency: str
    event_time: datetime
    received_at: datetime
    decision_time: datetime
    persisted_at: datetime
    score: float | None
    action: Action
    reason_codes: list[str]
    features: dict[str, Any]
    feature_freshness: dict[str, Any]
    model_version: str | None
    feature_version: str
    policy_version: str
    model_registry_ref: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> StoredDecision:
        return cls(**{**row, "request_hash": bytes(row["request_hash"])})


def decision_event_payload(decision: StoredDecision, event_id: uuid.UUID) -> dict[str, Any]:
    """Schema v1 of the `decision.made` event consumed by the feature worker."""
    return {
        "event_id": str(event_id),
        "event_type": DECISION_EVENT_TYPE,
        "schema_version": DECISION_EVENT_SCHEMA_VERSION,
        "transaction_id": decision.transaction_id,
        "payload_hash": decision.request_hash.hex(),
        "customer_id": decision.customer_id,
        "terminal_id": decision.terminal_id,
        "amount_minor": decision.amount_minor,
        "currency": decision.currency,
        "event_time": format_utc(decision.event_time),
        "received_at": format_utc(decision.received_at),
        "decision_time": format_utc(decision.decision_time),
        "persisted_at": format_utc(decision.persisted_at),
        "action": decision.action,
        "score": decision.score,
        "model_version": decision.model_version,
        "feature_version": decision.feature_version,
        "policy_version": decision.policy_version,
    }


class DecisionStore:
    def __init__(self, pool: AsyncConnectionPool[Any], stream: str = "live") -> None:
        self._pool = pool
        self._stream = stream

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        """A pooled connection; the wait for it is recorded as `db_pool_wait`."""
        started = time.perf_counter()
        async with self._pool.connection() as conn:
            STAGE.labels("db_pool_wait").observe(time.perf_counter() - started)
            yield conn

    async def outbox_backlog(self) -> tuple[int, datetime | None]:
        """Unpublished outbox rows for this stream and the creation time of the oldest."""
        try:
            async with self._connection() as conn:
                cur = await conn.execute(
                    "SELECT count(*), min(created_at) FROM outbox "
                    "WHERE published_at IS NULL AND stream = %s",
                    (self._stream,),
                )
                row = await cur.fetchone()
        except (PoolTimeout, psycopg.OperationalError) as exc:
            raise PersistenceUnavailable from exc
        assert row is not None
        return int(row[0]), row[1]

    async def ping(self) -> bool:
        try:
            async with self._connection() as conn:
                await conn.execute("SELECT 1")
            return True
        except (PoolTimeout, psycopg.Error):
            return False

    async def get(self, transaction_id: str) -> StoredDecision | None:
        try:
            async with self._connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"SELECT {_COLUMNS} FROM decisions WHERE transaction_id = %s",  # noqa: S608
                    (transaction_id,),
                )
                row = await cur.fetchone()
        except (PoolTimeout, psycopg.OperationalError) as exc:
            raise PersistenceUnavailable from exc
        return StoredDecision.from_row(row) if row else None

    async def insert_with_outbox(self, new: NewDecision) -> tuple[StoredDecision, bool]:
        """Insert the decision and its outbox event atomically.

        Returns (stored decision, created). When another request for the same transaction id
        committed first, `created` is False and the other request's decision is returned;
        the caller compares request hashes. Under READ COMMITTED, the conflicting insert
        waits for the competing transaction and then sees its committed row.
        """
        try:
            async with (
                self._connection() as conn,
                conn.transaction(),
                conn.cursor(row_factory=dict_row) as cur,
            ):
                await cur.execute(
                    f"""
                    INSERT INTO decisions (
                        transaction_id, request_hash, hash_version, customer_id, terminal_id,
                        amount_minor, currency, event_time, received_at, decision_time, score,
                        action, reason_codes, features, feature_freshness, model_version,
                        feature_version, policy_version, model_registry_ref
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (transaction_id) DO NOTHING
                    RETURNING {_COLUMNS}
                    """,  # noqa: S608
                    (
                        new.transaction_id,
                        new.request_hash,
                        new.hash_version,
                        new.customer_id,
                        new.terminal_id,
                        new.amount_minor,
                        new.currency,
                        new.event_time,
                        new.received_at,
                        new.decision_time,
                        new.score,
                        new.action,
                        list(new.reason_codes),
                        Jsonb(new.features),
                        Jsonb(new.feature_freshness),
                        new.model_version,
                        new.feature_version,
                        new.policy_version,
                        new.model_registry_ref,
                    ),
                )
                row = await cur.fetchone()
                if row is None:
                    await cur.execute(
                        f"SELECT {_COLUMNS} FROM decisions WHERE transaction_id = %s",  # noqa: S608
                        (new.transaction_id,),
                    )
                    existing = cast(dict[str, Any], await cur.fetchone())
                    return StoredDecision.from_row(existing), False

                stored = StoredDecision.from_row(row)
                event_id = uuid.uuid4()
                await cur.execute(
                    """
                    INSERT INTO outbox
                        (event_id, aggregate_id, event_type, schema_version, payload, stream)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event_id,
                        stored.transaction_id,
                        DECISION_EVENT_TYPE,
                        DECISION_EVENT_SCHEMA_VERSION,
                        Jsonb(decision_event_payload(stored, event_id)),
                        self._stream,
                    ),
                )
                return stored, True
        except (PoolTimeout, psycopg.OperationalError) as exc:
            raise PersistenceUnavailable from exc
        except psycopg.Error as exc:
            raise PersistenceFailed from exc
