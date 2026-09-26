"""A separately versioned review-only policy for the deployed baseline model.

The active `production` bundle serves `lr-f1-6f0ebad8fcc7` with its frozen policy
`pol-82d9e16668ae`, which includes automatic declines. The intended first release is
review-only. This does not modify or reuse that frozen policy: it derives a *new* policy for the
same model with the release-1 method (threshold = score of the top-1% transaction on pre-test
days 123-146, no labels used, automatic decline disabled), writes it under a distinct file name,
and registers model + new policy as a new registry version.

No held-out result exists for this policy: the test period was evaluated once with the frozen
baseline policy and is not used again. Development-window review rates are reported.

Run: `python -m fraudplat.release.baseline_review_policy`
"""

from __future__ import annotations

import json
import math
import os
import tomllib
from pathlib import Path

import mlflow
import numpy as np

from fraudplat.config import Settings
from fraudplat.policy import write_policy
from fraudplat.registry import publish
from fraudplat.simulator.dataset import _code_revision
from fraudplat.training.artifacts import load_any_model
from fraudplat.training.compare import load_data, window

BASELINE = "lr-f1-6f0ebad8fcc7"
SELECTION_DAYS = (123, 146)
FORWARD_DAYS = (146, 153)
TARGET = 0.01


def main() -> int:
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    code = _code_revision()
    if code.endswith("+dirty"):
        raise SystemExit(f"refusing to derive a release policy from a dirty tree: {code}")
    with Path("configs/compare-v1.toml").open("rb") as handle:
        data = load_data(tomllib.load(handle))  # pre-test rows only
    model = load_any_model(Path("artifacts/models") / BASELINE)
    sel = window(data, SELECTION_DAYS)
    scores = model.score_matrix(data.X[sel])
    k = math.ceil(TARGET * scores.size)
    threshold = float(np.sort(scores)[::-1][k - 1])
    weeks = []
    for start in range(SELECTION_DAYS[0], FORWARD_DAYS[1], 7):
        end = min(start + 7, FORWARD_DAYS[1])
        w = window(data, (start, end))
        weeks.append(
            {
                "days": [start, end],
                "review_rate": float((model.score_matrix(data.X[w]) >= threshold).mean()),
            }
        )
    forward = window(data, FORWARD_DAYS)
    forward_rate = float((model.score_matrix(data.X[forward]) >= threshold).mean())
    path = Path("artifacts/policies") / f"{BASELINE}__review-only-v1.json"
    if path.exists():
        raise SystemExit(f"{path} exists; policies are write-once")
    policy = write_policy(
        path,
        review_threshold=threshold,
        decline_threshold=None,
        derived_for_model=BASELINE,
        derivation={
            "type": "review-only (automatic decline disabled)",
            "method": "release-1 method: top-1% score on the selection window, no labels",
            "review_capacity_target": TARGET,
            "selection_days": list(SELECTION_DAYS),
            "supersedes_for_serving": "pol-82d9e16668ae (frozen; unchanged)",
            "held_out_evaluation": "none; the test period is not reused",
            "code_revision": code,
        },
    )
    settings = Settings()
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_experiment("fraud-f1-releases")
    with mlflow.start_run(run_name="baseline-review-only-policy") as run:
        mlflow.set_tags(
            {"model_version": BASELINE, "policy_version": policy.version, "code_revision": code}
        )
        version = publish(
            settings.mlflow_tracking_uri,
            "fraud-risk-f1",
            Path("artifacts/models") / BASELINE,
            path,
            run.info.run_id,
            description="baseline model with a review-only policy (no test result)",
        )
    result = {
        "policy_version": policy.version,
        "review_threshold": threshold,
        "registry_version": version,
        "development_review_rates": weeks,
        "forward_check_review_rate": forward_rate,
    }
    out = Path("reports/serving_diagnosis/baseline_review_only_policy.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
