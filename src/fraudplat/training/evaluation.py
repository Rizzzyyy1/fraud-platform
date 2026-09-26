"""Transaction-level ranking metrics, review-budget metrics, and threshold selection.

Average precision uses scikit-learn's definition: AP = sum_n (R_n - R_{n-1}) * P_n over
descending score thresholds (no interpolation). Budget metrics rank transactions by score and
review the top ⌈budget · N⌉. Customer-level metrics are not computed here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.metrics import average_precision_score, roc_auc_score


def ranking_metrics(y: np.ndarray, scores: np.ndarray) -> dict[str, float]:
    return {
        "average_precision": float(average_precision_score(y, scores)),
        "roc_auc": float(roc_auc_score(y, scores)),
        "prevalence": float(y.mean()),
    }


def budget_metrics(
    y: np.ndarray,
    scores: np.ndarray,
    amounts: np.ndarray,
    budget: float,
    scenarios: dict[str, np.ndarray] | None = None,
) -> dict[str, Any]:
    n = y.size
    k = math.ceil(budget * n)
    top = np.zeros(n, dtype=bool)
    top[np.argsort(-scores, kind="stable")[:k]] = True
    return flagged_metrics(y, top, amounts, scenarios) | {"budget": budget, "reviewed": k}


def flagged_metrics(
    y: np.ndarray,
    flagged: np.ndarray,
    amounts: np.ndarray,
    scenarios: dict[str, np.ndarray] | None = None,
) -> dict[str, Any]:
    fraud = y.astype(bool)
    tp = int((flagged & fraud).sum())
    fp = int((flagged & ~fraud).sum())
    out: dict[str, Any] = {
        "flagged_rate": float(flagged.mean()),
        "precision": tp / max(tp + fp, 1),
        "recall": tp / max(int(fraud.sum()), 1),
        "false_positive_rate": fp / max(int((~fraud).sum()), 1),
        "fraud_amount_captured": float(
            amounts[flagged & fraud].sum() / max(amounts[fraud].sum(), 1)
        ),
    }
    if scenarios:
        out["recall_by_scenario"] = {
            name: float((flagged & mask).sum() / max(int(mask.sum()), 1))
            for name, mask in scenarios.items()
        }
    return out


@dataclass(frozen=True)
class Thresholds:
    review: float
    decline: float | None


def choose_thresholds(
    y: np.ndarray,
    scores: np.ndarray,
    review_budget: float,
    decline_min_precision: float,
    decline_min_count: int,
) -> Thresholds:
    """Pick thresholds on validation scores.

    review: the score of the ⌈budget · N⌉-th highest transaction, so approximately `budget` of
    validation transactions are reviewed or declined (ties can add a few more).
    decline: the lowest threshold whose flagged set has at least `decline_min_count` rows and
    precision ≥ `decline_min_precision`; None (never auto-decline) if no threshold qualifies.
    """
    order = np.argsort(-scores, kind="stable")
    ranked_scores = scores[order]
    ranked_fraud = y[order].astype(bool)
    k = max(math.ceil(review_budget * y.size), 1)
    review = float(ranked_scores[k - 1])

    hits = np.cumsum(ranked_fraud)
    counts = np.arange(1, y.size + 1)
    precision = hits / counts
    # Only cut between distinct scores so a threshold flags exactly the rows above it.
    boundary = np.append(ranked_scores[1:] < ranked_scores[:-1], True)
    ok = (precision >= decline_min_precision) & (counts >= decline_min_count) & boundary
    ok &= ranked_scores >= review
    decline = float(ranked_scores[np.flatnonzero(ok)[-1]]) if ok.any() else None
    return Thresholds(review=review, decline=decline)
