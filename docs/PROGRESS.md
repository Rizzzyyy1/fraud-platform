# Progress

**CI:** `.github/workflows/ci.yml` runs lint, types, documentation links, unit tests, integration
tests on PostgreSQL, Redis and Kafka service containers, and the dashboard checks. It passed on
GitHub Actions for the v1.0.0 release commit (see *Publication* at the end); results in the
earlier sections are from local runs.

Status legend: **implemented + tested** · **planned** · *optional*.

## Checkpoint A — durable idempotency (implemented + tested)

| Item | Evidence |
|---|---|
| Contracts: integer minor units, one currency, UTC-normalised aware timestamps, strict ids | `tests/unit/test_contracts_and_hashing.py` |
| Canonical request hash (v1), hand-written expected literal | same file |
| Policy thresholds with inclusive boundaries; missing score never approved | `tests/unit/test_policy.py` |
| Decision + outbox in one transaction; DB-enforced uniqueness | `tests/integration/test_decisions_api.py` |
| Retry → stored decision (200); conflict → 409, row unchanged | same |
| 20 concurrent identical requests → 1 decision, 1 outbox event, identical persisted fields | same |
| Mixed concurrent payloads → one winner, rest replay or 409 | same |
| Race correctness without the pre-check (fast path bypassed) | same |
| Injected outbox failure → 500, no decision row | same |
| Unreachable database → 503, never 200; `/readyz` 503 | same |
| API-key authentication | same |

Mutation checks run once by hand: removing `ON CONFLICT` and removing the post-race hash
comparison each fail the suite.

The scorer is **temporary scaffolding** (`scaffold-0`: a hash of the transaction id). It has
no features and no predictive meaning.

**Measured:** PostgreSQL container ~33 MiB idle (512 MiB limit), Apple M1 / 16 GB.

**Recorded results (2026-09-24, local, PostgreSQL 17 in Colima):** `ruff check`, `ruff format
--check`, `mypy --strict` clean; 28 unit tests passed; 9 integration tests passed. Manual demo
against a running server: first request 201, identical retry 200 with identical persisted fields,
conflicting amount 409; one row in `decisions`, one unpublished row in `outbox`.

### Known limitations (Checkpoint A)

* The scorer is scaffolding; decisions carry `model_version=scaffold-0` and the reason code
  `SCAFFOLD_NOT_A_MODEL`. Policy `scaffold-0` thresholds are placeholders.
* Outbox rows are written but not yet published (publisher arrives with Kafka).
* Concurrency tests run 20 requests through one in-process ASGI client against a 10-connection
  pool; they exercise the database race, not multi-process server load.
* A 503 does not prove nothing was stored (see ADR 0002).
* 422 validation responses echo the offending input; acceptable for synthetic data, revisit
  before handling anything sensitive.

## Checkpoint B — dataset and time semantics (implemented + tested, offline only)

| Item | Status | Evidence |
|---|---|---|
| Simulator provenance: source material consulted, stated source licences, independent implementation, attribution, own choices listed | documented | ADR 0003, `docs/DATASET_CARD.md` |
| Immutable raw dataset: seed, full config, generator version, code revision, library versions, content + file SHA-256, read-only files, refuses overwrite | implemented + tested | `tests/unit/simulator/`, `make verify-data` |
| Determinism: same seed → same content; separate random streams (arrival changes do not alter transactions) | tested | same |
| Simulated payload hash equals the API request hash for the same transaction | tested | same |
| Time fields: `event_time`, `received_at`, `decision_time` (= `received_at`), `available_at`, `label_available_at` | implemented | `features/offline.py`, `labels.py`, DESIGN §3 |
| Late-arriving earlier transaction cannot influence an earlier decision (and the event-time shortcut demonstrably would) | tested | `tests/unit/features/test_point_in_time.py` |
| Event available exactly at decision time is visible (`<=`) | tested | same |
| Window boundaries (inclusive lower, exclusive upper), equal timestamps, future events | tested, hand-calculated | `tests/unit/features/test_windows_and_state.py` |
| Duplicates, conflicting payloads, out-of-order application (20 shuffles + all permutations of a subset), cold start, new terminal | tested, hand-calculated | same |
| Retention horizon rejection and its documented order dependence; `history_complete` flag | tested | same |
| Bootstrap strictly before cutoff, partition by arrival | tested | `test_point_in_time.py` |
| Customer inactivity vs pipeline staleness (`assess_pipeline` takes no customer data) | tested | `test_freshness_and_labels.py` |
| Label availability (`unknown` until available; legit only after maturity) | tested | same |

**Recorded results (2026-09-24, local):** ruff and mypy --strict clean; 79 unit tests passed;
9 integration tests passed (unchanged from A). Mutation checks run once by hand against the
feature code: making the window upper bound inclusive, the lower bound exclusive, processing
decisions before availability on ties, disabling dedup, and bootstrapping by event time each fail
the suite. Removing the own-event id exclusion does **not** fail it, because the strict upper
bound already excludes the own event; the id check is documented as a redundant guard.

**Demonstration** (`make data verify-data demo-b`, ~6 s): 58,643 synthetic transactions;
regeneration reproduces the content hash; cutoff at day 45 splits 43,949 bootstrap / 14,694
scoring (1 transaction happened before C but arrived after it and is scored). Under the
immediate-processing assumption, 650 decisions (1.11%) had a different feature vector when
history was filtered by event time only; the demo prints a concrete terminal-level example.
Whether those differences change scores or actions was not measured.

### Known limitations (Checkpoint B)

* sim-small-v1 fraud prevalence is 6.43%, because per-day scenario counts are fixed while the
  population is 10× smaller than the handbook example. Metrics on it describe that distribution.
  sim-v2 (handbook-scale population) is used for model work; see DATASET_CARD.md.
* Parity is *logic parity* only: shared code, one state class. No Redis backend yet, so no
  online/offline comparison exists.
* Offline reconstruction is single-threaded Python (~5 s for 58k rows); fine at this size, to be
  measured at larger sizes.
* `decision_time = received_at` is an assumption for historical data.

### Deferred (not in B, by scope)

Kafka, dashboard, Redis backend, model training, delay experiment beyond the unit test, label-
derived features.

## sim-v2 and pipeline benchmarks (implemented + measured)

* sim-v2: 1,834,753 transactions, prevalence 0.835% (s1 0.060%, s2 0.513%, s3 0.264%);
  generated from commit `7f9508c`; regeneration reproduces identical bytes. sim-small-v1 unchanged.
* Benchmarks at 10/25/50/100% (`reports/benchmarks/data_pipeline.md`): full size generates in
  9.1 s (1.38 GiB peak) and reconstructs in 97 s (1.64 GiB peak); per-row reconstruction cost
  rises ~1.8x across sizes, with no algorithmic growth found by profiling. A binary-search window
  rewrite cut reconstruction time by ~40% without changing any tested output.

## Checkpoint C — first genuine ML decision (implemented + tested)

| Item | Status | Evidence |
|---|---|---|
| Chronological timeline; test period (day >= 153) never reconstructed or loaded; `split_masks` refuses it | implemented + tested | ADR 0006, `tests/unit/training/` |
| Training eligibility = label available at the cutoff; excluded count reported (0 on sim-v2) | implemented + tested | same |
| Preprocessing fitted on training rows only | tested | same |
| Rules baselines and logistic regression; C and class weight chosen on validation | implemented | `reports/training/lr-f1-6f0ebad8fcc7/report.json` |
| JSON model + policy artifacts: feature order and version, preprocessing, dataset identity, training config, integrity digest; tamper rejected; NumPy scores equal scikit-learn | implemented + tested | ADR 0005, `tests/unit/training/` |
| Policy thresholds from validation; review rate reported | implemented | MODEL_CARD.md |
| Redis state: atomic apply + consistent read (Lua), namespaces | implemented + tested | `tests/integration/test_redis_state.py` |
| Redis/in-memory parity: hand-calculated fixture, duplicates, conflicts, 5 randomized shuffled streams crossing the horizon | tested | same |
| Replay namespace leaves live keys byte-identical; bootstrap only before cutoff; windowed = full bootstrap | tested | same, `tests/unit/features/test_point_in_time.py` |
| API scoring with model: stored feature vector and versions, retry 200, conflict 409, 20-way concurrency, cold start, feature store down -> persisted review, incompatible model -> readiness 503 and no decision, mismatched policy stops startup | tested | `tests/integration/test_model_scoring_api.py` |

**Recorded results (commit `39be6ea`, local):** ruff and mypy --strict clean; 93 unit tests and
26 integration tests passed.

**Model (validation, optimistic — it selected C and thresholds):** LR AP 0.274 vs rules 0.244
(amount) and 0.252 (amount ÷ customer mean). Policy review-or-decline rate 1.00% (review 0.80%,
decline 0.20%); decline precision 0.902. Observed: scenario 2 recall ~1% (cause untested).
Details in MODEL_CARD.md.

**Demonstration** (`make demo-c`, transcript `reports/demos/checkpoint_c.txt`): 321,167 events
bootstrapped into Redis in 10.7 s (cutoff day 140, validation period); transaction TX001403579
scored through the app with real PostgreSQL and Redis → 201, approve, score 0.00135; stored
15-feature vector equals an independent in-memory reconstruction; score recomputed from that
reconstruction is identical; identical retry → 200 with identical persisted fields; conflicting
retry → 409; one decision row and one outbox row.

### Findings during C

* The Redis/in-memory parity test found that the two implementations reported different statuses
  for duplicates older than the horizon (dedup records are dropped at different times). Both now
  check the horizon before dedup.
* The first development run picked C at the grid edge and LR under-performed both rules on AP;
  the grid was widened and class weighting added as a validation-selected choice (disclosed in
  the model card).
* PostgreSQL `jsonb` does not preserve key order; stored features are keyed by name and the
  artifact defines input order.

### Known limitations (C)

* A decision does not update feature state; the event pipeline (outbox → Kafka → worker) is next.
  Until then only the first post-cutoff decision is guaranteed to match offline reconstruction.
* No end-to-end API latency measurement yet; only in-process inference latency.
* Calibration not evaluated; customer-level metrics not computed; test period not yet used.
* MLflow is not integrated; artifacts are local files reproducible with `make train`.

### Deferred (by scope)

XGBoost, Kafka, dashboard, delay experiment on the model, calibration.

## Event pipeline checkpoint (implemented + tested)

Model `lr-f1-6f0ebad8fcc7`, feature version f1 and policy `pol-82d9e16668ae` were kept fixed. The
final test period was not read.

### Verified results

| Item | Evidence |
|---|---|
| `decision.made` v1 contract; invalid messages (malformed JSON, schema violation, key ≠ customer, header/payload mismatch, payload-hash mismatch) dead-lettered and committed | `test_invalid_events_are_dead_lettered_and_committed` |
| Outbox rows marked published only after Kafka acks; Kafka unreachable → nothing marked, API keeps accepting | `test_kafka_unavailable_decisions_accumulate_then_publish` |
| Publisher crash after ack, before marking → every event in Kafka twice → worker 12 applied + 12 duplicate, counts not inflated | `test_crash_after_ack_before_marking_causes_harmless_duplicate` |
| Worker crash after Redis update, before offset commit → redelivery recognised; features equal single application | `test_worker_crash_after_redis_update_before_commit` |
| Redis outage within retry budget recovers; beyond budget the worker stops without committing and a restart applies each event once | two `test_redis_outage_*` tests (Compose pause/unpause) |
| Conflicting duplicates (payload, event id both directions) rejected, dead-lettered, history unchanged | `test_conflicting_duplicates_are_observable_and_do_not_overwrite` |
| Out-of-order events give features equal to in-order application; an event 32 days behind the watermark is rejected as late | `test_out_of_order_and_too_late_events` |
| Redis/in-memory parity over 20 randomized streams, now including event-id conflicts; two divergences found and fixed (identity records now valid only inside the horizon) | `test_redis_state.py` |
| Stalled update (Redis reachable) is not reported healthy: heartbeat goes stale, lag ≠ 0 | `test_stalled_update_is_not_reported_healthy` |
| Live state (stale heartbeat, old backlog) does not affect a replay run's health; a heartbeat from another namespace is rejected | `test_live_state_does_not_influence_replay_health` |
| Degraded pipeline → decisions still made with `PIPELINE_DEGRADED`; backlog above limit → 503 for new decisions, retries answered; Redis down → `/readyz` 200 `degraded` and persisted review | `test_api_detects_*`, `test_backlog_limit_*`, `test_unavailable_feature_store_*` |

**Recorded results (local):** ruff and mypy --strict clean; 95 unit and 54 integration tests
passed; datasets verify.

**Continuous demonstration** (`make demo-pipeline`, `reports/pipeline/demo.json`):
* *Deterministic sequence*, each transaction applied before the next is sent: 300 of 300 stored
  feature vectors equal the offline point-in-time values, 0 score differences. For the six
  customers with repeat transactions, each earlier transaction adds exactly one to the next
  decision's 1-day count (e.g. 6 → 7 → 8), matching the expected values.
* *Burst with deliberate consumer delay* (20 ms per event, concurrency 20, 1,000 transactions):
  in this run 128 of 1,000 decisions had feature differences from the offline values (133 in an
  earlier run of the same experiment); all 128 scores differed, by at most 0.0021 (median
  0.00009); no action changed in this experiment. This does not show that lag is harmless under
  other workloads, delays or models. 253 decisions carried `PIPELINE_DEGRADED` /
  `CONSUMER_LAG_UNKNOWN` while the restarted worker had not yet reported a known lag.

**Delivery guarantee:** at-least-once delivery with idempotent feature application. The tests
show duplicate-safe effects in the scenarios above, inside the retention horizon; this is not a
general exactly-once guarantee (ADR 0007).

**Benchmark** (`reports/benchmarks/e2e.md`): at 25/50/100 rps every measured request was
model-scored (no degraded reviews, rejections, timeouts or errors). At 100 rps: p50 7.2 ms,
p95 12.1 ms, p99 17.7 ms (repeat run: 7.2 / 14.9 / 43.8 ms); decision → feature application p95
87 ms (repeat 95 ms), p99 100 ms (repeat 341 ms); backlog ≤ 6 rows; drain ≤ 0.3 s.

### Targets

| Target | Status |
|---|---|
| 100 rps | Met in two complete runs |
| p95 < 100 ms | Met (12.1, 14.9 ms) |
| p99 < 200 ms | Met (17.7, 43.8 ms) |
| Interrupted early run at 100 rps | Missed (p95 314 ms, p99 1,295 ms); not reproduced; cause not established |

### Findings during this checkpoint

* Consumer-lag calculation made per-partition broker calls inside the processing loop (~2.5 s
  each time); moved to a background thread using batched admin requests.
* Lag previously used the fetch position, which advances before an update completes; it now uses
  committed progress, carries the broker view's age, and is reported unknown when stale.
* Readiness returned 503 when Redis was down, contradicting the persisted-review design; now 200
  `degraded`.
* The database (Colima VM) clock runs ~64 ms ahead of the host; delay measurements now use one
  clock.
* Redis reached `maxmemory` when three populated namespaces accumulated; runs now delete their
  own namespace.
* An early version of the demo's pre-run cleanup deleted decision rows by transaction id
  regardless of owner and removed the local Checkpoint C `live` demo decision (TX001403579).
  Cleanup now touches only rows, keys, topics and consumer groups owned by the run, and excludes
  transactions already decided by another stream from the workload.

### Known limitations

* Tail latency varies considerably between runs on this shared laptop; one uvicorn process; the
  client shares the machine; rates above 100 rps were not measured to completion.
* Redis: the benchmark namespace used 142 MB for 321k events; with the `live` namespace
  present, peak was 252 MB of 384 MB. No capacity beyond that measured combination is claimed.
  Redis persistence is off locally; state is rebuilt by bootstrap/replay.
* Kafka container reached 688 MiB of its 1 GiB limit after these runs.
* Single broker, replication factor 1: no broker-failure testing.
* Pipeline-health thresholds (10 s heartbeat, 30 s outbox age, 1,000 lag, 50,000-row / 15 min
  reject) are configuration, not tuned.
* A late event beyond the 31-day horizon is rejected, not applied.
* Skew under delay was measured for one workload only.

### Deferred

XGBoost, new features, MLflow, dashboard, Prometheus/Grafana servers (metrics endpoints exist),
shadow deployment, test-period evaluation, the processing-delay experiment on the model,
calibration, customer-level metrics.

## Model evaluation and MLflow checkpoint (implemented + tested)

Feature version f1, the serving architecture and the final test period (day ≥ 153) were kept
unchanged / unused.

### Closed issues

* **Degraded modes** documented (DESIGN §9) and tested (`test_degraded_modes.py`): unknown lag
  and a stale worker keep model scoring with `PIPELINE_DEGRADED`; an unreadable feature store
  persists a scoreless review; the backlog limit returns 503 and persists nothing.
* **Cleanup ownership** regression test with overlapping live/replay transaction ids
  (`test_cleanup_ownership.py`): live rows are excluded, never deleted; only the run's own
  stream rows are removed. The Checkpoint C demo's reset now refuses to delete rows owned by
  another stream.
* **Deleted live demo record restored:** `make demo-c` recreated TX001403579 in the `live`
  stream (same model, policy and score 0.001352; new decision time). The demo now runs with
  pipeline monitoring disabled: with it on, the hours-old unpublished `live` outbox row from the
  Checkpoint A manual demo correctly triggered the backlog limit (503).

### Verified results

* Comparison (`reports/model_comparison/report.md`): 20 configurations × 3 pre-test folds
  (2 rules, 10 logistic regression, 8 XGBoost), 0 failures, all logged to MLflow
  (`fraud-f1-model-comparison`, 21 runs). A second run reproduced every mean AP exactly and a
  byte-identical XGBoost booster.
* Selection rule declared before running; it selected XGBoost `xgb-f1-fcf93e5d253d`:
  mean AP 0.333 vs 0.297 (gain +0.0357), better in 3/3 folds, serving constraints met.
* XGBoost artifact: native JSON + verified manifest; tamper, feature-order and pairing checks
  tested; artifact scores equal training scores exactly.
* Registry: baseline (v1), superseded v2, candidate (v3) registered with their policies;
  promotion (alias → v3) and rollback (alias → v1) each recorded in decisions
  (`model_registry_ref`); registry-down startup uses a verified pin, no pin → startup fails,
  tampered cache rejected (tests with a throwaway SQLite MLflow store).
* Tests: 103 unit, 63 integration passing (before the documentation commit).

### Recommendation

The declared rule selects XGBoost, and it ranks better in every fold. Most of the gain is on the
simulator's deliberately simple scenario 1; no model detects scenario 2; and on the latest fold
XGBoost's fixed decline threshold reached 0.857 precision against a 0.90 target. Recommended
next step: keep `production` on the baseline, re-derive or tighten the candidate's decline rule
on pre-test data (for example require a precision margin, or review-only), then freeze model and
policy and evaluate once on the untouched test period before any real promotion.

### Findings during this checkpoint

* The first comparison run logged to MLflow's local SQLite default because the tracking URI was
  not set; its registered bundle (v2) is tagged superseded. The runner now requires the
  configured server.
* Platt scaling on a separate calibration window did not consistently improve calibration.
* `search_model_versions` does not return aliases; aliases were verified with
  `get_model_version_by_alias`.

### Known limitations

* All comparison numbers are development evidence on synthetic data; the day-130 window has
  guided earlier choices. No test-period result exists.
* End-to-end API latency with the XGBoost model has not been benchmarked (in-process single-row
  p99 376 µs vs 24 µs for logistic regression).
* The registry bundle cache is per host; an alias change takes effect on restart.
* Transaction-level metrics only.

### Deferred

Promotion of the candidate to production, test-period evaluation, decline-rule revision, new
features (including terminal fraud history), dashboard, shadow deployment, cloud.

## Release-1 checkpoint: freeze, held-out evaluation, release validation

### Verified results

* **Staleness limits** (DESIGN §9): confirmed staleness beyond 60 s (worker heartbeat or oldest
  unpublished outbox row) and an unknown state lasting beyond 60 s now persist a scoreless review
  (`PIPELINE_STALE_BEYOND_LIMIT` / `PIPELINE_UNKNOWN_BEYOND_LIMIT`); within the limits scoring
  continues with `PIPELINE_DEGRADED`; backlog rejection takes precedence. Boundaries unit-tested
  (exactly 60 s still scores); API behaviour integration-tested.
* **Freeze** (`releases/release-1/manifest.json`, committed in `f2a33ed` before any test row was
  read): XGBoost refit on days 30–123 with the comparison-selected configuration (no new tuning),
  review-only policy `pol-c51799ac9da6` (threshold for a 1% review-capacity target chosen on days
  123–146 without labels), artifact and policy hashes, evaluation plan, acceptance criteria.
* **Held-out evaluation, run once** (`reports/release-1/test_evaluation.md`; guarded script refuses
  to rerun): candidate AP 0.357 vs baseline 0.319; review rate
  1.03%; precision 0.317; recall 0.363; 2,110 legitimate
  transactions reviewed; bootstrap intervals, scenario and weekly breakdowns included.
  Condition B (1 s simulated worker delay) identical; 8 test feature vectors differ at 1 s.
* **Serving:** 0/4 candidate 60 s runs pass (achieved 80.5–87.5 rps, p95 2048–2356 ms); one baseline run in the same session p95 46 ms (the later diagnosis found both models inconsistent, 4/6 passing runs each; the baseline is the retained default, not a consistently passing control). Every decision recorded the immutable identity
  (`xgb-f1-1f168d371c42` / `pol-c51799ac9da6` / `fraud-risk-f1/4`).
* **Promotion and rollback with restarts** (`reports/release-1/rollout.json`): an alias change
  without restart did not change the serving model; after restart the candidate served; after
  rollback and restart the baseline served. All decisions recorded model, policy and registry
  version. Final `production` alias: baseline (version 1).

### Acceptance criteria (declared before the test)

| Criterion | Result |
|---|---|
| Candidate AP > baseline AP | pass (0.357 vs 0.319) |
| Review rate in [0.5%, 2.0%] | pass (1.03%) |
| Flagged precision ≥ 0.20 | pass (0.317) |
| Serving at 100 rps (p95 ≤ 100 ms, p99 ≤ 200 ms, errors ≤ 0.1%) | **fail** (0/4 runs) |

### Release recommendation

Do not promote release-1. The candidate ranks better than the deployed baseline on untouched
data (AP 0.357 vs 0.319, non-overlapping bootstrap intervals) and its review-only policy held
the workload near target, but it failed the serving requirement in every sustained run of that session, while one
baseline run passed (the later diagnosis showed both models inconsistent). Keep `production` on the baseline. Next: diagnose
the serving degradation (for example by moving inference off the event loop or profiling the
process under sustained load), re-run the same serving benchmark, and promote only if it passes.
The model, policy and test result stay frozen; the test period must not be used again to choose
among fixes.

### Findings

* 20-second probes passed while every 60-second run failed; one 60-second baseline run passed in
  that session. The later diagnosis found the baseline inconsistent as well (4/6). Root cause not
  established.
* Condition B barely differs from A because consecutive transactions of the same customer or
  terminal are rarely seconds apart in this simulator; lag matters mainly under bursts.
* 9 transactions decided on day 183 (late arrivals) fall outside the evaluation window.

### Known limitations

Synthetic data only; single local machine; one uvicorn process; serving root cause open;
scenario 2 undetected; calibration not established; transaction-level metrics only.

### Deferred

Serving fix and re-validation, dashboard and portfolio presentation, new features, shadow
deployment, cloud.

## Bounded serving diagnosis (release-1 follow-up)

Model, features, frozen policies and held-out results unchanged; development traffic only.

### Verified results

* Harness committed before recorded runs (`b583f49`, `774ee9a`): per-stage Prometheus timings,
  event-loop lag, per-process CPU/RSS/threads, environment record, request-list hash.
* 12 runs at 100 rps with one identical request list (`reports/serving_diagnosis/summary.md`):
  baseline 4/6 pass, candidate 4/6 pass overall. Inference mean 0.09 ms (LR) / 0.26–0.29 ms
  (XGBoost); event-loop lag small in every run.
* Large stalls coincided with the harness's in-window `docker stats` sample (PostgreSQL pool-wait
  and transaction spikes, load-generator scheduling delay of hundreds of ms); without it no run
  stalled that badly (worst p95 39 ms). Residual p99 tail near 200 ms for either model.
* Serving-path parity on 5,000 pre-test fixtures: identical scores, no action changes.
* **Root cause:** of the release-session failures, not established; of this session's large
  stalls, largely the measurement harness. **No serving fix applied** — none was evidenced.

### Report and policy status corrections

* Frozen baseline flagged 3,113 = 2,410 reviewed + 703 declined (661 fraud, 42 legitimate);
  legitimate reviewed 2,163 (the frozen field counted 2,205 flagged). Derived arithmetically;
  the evaluation was not rerun.
* 301,010 vs 301,001: nine day-182 transactions decided on day 183, outside the predefined
  evaluation window.
* The active alias had served the frozen baseline policy with automatic declines, while the
  intended first release is review-only. A separately versioned review-only policy
  `pol-6164cb21826d` was derived on pre-test data (weekly review rates 0.88–1.03%, forward
  0.93%) and `production` now points to `fraud-risk-f1/5` (baseline +
  that policy). It has no held-out result. `fraud-risk-f1/1` remains for rollback.

### Decision

The candidate is not promoted: the pre-declared serving criterion was not met reproducibly,
even though no model-specific serving defect was found. The baseline with the review-only
policy is active. Moving on to the dashboard and portfolio presentation.

## Dashboard and portfolio checkpoint

No new models, features, SHAP or held-out reads; development/demo traffic only.

### Reporting checks

* **Running deployment vs registry target** (`reports/dashboard/deployment_identity.json`): the
  `production` alias resolves to `fraud-risk-f1/5` (created 13:20:07Z); the dashboard deployment's
  API process started afterwards and every persisted decision in its namespace records
  `lr-f1-6f0ebad8fcc7` / `pol-6164cb21826d` / `fraud-risk-f1/5`. The target and the running
  deployment are reported separately; an alias change applies only after a restart.
* Sustained 100 rps performance is inconsistent for **both** models (4/6 runs each). The baseline
  is the retained default, not a consistently passing control; the release documents now say so.
* Storage wording: checkpoints did not coincide with stall windows and mean fsync was ~87 µs; a
  fast mean does not exclude intermittent storage or VM stalls, which were not observable.
* The active review-only policy's development review rates are shown separately from the frozen
  policies' held-out results everywhere (console, README, model card).

### Delivered

* **Console backend** (`fraudplat.console`, ADR 0011, DESIGN §11): session-cookie auth with CSRF
  tokens and roles; the scoring API key stays server-side; bounded, keyset-paginated reads on a
  separate pool; append-only `reviews` (migration 0004; UPDATE/DELETE rules; FK protects reviewed
  decisions); telemetry cached (≤ every 5 s) with fresh/stale/unavailable states; registry target
  from metadata; two predefined jobs (traffic, failure drill) with enumerated parameters, fixed
  argv, one active job (partial unique index), run-owned request slices, verified worker pid and
  an expiring hold file.
* **Launcher** (`make dashboard`): dedicated `replay:dashboard-<ts>` namespace with prefixed ids,
  bootstrapped once; supervises API, publisher, worker and console with bounded restarts.
* **API:** `fraud_decision_responses_total{status}` counter; `/readyz` exposes `suppress_scoring`.
* **Dashboard** (React 19 + TypeScript, Vite, no UI libraries): live activity, investigation, and
  model/system health views; "Not scored" for missing scores; reason codes labelled as policy and
  data-quality flags; stale and unavailable telemetry shown explicitly.

### Verified

* `make check`: ruff, format, mypy --strict (97 files), 124 unit and 72 integration tests pass.
  Dashboard: typecheck, 11 component tests (loading/empty/error/stale, Not scored, identity
  mismatch, unavailable telemetry, review POST with CSRF, disposition required), build.
* Headless-Chrome walkthrough (`reports/dashboard/walkthrough.json`, `docs/screenshots/`):
  unauthenticated reads 401; HttpOnly/SameSite=Strict cookie; bundle without the API-key header;
  traffic job from the UI; review recorded with the decision byte-identical before and after;
  health and reports; browser-side console outage shows stale badges; failure drill degraded →
  scoring suppressed → recovered; sign-out revokes the session.
* Failure drill at 5/s: 750 decisions, 157 scoreless reviews, no HTTP errors; the worker was
  restarted by the launcher when the hold was released.
* Outside drills, the two 1,800-decision traffic jobs at 10/s produced 1 and 2 scoreless reviews
  (`FEATURES_UNAVAILABLE`: feature reads over the 50 ms timeout); cause not investigated.
* Polling overhead (one tab, live view): 44 console requests/min; mean console response 9–46 ms
  by endpoint; the scoring API receives at most one `/readyz` + `/metrics` pair per 5 s.

### Not done / limitations

Remote CI has not run (its integration job also lacks Kafka). No browser test in CI. Console is
local HTTP with in-memory sessions. Labels are not connected to the console. Nothing pushed or
published. See `docs/RELEASE_CHECKLIST.md`.

## Release-readiness pass

No features, models or policies changed; the performance investigation was not reopened.

* **CI configuration:** backend job now runs PostgreSQL, Redis and **Kafka** services with health
  checks, an explicit readiness step (`scripts/wait_for_services.py`) and per-step timeouts;
  dashboard job runs type check, tests and build. Redis-outage tests now pause an explicit
  container (`FRAUD_TEST_REDIS_CONTAINER`, the CI service id) instead of `docker compose pause`,
  which could not work in CI and, without `.env`, would have targeted the default project.
  Status: **not yet executed remotely**.
* **Reproducible setup:** the active bundle is committed under `releases/active/`; `make setup`
  registers it by identity (tags + file hashes) and sets `production`. On a clean isolated copy
  it registered as `fraud-risk-f1/1` and served correctly. Setup, start, demonstrate, stop and
  destructive reset are documented separately in the README with measured timings.
* **Demo repeatability** and **reset ownership** verified (`reports/dashboard/repeatability.json`,
  `reports/dashboard/reset_ownership.json`). Job outcomes now count HTTP 200 responses as
  `idempotent_replay`, so a demo that only retried would be visible.
* **Fixes found by the pass:** reset after recreated containers failed deleting an absent consumer
  group (fixed + regression test); the live view showed the last pipeline status without a stale
  marker when the console stopped answering (fixed + component test); `mlflow.db` was tracked
  (untracked; still in history).
* Local verification at the end of this pass: `make check` (ruff, format, mypy --strict on 98
  files, 124 unit and 73 integration tests), 12 dashboard component tests, production build,
  documentation link check, and a repeated headless-Chrome walkthrough with refreshed screenshots.
* See `docs/RELEASE_CHECKLIST.md` for verified items, limitations and deferred requirements.

## v1.0.0 release pass

No models, features, policies or thresholds changed; no new benchmark campaign.

* **Console polish:** decision summary states the policy rule applied (score vs thresholds of the
  verified local policy file; never a feature explanation); lifecycle from transaction to
  published event with same-clock durations; readable feature values next to stored values;
  scoreless decisions no longer labelled as scored; negative ages and row counts fixed.
* **Demonstration:** `dashboard/e2e/demo_recording.mjs` records a 4 min 15 s real-time video from
  the running application with labelled captions and no credentials on screen; the demo script
  matches it. Final screenshots and `reports/dashboard/walkthrough.json` refreshed; the
  walkthrough now fails unless the drill completes with suppression (one earlier run was
  distorted by the laptop sleeping mid-drill and is not used).
* **Clean-clone validation:** a real `git clone` with isolated services, empty uv/npm caches and
  no registry: install, services, data, setup (registered as version 1), build and first start in
  about 2 minutes plus downloads; repeatability check passed (2,221 requests → 2,221 decisions,
  0 dead-lettered events); CI-like unit and integration runs passed; runs left no tracked file
  modified except the report they write.
* **Found, documented:** Redis peaked at its 384 MB cap on the long-running development instance
  (dashboard namespace plus an older `live` namespace and integration tests on the same server).
* **Publication prep:** MIT license, attribution section, release notes, repository description,
  full-history credential scan (clean), `mlflow.db` history options analysed.

## Audit fixes and privacy-safe publication preparation

Driven by an independent local audit. No models, policies, thresholds or held-out results changed.

* **Pagination:** cursors are validated for arity, types, ISO-8601 timestamps with a timezone,
  identifier length and finite queue priority; every malformed cursor is a clean HTTP 400
  (previously some raised uncaught IndexError/TypeError). Unit tests for 23 malformed cursors and
  endpoint tests for valid multi-page pagination and malformed cursors on both the live-activity
  and investigation endpoints.
* **Destructive-test safeguards (`tests/safety.py`):** before migrating, truncating, flushing,
  deleting topics or pausing a container, the fixtures reject any database not named `*_test`,
  the application's database or server, the application's Redis or Kafka, and any outage
  container not named explicitly and publishing the test Redis port. Unit tests show unsafe
  targets are rejected before the reset runs. Integration tests now run on disposable services
  (`docker-compose.test.yml`, project `fraud-itest`, tmpfs database) via `make test-integration`.
* **Browser walkthrough:** every observation is an assertion that keeps evidence on failure
  (screenshot, HTML, observed values): anonymous reads 401, HttpOnly/SameSite=Strict cookie not
  visible to scripts, pagination on both lists, loading/empty/error/stale states, review
  persistence with the decision unchanged, the drill's degraded phase, scoring suppression with
  the exact stale-limit reason, full recovery, and logout invalidating the old cookie.
* **Bug found by the stronger walkthrough:** after "Next", the table briefly showed page 1's rows
  labelled "Page 2" (polling data was cleared in an effect, after the first render). Results are
  now tagged with their query; a render-log test fails on the old hook and passes on the fix.
* **Privacy:** `scripts/privacy_check.py` reports locations and categories without printing
  matches (personal terms from a local file outside the repository);
  `scripts/build_publication_copy.py` builds a sanitized copy from the committed tree with no
  `.git`, frozen artifacts byte-identical, and a provenance note for private commit references.
  The account handle was removed from the release checklist. Nothing was pushed or published and
  no history was rewritten.
* **Verification:** ruff, format, mypy --strict (100 files); 167 unit, 75 isolated integration and
  14 dashboard tests; production build; links; the full walkthrough (21 checks) with a neutral
  local test account.

## Next

* Your privacy decisions (copyright holder, publishing account) and a new explicit approval.

## Known issues

* Port 8000 is used by another local process; the API serves on 8100.

## Publication (v1.0.0)

Published as a sanitized snapshot of the private development repository with fresh history
(`PROVENANCE.md`). The first remote CI run failed on a workflow error (`job` context used in
job-level `env`); after moving it to step `env`, GitHub Actions passed: 167 unit and 75
integration tests on PostgreSQL, Redis and Kafka services, 14 dashboard tests and the production
build (https://github.com/Rizzzyyy1/fraud-platform/actions/runs/36205313126). Release: https://github.com/Rizzzyyy1/fraud-platform/releases/tag/v1.0.0 (demo video attached).


## v1.0.1 maintenance

* Current summaries reconciled with the committed evidence (drill 156 of 750; dashboard response
  means 6.7–62.1 ms; repeatability 2,221; calibration scoped to the comparison-stage models, not
  the deployed artifact); `scripts/check_evidence.py` enforces this in CI. Entries above are
  historical and describe the runs current at the time.
* Browser smoke test added to CI (real console and dashboard build, seeded disposable database;
  not the streaming system). The failure drill remains in the full local walkthrough.
* This public repository is the source of truth for future public development (`PROVENANCE.md`).

## v1.1.0 real-data benchmark

* Offline benchmark on the real ULB card-fraud data (`reports/external/ulb/benchmark.md`),
  protocol committed before the held-out window was evaluated once. Held-out AP: XGBoost 0.746,
  logistic regression 0.647 (logistic regression higher on validation; no model selected from
  held-out results). Validation threshold gave a lower-than-budgeted review rate; the random-split
  column is descriptive only. Imported into MLflow as a historical run and shown in the dashboard's
  historical results. No change to the platform or its results.
* Source provenance: the owner-listed Kaggle copy (`mlg-ulb/creditcardfraud` v3; ODbL v1.0 for the
  database, DbCL v1.0 for the contents) is identical to the OpenML 1597 v1 copy the benchmark used.
  The comparison covered columns, 284,807 rows in order, and 8,829,017 cells as text and as parsed
  numbers (`reports/external/ulb/source_comparison.md`). `make ulb-fetch` now uses Kaggle. The
  benchmark's recorded provenance stays OpenML. The applicable ODbL conditions (§4.3 notice, and
  §4.6 met by publishing the derivation code) are mapped in `THIRD_PARTY_NOTICES.md`.
* Reproducibility rerun (`reports/external/ulb/reproduction.md`): the frozen protocol was rerun with
  the original's selected configurations. All 41 recorded values matched exactly (max difference 0)
  in the recorded rerun environment; the original run's environment was not recorded.
  Saved models reload to identical scores. Two reruns produced byte-identical models and
  predictions. The artifacts are local only, under git-ignored `artifacts/`. The rerun is logged
  as a separate MLflow run that links to the imported run, which is unchanged.
