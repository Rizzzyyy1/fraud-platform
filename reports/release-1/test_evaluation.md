# Held-out evaluation — release-1

Frozen manifest `releases/release-1/manifest.json` (sha256 `c89d22ee4bd0e8ee…`), committed before any test row was read. Evaluation code `572eaae`; started 2026-09-25T02:28:56+00:00. Test period: days 153–183 (synthetic sim-v2), evaluated **once**. Labels: eventual simulated labels.

> **Denominator:** 301,001 transactions were evaluated. The evaluation window [153, 183) in decision time was fixed in the release manifest before the test was read. Nine transactions happened on day 182 but arrived (and so were decided) on day 183, after the simulated period; the window excludes them. The follow-up delay count used day >= 153 with no upper bound and so included them. The window itself was predefined; the existence of these nine late arrivals was not specifically anticipated. The follow-up delay count below therefore reports 301,010.

Nothing was trained, tuned, recalibrated or re-thresholded on the test period. Features for test rows were reconstructed from the full history under the documented availability rules. 95% intervals: day-block bootstrap (500 replicates) under condition A.

## Condition A — immediate processing

301,001 transactions, 2,703 fraud.

| | Candidate (review-only) | Deployed baseline (review + decline) |
|---|---|---|
| Model / policy | `xgb-f1-1f168d371c42` / `pol-c51799ac9da6` | `lr-f1-6f0ebad8fcc7` / `pol-82d9e16668ae` |
| Thresholds | review ≥ 0.0223; no decline | review ≥ 0.0378; decline ≥ 0.0745 |
| Average precision | 0.357 (0.340–0.374) | 0.319 (0.299–0.336) |
| Flagged rate (reviewed + declined) | 3,090 = 1.03% (0.98%–1.07%) | 3,113 = 1.03% (1.01%–1.06%) |
| Reviewed (sent to analysts) | 3,090 = 1.03% | 2,410 = 0.80% |
| Automatically declined | — (disabled) | 703 = 0.23% (661 fraud, 42 legitimate) |
| Precision of flagged | 0.317 (0.300–0.338) | 0.292 (0.273–0.309) |
| Recall | 0.363 (0.345–0.378) | 0.336 (0.321–0.351) |
| Legitimate transactions reviewed / declined | 2,110 / 0 | 2,163 / 42 |
| Fraud amount captured | 73.71% | 71.73% |
| Top-1% ranking diagnostic (retrospective) | precision 0.325, recall 0.362 | precision 0.301, recall 0.336 |

The capacity target was 1.00% reviewed. "Flagged" = reviewed + automatically declined; for the review-only candidate the two are the same. Review/decline splits for the baseline are derived arithmetically from the frozen results (`test_evaluation_corrections.json`); the frozen JSON field `legitimate_sent_to_review` counts all flagged legitimate transactions.

### Scenarios

Population for scenario s: its fraud plus all legitimate rows; a transaction in several scenarios counts in each.

| Scenario | Positives | Candidate flagged (recall) | Candidate AP vs legit | Baseline flagged (recall) | Baseline AP vs legit |
|---|---|---|---|---|---|
| s1 | 172 | 172 (1.000) | 0.864 | 166 (0.965) | 0.278 |
| s2 | 1,653 | 16 (0.010) | 0.010 | 18 (0.011) | 0.007 |
| s3 | 886 | 800 (0.903) | 0.850 | 732 (0.826) | 0.777 |

Scenario 2 (compromised terminals) is essentially undetected by both models. Missing terminal fraud-history features remains a hypothesis, not a tested cause.

### Weekly windows (fixed policy)

| Days | Rows | Fraud | Candidate flagged rate / precision / recall / AP | Baseline flagged rate / precision / recall / AP |
|---|---|---|---|---|
| 153–160 | 70,387 | 621 | 0.96% / 0.315 / 0.341 / 0.339 | 1.05% / 0.279 / 0.333 / 0.303 |
| 160–167 | 69,703 | 602 | 1.02% / 0.306 / 0.362 / 0.358 | 1.03% / 0.276 / 0.329 / 0.309 |
| 167–174 | 70,639 | 651 | 1.08% / 0.319 / 0.375 / 0.371 | 1.03% / 0.312 / 0.350 / 0.349 |
| 174–183 | 90,272 | 829 | 1.04% / 0.326 / 0.369 / 0.360 | 1.02% / 0.298 / 0.332 / 0.317 |

## Condition B — simulated worker delay 1000 ms

Candidate AP 0.3573, flagged 1.03%, precision 0.317, recall 0.363; baseline AP 0.3188 — identical to condition A at the reported precision.
 A follow-up count (features only, no labels): of 301,010 test rows, 8 had a different feature vector with a 1 s processing delay and 1,067 with a 60 s delay (features only, no labels; test_rows counts decisions from day 153 onward, including 9 transactions decided on day 183 or later (late arrivals) that fall outside the evaluation window [153, 183)).

## Acceptance criteria (declared in the manifest before the test)

| Criterion | Observed | Result |
|---|---|---|
| Candidate AP > baseline AP (A) | 0.357 vs 0.319 | pass |
| Review rate in [0.5%, 2.0%] | 1.03% | pass |
| Flagged precision ≥ 0.20 | 0.317 | pass |
| Serving at 100 rps: p95 ≤ 100 ms, p99 ≤ 200 ms, error rate ≤ 0.1% | 0/4 candidate 60 s runs pass (achieved 80.5–87.5 rps, p95 2048–2356 ms); baseline control p95 46 ms | fail |

The release-session "baseline control" was a single 60 s run. The later bounded diagnosis (`reports/serving_diagnosis/summary.md`) found sustained performance inconsistent for both models (4/6 runs passed each); the baseline is the retained default, not a consistently passing control.
