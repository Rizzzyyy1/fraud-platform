"""Linear model artifact: preprocessing + logistic regression parameters as integrity-checked JSON.

The same `Preprocessing.transform` runs in training and in the scoring service, and inference is
a dot product and a sigmoid in NumPy, so serving needs neither scikit-learn nor unpickling.
Floats are serialised with `repr` (Python's JSON default), which round-trips exactly.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from fraudplat.features.compute import FeatureValue

MODEL_SCHEMA = "fraudplat.linear-model/v1"


@dataclass(frozen=True)
class Preprocessing:
    """missing indicators → median imputation → log1p → standardisation (train-fitted)."""

    feature_names: tuple[str, ...]
    nullable: tuple[str, ...]
    log1p: tuple[str, ...]
    medians: tuple[float, ...]  # per feature
    means: tuple[float, ...]  # per output column
    scales: tuple[float, ...]  # per output column

    @property
    def output_names(self) -> tuple[str, ...]:
        return (*self.feature_names, *(f"{name}__missing" for name in self.nullable))

    def _impute_and_log(self, values: np.ndarray) -> np.ndarray:
        if values.ndim != 2 or values.shape[1] != len(self.feature_names):
            raise ValueError("input does not match the artifact's feature order")
        index = {name: i for i, name in enumerate(self.feature_names)}
        nullable_idx = [index[name] for name in self.nullable]
        missing = np.isnan(values)
        unexpected = missing.copy()
        unexpected[:, nullable_idx] = False
        if unexpected.any():
            raise ValueError("missing value in a non-nullable feature")
        indicators = missing[:, nullable_idx].astype(np.float64)
        filled = np.where(missing, np.asarray(self.medians), values)
        log_idx = [index[name] for name in self.log1p]
        if (filled[:, log_idx] < 0).any():
            raise ValueError("log1p feature has a negative value")
        filled[:, log_idx] = np.log1p(filled[:, log_idx])
        return np.hstack([filled, indicators])

    def transform(self, values: np.ndarray) -> np.ndarray:
        scaled: np.ndarray = (self._impute_and_log(values) - np.asarray(self.means)) / np.asarray(
            self.scales
        )
        return scaled

    @classmethod
    def fit(
        cls,
        train_values: np.ndarray,
        feature_names: tuple[str, ...],
        nullable: tuple[str, ...],
        log1p: tuple[str, ...],
    ) -> Preprocessing:
        medians = np.nanmedian(train_values, axis=0)
        medians = np.where(np.isnan(medians), 0.0, medians)  # all-missing column → 0
        partial = cls(feature_names, nullable, log1p, tuple(map(float, medians)), (), ())
        raw = partial._impute_and_log(train_values)
        means = raw.mean(axis=0)
        scales = raw.std(axis=0)
        scales = np.where(scales > 0, scales, 1.0)
        return cls(
            feature_names,
            nullable,
            log1p,
            tuple(map(float, medians)),
            tuple(map(float, means)),
            tuple(map(float, scales)),
        )


@dataclass(frozen=True)
class LinearModel:
    model_version: str
    feature_version: str
    preprocessing: Preprocessing
    coefficients: tuple[float, ...]
    intercept: float
    metadata: dict[str, Any]

    @property
    def feature_names(self) -> tuple[str, ...]:
        return self.preprocessing.feature_names

    def score_matrix(self, values: np.ndarray) -> np.ndarray:
        logits = self.preprocessing.transform(values) @ np.asarray(self.coefficients)
        scores: np.ndarray = 1.0 / (1.0 + np.exp(-(logits + self.intercept)))
        return scores

    def score(self, features: dict[str, FeatureValue]) -> float:
        row = [
            math.nan if features[name] is None else float(features[name])  # type: ignore[arg-type]
            for name in self.preprocessing.feature_names
        ]
        return float(self.score_matrix(np.asarray([row], dtype=np.float64))[0])


def _core(model_fields: dict[str, Any]) -> dict[str, Any]:
    return {"schema": MODEL_SCHEMA, **model_fields}


def _digest(core: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_model(
    *,
    feature_version: str,
    preprocessing: Preprocessing,
    coefficients: tuple[float, ...],
    intercept: float,
    metadata: dict[str, Any],
) -> LinearModel:
    if len(coefficients) != len(preprocessing.output_names):
        raise ValueError("coefficient count does not match preprocessing outputs")
    core = _core(_fields(feature_version, preprocessing, coefficients, intercept, metadata))
    version = f"lr-{feature_version}-{_digest(core)[:12]}"
    return LinearModel(version, feature_version, preprocessing, coefficients, intercept, metadata)


def _fields(
    feature_version: str,
    pre: Preprocessing,
    coefficients: tuple[float, ...],
    intercept: float,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "feature_version": feature_version,
        "feature_names": list(pre.feature_names),
        "preprocessing": {
            "nullable": list(pre.nullable),
            "log1p": list(pre.log1p),
            "medians": list(pre.medians),
            "means": list(pre.means),
            "scales": list(pre.scales),
            "output_names": list(pre.output_names),
        },
        "coefficients": list(coefficients),
        "intercept": intercept,
        "metadata": metadata,
    }


def save_model(model: LinearModel, directory: Path) -> Path:
    core = _core(
        _fields(
            model.feature_version,
            model.preprocessing,
            model.coefficients,
            model.intercept,
            model.metadata,
        )
    )
    digest = _digest(core)
    if model.model_version != f"lr-{model.feature_version}-{digest[:12]}":
        raise ValueError("model_version does not match its content")
    target = directory / model.model_version
    target.mkdir(parents=True, exist_ok=False)
    path = target / "model.json"
    document = {**core, "model_version": model.model_version, "integrity_sha256": digest}
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    path.chmod(0o444)
    return path


def load_model(path: Path) -> LinearModel:
    document = json.loads(path.read_text())
    if document.get("schema") != MODEL_SCHEMA:
        raise ValueError(f"unsupported model schema: {document.get('schema')!r}")
    core = {k: v for k, v in document.items() if k not in ("model_version", "integrity_sha256")}
    digest = _digest(core)
    expected_version = f"lr-{document['feature_version']}-{digest[:12]}"
    if digest != document["integrity_sha256"] or document["model_version"] != expected_version:
        raise ValueError(f"model integrity check failed: {path}")
    pre = document["preprocessing"]
    preprocessing = Preprocessing(
        feature_names=tuple(document["feature_names"]),
        nullable=tuple(pre["nullable"]),
        log1p=tuple(pre["log1p"]),
        medians=tuple(pre["medians"]),
        means=tuple(pre["means"]),
        scales=tuple(pre["scales"]),
    )
    if tuple(pre["output_names"]) != preprocessing.output_names:
        raise ValueError("model output columns are inconsistent")
    return LinearModel(
        model_version=document["model_version"],
        feature_version=document["feature_version"],
        preprocessing=preprocessing,
        coefficients=tuple(document["coefficients"]),
        intercept=document["intercept"],
        metadata=document["metadata"],
    )
