"""API request/response contracts for the synchronous decision endpoint.

Amounts are integer minor units with an explicit ISO 4217 currency. Timestamps must carry a
UTC offset and are normalised to UTC on validation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, field_validator

Identifier = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
]
Action = Literal["approve", "review", "decline"]

# v1 accepts one currency so amount aggregates never mix units. Simulated amounts are
# labelled synthetic USD; see docs/DESIGN.md.
ACCEPTED_CURRENCIES = frozenset({"USD"})
MAX_AMOUNT_MINOR = 10**12


class DecisionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    transaction_id: Identifier
    customer_id: Identifier
    terminal_id: Identifier
    amount_minor: int = Field(ge=0, le=MAX_AMOUNT_MINOR, strict=True)
    currency: str
    event_time: AwareDatetime

    @field_validator("currency", mode="before")
    @classmethod
    def _normalise_currency(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("currency must be a string")
        code = value.upper()
        if code not in ACCEPTED_CURRENCIES:
            raise ValueError(f"unsupported currency; accepted: {sorted(ACCEPTED_CURRENCIES)}")
        return code

    @field_validator("event_time")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class DecisionRecord(BaseModel):
    """The persisted decision. Identical for every response about the same transaction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transaction_id: str
    action: Action
    score: float | None
    reason_codes: list[str]
    model_version: str | None
    feature_version: str
    policy_version: str
    model_registry_ref: str | None = None
    event_time: datetime
    received_at: datetime
    decision_time: datetime


class DecisionResponse(DecisionRecord):
    """Adds request-specific metadata; `idempotent_replay` differs between first and retry."""

    idempotent_replay: bool
