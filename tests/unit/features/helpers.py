"""Small builders for hand-calculated feature fixtures."""

from __future__ import annotations

from datetime import UTC, datetime

from fraudplat.features.offline import HistoricalTxn
from fraudplat.features.spec import US
from fraudplat.features.state import TxnEvent


def us(iso: str) -> int:
    """'2025-01-10T12:00:00' (UTC) -> epoch microseconds."""
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp()) * US


def event(
    event_id: str,
    event_time_us: int,
    *,
    customer: str = "C1",
    terminal: str = "T1",
    amount: int = 100,
    payload: str | None = None,
) -> TxnEvent:
    return TxnEvent(
        event_id=event_id,
        customer_id=customer,
        terminal_id=terminal,
        amount_minor=amount,
        event_time_us=event_time_us,
        payload_hash=payload or f"{event_id}:{amount}:{terminal}:{event_time_us}",
    )


def historical(ev: TxnEvent, received_at_us: int | None = None) -> HistoricalTxn:
    return HistoricalTxn(
        event=ev, received_at_us=ev.event_time_us if received_at_us is None else received_at_us
    )
