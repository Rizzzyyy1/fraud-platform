# ULB benchmark: reproducibility rerun

Generated from `reproduction.json` by `python -m fraudplat.external.reproduce --summarize`. The reference results (`benchmark.json`, `benchmark.md`) are unchanged.

## Three records, kept apart

| Record | When (UTC) | What it is |
|---|---|---|
| Original benchmark | 2026-09-26T03:52:30+00:00 | The one pre-registered evaluation. Metrics only; models and predictions were not saved. |
| MLflow import | 2026-09-26T13:15:55+00:00 | The original's committed results recorded afterwards as an imported historical run (start time set to the original evaluation). Nothing recomputed. |
| Reproducibility rerun | 2026-09-26T13:55:29+00:00 to 2026-09-26T13:57:03+00:00 | Same protocol code, windows, seeds and the original's selected configurations; no search, no new thresholds. Saves artifacts locally. Logged as its own MLflow run linked to the import. |

## Result

41 recorded values compared (validation AP, threshold, every test metric and interval bound, random-split contrast, per family); maximum absolute difference 0; all within 1e-09: yes.

Scope of this result: the values were reproduced identically in the rerun environment recorded below. The original run's environment was not recorded, so this does not show that the original ran in the same environment, nor that other platforms or library versions give the same digits.

| Family | Selected | Test AP reference | Test AP rerun | Threshold reference | Threshold rerun |
|---|---|---|---|---|---|
| lr | C=0.01 | 0.646621 | 0.646621 | 0.0226976 | 0.0226976 |
| xgb | max_depth=3, n_estimators=300 | 0.746157 | 0.746157 | 0.166904 | 0.166904 |

Saved models reloaded from disk give the same test scores (max abs difference: lr 0, xgb 0).

## Environment

* Dataset MD5 `178bcf9bb1f31a3dfe12d0e577884add`; code revision `152b84a0b1bc` (uncommitted changes: no); protocol SHA-256 `6bbbb5bdd9b0ce38…`; `uv.lock` SHA-256 `a91358fb3b0b4ef7…`
* Python 3.14.0, macOS-15.2 arm64; numpy 2.5.3, polars 1.44.2, scikit-learn 1.9.1, xgboost 3.4.1, scipy 1.18.1
* The original run did not record its environment. `uv.lock` and the protocol code are unchanged in the repository since the results commit; which versions were installed when the original ran is not known.

## Artifacts (local only, not published)

Written to `artifacts/external/ulb/reproduction/<rerun id>/` (git-ignored). Row-level predictions and model files stay local until their publication suitability is checked (see `THIRD_PARTY_NOTICES.md`).

| File | Bytes | SHA-256 |
|---|---|---|
| `config.json` | 395 | `ea75119ef12da4380f48a5b2db6fbe2e6ce7806d25f368390379f57f6970b2f4` |
| `lr/model.json` | 2,701 | `f29e1cfac4fe17fb28d9e77f5fe7df4f400b66e35b73c110a49a01c07c87e37c` |
| `predictions.parquet` | 2,018,617 | `a7fc0221535fbf171b7db2242785941b80a33c6e2baa42d86eaf1676f57f510f` |
| `run.json` | 12,457 | `61787eaf22da85913c58fd0d2e5de9d2993f86ea19fe72aadf17e2c600a54b66` |
| `schema.json` | 859 | `dc879574269fc32beb01335ba8bb91560623b0867b9c6de5cb8278b902779b1d` |
| `xgb/model.json` | 337,939 | `72b6cb55f8adf08876a3072912e35f5bfc78840520b27de1363c0f4b5579b837` |

Contains information from [Credit Card Fraud Detection](https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud), which is made available here under the [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/).
