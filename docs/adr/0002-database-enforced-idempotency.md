# ADR 0002 — Idempotency enforced by PostgreSQL constraints

**Status:** accepted

**Context.** Clients retry on timeouts, and concurrent duplicates must not create two durable
decisions or two outbox events. Application-level locks or a Redis "in-flight" key would add a
second source of truth that can disagree with the audit table.

**Decision.** The decisions table's primary key is the transaction id. The service scores, then
runs `INSERT … ON CONFLICT DO NOTHING RETURNING` and the outbox insert in one transaction. A
request that loses the race re-reads the committed row and compares canonical request hashes:
equal → replay (200), different → 409. The outbox has `UNIQUE (aggregate_id, event_type)`.

**Consequences.**
* Concurrent duplicates may each compute a score; only the winner's is stored and returned.
  Scoring is cheap, so wasted work is acceptable.
* A 503 means *durability not confirmed*, not *nothing stored*: a connection lost during COMMIT
  can leave a committed decision. Same-payload retries resolve this safely.
* Hash normalisation is versioned (`hash_version`); changing it requires a new version.
