# ULB credit-card benchmark (real data, offline)

Source: `reports/external/ulb/benchmark.json` (2026-09-26T03:52:30+00:00). Dataset: ULB credit-card fraud (OpenML 1597 v1), MD5 178bcf9bb1f31a3dfe12d0e577884add. Protocol fixed and committed before
the held-out window was evaluated (`src/fraudplat/external/benchmark.py`); evaluated once.

**Scope.** Real card transactions (two days, 2013), anonymised by the data owner into PCA
components. No customer or merchant identifiers, so the platform's streaming features cannot be
computed; this checks the modelling and evaluation method on real data and does not change the
platform, its models or its results.

| Window | Hours | Rows | Frauds |
|---|---|---|---|
| Train | 0–24 | 144,786 | 281 |
| Validation (model and threshold choice) | 24–32 | 17,739 | 75 |
| Held-out test | 32–48 | 122,282 | 136 |

| Model | Selected | Validation AP | Test AP (95% CI) | Test ROC AUC | Review rate at threshold | Precision | Recall (caught) | ECE raw | Random-split AP |
|---|---|---|---|---|---|---|---|---|---|
| Logistic regression | `{'C': 0.01}` | 0.864 | 0.647 (0.507–0.752) | 0.9736 | 0.22% | 0.410 | 0.809 (110/136) | 0.00034 | 0.725 |
| XGBoost | `{'max_depth': 3, 'n_estimators': 300}` | 0.828 | 0.746 (0.662–0.813) | 0.9708 | 0.16% | 0.542 | 0.801 (109/136) | 0.00055 | 0.826 |

Threshold: top 0.5% of validation scores, applied unchanged.
Intervals: 2000 resamples of test hour blocks.

**Findings**

* XGBoost ranks better than logistic regression on the held-out window, as on the simulator; the
  intervals overlap, so the size of the gap is uncertain with 136 test frauds.
* Validation AP was higher than test AP for both models; the validation window (8 night hours,
  75 frauds) is small and differs from the test period.
* The threshold chosen for a 0.5% review budget produced a
  lower review rate on the test window: a fixed threshold does not fix the workload when the
  score distribution shifts, the same lesson as on the simulator.
* A stratified random 70/30 split gave higher AP for both models. The two test sets differ, so
  this is indicative of optimism from ignoring time, not a controlled estimate.
* Raw ECE is small, but so is the fraud rate; scores are still treated as risk scores.
