# Release checklist — v1.0.0

State at the v1.0.0 release. "Verified locally" means run on the development laptop (Apple M1).
Remote CI passed on GitHub Actions for the release commit (https://github.com/Rizzzyyy1/fraud-platform/actions/runs/36205313126). Previously: remote CI
has **not** run and nothing has been pushed or published.

## Release blockers

| Blocker | Status |
|---|---|
| Fresh setup must not depend on local databases, caches, registry version numbers or untracked artifacts | **Done.** Active bundle committed in `releases/active/`; `make setup` registers by identity. Verified from a clean `git clone` with isolated services (registered as `fraud-risk-f1/1`, served correctly). |
| CI must provide every service the tests need, with explicit test-container targeting | **Done locally.** PostgreSQL, Redis, Kafka services, readiness step, timeouts; outage tests pause `FRAUD_TEST_REDIS_CONTAINER`. Local runs on disposable isolated services: 167 unit + 75 integration tests passed. **Remote CI passed** ([run](https://github.com/Rizzzyyy1/fraud-platform/actions/runs/36205313126)). |
| Documented recovery path must work (`make down`/`up` → `make dashboard-reset`) | **Done** (bug fixed and regression-tested). |
| No credentials, session material, account files or private runtime logs in the published files | **Prepared.** The private history (author metadata, historical `mlflow.db`) stays private; publication uses a sanitized snapshot without `.git` (`scripts/build_publication_copy.py`) that passes `scripts/privacy_check.py`. |
| License and provenance | **Prepared, holder undecided:** MIT scoped to the original code; the public copy's copyright holder is a placeholder until you choose one; `THIRD_PARTY_NOTICES.md` records what was consulted, adapted (simulator design and example parameters, one short attributed quote) and written independently. Needs your confirmation. |
| Demo must be reliable and reproducible | **Done.** Repeatability verified twice (isolated copy and clean clone); walkthrough asserts the drill completes with suppression. |

## Presentation improvements (hiring value)

| Item | Status |
|---|---|
| README opening a recruiter can read in 30 seconds: what, what I engineered, screenshot, four results with context, launch, limitations | Done |
| Console: decision summary stating the policy rule (not a model explanation), lifecycle from transaction to published event, readable feature values, clear action pills, honest stale states | Done |
| Real-time demo video with labelled captions, no credentials | Done (`media/fraud-platform-demo.webm`, 4 min 15 s, local; to attach to the GitHub release) |
| Screenshots refreshed from the final UI | Done (`docs/screenshots/`) |
| Release notes and repository description | Done (`docs/RELEASE_NOTES.md`) |

## Known limitations (stay documented)

* Synthetic data only; single machine; one API process; local HTTP with in-memory console sessions.
* Scenario 2 (compromised terminals, ~60% of simulated fraud) essentially undetected (recall ≈ 0.01);
  calibration was examined only for the comparison-stage models on development folds (raw ECE: logistic regression 0.0028–0.0034, XGBoost 0.0015–0.0027; Platt scaling did not consistently improve it). The deployed artifact `lr-f1-6f0ebad8fcc7` has no calibration evaluation of its own, so outputs are presented as risk scores, not probabilities. Transaction-level metrics only.
* Sustained 100 rps latency inconsistent for both models (4/6 runs each); cause not established.
* The active review-only policy has no held-out result.
* Occasional 50 ms feature-read timeouts produce scoreless reviews under normal demo traffic.
* Redis is capped at 384 MB without eviction; long-running demos plus other namespaces can fill
  it (the worker then fails its writes and decisions become scoreless);
  `make dashboard-reset` reclaims the dashboard namespace.
* System sleep pauses the stack; keep the machine awake for demonstrations.
* Confirmed labels are not connected to the console; dispositions are not labels.

## Publication (done)

Published as a sanitized snapshot (see `PROVENANCE.md`): repository created and `main` pushed; the
first CI run failed because the workflow used the `job` context in job-level `env` (not allowed);
fixed by moving it to step `env`, after which CI passed (https://github.com/Rizzzyyy1/fraud-platform/actions/runs/36205313126). README, screenshots and links
were checked on GitHub; the passing commit was tagged `v1.0.0` and released (https://github.com/Rizzzyyy1/fraud-platform/releases/tag/v1.0.0) with the demo
video attached, and the README links to it.

## Original requirements still deferred

Candidate promotion and a serving-latency fix; calibration of the deployed artifact; customer-level metrics; terminal
fraud-history features (scenario 2); shadow deployment; Prometheus/Grafana servers (metrics
endpoints exist); cloud deployment (outside the $0 constraint).
