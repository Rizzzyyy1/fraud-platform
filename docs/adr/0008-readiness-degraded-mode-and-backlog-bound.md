# ADR 0008 — Readiness, degraded mode, and a bound on outbox growth

**Status:** accepted

**Context.** The API is designed to keep deciding when the feature store or the event pipeline is
impaired (Redis down → persisted `review`; pipeline lagging → decisions flagged). An earlier
version returned 503 from `/readyz` when Redis was down, which would make an orchestrator route
traffic away and make the persisted-review path unreachable. Separately, if Kafka stays down the
outbox grows without limit.

**Decision.**
* `/readyz` returns 503 only when a new decision cannot be accepted and stored: database
  unreachable, model incompatible with the feature code, or pipeline past its hard limit.
  Feature-store outages and pipeline degradation return 200 with `status: degraded` and reasons.
* Pipeline health comes from pipeline signals only: outbox backlog and oldest-row age
  (PostgreSQL), worker heartbeat age and reported consumer lag (Redis key written by the worker).
  A stalled worker is detected while Redis is healthy because its heartbeat stops advancing.
* Degraded (heartbeat > 10 s or missing, oldest unpublished > 30 s, lag > 1,000 or unknown):
  decisions are made normally and carry `PIPELINE_DEGRADED` plus the specific reason.
* Reject (backlog > 50,000 rows or oldest unpublished > 15 min): new decisions return 503
  `pipeline_backlog_limit`; idempotent retries of stored decisions are still answered.
* Customer inactivity never contributes to any of these states.

**Consequences.** Thresholds are configuration, not tuned values. The reject limit trades
availability for bounded storage and bounded feature staleness; the right values depend on
real traffic and recovery objectives, which this project does not have.
