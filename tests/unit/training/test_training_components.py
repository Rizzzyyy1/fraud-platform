"""Timeline/eligibility, train-only preprocessing, artifact integrity and parity, thresholds."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression

from fraudplat.features.spec import DAY, FEATURE_NAMES, FEATURE_VERSION
from fraudplat.policy import load_policy, write_policy
from fraudplat.training.evaluation import budget_metrics, choose_thresholds
from fraudplat.training.model import Preprocessing, build_model, load_model, save_model
from fraudplat.training.splits import Timeline, split_masks

TL = Timeline(day0_us=0, burn_in_days=2, train_end_day=5, training_cutoff_day=8, test_start_day=10)


def test_eligibility_uses_labels_known_at_the_training_cutoff() -> None:
    decision = np.array([1, 3, 4, 4, 8, 9]) * DAY
    label_at = np.array([2, 4, 8, 8 + 1, 30, 30]) * DAY
    masks = split_masks(TL, decision, label_at)
    # day 1: burn-in. day 3: label at day 4 <= cutoff 8 -> train. day 4 (label day 8, exactly
    # at cutoff) -> train. day 4 (label day 9, after cutoff) -> excluded and counted.
    assert masks.train.tolist() == [False, True, True, False, False, False]
    assert masks.train_candidates == 3
    assert masks.excluded_unlabelled_at_cutoff == 1
    # validation: decisions in [cutoff 8, test start 10); labels are not required there.
    assert masks.validation.tolist() == [False, False, False, False, True, True]


def test_test_period_rows_are_refused() -> None:
    with pytest.raises(ValueError, match="final test period"):
        split_masks(TL, np.array([10 * DAY]), np.array([11 * DAY]))


def test_timeline_order_is_validated() -> None:
    with pytest.raises(ValueError):
        Timeline(
            day0_us=0, burn_in_days=5, train_end_day=5, training_cutoff_day=8, test_start_day=10
        )


NAMES = ("a", "b", "c")


def test_preprocessing_statistics_come_from_training_rows_only() -> None:
    train = np.array([[1.0, np.nan, 0.0], [3.0, 4.0, 1.0], [5.0, 8.0, 1.0]])
    pre = Preprocessing.fit(train, NAMES, nullable=("b",), log1p=("a",))
    # medians from train: a=3, b=median(4, 8)=6, c=1
    assert pre.medians == (3.0, 6.0, 1.0)
    # output column a after log1p: log(2), log(4), log(6)
    expected_mean_a = (math.log(2) + math.log(4) + math.log(6)) / 3
    assert pre.means[0] == pytest.approx(expected_mean_a, rel=1e-15)
    assert pre.output_names == ("a", "b", "c", "b__missing")
    # Transforming extreme validation rows does not change fitted statistics.
    pre.transform(np.array([[1e9, np.nan, 0.0]]))
    assert pre.medians == (3.0, 6.0, 1.0)


def test_missing_non_nullable_value_is_rejected() -> None:
    pre = Preprocessing.fit(np.array([[1.0, 2.0, 3.0]] * 3), NAMES, ("b",), ())
    with pytest.raises(ValueError, match="non-nullable"):
        pre.transform(np.array([[np.nan, 2.0, 3.0]]))


def _fitted(tmp_path: Path) -> tuple[object, Preprocessing, np.ndarray, Path]:
    rng = np.random.default_rng(0)
    X = rng.gamma(2.0, 3.0, size=(400, len(FEATURE_NAMES)))
    X[rng.random(400) < 0.2, FEATURE_NAMES.index("secs_since_prev_cust_txn")] = np.nan
    y = (X[:, 0] + rng.normal(0, 3, 400) > 9).astype(int)
    pre = Preprocessing.fit(X, FEATURE_NAMES, ("secs_since_prev_cust_txn",), FEATURE_NAMES[:3])
    clf = LogisticRegression(max_iter=1000).fit(pre.transform(X), y)
    model = build_model(
        feature_version=FEATURE_VERSION,
        preprocessing=pre,
        coefficients=tuple(float(v) for v in clf.coef_[0]),
        intercept=float(clf.intercept_[0]),
        metadata={"note": "unit test"},
    )
    path = save_model(model, tmp_path)
    return clf, pre, X, path


def test_artifact_inference_matches_scikit_learn_exactly(tmp_path: Path) -> None:
    clf, pre, X, path = _fitted(tmp_path)
    model = load_model(path)
    expected = clf.predict_proba(pre.transform(X))[:, 1]  # type: ignore[attr-defined]
    assert np.max(np.abs(model.score_matrix(X) - expected)) <= 1e-12
    row = {
        name: (None if np.isnan(v) else float(v))
        for name, v in zip(FEATURE_NAMES, X[0], strict=True)
    }
    assert model.score(row) == pytest.approx(expected[0], abs=1e-12)


def test_tampered_artifact_is_rejected(tmp_path: Path) -> None:
    _, _, _, path = _fitted(tmp_path)
    document = json.loads(path.read_text())
    document["coefficients"][0] += 1e-9
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="integrity"):
        load_model(tampered)


def test_artifact_records_feature_order_and_version(tmp_path: Path) -> None:
    _, _, _, path = _fitted(tmp_path)
    document = json.loads(path.read_text())
    assert document["feature_names"] == list(FEATURE_NAMES)
    assert document["feature_version"] == FEATURE_VERSION
    assert document["model_version"].startswith(f"lr-{FEATURE_VERSION}-")
    assert not (path.stat().st_mode & 0o222)  # read-only


def test_thresholds_from_hand_made_scores() -> None:
    # 1000 rows, scores 0.999, 0.998, ... ; the top 3 are fraud, then alternating none.
    scores = np.linspace(0.999, 0.0, 1000)
    y = np.zeros(1000, dtype=int)
    y[:3] = 1
    t = choose_thresholds(
        y, scores, review_budget=0.01, decline_min_precision=0.9, decline_min_count=3
    )
    assert t.review == pytest.approx(scores[9])  # 10th highest: ceil(0.01 * 1000) = 10
    assert t.decline == pytest.approx(scores[2])  # top 3 have precision 1.0 and count 3
    none = choose_thresholds(y, scores, 0.01, decline_min_precision=0.9, decline_min_count=50)
    assert none.decline is None  # no set of >= 50 rows reaches 90% precision


def test_budget_metrics_hand_calculated() -> None:
    y = np.array([1, 0, 1, 0, 0, 0, 0, 0, 0, 1])
    scores = np.array([0.9, 0.8, 0.7, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.05])
    amounts = np.array([100, 1, 50, 1, 1, 1, 1, 1, 1, 50], dtype=float)
    m = budget_metrics(y, scores, amounts, budget=0.3)  # top 3 reviewed
    assert m["precision"] == pytest.approx(2 / 3)
    assert m["recall"] == pytest.approx(2 / 3)
    assert m["false_positive_rate"] == pytest.approx(1 / 7)
    assert m["fraud_amount_captured"] == pytest.approx(150 / 200)


def test_policy_artifact_round_trip_and_tamper(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    policy = write_policy(
        path,
        review_threshold=0.7,
        decline_threshold=None,
        derived_for_model="lr-f1-x",
        derivation={"data": "validation"},
    )
    assert load_policy(path) == policy
    assert policy.decide(0.99)[0] == "review"  # declines disabled
    document = json.loads(path.read_text())
    document["review_threshold"] = 0.1
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="integrity"):
        load_policy(path)
