# Active serving bundle

The model-policy pair served by the local deployment, committed so a fresh clone can serve
without retraining. Both files are integrity-checked JSON (ADR 0005): loading fails if either
file is edited.

| File | Identity | sha256 |
|---|---|---|
| `model/model.json` | `lr-f1-6f0ebad8fcc7` (logistic regression, feature version f1; `make train`, commit `39be6ea`) | `bc23cc37…42cb0e` |
| `policy.json` | `pol-6164cb21826d` (review-only, review ≥ 0.0381, no automatic decline; derived for that model on pre-test days 123–146, `fraudplat.release.baseline_review_policy`, commit `e622374`) | `9e9a9db2…0488d` |

The policy has **no held-out result**; see [docs/MODEL_CARD.md](../../docs/MODEL_CARD.md).
`make setup` registers this bundle in MLflow by identity and points `production` at it.
