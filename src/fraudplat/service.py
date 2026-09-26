"""The decision use case: validate idempotency, score, apply policy, persist.

A retry never re-scores. The first committed decision for a transaction id wins, and every
later request with an equivalent payload receives that stored decision.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from fraudplat.contracts import DecisionRecord, DecisionRequest, DecisionResponse
from fraudplat.hashing import HASH_VERSION, request_hash
from fraudplat.observability import stage
from fraudplat.pipeline.status import PipelineMonitor
from fraudplat.policy import DecisionPolicy
from fraudplat.scoring import Scorer, ScoreResult
from fraudplat.storage.decisions import DecisionStore, NewDecision, StoredDecision

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ScorerNotReady(Exception):
    """The model is incompatible with the running feature code; no new decisions are made."""


class PipelineRejecting(Exception):
    """The event pipeline is past its hard backlog limit; new decisions are refused."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class IdempotencyConflict(Exception):
    def __init__(self, transaction_id: str) -> None:
        super().__init__(transaction_id)
        self.transaction_id = transaction_id


def to_record(decision: StoredDecision) -> DecisionRecord:
    return DecisionRecord(
        transaction_id=decision.transaction_id,
        action=decision.action,
        score=decision.score,
        reason_codes=decision.reason_codes,
        model_version=decision.model_version,
        feature_version=decision.feature_version,
        policy_version=decision.policy_version,
        model_registry_ref=decision.model_registry_ref,
        event_time=decision.event_time,
        received_at=decision.received_at,
        decision_time=decision.decision_time,
    )


@dataclass(frozen=True)
class DecisionOutcome:
    response: DecisionResponse
    created: bool


def _replay_or_conflict(stored: StoredDecision, incoming_hash: bytes) -> DecisionOutcome:
    if stored.request_hash != incoming_hash:
        raise IdempotencyConflict(stored.transaction_id)
    response = DecisionResponse(**to_record(stored).model_dump(), idempotent_replay=True)
    return DecisionOutcome(response=response, created=False)


class DecisionService:
    def __init__(
        self,
        store: DecisionStore,
        scorer: Scorer,
        policy: DecisionPolicy,
        clock: Clock = utc_now,
        pipeline: PipelineMonitor | None = None,
        model_registry_ref: str | None = None,
    ) -> None:
        self._registry_ref = model_registry_ref
        self._store = store
        self._scorer = scorer
        self._policy = policy
        self._clock = clock
        self._pipeline = pipeline

    async def decide(self, request: DecisionRequest, received_at: datetime) -> DecisionOutcome:
        incoming_hash = request_hash(request)

        # Fast path for retries: avoid scoring when a decision already exists.
        with stage("db_precheck"):
            existing = await self._store.get(request.transaction_id)
        if existing is not None:
            return _replay_or_conflict(existing, incoming_hash)

        if not self._scorer.ready():
            raise ScorerNotReady
        pipeline_reasons: tuple[str, ...] = ()
        suppressed: str | None = None
        if self._pipeline is not None:
            health = self._pipeline.current
            if health.reject is not None:
                raise PipelineRejecting(health.reject)
            if health.reasons:
                pipeline_reasons = ("PIPELINE_DEGRADED", *health.reasons)
            if health.suppress_scoring is not None:
                suppressed = health.suppress_scoring
        if suppressed is not None:
            # Too stale to trust the features: do not call the model; persist a review.
            result = ScoreResult(
                score=None,
                reason_codes=(suppressed,),
                feature_freshness={"state": "not_read", "reason": suppressed},
            )
        else:
            result = await self._scorer.score(request)
        with stage("policy"):
            action, policy_reason = self._policy.decide(result.score)
        new = NewDecision(
            transaction_id=request.transaction_id,
            request_hash=incoming_hash,
            hash_version=HASH_VERSION,
            customer_id=request.customer_id,
            terminal_id=request.terminal_id,
            amount_minor=request.amount_minor,
            currency=request.currency,
            event_time=request.event_time,
            received_at=received_at,
            decision_time=self._clock(),
            score=result.score,
            action=action,
            reason_codes=(*result.reason_codes, *pipeline_reasons, policy_reason),
            features=result.features,
            feature_freshness=result.feature_freshness,
            model_version=self._scorer.model_version,
            feature_version=self._scorer.feature_version,
            policy_version=self._policy.version,
            model_registry_ref=self._registry_ref,
        )
        with stage("db_insert"):
            stored, created = await self._store.insert_with_outbox(new)
        if not created:
            # Lost a concurrent race; the winner's decision is authoritative.
            return _replay_or_conflict(stored, incoming_hash)
        response = DecisionResponse(**to_record(stored).model_dump(), idempotent_replay=False)
        return DecisionOutcome(response=response, created=True)
