"""Serving-path parity on development fixtures (pre-test rows only).

Compares, for fixed pre-test feature vectors, the original scoring call (`model.score(features)`)
with the instrumented serving path (explicit input conversion + `score_matrix`), and the actions
each produces under every policy registered for that model. Test-period data is not read.

Run: `python -m fraudplat.release.parity`
"""

from __future__ import annotations

import json
import math
import tomllib
from pathlib import Path
from typing import Any

import numpy as np

from fraudplat.policy import load_policy
from fraudplat.training.artifacts import load_any_model
from fraudplat.training.compare import load_data, window

FIXTURE_DAYS = (123, 153)
ROWS = 5000
CASES = {
    "lr-f1-6f0ebad8fcc7": ["lr-f1-6f0ebad8fcc7.json", "lr-f1-6f0ebad8fcc7__review-only-v1.json"],
    "xgb-f1-1f168d371c42": ["xgb-f1-1f168d371c42.json"],
}


def serving_path(model: Any, features: dict[str, Any]) -> float:
    values = [math.nan if features[n] is None else float(features[n]) for n in model.feature_names]
    return float(model.score_matrix(np.asarray([values], dtype=np.float64))[0])


def main() -> int:
    with Path("configs/compare-v1.toml").open("rb") as handle:
        data = load_data(tomllib.load(handle))
    idx = np.flatnonzero(window(data, FIXTURE_DAYS))
    idx = idx[np.linspace(0, idx.size - 1, ROWS).astype(int)]
    names = load_any_model(Path("artifacts/models/lr-f1-6f0ebad8fcc7")).feature_names
    fixtures = [
        {n: (None if np.isnan(v) else float(v)) for n, v in zip(names, data.X[i], strict=True)}
        for i in idx
    ]
    result: dict[str, Any] = {"fixture_days": list(FIXTURE_DAYS), "rows": ROWS, "models": {}}
    ok = True
    for version, policies in CASES.items():
        model = load_any_model(Path("artifacts/models") / version)
        original = np.array([model.score(f) for f in fixtures])
        served = np.array([serving_path(model, f) for f in fixtures])
        diff = float(np.max(np.abs(original - served)))
        entry: dict[str, Any] = {"max_abs_score_difference": diff, "policies": {}}
        for pfile in policies:
            policy = load_policy(Path("artifacts/policies") / pfile)
            a = [policy.decide(float(s))[0] for s in original]
            b = [policy.decide(float(s))[0] for s in served]
            changed = sum(x != y for x, y in zip(a, b, strict=True))
            entry["policies"][policy.version] = {
                "action_changes": changed,
                "action_counts": {act: a.count(act) for act in ("approve", "review", "decline")},
            }
            ok &= changed == 0
        ok &= diff == 0.0
        result["models"][version] = entry
    result["parity"] = ok
    out = Path("reports/serving_diagnosis/parity.json")
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
