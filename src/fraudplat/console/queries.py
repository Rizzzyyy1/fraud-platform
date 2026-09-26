"""Read queries and review writes for the console, scoped to one deployment namespace.

Every list query is bounded: a time range of at most 24 hours on the `decision_time` index,
keyset pagination (no OFFSET), and a page size of at most 100. The console uses its own small
connection pool with a statement timeout, separate from the scoring API's pool.

Reviews are appended to `reviews`; `decisions` is only ever read here. A row's namespace is the
`stream` of its `decision.made` outbox event.
"""

from __future__ import annotations

import base64
import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

RANGES = {
    "15m": (timedelta(minutes=15), timedelta(seconds=30)),
    "1h": (timedelta(hours=1), timedelta(minutes=1)),
    "6h": (timedelta(hours=6), timedelta(minutes=5)),
    "24h": (timedelta(hours=24), timedelta(minutes=15)),
}
MAX_PAGE = 100
DEGRADED_CODES = [
    "PIPELINE_DEGRADED",
    "FEATURES_UNAVAILABLE",
    "PIPELINE_STALE_BEYOND_LIMIT",
    "PIPELINE_UNKNOWN_BEYOND_LIMIT",
]
ReviewStatus = Literal["open", "in_review", "closed"]
Disposition = Literal["suspected_fraud", "likely_legitimate", "needs_more_information"]

_IN_NAMESPACE = """
    EXISTS (SELECT 1 FROM outbox o WHERE o.aggregate_id = d.transaction_id
            AND o.event_type = 'decision.made' AND o.stream = %(ns)s)
"""
_LATEST_REVIEW = """
    LEFT JOIN LATERAL (
        SELECT r.status, r.disposition, r.analyst, r.created_at
        FROM reviews r WHERE r.transaction_id = d.transaction_id
        ORDER BY r.id DESC LIMIT 1
    ) lr ON true
"""


class NotFound(Exception):
    pass


class InvalidReview(ValueError):
    pass


def encode_cursor(values: list[Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(values, default=str).encode()).decode()


MAX_CURSOR_CHARS = 512


class InvalidCursor(ValueError):
    """A pagination cursor that is not one this service issued. Always a client error (400)."""


def _cursor_values(cursor: str) -> list[Any]:
    if len(cursor) > MAX_CURSOR_CHARS:
        raise InvalidCursor("invalid cursor")
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, TypeError) as exc:  # bad base64, bad UTF-8, bad JSON
        raise InvalidCursor("invalid cursor") from exc
    if not isinstance(values, list):
        raise InvalidCursor("invalid cursor")
    return values


def _cursor_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise InvalidCursor("invalid cursor")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidCursor("invalid cursor") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidCursor("invalid cursor")
    return parsed


def _cursor_id(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise InvalidCursor("invalid cursor")
    return value


def decode_decisions_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    """`[decision_time (ISO 8601 with offset), transaction_id]`."""
    if not cursor:
        return None
    values = _cursor_values(cursor)
    if len(values) != 2:
        raise InvalidCursor("invalid cursor")
    return _cursor_time(values[0]), _cursor_id(values[1])


def decode_queue_cursor(cursor: str | None) -> tuple[float, datetime, str] | None:
    """`[priority (finite number), decision_time (ISO 8601 with offset), transaction_id]`."""
    if not cursor:
        return None
    values = _cursor_values(cursor)
    if len(values) != 3:
        raise InvalidCursor("invalid cursor")
    priority = values[0]
    # bool is an int subclass; JSON also admits NaN and Infinity tokens.
    if isinstance(priority, bool) or not isinstance(priority, int | float):
        raise InvalidCursor("invalid cursor")
    if not math.isfinite(priority):
        raise InvalidCursor("invalid cursor")
    return float(priority), _cursor_time(values[1]), _cursor_id(values[2])


def _row_summary(row: dict[str, Any]) -> dict[str, Any]:
    codes = list(row["reason_codes"])
    return {
        "transaction_id": row["transaction_id"],
        "decision_time": row["decision_time"],
        "amount_minor": row["amount_minor"],
        "currency": row["currency"],
        "score": row["score"],
        "action": row["action"],
        "reason_codes": codes,
        "degraded": row["score"] is None or any(c in DEGRADED_CODES for c in codes),
        "scoreless": row["score"] is None,
        "review_status": row.get("review_status")
        or ("open" if row["action"] == "review" else None),
    }


class ConsoleStore:
    def __init__(self, pool: AsyncConnectionPool[Any], namespace: str) -> None:
        self.pool = pool
        self.namespace = namespace

    async def decisions(
        self,
        range_key: str,
        action: str | None = None,
        scoring: str | None = None,
        degraded: bool | None = None,
        cursor: str | None = None,
        limit: int = 50,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        span, _ = RANGES[range_key]
        now = now or datetime.now(UTC)
        params: dict[str, Any] = {
            "ns": self.namespace,
            "since": now - span,
            "limit": min(max(limit, 1), MAX_PAGE) + 1,
            "codes": DEGRADED_CODES,
        }
        where = ["d.decision_time >= %(since)s", _IN_NAMESPACE]
        if action is not None:
            where.append("d.action = %(action)s")
            params["action"] = action
        if scoring == "scored":
            where.append("d.score IS NOT NULL")
        elif scoring == "scoreless":
            where.append("d.score IS NULL")
        if degraded is True:
            where.append("(d.score IS NULL OR d.reason_codes && %(codes)s::text[])")
        elif degraded is False:
            where.append("(d.score IS NOT NULL AND NOT d.reason_codes && %(codes)s::text[])")
        after = decode_decisions_cursor(cursor)
        if after is not None:
            where.append("(d.decision_time, d.transaction_id) < (%(c_time)s, %(c_id)s)")
            params["c_time"], params["c_id"] = after
        sql = f"""
            SELECT d.transaction_id, d.decision_time, d.amount_minor, d.currency, d.score,
                   d.action, d.reason_codes, lr.status AS review_status
            FROM decisions d {_LATEST_REVIEW}
            WHERE {" AND ".join(where)}
            ORDER BY d.decision_time DESC, d.transaction_id DESC
            LIMIT %(limit)s
        """  # noqa: S608 - fixed fragments; values are bound parameters
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
        page, more = rows[: params["limit"] - 1], len(rows) >= params["limit"]
        return {
            "items": [_row_summary(r) for r in page],
            "next_cursor": (
                encode_cursor([page[-1]["decision_time"].isoformat(), page[-1]["transaction_id"]])
                if more and page
                else None
            ),
            "range": range_key,
            "since": params["since"],
            "until": now,
        }

    async def activity(self, range_key: str, now: datetime | None = None) -> dict[str, Any]:
        span, bucket = RANGES[range_key]
        now = now or datetime.now(UTC)
        sql = f"""
            SELECT date_bin(%(bucket)s, d.decision_time, %(origin)s) AS bucket,
                   count(*) AS total,
                   count(*) FILTER (WHERE d.action = 'approve') AS approve,
                   count(*) FILTER (WHERE d.action = 'review') AS review,
                   count(*) FILTER (WHERE d.action = 'decline') AS decline,
                   count(*) FILTER (WHERE d.score IS NULL) AS scoreless,
                   count(*) FILTER (WHERE d.score IS NULL
                                    OR d.reason_codes && %(codes)s::text[]) AS degraded
            FROM decisions d
            WHERE d.decision_time >= %(since)s AND {_IN_NAMESPACE}
            GROUP BY 1 ORDER BY 1
        """  # noqa: S608 - fixed fragments
        params = {
            "bucket": bucket,
            "origin": datetime(2000, 1, 1, tzinfo=UTC),
            "since": now - span,
            "ns": self.namespace,
            "codes": DEGRADED_CODES,
        }
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
        totals = {
            k: sum(int(r[k]) for r in rows)
            for k in ("total", "approve", "review", "decline", "scoreless", "degraded")
        }
        return {
            "range": range_key,
            "bucket_seconds": int(bucket.total_seconds()),
            "since": params["since"],
            "until": now,
            "buckets": rows,
            "totals": totals,
        }

    async def decision(self, transaction_id: str) -> dict[str, Any]:
        sql = """
            SELECT d.*, o.published_at, o.created_at AS outbox_created_at
            FROM decisions d
            JOIN outbox o ON o.aggregate_id = d.transaction_id AND o.event_type = 'decision.made'
            WHERE d.transaction_id = %(id)s AND o.stream = %(ns)s
        """
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, {"id": transaction_id, "ns": self.namespace})
            row = await cur.fetchone()
            if row is None:
                raise NotFound(transaction_id)
            await cur.execute(
                "SELECT id, status, disposition, note, analyst, created_at FROM reviews "
                "WHERE transaction_id = %s ORDER BY id",
                (transaction_id,),
            )
            reviews = await cur.fetchall()
        row.pop("request_hash", None)
        return {
            "decision": {**row, "reason_codes": list(row["reason_codes"])},
            "reviews": reviews,
        }

    async def queue(
        self,
        status: ReviewStatus = "open",
        scoring: str | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Decisions with action `review`, ordered by score (highest first; scoreless last),
        then newest first. A decision without a review record is `open`."""
        params: dict[str, Any] = {
            "ns": self.namespace,
            "status": status,
            "limit": min(max(limit, 1), MAX_PAGE) + 1,
        }
        where = ["q.status = %(status)s"]
        if scoring == "scored":
            where.append("q.score IS NOT NULL")
        elif scoring == "scoreless":
            where.append("q.score IS NULL")
        after = decode_queue_cursor(cursor)
        if after is not None:
            where.append("(q.priority, q.decision_time, q.transaction_id) < (%(p)s, %(t)s, %(i)s)")
            params["p"], params["t"], params["i"] = after
        sql = f"""
            WITH q AS (
                SELECT d.transaction_id, d.decision_time, d.amount_minor, d.currency, d.score,
                       d.action, d.reason_codes, coalesce(d.score, -1.0) AS priority,
                       coalesce(lr.status, 'open') AS status, lr.status AS review_status,
                       lr.disposition, lr.analyst, lr.created_at AS reviewed_at
                FROM decisions d {_LATEST_REVIEW}
                WHERE d.action = 'review' AND {_IN_NAMESPACE}
            )
            SELECT *, (SELECT json_object_agg(s, n) FROM
                         (SELECT status AS s, count(*) AS n FROM q GROUP BY status) c) AS counts
            FROM q WHERE {" AND ".join(where)}
            ORDER BY q.priority DESC, q.decision_time DESC, q.transaction_id DESC
            LIMIT %(limit)s
        """  # noqa: S608
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            counts: dict[str, int] = rows[0]["counts"] if rows else {}
            if not rows:
                await cur.execute(
                    f"""SELECT coalesce(lr.status, 'open') AS s, count(*) AS n
                        FROM decisions d {_LATEST_REVIEW}
                        WHERE d.action = 'review' AND {_IN_NAMESPACE} GROUP BY 1""",  # noqa: S608
                    params,
                )
                counts = {r["s"]: r["n"] for r in await cur.fetchall()}
        page, more = rows[: params["limit"] - 1], len(rows) >= params["limit"]
        items = [
            {
                **_row_summary(r),
                "review_status": r["status"],
                "disposition": r["disposition"],
                "last_analyst": r["analyst"],
                "reviewed_at": r["reviewed_at"],
            }
            for r in page
        ]
        return {
            "items": items,
            "counts": {s: int(counts.get(s, 0)) for s in ("open", "in_review", "closed")},
            "next_cursor": (
                encode_cursor(
                    [
                        page[-1]["priority"],
                        page[-1]["decision_time"].isoformat(),
                        page[-1]["transaction_id"],
                    ]
                )
                if more and page
                else None
            ),
        }

    async def add_review(
        self,
        transaction_id: str,
        status: ReviewStatus,
        disposition: Disposition | None,
        note: str | None,
        analyst: str,
    ) -> dict[str, Any]:
        if status == "closed" and disposition is None:
            raise InvalidReview("closing a review requires a disposition")
        if note is not None and len(note) > 2000:
            raise InvalidReview("note exceeds 2000 characters")
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT 1 FROM decisions d WHERE d.transaction_id = %(id)s AND {_IN_NAMESPACE}",  # noqa: S608
                {"id": transaction_id, "ns": self.namespace},
            )
            if await cur.fetchone() is None:
                raise NotFound(transaction_id)
            await cur.execute(
                "INSERT INTO reviews (transaction_id, status, disposition, note, analyst) "
                "VALUES (%s, %s, %s, %s, %s) "
                "RETURNING id, status, disposition, note, analyst, created_at",
                (transaction_id, status, disposition, note or None, analyst),
            )
            row: dict[str, Any] | None = await cur.fetchone()
        assert row is not None
        return row

    async def ping(self) -> bool:
        try:
            async with self.pool.connection(timeout=1) as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            return False
