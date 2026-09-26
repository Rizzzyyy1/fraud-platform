"""Temporary deterministic scorer for Checkpoint A.

It exists only to exercise durable idempotency and outbox atomicity. Its output is a hash of
the transaction id: it uses no features and says nothing about fraud. Replaced in Checkpoint C.
"""

from __future__ import annotations

import hashlib

from fraudplat.contracts import DecisionRequest
from fraudplat.scoring import ScoreResult


class ScaffoldScorer:
    model_version = "scaffold-0"
    feature_version = "none-0"

    def ready(self) -> bool:
        return True

    async def score(self, request: DecisionRequest) -> ScoreResult:
        digest = hashlib.sha256(request.transaction_id.encode("utf-8")).digest()
        pseudo_score = int.from_bytes(digest[:8], "big") / 2**64
        return ScoreResult(score=pseudo_score, reason_codes=("SCAFFOLD_NOT_A_MODEL",))
