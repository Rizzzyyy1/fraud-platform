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
essentially undetected; calibration not evaluated; sustained latency inconsistent (cause not
established); occasional 50 ms feature-read timeouts produce scoreless reviews; console over
local HTTP with in-memory sessions; confirmed labels not connected to the console.

## Verification status

Verified locally (unit, integration, dashboard tests; clean-copy setup; browser walkthrough) and
on GitHub Actions for the release commit: 167 unit and 75 integration tests (PostgreSQL, Redis,
Kafka services), 14 dashboard tests and the production build (https://github.com/Rizzzyyy1/fraud-platform/actions/runs/36205313126).
