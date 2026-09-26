# ADR 0007 — At-least-once event pipeline with idempotent feature application

**Status:** accepted

**Context.** Decisions must update behavioural state asynchronously. Kafka does not make the
path from a PostgreSQL commit to a Redis update exactly-once: the publisher can crash after Kafka
accepts an event but before PostgreSQL records it, and the worker can crash after updating Redis
but before committing its offset.

**Decision.**
* Outbox rows are marked `published_at` only after Kafka acknowledges them (idempotent producer,
  `acks=all`). Unacknowledged rows are retried with backoff.
* The worker commits a partition offset only after that message is durably handled: applied to
  Redis, or written to the DLQ and acknowledged. Messages are handled sequentially, so no offset
  passes an unfinished earlier message. Auto-commit and auto offset store are disabled.
* Redis application is idempotent. History is keyed by transaction id; the outbox event id is
  bound to it one-to-one. Identical redelivery → `DUPLICATE` (no change). Different payload for a
  known transaction → `PAYLOAD_CONFLICT`; a transaction under a new event id, or an event id
  claimed by another transaction → `EVENT_ID_CONFLICT`. Conflicts and too-late events go to the
  DLQ with a reason; history is never overwritten.
* Identity records count only while their transaction is inside the retention horizon, in both
  the Redis and in-memory implementations, so status never depends on when cleanup runs.
* Invalid messages (malformed, schema violation, key ≠ customer_id, header/payload disagreement,
  payload hash ≠ canonical hash of the fields) go to the DLQ and are committed.
* Redis failures are retried a bounded number of times; the worker then stops *without*
  committing, so a restart redelivers the message.
* One namespace (`live` or `replay:<run>`) derives the outbox stream, topic, DLQ topic, consumer
  group and Redis prefix. Replays cannot write live state.

**Consequences.** The guarantee is *at-least-once delivery with idempotent feature
application*. The tests show duplicate-safe effects in specific scenarios — redelivery after a
publisher crash between ack and commit, redelivery after a worker crash between the Redis update
and the offset commit, identical duplicates, conflicting duplicates — for events inside the
retention horizon. That is not a general exactly-once guarantee: for example, an event older
than the horizon is rejected rather than applied, and Redis durability (persistence is off
locally) is outside what is tested. This does not remove training/serving skew:
a decision made before earlier events are applied sees less history than the offline
reconstruction (measured in `reports/pipeline/demo.json`).
