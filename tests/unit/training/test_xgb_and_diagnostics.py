"""XGBoost artifact guarantees and the evaluation definitions used in the comparison."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
import xgboost as xgb

from fraudplat.features.spec import FEATURE_NAMES, FEATURE_VERSION
from fraudplat.training.artifacts import load_any_model
from fraudplat.training.diagnostics import (
    PlattCalibrator,
    calibration,
    scenario_metrics,
    top_k_mask,
)
from fraudplat.training.xgb_model import load_xgb_model, save_xgb_model


def _booster() -> tuple[xgb.Booster, np.ndarray]:
    rng = np.random.default_rng(1)
    X = rng.gamma(2.0, 2.0, size=(300, len(FEATURE_NAMES)))
    X[rng.random(300) < 0.3, 5] = np.nan
    y = (X[:, 0] > 4).astype(int)
    dtrain = xgb.DMatrix(X, label=y, missing=np.nan, feature_names=list(FEATURE_NAMES))
    booster = xgb.train(
        {"objective": "binary:logistic", "max_depth": 2, "seed": 0, "nthread": 1}, dtrain, 10
    )
    return booster, X


def _save(tmp_path: Path) -> tuple[Path, xgb.Booster, np.ndarray]:
    booster, X = _booster()
    path = save_xgb_model(
        booster,
        feature_version=FEATURE_VERSION,
        feature_names=FEATURE_NAMES,
        params={"max_depth": 2},
        metadata={"note": "test"},
        directory=tmp_path,
    )
    return path, booster, X


def test_xgb_artifact_round_trip_matches_booster(tmp_path: Path) -> None:
    path, booster, X = _save(tmp_path)
    model = load_any_model(path.parent)  # directory dispatch
    assert model.model_version.startswith(f"xgb-{FEATURE_VERSION}-")
    expected = booster.inplace_predict(X, missing=np.nan)
    assert np.max(np.abs(model.score_matrix(X) - expected)) == 0.0
    row = {
        n: (None if math.isnan(v) else float(v)) for n, v in zip(FEATURE_NAMES, X[0], strict=True)
    }
    assert model.score(row) == pytest.approx(float(expected[0]), abs=1e-7)
    assert all(not (p.stat().st_mode & 0o222) for p in path.parent.iterdir())


def test_tampered_booster_is_rejected(tmp_path: Path) -> None:
    path, _, _ = _save(tmp_path)
    booster_file = path.parent / "booster.json"
    booster_file.chmod(0o644)
    booster_file.write_bytes(
        booster_file.read_bytes().replace(b'"base_score"', b'"base_score" ', 1)
    )
    with pytest.raises(ValueError, match="booster file"):
        load_xgb_model(path)


def test_tampered_manifest_is_rejected(tmp_path: Path) -> None:
    path, _, _ = _save(tmp_path)
    manifest = json.loads(path.read_text())
    manifest["feature_names"] = list(reversed(manifest["feature_names"]))
    path.chmod(0o644)
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="integrity"):
        load_xgb_model(path)


def test_save_refuses_mismatched_feature_order(tmp_path: Path) -> None:
    booster, _ = _booster()
    with pytest.raises(ValueError, match="feature names"):
        save_xgb_model(
            booster,
            feature_version=FEATURE_VERSION,
            feature_names=tuple(reversed(FEATURE_NAMES)),
            params={},
            metadata={},
            directory=tmp_path,
        )


def test_top_k_tie_break_is_earlier_decision_then_position() -> None:
    scores = np.array([0.5, 0.9, 0.5, 0.5, 0.1])
    decided = np.array([30, 10, 20, 20, 5])
    # k = ceil(0.4 * 5) = 2: 0.9 first; among the three 0.5s the earliest decision (t=20)
    # wins, and between the two t=20 rows the earlier position (index 2) wins.
    assert top_k_mask(scores, decided, 0.4).tolist() == [False, True, True, False, False]


def test_scenario_population_is_scenario_fraud_plus_legitimate() -> None:
    y = np.array([1, 1, 0, 0, 1])
    s1 = np.array([True, False, False, False, True])
    s2 = np.array([False, True, False, False, True])  # row 4 is in both scenarios
    scores = np.array([0.9, 0.8, 0.1, 0.2, 0.7])
    flagged = np.array([True, True, False, False, False])
    out = scenario_metrics(y, scores, flagged, {"s1": s1, "s2": s2})
    # s1 population: rows 0, 4 (s1 fraud) + rows 2, 3 (legit); row 1 (s2-only fraud) excluded.
    assert out["s1"]["population"] == 4 and out["s1"]["positives"] == 2
    assert out["s1"]["average_precision_vs_legit"] == pytest.approx(1.0)  # both above legit
    assert out["s1"]["recall_in_flagged"] == pytest.approx(0.5)  # row 0 flagged, row 4 not
    assert out["s2"]["population"] == 4  # rows 1, 4 + legit rows 2, 3


def test_calibration_hand_calculated() -> None:
    y = np.array([0, 0, 1, 1])
    scores = np.array([0.1, 0.3, 0.6, 0.8])
    c = calibration(y, scores, bins=2)
    # bins {0.1, 0.3} (rate 0) and {0.6, 0.8} (rate 1): ECE = 0.5 * 0.2 + 0.5 * 0.3 = 0.25
    assert c["ece_10_equal_count"] == pytest.approx(0.25)
    assert c["brier"] == pytest.approx((0.01 + 0.09 + 0.16 + 0.04) / 4)


def test_platt_is_monotone_so_ranking_metrics_do_not_change() -> None:
    rng = np.random.default_rng(3)
    scores = rng.random(500)
    y = (rng.random(500) < scores * 0.2).astype(int)
    calibrated = PlattCalibrator.fit(y, scores).apply(scores)
    assert (np.argsort(calibrated, kind="stable") == np.argsort(scores, kind="stable")).all()
