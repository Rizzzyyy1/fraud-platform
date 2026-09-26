# Design

Production-oriented portfolio system on simulated payments. This document is the contract the
implementation is tested against. Sections marked *(planned)* are not implemented yet; see
[PROGRESS.md](PROGRESS.md).

## 1. Paths

```text
SYNC   client → FastAPI → validate → idempotency pre-check → read features (Redis, one Lua
       snapshot) → score (in-process model) → policy → BEGIN; INSERT decision; INSERT outbox;
       COMMIT → respond
ASYNC  outbox → publisher → Kafka decisions.v1 (key = customer_id) → feature worker → Redis
OFFLINE simulator → raw Parquet (immutable) → replay through feature code → splits → train/eval
       → MLflow (alias resolved to an immutable version at startup)
```

## 2. Idempotency (implemented)

* **Canonical hash** — SHA-256 of the *validated* request serialised with sorted keys and no
  whitespace; `event_time` in UTC as `YYYY-MM-DDTHH:MM:SS.ffffffZ`; currency upper-cased;
  integer amounts; identifiers compared exactly. `hash_version` is hashed and stored.
* **Durability** — `decisions.transaction_id` is the primary key. `INSERT … ON CONFLICT DO
  NOTHING RETURNING` decides the winner; a loser re-reads the committed row and compares
  hashes. The pre-check read is an optimisation only; a test bypasses it.
* **Outbox** — inserted in the same transaction. `UNIQUE (aggregate_id, event_type)` makes
  "one `decision.made` event per decision" a database invariant.
* **Responses** — 201 new, 200 replay (`idempotent_replay: true`; all persisted fields
  identical), 409 conflicting payload, 503 database unreachable (*durability not confirmed*;
  a failure during COMMIT may have stored the decision, and a same-payload retry resolves it),
  500 write rejected and rolled back.
* **Retries never re-score.** The first committed decision is authoritative.

## 3. Time semantics (implemented offline — Checkpoint B; ADR 0004)

| Field | Meaning |
|---|---|
| `event_time` | when the transaction happened (client-asserted) |
| `received_at` | API receipt |
| `decision_time` | when the decision was made |
| `persisted_at` | row insert time (`clock_timestamp()`), inside the committing transaction |
| `applied_at` | worker applied the event to Redis (state metadata) |
| `label_available_at` | when the fraud outcome becomes known |

**Live observation** for decision *d*: events *e* of the same entity with
`e.applied_at ≤ d.feature_read_at`, `d.event_time − W ≤ e.event_time < d.event_time`,
`e ≠ d`, not retention-trimmed.

**Offline reconstruction:** `available_at = received_at + δ`; *e* is eligible for *d* iff
`available_at ≤ d.decision_time` and it is in the event-time window. The main evaluation uses
δ = 0 and is labelled *immediate-processing assumption*. A separate experiment varies δ.
Historical `decision_time = received_at`. At equal instants availability is processed before
decisions (that is what `<=` means). The persisted feature vector is the audit record of what was
actually used. Code: `features/offline.py`.

**Parity** — shared feature code gives *logic parity* only. *Availability parity* (same history
visible) is measured by comparing persisted live vectors with offline reconstruction.

## 4. Freshness and stale-state policy (implemented; see §8 and ADR 0008)

Four separate signals: customer recency (a model feature, never a health signal); worker
heartbeat and `applied_at − persisted_at` delay; Kafka consumer lag; per-entity state metadata.
Redis unreachable or slower than `FRAUD_FEATURE_READ_TIMEOUT_MS` (default 50 ms) → no score,
`FEATURES_UNAVAILABLE`, policy returns `review`, decision persisted (implemented, tested).
Customer with no 30-day history → scored from train-median imputation plus missing indicators,
reason `COLD_START_CUSTOMER` (implemented, tested). Pipeline degraded (heartbeat > 10 s,
oldest unpublished outbox row > 30 s, or lag > 1000) → score normally, add
`PIPELINE_DEGRADED`, alert. Inactive and new customers are never "stale". Completeness of an
entity's history is not provable and is not claimed.

## 5. Feature-state update rules (in-memory and Redis implemented and parity-tested)

Each event is inserted once (member = `event_id`, score = `event_time`) into customer and
terminal histories; aggregates are derived at read time. Under three assumptions — one
immutable payload per `event_id`, event times inside the retention horizon, features are pure
functions of the in-window event set — updates commute and are idempotent.

* One Lua script updates customer and terminal state atomically (single-node Redis).
* One Lua script reads every feature of a decision: a consistent snapshot, possibly missing
  in-flight events.
* Horizon R = 30 d + 1 d late allowance from a per-namespace event-time watermark. Older
  events → `LATE_BEYOND_HORIZON` (DLQ once the worker exists). The horizon is checked *before*
  dedup, so an out-of-horizon duplicate is reported late in both implementations; a Redis/in-
  memory parity test found and fixed the earlier opposite ordering. Order-dependent at the
  boundary.
* Dedup record `payload_hash` per event, retained ≥ R. Same hash → no-op; different hash →
  no-op, DLQ `PAYLOAD_CONFLICT`, alert.
* Invalid or incomplete events are rejected before any write. v1 accepts one currency.
* Namespaces `live` and `replay:{run_id}` isolate keys; replays use `decisions.replay.v1`.
* Bootstrap applies only events with `available_at < C` (demo cutoff); the demo scores events
  with `decision_time ≥ C`. The split is by arrival, so an event that happened before C but
  arrived after it is scored, not bootstrapped.
* Windows are `[t − W, t)`; the decision's own event is additionally excluded by id. That id
  check is a redundant guard: the strict upper bound already excludes the own event.
* Feature version f1 (15 features) is listed in `features/spec.py`. Customer–terminal novelty
  is derived from the customer's 30-day history, so it needs no separate order-dependent state.

## 6. Data (implemented — Checkpoint B)

Independent implementation of the Fraud Detection Handbook's prose description (handbook code is
GPL-3.0 and was not used; ADR 0003). Immutable, hashed, regenerable; see
[DATASET_CARD.md](DATASET_CARD.md). Metrics will be reported per fraud scenario because
scenario 1 is trivially learnable.

## 7. Model scoring (implemented — Checkpoint C; ADRs 0005, 0006)

* Scoring reads one Redis snapshot, computes f1 features with the shared function, scores with
  the JSON linear-model artifact, and applies a validation-derived policy bound to that model.
* The persisted `features` column is the vector actually used, keyed by name. PostgreSQL `jsonb`
  does not preserve key order; the artifact's `feature_names` define the model input order.
* A decision does not write feature state; the event pipeline (§8) applies it asynchronously.
* Bootstrap for demonstrations loads only events available in `[C − R − 1 d, C)`; older events
  cannot be visible to decisions at or after C (tested in memory and in Redis).

## 8. Event pipeline (implemented; ADRs 0007, 0008)

```text
API ── one PostgreSQL transaction ──► decisions + outbox(stream)
publisher: lock unpublished rows (SKIP LOCKED) → produce (key customer_id, acks=all, idempotent)
           → wait for acks → mark only acked rows published → commit
Kafka:     fraud.decisions.v1 (6 partitions, key customer_id), fraud.decisions.dlq.v1
worker:    poll → validate → Lua apply to Redis (or DLQ) → commit that offset → heartbeat
```

* **Contract** `decision.made` v1 (`pipeline/events.py`): value is the outbox payload JSON; key
  is `customer_id`; headers repeat `event_id`, `event_type`, `schema_version`. Validation includes
  recomputing `payload_hash` from the transaction fields.
* **Names** derive from one namespace (`pipeline/streams.py`); consumer group
  `fraud-feature-worker.<live|replay.run>`; auto-commit off; `auto.offset.reset=earliest`.
* **Delivery** at-least-once; application idempotent (ADR 0007). Crash windows tested: after
  Kafka ack before outbox marking; after Redis update before offset commit.
* **Retries** publisher: exponential backoff up to 5 s between failing cycles, rows stay
  unpublished. Worker: 8 Redis attempts with backoff (≈ 12 s), then stop without committing.
* **Health** (ADR 0008): worker heartbeat + lag in `{ns}:pipeline:worker`; outbox backlog from
  PostgreSQL; `/readyz` distinguishes `ready`, `degraded`, `not_ready`; `/metrics` on the API,
  publisher (:9101) and worker (:9102).
* **Skew** is not removed by Kafka. A decision sees only events already applied. Measured in the
  continuous demo: with each transaction applied before the next is sent, every stored vector
  equals the offline reconstruction; in a burst with a deliberately slowed worker, some do not
  (numbers in PROGRESS.md and `reports/pipeline/demo.json`).
* **Lag semantics.** The worker distinguishes its *fetch position* (next offset it will read),
  its *committed progress* (next offset after the last message fully handled — Redis updated or
  DLQ acknowledged — and committed) and the broker's *end offset*. Reported lag is end offset
  minus committed progress, so a fetched message whose update is stuck still counts. End and
  committed offsets come from a background thread using a separate admin client (batched
  requests, no broker call in the processing loop); each value carries its age, and lag is
  reported as unknown (-1) when any assigned partition has no entry or the view is older than
  10 s. The heartbeat is written only by the processing loop, so a stuck update also stops the
  heartbeat (tested). The heartbeat records its namespace; the monitor ignores heartbeats from
  another namespace (tested).
* **Clocks.** PostgreSQL runs in the Colima VM; its clock was measured 64 ms ahead of the host.
  Delays are therefore measured on one clock: `decision_time` (API) to `applied_at` (worker),
  both host clocks. The monitor's oldest-unpublished age mixes the two clocks; the ~64 ms error
  is small against its 30 s and 15 min thresholds.
* **Redis sizing** (measured, conservative): see PROGRESS.md for populated-state and peak memory.
  `maxmemory` is 384 MB with `noeviction`, so exceeding it makes writes fail loudly (observed
  once during development) rather than evict state. The configuration is only claimed to hold the
  combination that was measured; capacity for more namespaces is not claimed.

## 9. Degraded modes (tested in `tests/integration/test_degraded_modes.py`)

| State | HTTP | Model score | Action | Reason codes added | Persisted |
|---|---|---|---|---|---|
| Unknown lag (worker starting, heartbeat fresh, lag -1) | 201 | yes | policy on score | `PIPELINE_DEGRADED`, `CONSUMER_LAG_UNKNOWN` | yes |
| Confirmed stale worker within grace (heartbeat 10–60 s) | 201 | yes | policy on score | `PIPELINE_DEGRADED`, `WORKER_HEARTBEAT_STALE` | yes |
| Stale beyond limit (heartbeat or oldest unpublished row > 60 s) | 201 | **no** | review | `PIPELINE_STALE_BEYOND_LIMIT`, pipeline reasons, `NO_SCORE` | yes |
| Unknown state (no heartbeat / lag unknown) for > 60 s | 201 | **no** | review | `PIPELINE_UNKNOWN_BEYOND_LIMIT`, pipeline reasons, `NO_SCORE` | yes |
| Feature store unreachable | 201 | **no** | review | `FEATURES_UNAVAILABLE`, `NO_SCORE` (+ pipeline reasons) | yes |
| Outbox backlog over hard limit | 503 | — | — | — | **no** |

Scoring continues through short degradation (startup, brief stalls) and stops once staleness is
confirmed for longer than `stale_scoring_limit_s` (60 s) or the state has been unknown for longer
than `unknown_state_limit_s` (60 s); boundaries are inclusive of the limit (exactly 60 s still
scores). An unreadable feature store always produces a scoreless review. Backlog rejection takes
precedence over all of these. `/readyz` is 200 `degraded` except in the rejection state (503).

## 10. Model registry (ADR 0010)

`FRAUD_MODEL_URI=models:/fraud-risk-f1@production` is resolved once at startup to an immutable
registry version; the verified bundle (model + policy) is cached with a pin; decisions record
`model_registry_ref`. Registry down at startup → verified pinned cache, else refuse to start.

## 11. Analyst console and dashboard (ADR 0011)

```text
browser ──cookie──▶ console (BFF, :8200) ──SQL (read; reviews append)──▶ PostgreSQL
                          │  ──/readyz, /metrics (≤ every 5 s)──▶ scoring API (:8110)
                          │  ──alias metadata (≤ every 30 s)────▶ MLflow
                          └─ fixed-argv job process ──X-API-Key──▶ scoring API
```

* `make dashboard` starts a supervised local deployment: scoring API (model from
  `models:/fraud-risk-f1@production`, resolved at startup), publisher, worker and console, in a
  dedicated `replay:dashboard-<ts>` namespace bootstrapped with history before day 140. Demo
  traffic is pre-test transactions decided on days [140, 153); the held-out period is never read.
* Views: live activity (range, filters, keyset pages, outcome buckets, API throughput and errors),
  investigation (queue ordered by score, scoreless last; stored record, feature values, reason
  codes, identities; append-only review), model and system health (running identity vs alias
  target, pipeline, processes, historical results labelled by split, reports).
* A missing score is shown as "Not scored". Reason codes are presented as policy and data-quality
  flags, not model explanations. Stale or unavailable telemetry is labelled, never zero-filled.
