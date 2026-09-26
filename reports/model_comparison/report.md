# Model comparison (development evidence)

Generated from `report.json` at commit `8c74a0bb9dcc`; dataset `sim-v2` (`97481ae593458ff0…`, synthetic); feature version `f1`. MLflow experiment `fraud-f1-model-comparison`, parent run `e79eaee27268443c9cf63743ca8a8a41`.

**The final test period (day ≥ 153) was not used.** All windows below are pre-test and have been used for development; the day-130 evaluation window is the former validation period, which already guided earlier choices. These are development estimates, not untouched ones.

## Folds

| Fold | Fit days | Calibration days | Evaluation days | Fit rows (fraud) | Excluded unlabelled | Eval rows (fraud) |
|---|---|---|---|---|---|---|
| cutoff084 | [30, 44] | [44, 54] | [84, 107] | 140,093 (1,222) | 0 | 229,927 (2,118) |
| cutoff107 | [30, 67] | [67, 77] | [107, 130] | 370,547 (3,280) | 0 | 231,369 (2,052) |
| cutoff130 | [30, 90] | [90, 100] | [130, 153] | 601,543 (5,307) | 0 | 230,075 (1,891) |

Eligibility: fit and calibration rows need `label_available_at <= cutoff`. The 30-day gap between calibration end and cutoff means every such label is known.

## All attempted configurations

| Family | Configuration | Status | AP cutoff084 | AP cutoff107 | AP cutoff130 | Mean AP |
|---|---|---|---|---|---|---|
| rules | rule=amount | ok | 0.257 | 0.261 | 0.244 | 0.254 |
| rules | rule=amount_to_cust_mean_30d | ok | 0.279 | 0.286 | 0.252 | 0.272 |
| lr | C=0.0001, class_weight=none | ok | 0.300 | 0.310 | 0.268 | 0.293 |
| lr | C=0.001, class_weight=none | ok | 0.303 | 0.311 | 0.273 | 0.296 |
| lr | C=0.01, class_weight=none | ok | 0.311 | 0.312 | 0.268 | 0.297 |
| lr | C=0.1, class_weight=none | ok | 0.306 | 0.306 | 0.265 | 0.292 |
| lr | C=1.0, class_weight=none | ok | 0.308 | 0.304 | 0.264 | 0.292 |
| lr | C=0.0001, class_weight=balanced | ok | 0.302 | 0.311 | 0.266 | 0.293 |
| lr | C=0.001, class_weight=balanced | ok | 0.299 | 0.235 | 0.183 | 0.239 |
| lr | C=0.01, class_weight=balanced | ok | 0.232 | 0.189 | 0.158 | 0.193 |
| lr | C=0.1, class_weight=balanced | ok | 0.216 | 0.184 | 0.155 | 0.185 |
| lr | C=1.0, class_weight=balanced | ok | 0.214 | 0.183 | 0.155 | 0.184 |
| xgb | max_depth=3, n_estimators=200, scale_pos_weight=1.0 | ok | 0.333 | 0.343 | 0.313 | 0.330 |
| xgb | max_depth=3, n_estimators=200, scale_pos_weight=10.0 | ok | 0.324 | 0.335 | 0.306 | 0.321 |
| xgb | max_depth=3, n_estimators=500, scale_pos_weight=1.0 | ok | 0.336 | 0.348 | 0.315 | 0.333 |
| xgb | max_depth=3, n_estimators=500, scale_pos_weight=10.0 | ok | 0.325 | 0.341 | 0.310 | 0.325 |
| xgb | max_depth=6, n_estimators=200, scale_pos_weight=1.0 | ok | 0.332 | 0.349 | 0.315 | 0.332 |
| xgb | max_depth=6, n_estimators=200, scale_pos_weight=10.0 | ok | 0.321 | 0.345 | 0.312 | 0.326 |
| xgb | max_depth=6, n_estimators=500, scale_pos_weight=1.0 | ok | 0.323 | 0.348 | 0.311 | 0.328 |
| xgb | max_depth=6, n_estimators=500, scale_pos_weight=10.0 | ok | 0.313 | 0.343 | 0.309 | 0.322 |

## Representative per family (highest mean AP)

Retrospective top-k: the top 1% of each evaluation window by score, ties broken by earlier decision time then row position. This uses the whole window in hindsight.

| Family | Fold | AP | ROC AUC | Top-1% precision | Top-1% recall | FPR | Fraud amount captured |
|---|---|---|---|---|---|---|---|
| rules | cutoff084 | 0.279 | 0.662 | 0.281 | 0.305 | 0.0073 | 0.690 |
| rules | cutoff107 | 0.286 | 0.678 | 0.278 | 0.314 | 0.0073 | 0.691 |
| rules | cutoff130 | 0.252 | 0.662 | 0.238 | 0.290 | 0.0077 | 0.664 |
| lr | cutoff084 | 0.311 | 0.664 | 0.303 | 0.329 | 0.0070 | 0.730 |
| lr | cutoff107 | 0.312 | 0.680 | 0.297 | 0.335 | 0.0071 | 0.721 |
| lr | cutoff130 | 0.268 | 0.670 | 0.255 | 0.310 | 0.0075 | 0.692 |
| xgb | cutoff084 | 0.336 | 0.673 | 0.315 | 0.342 | 0.0069 | 0.741 |
| xgb | cutoff107 | 0.348 | 0.677 | 0.314 | 0.354 | 0.0069 | 0.742 |
| xgb | cutoff130 | 0.315 | 0.662 | 0.268 | 0.326 | 0.0074 | 0.708 |

## Fixed-threshold policy (what the online service can do)

Thresholds chosen on each fold's calibration window (review: top 1% of calibration scores; decline: lowest threshold with precision ≥ 0.90 on ≥ 20 calibration rows), then applied unchanged to the following evaluation window.

| Family | Fold | Review rate | Decline rate | Flagged precision | Flagged recall | Decline precision | Weekly review+decline rates |
|---|---|---|---|---|---|---|---|
| rules | cutoff084 | 0.80% | 0.23% | 0.273 | 0.305 | 0.885 | 0.98%, 1.11%, 1.01% |
| rules | cutoff107 | 0.77% | 0.20% | 0.284 | 0.311 | 0.918 | 0.97%, 0.99%, 0.97% |
| rules | cutoff130 | 0.78% | 0.17% | 0.248 | 0.290 | 0.918 | 1.01%, 0.90%, 0.97% |
| lr | cutoff084 | 0.82% | 0.27% | 0.280 | 0.330 | 0.884 | 1.07%, 1.11%, 1.08% |
| lr | cutoff107 | 0.75% | 0.22% | 0.308 | 0.334 | 0.930 | 0.96%, 0.99%, 0.95% |
| lr | cutoff130 | 0.77% | 0.19% | 0.263 | 0.308 | 0.904 | 1.01%, 0.97%, 0.93% |
| xgb | cutoff084 | 0.78% | 0.30% | 0.293 | 0.342 | 0.893 | 1.05%, 1.09%, 1.08% |
| xgb | cutoff107 | 0.74% | 0.28% | 0.309 | 0.354 | 0.927 | 1.03%, 1.01%, 1.01% |
| xgb | cutoff130 | 0.67% | 0.27% | 0.286 | 0.325 | 0.857 | 1.03%, 0.90%, 0.89% |

## Scenarios

Population for scenario s: fraud under s plus all legitimate rows; rows that are fraud only under other scenarios are excluded; a row in several scenarios is positive in each. Recall and precision refer to the retrospective top-1% set.

| Family | Fold | s1 AP / recall | s2 AP / recall | s3 AP / recall |
|---|---|---|---|---|
| rules | cutoff084 | 0.095 / 0.836 | 0.006 / 0.006 | 0.734 / 0.793 |
| rules | cutoff107 | 0.120 / 0.870 | 0.006 / 0.011 | 0.764 / 0.816 |
| rules | cutoff130 | 0.093 / 0.819 | 0.006 / 0.013 | 0.737 / 0.835 |
| lr | cutoff084 | 0.228 / 0.961 | 0.005 / 0.007 | 0.800 / 0.844 |
| lr | cutoff107 | 0.154 / 0.911 | 0.006 / 0.005 | 0.839 / 0.889 |
| lr | cutoff130 | 0.098 / 0.871 | 0.006 / 0.014 | 0.788 / 0.896 |
| xgb | cutoff084 | 0.723 / 0.992 | 0.006 / 0.005 | 0.834 / 0.884 |
| xgb | cutoff107 | 0.851 / 1.000 | 0.007 / 0.005 | 0.873 / 0.932 |
| xgb | cutoff130 | 0.820 / 1.000 | 0.008 / 0.010 | 0.861 / 0.932 |

Scenario 2 (compromised terminals) remains weakly detected. Missing terminal fraud-history features is a hypothesis for this, not a tested cause.

## Calibration (evaluation windows)

Raw scores, and scores after Platt scaling fitted on the calibration window (separate from the fit window). ECE over 10 equal-count bins.

| Family | Fold | Fraud rate | Mean raw score | Brier raw | ECE raw | Brier Platt | ECE Platt |
|---|---|---|---|---|---|---|---|
| lr | cutoff084 | 0.0092 | 0.0087 | 0.00789 | 0.00343 | 0.00767 | 0.00380 |
| lr | cutoff107 | 0.0089 | 0.0090 | 0.00735 | 0.00283 | 0.00729 | 0.00280 |
| lr | cutoff130 | 0.0082 | 0.0087 | 0.00692 | 0.00317 | 0.00682 | 0.00354 |
| xgb | cutoff084 | 0.0092 | 0.0087 | 0.00671 | 0.00271 | 0.00673 | 0.00283 |
| xgb | cutoff107 | 0.0089 | 0.0090 | 0.00630 | 0.00180 | 0.00633 | 0.00131 |
| xgb | cutoff130 | 0.0082 | 0.0084 | 0.00609 | 0.00153 | 0.00609 | 0.00158 |

## Serving checks (models fitted on the last fold)

| Family | Model version | Artifact size | Single-row p50 / p95 / p99 | Artifact vs training max abs diff |
|---|---|---|---|---|
| lr | `lr-f1-add35a7584bf` | 4,258 B | 19.8 / 20.9 / 23.8 µs | 0.00e+00 |
| xgb | `xgb-f1-fcf93e5d253d` | 604,428 B | 56.2 / 70.0 / 375.5 µs | 0.00e+00 |

## Selection (rule declared in `configs/compare-v1.toml` before running)

* primary_metric = mean_eval_average_precision
* xgboost_min_ap_gain = 0.02
* xgboost_min_fold_wins = 2
* max_single_row_p99_us = 1000
* max_artifact_bytes = 10000000

XGBoost minus logistic regression, mean AP: **+0.0357**; folds won by XGBoost: **3/3**; serving constraints met: **True**.

**Selected: xgb** — `xgb-f1-fcf93e5d253d` with policy `pol-1e442fb0f055`.

