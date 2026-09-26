"""Load any supported model artifact by its declared schema, with its integrity checks.

Supported: `fraudplat.linear-model/v1` (a `model.json` file, ADR 0005) and
`fraudplat.xgb-model/v1` (a directory with `manifest.json` + `booster.json`, ADR 0009).
Both expose `model_version`, `feature_version`, `feature_names`, `score(features)` and
`score_matrix(values)`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

import numpy as np

from fraudplat.features.compute import FeatureValue
from fraudplat.training.model import MODEL_SCHEMA, load_model
from fraudplat.training.xgb_model import XGB_SCHEMA, load_xgb_model


class ServableModel(Protocol):
    @property
    def model_version(self) -> str: ...

    @property
    def feature_version(self) -> str: ...

    @property
    def feature_names(self) -> tuple[str, ...]: ...

    def score(self, features: dict[str, FeatureValue]) -> float: ...

    def score_matrix(self, values: np.ndarray) -> np.ndarray: ...


def artifact_file(path: Path) -> Path:
    """Accept a file or an artifact directory."""
    if path.is_dir():
        for name in ("manifest.json", "model.json"):
            if (path / name).exists():
                return path / name
        raise FileNotFoundError(f"no model manifest in {path}")
    return path


def load_any_model(path: Path) -> ServableModel:
    file = artifact_file(path)
    schema = json.loads(file.read_text()).get("schema")
    if schema == MODEL_SCHEMA:
        return load_model(file)
    if schema == XGB_SCHEMA:
        return load_xgb_model(file)
    raise ValueError(f"unsupported model schema {schema!r} in {file}")
