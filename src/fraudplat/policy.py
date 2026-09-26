"""Decision policy: maps a risk score to an action. Versioned independently of the model.

A policy derived from validation data records the model version it was derived for, because
thresholds only make sense for one model's score distribution. `decline_threshold=None`
disables automatic declines.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fraudplat.contracts import Action

POLICY_SCHEMA = "fraudplat.policy/v1"


@dataclass(frozen=True)
class DecisionPolicy:
    version: str
    review_threshold: float
    decline_threshold: float | None
    derived_for_model: str | None = None

    def __post_init__(self) -> None:
        upper = 1.0 if self.decline_threshold is None else self.decline_threshold
        if not 0.0 <= self.review_threshold <= upper <= 1.0:
            raise ValueError("thresholds must satisfy 0 <= review <= decline <= 1")

    def decide(self, score: float | None) -> tuple[Action, str]:
        """Return the action and the policy reason code. A missing score is never approved."""
        if score is None:
            return "review", "NO_SCORE"
        if self.decline_threshold is not None and score >= self.decline_threshold:
            return "decline", "SCORE_AT_OR_ABOVE_DECLINE_THRESHOLD"
        if score >= self.review_threshold:
            return "review", "SCORE_AT_OR_ABOVE_REVIEW_THRESHOLD"
        return "approve", "SCORE_BELOW_REVIEW_THRESHOLD"


def _digest(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_policy(
    path: Path,
    *,
    review_threshold: float,
    decline_threshold: float | None,
    derived_for_model: str,
    derivation: dict[str, Any],
) -> DecisionPolicy:
    core = {
        "schema": POLICY_SCHEMA,
        "review_threshold": review_threshold,
        "decline_threshold": decline_threshold,
        "derived_for_model": derived_for_model,
        "derivation": derivation,
    }
    digest = _digest(core)
    version = f"pol-{digest[:12]}"
    policy = DecisionPolicy(version, review_threshold, decline_threshold, derived_for_model)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {**core, "policy_version": version, "integrity_sha256": digest}
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return policy


def load_policy(path: Path) -> DecisionPolicy:
    document = json.loads(path.read_text())
    if document.get("schema") != POLICY_SCHEMA:
        raise ValueError(f"unsupported policy schema: {document.get('schema')!r}")
    core = {k: v for k, v in document.items() if k not in ("policy_version", "integrity_sha256")}
    digest = _digest(core)
    if digest != document["integrity_sha256"] or document["policy_version"] != f"pol-{digest[:12]}":
        raise ValueError(f"policy integrity check failed: {path}")
    return DecisionPolicy(
        version=document["policy_version"],
        review_threshold=document["review_threshold"],
        decline_threshold=document["decline_threshold"],
        derived_for_model=document["derived_for_model"],
    )


# Checkpoint A scaffolding only: thresholds are placeholders, not chosen on validation data.
SCAFFOLD_POLICY = DecisionPolicy(version="scaffold-0", review_threshold=0.9, decline_threshold=0.99)
