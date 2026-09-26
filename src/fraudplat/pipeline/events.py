"""`decision.made` v1: the Kafka event contract, and message validation for the worker.

Message layout:
    key      customer_id (UTF-8) — the partition key
    value    JSON object below (sorted keys, compact)
    headers  event_id, event_type, schema_version (copies used for routing and checks)

A message is *invalid* (sent to the DLQ, never applied) if the value is not valid JSON or does
not match the schema, the schema version or event type is unsupported, the key differs from
`customer_id`, a header disagrees with the value, or `payload_hash` does not equal the canonical
hash recomputed from the transaction fields. The last check binds the event to exactly one
transaction payload: a message cannot claim a transaction's identity with different contents.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from fraudplat.contracts import ACCEPTED_CURRENCIES, Action, Identifier
from fraudplat.features.spec import US
from fraudplat.features.state import TxnEvent
from fraudplat.hashing import canonical_fields_json, sha256_hex

Headers = list[tuple[str, str | bytes | None]]

EVENT_TYPE = "decision.made"
SCHEMA_VERSION = 1


class DecisionMadeV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: uuid.UUID
    event_type: Literal["decision.made"]
    schema_version: Literal[1]
    transaction_id: Identifier
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    customer_id: Identifier
    terminal_id: Identifier
    amount_minor: int = Field(ge=0, strict=True)
    currency: str
    event_time: AwareDatetime
    received_at: AwareDatetime
    decision_time: AwareDatetime
    persisted_at: AwareDatetime
    action: Action
    score: float | None
    model_version: str | None
    feature_version: str
    policy_version: str


def _epoch_us(value: datetime) -> int:
    return int(value.timestamp()) * US + value.microsecond


@dataclass(frozen=True)
class InvalidEvent:
    reason: str
    detail: str
    event_id: str | None = None


def parse_message(
    key: bytes | None, value: bytes | None, headers: Headers | dict[str, str | bytes | None] | None
) -> DecisionMadeV1 | InvalidEvent:
    try:
        raw: Any = json.loads(value or b"")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return InvalidEvent("MALFORMED_JSON", str(exc)[:200])
    claimed = str(raw.get("event_id")) if isinstance(raw, dict) else None
    try:
        event = DecisionMadeV1.model_validate(raw)
    except ValidationError as exc:
        return InvalidEvent("SCHEMA_VIOLATION", str(exc.errors()[:3])[:500], claimed)
    if event.currency not in ACCEPTED_CURRENCIES:
        return InvalidEvent("UNSUPPORTED_CURRENCY", event.currency, claimed)
    if key is None or key.decode("utf-8", "replace") != event.customer_id:
        return InvalidEvent("KEY_MISMATCH", "message key is not the customer_id", claimed)
    pairs = list(headers.items()) if isinstance(headers, dict) else list(headers or [])
    header_map = {
        k: v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v) for k, v in pairs
    }
    expected = {
        "event_id": str(event.event_id),
        "event_type": EVENT_TYPE,
        "schema_version": str(SCHEMA_VERSION),
    }
    for name, value_ in expected.items():
        if name in header_map and header_map[name] != value_:
            return InvalidEvent("HEADER_MISMATCH", f"header {name} disagrees with payload", claimed)
    recomputed = sha256_hex(
        canonical_fields_json(
            transaction_id=event.transaction_id,
            customer_id=event.customer_id,
            terminal_id=event.terminal_id,
            amount_minor=event.amount_minor,
            currency=event.currency,
            event_time=event.event_time,
        )
    )
    if recomputed != event.payload_hash:
        return InvalidEvent("PAYLOAD_HASH_MISMATCH", "payload_hash does not match fields", claimed)
    return event


def to_txn_event(event: DecisionMadeV1) -> TxnEvent:
    return TxnEvent(
        event_id=event.transaction_id,
        customer_id=event.customer_id,
        terminal_id=event.terminal_id,
        amount_minor=event.amount_minor,
        event_time_us=_epoch_us(event.event_time),
        payload_hash=event.payload_hash,
        source_event_id=str(event.event_id),
    )


def persisted_at_us(event: DecisionMadeV1) -> int:
    """Database clock (may be skewed relative to application hosts)."""
    return _epoch_us(event.persisted_at)


def decision_time_us(event: DecisionMadeV1) -> int:
    """API process clock."""
    return _epoch_us(event.decision_time)


def encode_value(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def headers_for(payload: dict[str, Any]) -> Headers:
    return [
        ("event_id", str(payload["event_id"]).encode()),
        ("event_type", str(payload["event_type"]).encode()),
        ("schema_version", str(payload["schema_version"]).encode()),
    ]
