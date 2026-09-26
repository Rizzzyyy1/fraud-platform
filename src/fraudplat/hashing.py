"""Canonical payload hashing for idempotency and duplicate-event detection.

The hash is computed from *validated* fields, never a raw body, so payloads that differ only in
key order, whitespace, timezone offset spelling, or currency case hash equally. API requests and
simulated events share `canonical_fields_json`, so one transaction has one payload hash
everywhere. Bump HASH_VERSION if normalisation rules change; it is stored beside every hash.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from fraudplat.contracts import DecisionRequest

HASH_VERSION = 1


def format_utc(value: datetime) -> str:
    """Fixed-width UTC timestamp with microsecond precision, e.g. 2026-01-02T03:04:05.000000Z."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_fields_json(
    *,
    transaction_id: str,
    customer_id: str,
    terminal_id: str,
    amount_minor: int,
    currency: str,
    event_time: datetime,
) -> str:
    payload = {
        "hash_version": HASH_VERSION,
        "transaction_id": transaction_id,
        "customer_id": customer_id,
        "terminal_id": terminal_id,
        "amount_minor": amount_minor,
        "currency": currency.upper(),
        "event_time": format_utc(event_time),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_request_json(request: DecisionRequest) -> str:
    return canonical_fields_json(
        transaction_id=request.transaction_id,
        customer_id=request.customer_id,
        terminal_id=request.terminal_id,
        amount_minor=request.amount_minor,
        currency=request.currency,
        event_time=request.event_time,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def request_hash(request: DecisionRequest) -> bytes:
    return hashlib.sha256(canonical_request_json(request).encode("utf-8")).digest()
