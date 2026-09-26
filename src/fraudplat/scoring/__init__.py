"""Risk scorers. Each scorer declares the model and feature versions it produces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from fraudplat.contracts import DecisionRequest


@dataclass(frozen=True)
class ScoreResult:
    score: float | None
    reason_codes: tuple[str, ...]
    features: dict[str, Any] = field(default_factory=dict)
    feature_freshness: dict[str, Any] = field(default_factory=dict)


class Scorer(Protocol):
    model_version: str
    feature_version: str

    def ready(self) -> bool: ...

    async def score(self, request: DecisionRequest) -> ScoreResult: ...
