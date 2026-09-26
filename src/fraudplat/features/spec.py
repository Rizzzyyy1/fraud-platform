"""Feature version f1: window definitions and the ordered feature list.

All times are integer microseconds since the Unix epoch (UTC). For a decision about a
transaction with event time t, a window of width W contains prior events e with

    t - W <= e.event_time < t        (lower bound inclusive, upper bound exclusive)

so the transaction itself, simultaneous transactions, and future transactions are never history.
Which prior events are *visible* is decided separately by arrival/availability (see `offline`).
"""

from __future__ import annotations

FEATURE_VERSION = "f1"

US = 1_000_000
MINUTE = 60 * US
HOUR = 60 * MINUTE
DAY = 24 * HOUR

CUSTOMER_COUNT_WINDOWS: dict[str, int] = {"1h": HOUR, "1d": DAY, "7d": 7 * DAY, "30d": 30 * DAY}
CUSTOMER_AMOUNT_WINDOWS: dict[str, int] = {"7d": 7 * DAY, "30d": 30 * DAY}
TERMINAL_COUNT_WINDOWS: dict[str, int] = {"1d": DAY, "7d": 7 * DAY}

MAX_WINDOW = 30 * DAY
# Events may arrive up to LATE_ALLOWANCE behind the newest applied event time and still be
# accepted. State is retained for RETENTION_HORIZON behind that watermark.
LATE_ALLOWANCE = DAY
RETENTION_HORIZON = MAX_WINDOW + LATE_ALLOWANCE

FEATURE_NAMES: tuple[str, ...] = (
    "amount_minor",
    "hour_of_day_utc",
    "day_of_week_utc",
    *(f"cust_txn_count_{name}" for name in CUSTOMER_COUNT_WINDOWS),
    *(f"cust_amount_mean_{name}" for name in CUSTOMER_AMOUNT_WINDOWS),
    "amount_to_cust_mean_30d",
    "secs_since_prev_cust_txn",
    "cust_has_history_30d",
    "cust_terminal_is_new_30d",
    *(f"term_txn_count_{name}" for name in TERMINAL_COUNT_WINDOWS),
)
