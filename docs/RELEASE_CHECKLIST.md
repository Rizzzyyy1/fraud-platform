# Release checklist — v1.0.0

State on 2026-09-25. "Verified locally" means run on the development laptop (Apple M1). Remote CI
has **not** run and nothing has been pushed or published.

## Release blockers

| Blocker | Status |
|---|---|
| Fresh setup must not depend on local databases, caches, registry version numbers or untracked artifacts | **Done.** Active bundle committed in `releases/active/`; `make setup` registers by identity. Verified from a clean `git clone` with isolated services (registered as `fraud-risk-f1/1`, served correctly). |
| CI must provide every service the tests need, with explicit test-container targeting | **Done locally.** PostgreSQL, Redis, Kafka services, readiness step, timeouts; outage tests pause `FRAUD_TEST_REDIS_CONTAINER`. Local runs on disposable isolated services: 167 unit + 75 integration tests passed. **Remote run pending.** |
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
  calibration not evaluated; transaction-level metrics only.
* Sustained 100 rps latency inconsistent for both models (4/6 runs each); cause not established.
* The active review-only policy has no held-out result.
* Occasional 50 ms feature-read timeouts produce scoreless reviews under normal demo traffic.
* Redis is capped at 384 MB without eviction; long-running demos plus other namespaces can fill
  it (the worker then fails its writes and decisions become scoreless);
  `make dashboard-reset` reclaims the dashboard namespace.
* System sleep pauses the stack; keep the machine awake for demonstrations.
* Confirmed labels are not connected to the console; dispositions are not labels.

## Publication sequence (after approval)

1. Create the public repository under the account chosen for publication and push `main` (no tag yet).
2. Run the real remote CI; fix any failure and push until CI passes on the intended release commit.
3. Check the rendered README, screenshots and setup/installation links on GitHub.
4. Only then tag that commit `v1.0.0` and create the GitHub release from `docs/RELEASE_NOTES.md`,
   uploading `media/fraud-platform-demo.webm` (git-ignored, so it is attached to the release,
   not committed).
5. Add a prominent README link to the uploaded release asset in a docs-only commit, and confirm CI
   passes on it. Until then the README does not reference the video.

## Original requirements still deferred

Candidate promotion and a serving-latency fix; calibration; customer-level metrics; terminal
fraud-history features (scenario 2); shadow deployment; Prometheus/Grafana servers (metrics
endpoints exist); cloud deployment (outside the $0 constraint).
