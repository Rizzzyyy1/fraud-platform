# Model card

**Active (registry alias `production`; `fraud-risk-f1/5` in the recorded local registry — version
numbers are assigned per registry, and `make setup` finds or registers the bundle by identity):**
the logistic-regression
baseline `lr-f1-6f0ebad8fcc7` with a **review-only** policy `pol-6164cb21826d` (review ≥
0.0381, no automatic declines). This policy is new and separately versioned; it was
derived on pre-test days 123–146 with the release-1 method and **has no held-out result** (the
test period is not reused). The frozen baseline policy `pol-82d9e16668ae` (with automatic
declines) is unchanged (recorded registry: `fraud-risk-f1/1`, kept for rollback) and is **not
serving**; all held-out baseline numbers below refer to that frozen policy.

**Release-1 candidate:** passed the three held-out quality criteria but did **not reproducibly
pass the serving criterion** (release session: 0/4; bounded diagnosis: 3/4 with the harness's
in-window `docker stats` sample, 1/2 without it). The diagnosis found no model-specific
mechanism (`reports/serving_diagnosis/summary.md`); the candidate is not promoted. Local release
exercise on simulated data, not a production deployment.

## Model identities

| Role | Model artifact | Fit days (labels known by) | Configuration | Policy | Registry |
|---|---|---|---|---|---|
| **Active**: baseline + review-only policy | `lr-f1-6f0ebad8fcc7` | 30–100 (day 130) | LR, C = 0.001, no class weight | `pol-6164cb21826d`: review ≥ 0.0381, no decline (days 123–146; no held-out result) | `fraud-risk-f1/5`, alias `production` |
| Frozen baseline (evaluated) | `lr-f1-6f0ebad8fcc7` | 30–100 (day 130) | same | `pol-82d9e16668ae`: review ≥ 0.0378, decline ≥ 0.0745 (days 130–153) | `fraud-risk-f1/1` (rollback) |
| Comparison best LR | `lr-f1-add35a7584bf` | 30–90 (day 130) | LR, C = 0.01, no class weight | comparison diagnostics only | not registered |
| Comparison-selected XGBoost | `xgb-f1-fcf93e5d253d` | 30–90 (day 130) | depth 3, 500 trees, lr 0.1 | `pol-1e442fb0f055` (calibration window) | `fraud-risk-f1/3` |
| **Release-1 candidate** (final refit) | `xgb-f1-1f168d371c42` | 30–123 (day 153) | same XGBoost configuration, no new tuning | `pol-c51799ac9da6`: **review-only**, review ≥ 0.0223 | `fraud-risk-f1/4` |

Comparison results (`reports/model_comparison/report.md`) belong to the comparison models, not to
the deployed baseline; the baseline's own numbers are in the sections below and in the held-out
report. `fraud-risk-f1/2` is superseded (see PROGRESS.md).

## Release-1 candidate

* **Frozen before the test period was read:** `releases/release-1/manifest.json` (commit `f2a33ed`,
  sha256 `c89d22ee4bd0e8ee…`) records dataset identity, feature version and order,
  fit window and label rule, hyperparameters, refit procedure, artifact and policy hashes,
  evaluation plan and acceptance criteria.
* **Policy:** review-only (automatic decline disabled). Threshold = score of the top 1% of
  transactions on days 123–146 scored by the final model; no labels used. Review-capacity
  *target* 1.00%; observed fixed-threshold review rate by pre-test week: 1.05%, 1.07%, 0.93%, 0.85%, 1.00%; forward
  check days 146–153: 0.90%. A fixed threshold does not fix the workload.
* **Scores are risk scores.** Calibration diagnostics in the comparison did not justify a
  probability interpretation.

### Held-out evaluation (days 153–183, once; `reports/release-1/test_evaluation.md`)

Condition A (immediate processing), 301,001 transactions, 2,703 fraud; 95% day-block bootstrap
intervals.

| | Candidate (review-only) | Frozen baseline (with declines) |
|---|---|---|
| Average precision | 0.357 (0.340–0.374) | 0.319 (0.299–0.336) |
| Flagged rate (reviewed + declined) | 1.03% | 1.03% (incl. 703 automatic declines) |
| Precision / recall of flagged | 0.317 / 0.363 | 0.292 / 0.336 |
| Legitimate reviewed / declined | 2,110 / 0 | 2,163 / 42 |
| Fraud amount captured | 73.7% | 71.7% |
| Scenario recall s1 / s2 / s3 | 1.00 / 0.01 / 0.90 | 0.97 / 0.01 / 0.83 |

Condition B (simulated 1 s worker delay) gave identical metrics: only 8 test feature vectors
differed at 1 s (1,067 at 60 s). Scenario 2 remains essentially undetected by both models; missing
terminal fraud-history features is a hypothesis, not a tested cause.

### Serving (`reports/release-1/serving.json`)

0/4 candidate 60 s runs pass (achieved 80.5–87.5 rps, p95 2048–2356 ms); one baseline run in the same session p95 46 ms (the later diagnosis found both models inconsistent, 4/6 passing runs each; the baseline is the retained default, not a consistently passing control). 20-second probes of the candidate passed; the degradation appears under
sustained load. Established: the prediction itself costs ~0.1 ms per request and sequential
in-process request cost matches LR; limiting OpenMP threads did not help. Observed: API CPU
oscillating 24–75% (LR: steady 22–26%) and feature reads missing their 50 ms timeout, producing
15–30 scoreless reviews per run. **Root cause not established.**

## Baseline model: `lr-f1-6f0ebad8fcc7` (frozen policy `pol-82d9e16668ae`, registry version 1)

Source of every number: `reports/training/lr-f1-6f0ebad8fcc7/report.json`, produced by
`make train` at commit `39be6ea`. **Simulated data only**; nothing here describes real payments.

### What it is

Logistic regression (scikit-learn, lbfgs) on the 15 features of version f1 plus 4 missing-value
indicators, after median imputation, log1p on skewed non-negative features and standardisation,
all fitted on training rows only. Stored as an integrity-checked JSON artifact (ADR 0005). The
output is used as a ranking score; **calibration has not been evaluated**, so it is not presented
as a probability.

### Data and timeline (ADR 0006)

sim-v2 (content SHA-256 `97481ae5…`). Features reconstructed with arrival-time availability under
the immediate-processing assumption. Training examples: decisions on days 30–99 with labels known
at the day-130 cutoff — 701,434 rows, 6,211 fraud; 0 excluded as unlabelled. Validation: days
130–152 — 230,075 rows, 1,891 fraud (0.82%). **The test period (day ≥ 153) has not been used.**

### Selection

C ∈ {1e-5 … 10} × class weight ∈ {balanced, none}, chosen by validation average precision.
Selected: C = 0.001, no class weighting (interior of the grid). Among class-balanced candidates
the best C (1e-5) is at the grid's lower edge; they were not the overall choice. The grid was
widened after an earlier development run picked C = 0.001 at the edge of [0.001, 10].

### Validation results (optimistic: validation chose C and the thresholds)

Transaction-level. AP = Σ (Rₙ − Rₙ₋₁)·Pₙ (scikit-learn). "Top 1%" = the 2,301 highest-scored
validation transactions.

| Scorer | AP | ROC AUC | Top 1% precision | Top 1% recall | FPR | Fraud amount captured | Recall s1 / s2 / s3 |
|---|---|---|---|---|---|---|---|
| Logistic regression | 0.274 | 0.666 | 0.249 | 0.304 | 0.0076 | 0.691 | 0.948 / 0.013 / 0.856 |
| Rule: amount | 0.244 | 0.643 | 0.200 | 0.244 | 0.0081 | 0.655 | 1.000 / 0.010 / 0.636 |
| Rule: amount ÷ customer 30-day mean | 0.252 | 0.662 | 0.238 | 0.290 | 0.0077 | 0.664 | 0.819 / 0.013 / 0.835 |

### Policy (thresholds chosen on validation)

Assumptions (not measured): analysts can review ~1% of transactions; automatic decline only
where validation precision ≥ 90% on ≥ 50 rows.

| | Threshold | Validation rate | Precision | Recall |
|---|---|---|---|---|
| Decline | score ≥ 0.0745 | 0.20% | 0.902 | 0.218 |
| Review | 0.0378 ≤ score < 0.0745 | 0.80% | — | — |
| Review or decline combined | score ≥ 0.0378 | 1.00% | 0.249 | 0.304 |
| Approve | score < 0.0378 | 99.00% | | |

### Serving

Artifact scores equal scikit-learn's on all validation rows (max abs difference 0.0). In-process
single-row inference: p50 19.9 µs, p95 21.3 µs, p99 23.9 µs (2,000 calls, no I/O). End-to-end API
latency through PostgreSQL, Redis and the pipeline is in `reports/benchmarks/e2e.md`.

### Limitations

* Observed: scenario 2 (compromised terminals) is essentially undetected on validation (recall
  about 1%); most flagged fraud comes from scenarios 1 and 3. A plausible explanation is that f1
  has no terminal fraud-history feature, but that explanation has not been tested (for example
  by an ablation adding such a feature).
* A linear model on log-amount approximates scenario 1's sharp amount rule imperfectly (0.948 vs
  the amount rule's 1.000).
* Validation metrics are optimistic; no final test result exists yet.
* Transaction-level metrics only; customer-level review metrics are not computed.
* Trained under the immediate-processing assumption; the effect of processing delay has not been
  measured on this model.

## Comparison-selected XGBoost: `xgb-f1-fcf93e5d253d` (registry version 3; refitted for release-1 as `xgb-f1-1f168d371c42`)

Source: `reports/model_comparison/report.{json,md}` (commit `8c74a0b`, MLflow
run `e79eaee27268443c9cf63743ca8a8a41`). **Development evidence only**: three pre-test temporal
backtests (evaluation windows days 84–107, 107–130, 130–153; the last is the former validation
period). The final test period (day ≥ 153) has not been used.

XGBoost (`max_depth=3`, 500 trees, learning rate 0.1, no class weighting; native JSON artifact,
ADR 0009), fitted on days 30–90 with labels known at day 130; thresholds from the calibration
window days 90–100. It was selected by the rule declared in `configs/compare-v1.toml` before
running: mean AP gain over logistic regression +0.0357 (≥ 0.02 required),
better AP in 3/3 folds, serving constraints met. A second run reproduced all 20
configurations' mean AP exactly and a byte-identical booster.

The `lr` rows below are logistic regression's best configuration *under the same protocol*
(C = 0.01, no class weighting, fitted on days 30–90; artifact `lr-f1-*` in the report), not the
production baseline above (C = 0.001, fitted on days 30–100), so both families are compared on
identical data.

| Family | Fold | AP | Top-1% precision | Top-1% recall | Fixed-threshold review rate | Decline rate | Decline precision |
|---|---|---|---|---|---|---|---|
| lr | cutoff084 | 0.311 | 0.303 | 0.329 | 0.82% | 0.27% | 0.884 |
| lr | cutoff107 | 0.312 | 0.297 | 0.335 | 0.75% | 0.22% | 0.930 |
| lr | cutoff130 | 0.268 | 0.255 | 0.310 | 0.77% | 0.19% | 0.904 |
| xgb | cutoff084 | 0.336 | 0.315 | 0.342 | 0.78% | 0.30% | 0.893 |
| xgb | cutoff107 | 0.348 | 0.314 | 0.354 | 0.74% | 0.28% | 0.927 |
| xgb | cutoff130 | 0.315 | 0.268 | 0.326 | 0.67% | 0.27% | 0.857 |

Retrospective top-1%: the whole window ranked in hindsight (ties: earlier decision, then row
order). Fixed thresholds: set on the preceding calibration window and applied forward — what the
service can actually do.

Scenario AP against legitimate rows (population = that scenario's fraud + all legitimate rows):

| Family | Fold | s1 | s2 | s3 |
|---|---|---|---|---|
| lr | cutoff084 | 0.228 | 0.005 | 0.800 |
| lr | cutoff107 | 0.154 | 0.006 | 0.839 |
| lr | cutoff130 | 0.098 | 0.006 | 0.788 |
| xgb | cutoff084 | 0.723 | 0.006 | 0.834 |
| xgb | cutoff107 | 0.851 | 0.007 | 0.873 |
| xgb | cutoff130 | 0.820 | 0.008 | 0.861 |

* The AP gain is concentrated in scenario 1 (the simulator's deliberately simple "amount > 220"
  rule), which trees represent exactly and the log-linear model does not. Scenario 3 improves
  modestly. **Scenario 2 is not detected by any model (AP ≈ 0.006, near its base rate)**;
  missing terminal fraud-history features is a hypothesis, not a tested cause.
* On the latest fold the fixed decline threshold reached precision 0.857, below the 0.90
  it was derived to meet on the calibration window; logistic regression reached 0.904.
* Calibration: mean raw score is close to the fraud rate in each window (ECE
  0.0015–0.0027); Platt scaling fitted on the calibration window did not
  improve it consistently. Scores remain **risk scores**; no probability claim is made.
* Serving: artifact 604,428 B vs 4,258 B; single-row in-process
  inference p50 / p99 56 / 376 µs vs 20 / 24 µs. End-to-end API latency
  with XGBoost has not been benchmarked.

**Status:** registered, promotion and rollback demonstrated (`reports/model_comparison/
release_demo.json`); the `production` alias remains on the baseline (version 1).
