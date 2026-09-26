"""Evaluation diagnostics used by the model comparison.

Definitions (all transaction-level):
* **Average precision** — scikit-learn's AP = sum_n (R_n - R_{n-1}) * P_n over descending score.
* **Retrospective top-k** — rank the evaluation window by score, descending; ties are broken by
  earlier decision time, then by transaction position (deterministic). The top ceil(b * N) rows
  are "reviewed". This uses the whole window in hindsight and is not available online.
* **Fixed-threshold policy** — thresholds chosen *before* the window (on the calibration window)
  and applied to each transaction as it arrives. This is what the online service can do; its
  review rate drifts when the score distribution drifts.
* **Scenario metrics** — for scenario s, the comparison population is every row that is fraud
  under s plus every legitimate row (`is_fraud` = 0). Rows that are fraud only under other
  scenarios are excluded. A row in several scenarios is a positive in each of them. Ranking
  metrics are never computed on fraud-only subsets.
* **Calibration** — Brier score, and expected calibration error (ECE) over 10 equal-count bins:
  sum_b (n_b / N) * |mean score_b - fraud rate_b|.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score

from fraudplat.training.evaluation import choose_thresholds, flagged_metrics


def ranked_order(scores: np.ndarray, decision_time_us: np.ndarray) -> np.ndarray:
    """Indices by score descending; ties by earlier decision time, then original position."""
    return np.lexsort((np.arange(scores.size), decision_time_us, -scores))


def top_k_mask(scores: np.ndarray, decision_time_us: np.ndarray, budget: float) -> np.ndarray:
    k = math.ceil(budget * scores.size)
    mask = np.zeros(scores.size, dtype=bool)
    mask[ranked_order(scores, decision_time_us)[:k]] = True
    return mask


def average_precision(y: np.ndarray, scores: np.ndarray) -> float | None:
    if y.sum() == 0 or y.sum() == y.size:
        return None
    return float(average_precision_score(y, scores))


def scenario_metrics(
    y: np.ndarray,
    scores: np.ndarray,
    flagged: np.ndarray,
    scenarios: dict[str, np.ndarray],
) -> dict[str, dict[str, Any]]:
    legit = y == 0
    out: dict[str, dict[str, Any]] = {}
    for name, positive in scenarios.items():
        population = positive | legit
        yp = positive[population].astype(int)
        flagged_pos = int((flagged & positive).sum())
        flagged_legit = int((flagged & legit).sum())
        out[name] = {
            "positives": int(positive.sum()),
            "population": int(population.sum()),
            "average_precision_vs_legit": average_precision(yp, scores[population]),
            "recall_in_flagged": flagged_pos / max(int(positive.sum()), 1),
            "precision_vs_legit_in_flagged": flagged_pos / max(flagged_pos + flagged_legit, 1),
        }
    return out


def calibration(y: np.ndarray, scores: np.ndarray, bins: int = 10) -> dict[str, Any]:
    order = np.argsort(scores, kind="stable")
    chunks = np.array_split(order, bins)
    table, ece = [], 0.0
    for chunk in chunks:
        mean_score = float(scores[chunk].mean())
        rate = float(y[chunk].mean())
        ece += chunk.size / y.size * abs(mean_score - rate)
        table.append({"n": int(chunk.size), "mean_score": mean_score, "fraud_rate": rate})
    return {
        "brier": float(np.mean((scores - y) ** 2)),
        "ece_10_equal_count": ece,
        "mean_score": float(scores.mean()),
        "fraud_rate": float(y.mean()),
        "reliability": table,
    }


@dataclass(frozen=True)
class PlattCalibrator:
    """sigmoid(a * logit(score) + b), fitted on a calibration window."""

    a: float
    b: float

    @classmethod
    def fit(cls, y: np.ndarray, scores: np.ndarray) -> PlattCalibrator:
        z = _logit(scores).reshape(-1, 1)
        model = LogisticRegression(C=1e6, max_iter=1000).fit(z, y)
        return cls(float(model.coef_[0, 0]), float(model.intercept_[0]))

    def apply(self, scores: np.ndarray) -> np.ndarray:
        out: np.ndarray = 1.0 / (1.0 + np.exp(-(self.a * _logit(scores) + self.b)))
        return out


def _logit(p: np.ndarray) -> np.ndarray:
    q = np.clip(p, 1e-12, 1 - 1e-12)
    out: np.ndarray = np.log(q / (1 - q))
    return out


def fixed_threshold_results(
    y: np.ndarray,
    scores: np.ndarray,
    amounts: np.ndarray,
    review: float,
    decline: float | None,
    scenarios: dict[str, np.ndarray],
) -> dict[str, Any]:
    declined = scores >= decline if decline is not None else np.zeros(y.size, dtype=bool)
    flagged = scores >= review
    return {
        "review_rate": float((flagged & ~declined).mean()),
        "decline_rate": float(declined.mean()),
        "review_or_decline": flagged_metrics(y, flagged, amounts, scenarios),
        "decline_only": flagged_metrics(y, declined, amounts, scenarios) if decline else None,
    }


def thresholds_from_calibration_window(
    y: np.ndarray, scores: np.ndarray, budget: float, min_precision: float, min_count: int
) -> tuple[float, float | None]:
    t = choose_thresholds(y, scores, budget, min_precision, min_count)
    return t.review, t.decline
