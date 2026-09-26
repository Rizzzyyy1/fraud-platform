# ULB credit-card benchmark (real data, offline)

Source: `reports/external/ulb/benchmark.json` (2026-09-26T03:52:30+00:00). Dataset: ULB credit-card fraud (OpenML 1597 v1), MD5 178bcf9bb1f31a3dfe12d0e577884add. Protocol fixed and committed before
the held-out window was evaluated (`src/fraudplat/external/benchmark.py`); evaluated once. These
models do not power the live scoring service, and their AP is not comparable with the synthetic
platform's AP: the datasets, features and tasks differ.

**Data provenance and limits.** Real card transactions over 48 hours (2013), released by the data
owner with features V1–V28 already transformed by PCA. The PCA was performed upstream; whether it
was fitted on all 48 hours or on a subset cannot be verified from the released features, so some
information from the held-out hours may be embedded in the features of every window. `Time` is
elapsed seconds since the first transaction; it does not give time of day, so windows are
described in elapsed hours. Actual label-arrival times are unavailable, so labels are treated as
known. There are no customer or merchant identifiers. Licensing metadata is recorded separately in
`THIRD_PARTY_NOTICES.md`; the raw data is not redistributed.

| Window | Elapsed hours | Rows | Frauds |
|---|---|---|---|
| Train | 0–24 | 144,786 | 281 |
| Validation (model and threshold choice) | 24–32 | 17,739 | 75 |
| Held-out test | 32–48 | 122,282 | 136 |

| Model | Selected | Validation AP | Test AP (95% CI) | Test ROC AUC | Review rate at threshold | Precision | Recall (caught) | ECE raw | Random-split AP |
|---|---|---|---|---|---|---|---|---|---|
| Logistic regression | `{'C': 0.01}` | 0.864 | 0.647 (0.507–0.752) | 0.9736 | 0.22% | 0.410 | 0.809 (110/136) | 0.00034 | 0.725 |
| XGBoost | `{'max_depth': 3, 'n_estimators': 300}` | 0.828 | 0.746 (0.662–0.813) | 0.9708 | 0.16% | 0.542 | 0.801 (109/136) | 0.00055 | 0.826 |

Threshold: top 0.5% of validation scores, applied unchanged. Intervals: 2000 resamples of test hour blocks
(per model, not paired).

**Findings**

* Logistic regression had the higher validation AP (0.864 vs
  0.828); XGBoost had the higher held-out AP
  (0.746 vs 0.647).
  No model is selected for deployment from these results; choosing by held-out AP would use the
  test window for a decision.
* The confidence intervals are computed per model. No paired comparison or significance test was
  run, so no claim is made about whether the held-out difference is statistically significant.
* Validation AP exceeded test AP for both models. The validation window (elapsed hours 24–32,
  75 frauds) is small, and its composition differs from the test window.
* The threshold chosen for a 0.5% review budget produced review rates of
  0.16% (XGBoost) and
  0.22% (logistic regression) on the
  test window: a fixed threshold did not hold the intended workload when the score distribution
  shifted.
* Random-split column: the same configurations fitted on a stratified random 70/30 split of all 48
  hours scored higher AP. This is a descriptive comparison only: the training sets differ in size
  (70% of 48 hours vs the first 24 hours) and the test populations differ, so it neither proves
  leakage nor quantifies optimism from ignoring time.
* Raw ECE is small, but so is the fraud rate; scores are treated as risk scores.

**Not saved.** The fitted models and per-transaction predictions were not saved by the protocol run;
only the aggregate metrics above exist. They are not recomputed here.
