"""XGBoost model artifact: native XGBoost JSON booster + an integrity-checked manifest.

Directory layout (write-once, read-only files):
    booster.json   XGBoost's own JSON model format (no pickle)
    manifest.json  schema, feature version, ordered feature names, booster SHA-256, XGBoost
                   version, training parameters, metadata, model_version, integrity digest

`model_version = xgb-<feature_version>-<first 12 hex of the manifest-core digest>`; the core
includes the booster's SHA-256, so any change to either file changes or breaks the version.
Loading verifies both hashes and that the booster's own feature names equal the manifest's.
Missing values are passed as NaN; XGBoost's learned default directions handle them.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import xgboost as xgb

from fraudplat.features.compute import FeatureValue

XGB_SCHEMA = "fraudplat.xgb-model/v1"


def _digest(core: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass
class XGBModel:
    model_version: str
    feature_version: str
    feature_names: tuple[str, ...]
    booster: xgb.Booster
    metadata: dict[str, Any] = field(default_factory=dict)

    def score_matrix(self, values: np.ndarray) -> np.ndarray:
        if values.ndim != 2 or values.shape[1] != len(self.feature_names):
            raise ValueError("input does not match the artifact's feature order")
        out: np.ndarray = self.booster.inplace_predict(values, missing=np.nan)
        return out.astype(np.float64)

    def score(self, features: dict[str, FeatureValue]) -> float:
        row = [
            math.nan if features[n] is None else float(features[n])  # type: ignore[arg-type]
            for n in self.feature_names
        ]
        return float(self.score_matrix(np.asarray([row], dtype=np.float64))[0])


def save_xgb_model(
    booster: xgb.Booster,
    *,
    feature_version: str,
    feature_names: tuple[str, ...],
    params: dict[str, Any],
    metadata: dict[str, Any],
    directory: Path,
) -> Path:
    if tuple(booster.feature_names or ()) != feature_names:
        raise ValueError("booster feature names differ from the declared feature order")
    raw = booster.save_raw(raw_format="json")
    booster_sha = hashlib.sha256(bytes(raw)).hexdigest()
    core = {
        "schema": XGB_SCHEMA,
        "feature_version": feature_version,
        "feature_names": list(feature_names),
        "booster_file": "booster.json",
        "booster_sha256": booster_sha,
        "xgboost_version": xgb.__version__,
        "params": params,
        "metadata": metadata,
    }
    digest = _digest(core)
    version = f"xgb-{feature_version}-{digest[:12]}"
    target = directory / version
    target.mkdir(parents=True, exist_ok=False)
    (target / "booster.json").write_bytes(bytes(raw))
    manifest = {**core, "model_version": version, "integrity_sha256": digest}
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    for path in target.iterdir():
        path.chmod(0o444)
    return target / "manifest.json"


def load_xgb_model(manifest_path: Path) -> XGBModel:
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema") != XGB_SCHEMA:
        raise ValueError(f"unsupported model schema: {manifest.get('schema')!r}")
    core = {k: v for k, v in manifest.items() if k not in ("model_version", "integrity_sha256")}
    digest = _digest(core)
    expected = f"xgb-{manifest['feature_version']}-{digest[:12]}"
    if digest != manifest["integrity_sha256"] or manifest["model_version"] != expected:
        raise ValueError(f"model integrity check failed: {manifest_path}")
    raw = (manifest_path.parent / manifest["booster_file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["booster_sha256"]:
        raise ValueError(f"booster file does not match its recorded hash: {manifest_path}")
    booster = xgb.Booster()
    booster.load_model(bytearray(raw))
    names = tuple(manifest["feature_names"])
    if tuple(booster.feature_names or ()) != names:
        raise ValueError("booster feature names differ from the manifest")
    booster.set_param({"nthread": 1})  # single-row serving: avoid thread start-up per call
    return XGBModel(
        manifest["model_version"], manifest["feature_version"], names, booster, manifest["metadata"]
    )
