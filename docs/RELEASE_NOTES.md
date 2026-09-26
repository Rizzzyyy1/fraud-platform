# Release notes — v1.1.0 (real-data benchmark)

Adds an offline benchmark of the modelling method on real data. The platform, its synthetic data,
models, policies and results are unchanged.

* **ULB credit-card data** (real, anonymised, 2 days, 492 frauds), downloaded from OpenML with a
  checksum check and never committed.
* **Protocol committed before evaluation**; held-out window (elapsed hours 32–48) evaluated once:
  XGBoost AP 0.746 (95% CI 0.662–0.813), logistic regression 0.647 (0.507–0.752). Logistic
  regression had the higher validation AP. No model is selected from held-out results, no
  significance is claimed from the per-model intervals, and these models do not power the live
  scoring service.
* **Findings reported as measured:** a threshold fixed on validation produced a lower review rate
  than its 0.5% budget on the test window. A stratified random split scored higher AP for both
  models; this is descriptive only (different training sizes and test populations).
* **Data limits:** PCA was applied upstream by the data owner and its fitting scope cannot be
  verified; label-arrival times are unavailable; raw data is not redistributed.
* **Source provenance:** the owner-listed Kaggle copy (`mlg-ulb/creditcardfraud` v3, ODbL v1.0
  for the database, DbCL v1.0 for its contents) was compared cell by cell with the OpenML 1597 v1
  copy the benchmark used. The data rows are identical (`reports/external/ulb/source_comparison.md`).
  Kaggle is now the default acquisition route; the benchmark's recorded OpenML provenance is kept.
  The ODbL §4.3 notice is carried with the published results. The derived tables and models stay
  local (`THIRD_PARTY_NOTICES.md`).
* **Reproducibility rerun:** the frozen protocol was rerun with the original's selected
  configurations (no search). All 41 recorded values matched exactly in the recorded rerun
  environment; the original run's environment was not recorded. Two reruns gave byte-identical
  models and predictions. Those artifacts are saved locally and not published
  (`reports/external/ulb/reproduction.md`). The rerun is logged as a separate MLflow run, linked
  to the imported historical run.
* **Experiment tracking and dashboard:** the completed run is imported into MLflow as a historical
  run, and the dashboard's historical results show a labelled "Real-data offline benchmark" entry.
* **Integration-test harness:** the Redis-outage tests now verify the disposable Redis container
  while it is running and record its immutable ID. Ownership is an explicit label,
  `fraudplat.test-resource=disposable-redis`. It is set only by `docker-compose.test.yml` and by
  the CI Redis service container, which has no Compose labels. They pause it and clean up by that ID in a
  `finally` clause, without re-checking ownership through `docker port`, which reports nothing
  for a paused container on Docker Engine 29.8.0. Cleanup failures are attached to the original
  test failure. `make test-redis-recover` unpauses only the verified test container after an
  interrupted run.
* Reports: `reports/external/ulb/benchmark.md`, `reproduction.md`; how to reproduce:
  `docs/DATASET_CARD.md`; provenance: `THIRD_PARTY_NOTICES.md`.

---

# Release notes — v1.0.1 (maintenance)

No models, policies, thresholds or held-out results changed; no evaluation or benchmark was rerun.

* **Documentation matches its evidence.** Four current summaries disagreed with the reports they
  cite and were corrected: failure-drill scoreless reviews (156 of 750, not 157), dashboard
  response means (6.7–62.1 ms, not 9–46 ms), demo repeatability (2,221 requests and decisions,
  not 2,220), and the calibration statement, which now separates the comparison-stage models
  (raw ECE: logistic regression 0.0028–0.0034, XGBoost 0.0015–0.0027; Platt scaling did not
  consistently improve it) from the deployed artifact, which has no calibration evaluation of its
  own. `scripts/check_evidence.py` (in CI) recomputes these figures from the committed reports.
* **Browser smoke test in CI.** The real console and dashboard build in Chrome, against a seeded
  disposable database: sign-in, anonymous rejection, cookie protections, all views, pagination,
  decision identities, an analyst review that leaves the decision unchanged, and logout. It does
  not exercise the scoring API or the streaming pipeline.
* **Console:** the state directory can be set with `FRAUD_CONSOLE_STATE_DIR`, and an empty
  `FRAUD_CONSOLE_MODEL_URI` means "no registry" (used by the smoke test).
* **CI workflow header** no longer says the workflow has never run.

---

# Release notes — v1.0.0 (first public release)

**Repository description (GitHub "About"):** Locally deployed, production-oriented fraud
decisioning platform on synthetic data: idempotent real-time decisions, point-in-time Kafka/Redis
features, versioned model and policy deployment, and an analyst console.

**Topics:** fraud-detection, mlops, fastapi, kafka, redis, postgresql, mlflow, xgboost, react,
typescript, synthetic-data

## What this release is

A finished, inspectable first release that runs entirely on one machine at no cost, on
**synthetic** payment data. It is a portfolio project, not a production service.

* **Decisions:** FastAPI scoring with database-enforced idempotency and a transactional outbox;
  every decision stores its features, reason codes and exact model, policy and registry version.
* **Features:** point-in-time features over arrival time, maintained by a Kafka worker in Redis;
  Redis and in-memory implementations parity-tested; offline training uses the same rules.
* **Reliability:** at-least-once delivery with idempotent application; degraded-mode policy that
  flags decisions, then stops scoring after 60 s of staleness, then rejects past a backlog limit.
* **Models:** logistic regression (active) and XGBoost (candidate) on chronological folds; a
  once-only held-out evaluation; MLflow registry bundles resolved at startup; restart-based
  promotion and rollback.
* **Console:** authenticated React/TypeScript analyst console (backend-for-frontend; the API key
  never reaches the browser) with live activity, investigation with append-only reviews, model
  and system health, and bounded demo jobs including a failure drill.

## Active model and policy

`lr-f1-6f0ebad8fcc7` with the **review-only** policy `pol-6164cb21826d` (review ≥ 0.0381, no
automatic declines), committed under `releases/active/` and registered by `make setup`. This
policy has **no held-out result**; its development review rates were 0.88–1.03% per week.

The XGBoost candidate `xgb-f1-1f168d371c42` + `pol-c51799ac9da6` reached held-out AP 0.357 vs
0.319 for the baseline (with its frozen, decline-enabled policy) but is **not promoted**: sustained
100 rps latency did not reproducibly meet the pre-declared criterion.

## Evidence

Held-out evaluation `reports/release-1/test_evaluation.md` · serving diagnosis
`reports/serving_diagnosis/summary.md` · failure drill and walkthrough
`reports/dashboard/walkthrough.json` · repeatability `reports/dashboard/repeatability.json` ·
reset ownership `reports/dashboard/reset_ownership.json` · screenshots `docs/screenshots/` ·
demonstration video (attached to this release; real time, captions added).

## Known limitations

Synthetic data only; single machine; scenario 2 (compromised terminals, ~60% of simulated fraud)
essentially undetected; calibration was examined only for the comparison-stage models on development folds (raw ECE: logistic regression 0.0028–0.0034, XGBoost 0.0015–0.0027; Platt scaling did not consistently improve it); the deployed artifact `lr-f1-6f0ebad8fcc7` has no calibration evaluation of its own, so outputs are presented as risk scores, not probabilities; sustained latency inconsistent (cause not
established); occasional 50 ms feature-read timeouts produce scoreless reviews; console over
local HTTP with in-memory sessions; confirmed labels not connected to the console.

## Verification status

Verified locally (unit, integration, dashboard tests; clean-copy setup; browser walkthrough) and
on GitHub Actions for the release commit: 167 unit and 75 integration tests (PostgreSQL, Redis,
Kafka services), 14 dashboard tests and the production build (https://github.com/Rizzzyyy1/fraud-platform/actions/runs/36205313126).
