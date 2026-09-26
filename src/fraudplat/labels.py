"""Label availability: what was known about a transaction's outcome at a given instant.

Fraud outcomes become known at `label_available_at` (a synthetic chargeback delay). Legitimate
transactions are treated as confirmed legitimate only after a maturity period without a fraud
report; before that they are *unknown*, never assumed legitimate.
"""

from __future__ import annotations

from typing import Literal

LabelState = Literal["fraud", "legit", "unknown"]


def label_as_of(*, is_fraud: bool, label_available_at_us: int, as_of_us: int) -> LabelState:
    if as_of_us < label_available_at_us:
        return "unknown"
    return "fraud" if is_fraud else "legit"
