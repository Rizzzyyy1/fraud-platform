"""Request validation and canonical hashing. Expected hashes are computed from literal strings."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from fraudplat.contracts import DecisionRequest
from fraudplat.hashing import canonical_request_json, request_hash

BASE = {
    "transaction_id": "tx-1",
    "customer_id": "c-1",
    "terminal_id": "t-1",
    "amount_minor": 12345,
    "currency": "USD",
    "event_time": "2026-01-02T03:04:05Z",
}


def parse(raw: str) -> DecisionRequest:
    return DecisionRequest.model_validate_json(raw)


def test_canonical_json_matches_hand_written_literal() -> None:
    expected = (
        '{"amount_minor":12345,"currency":"USD","customer_id":"c-1",'
        '"event_time":"2026-01-02T03:04:05.000000Z","hash_version":1,'
        '"terminal_id":"t-1","transaction_id":"tx-1"}'
    )
    request = parse(json.dumps(BASE))
    assert canonical_request_json(request) == expected
    assert request_hash(request) == hashlib.sha256(expected.encode()).digest()


@pytest.mark.parametrize(
    "raw",
    [
        # key order and whitespace
        '{ "event_time": "2026-01-02T03:04:05Z", "currency": "USD", "amount_minor": 12345,'
        '  "terminal_id": "t-1", "customer_id": "c-1", "transaction_id": "tx-1" }',
        # equivalent instant, different offset spellings
        json.dumps({**BASE, "event_time": "2026-01-02T03:04:05+00:00"}),
        json.dumps({**BASE, "event_time": "2026-01-01T22:04:05-05:00"}),
        json.dumps({**BASE, "event_time": "2026-01-02T03:04:05.000000Z"}),
        # currency case
        json.dumps({**BASE, "currency": "usd"}),
    ],
)
def test_equivalent_payloads_hash_equally(raw: str) -> None:
    assert request_hash(parse(raw)) == request_hash(parse(json.dumps(BASE)))


@pytest.mark.parametrize(
    "change",
    [
        {"amount_minor": 12346},
        {"event_time": "2026-01-02T03:04:05.000001Z"},
        {"terminal_id": "t-2"},
        {"customer_id": "c-2"},
        {"transaction_id": "tx-2"},
    ],
)
def test_meaningful_changes_change_the_hash(change: dict[str, object]) -> None:
    assert request_hash(parse(json.dumps({**BASE, **change}))) != request_hash(
        parse(json.dumps(BASE))
    )


@pytest.mark.parametrize(
    "change",
    [
        {"event_time": "2026-01-02T03:04:05"},  # naive timestamp
        {"amount_minor": 123.0},  # float amount
        {"amount_minor": "12345"},  # string amount
        {"amount_minor": True},  # bool is not an amount
        {"amount_minor": -1},
        {"currency": "EUR"},  # v1 accepts one currency
        {"transaction_id": ""},
        {"transaction_id": "has space"},
        {"extra_field": 1},
    ],
)
def test_invalid_requests_are_rejected(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        parse(json.dumps({**BASE, **change}))
