"""Scores a request with the linear model using behavioural features read from Redis.

Behaviour by case (reason codes are persisted with the decision):
* Features read: model score; `COLD_START_CUSTOMER` when the customer has no 30-day history
  (scored normally from imputed values); `HISTORY_POSSIBLY_TRIMMED` when retention may have
  removed part of the window.
* Feature store unreachable or slow (timeout): no score, `FEATURES_UNAVAILABLE`; the policy then
  returns `review`. There is no fallback model.
* Artifact built for a different feature version or feature order: `ready()` is False, so
  `/readyz` fails and the service refuses to decide.

Live state is updated only by the (later) event pipeline; a decision does not write to Redis.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Any

import numpy as np
from redis.exceptions import RedisError

from fraudplat.contracts import DecisionRequest
from fraudplat.features.compute import compute_features
from fraudplat.features.redis_state import RedisFeatureState
from fraudplat.features.spec import FEATURE_NAMES, FEATURE_VERSION, US
from fraudplat.features.state import EntityMeta
from fraudplat.observability import FEATURE_TIMEOUTS, cpu_stage, stage
from fraudplat.scoring import ScoreResult
from fraudplat.training.artifacts import ServableModel


def _meta(meta: EntityMeta | None) -> dict[str, Any] | None:
    if meta is None:
        return None
    return {
        "applied_count": meta.applied_count,
        "last_applied_at_us": meta.last_applied_at_us,
        "last_event_id": meta.last_event_id,
    }


def _epoch_us(value: datetime) -> int:
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * US + delta.microseconds


class ModelScorer:
    def __init__(
        self, model: ServableModel, store: RedisFeatureState, read_timeout_s: float = 0.05
    ) -> None:
        self._model = model
        self._store = store
        self._timeout = read_timeout_s
        self.model_version = model.model_version
        self.feature_version = model.feature_version

    def ready(self) -> bool:
        return (
            self._model.feature_version == FEATURE_VERSION
            and tuple(self._model.feature_names) == FEATURE_NAMES
        )

    async def feature_store_ok(self) -> bool:
        try:
            return await asyncio.wait_for(self._store.ping(), self._timeout * 10)
        except (RedisError, OSError, TimeoutError):
            return False

    async def score(self, request: DecisionRequest) -> ScoreResult:
        t = _epoch_us(request.event_time)
        read_at = _epoch_us(datetime.now(UTC))
        try:
            with stage("feature_read"):
                snapshot = await asyncio.wait_for(
                    self._store.read(
                        request.customer_id, request.terminal_id, t, request.transaction_id
                    ),
                    self._timeout,
                )
        except (RedisError, OSError, TimeoutError) as exc:
            if isinstance(exc, TimeoutError):
                FEATURE_TIMEOUTS.inc()
            return ScoreResult(
                score=None,
                reason_codes=("FEATURES_UNAVAILABLE",),
                feature_freshness={"feature_read_at_us": read_at, "state": "unavailable"},
            )
        with stage("feature_compute"):
            features = compute_features(
                amount_minor=request.amount_minor,
                terminal_id=request.terminal_id,
                event_time_us=t,
                snapshot=snapshot,
            )
        with stage("model_input"):
            values: list[float] = [
                math.nan if features[n] is None else float(features[n])  # type: ignore[arg-type]
                for n in self._model.feature_names
            ]
            row = np.asarray([values], dtype=np.float64)
        with cpu_stage("inference"):
            score = float(self._model.score_matrix(row)[0])
        reasons: list[str] = []
        if features["cust_has_history_30d"] == 0:
            reasons.append("COLD_START_CUSTOMER")
        if not snapshot.history_complete:
            reasons.append("HISTORY_POSSIBLY_TRIMMED")
        return ScoreResult(
            score=score,
            reason_codes=tuple(reasons),
            features=dict(features),
            feature_freshness={
                "feature_read_at_us": read_at,
                "state": "read",
                "namespace": self._store.namespace,
                "watermark_us": snapshot.watermark_us,
                "history_complete": snapshot.history_complete,
                "customer_meta": _meta(snapshot.customer_meta),
                "terminal_meta": _meta(snapshot.terminal_meta),
            },
        )
