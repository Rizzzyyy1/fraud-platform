# ADR 0004 — Feature history is filtered by availability, not only by event time

**Status:** accepted

**Context.** The usual point-in-time recipe keeps prior events with `event_time < t`. A live
service cannot see an event before it has arrived and been processed, so that recipe leaks late
arrivals into training features. On `sim-small-v1`, 650 decisions (1.11%) had a different feature
vector under that recipe; the effect on scores or actions was not measured.

**Decision.**
* A prior event is visible to a decision iff `available_at <= decision_time` **and**
  `t - W <= event_time < t`. Offline, `available_at = received_at + processing_delay`.
* Historical `decision_time = received_at` (scoring on receipt).
* Offline reconstruction replays availability and decision instants through the same state class
  and feature function as the online path; availability is processed first on ties.
* The main evaluation uses processing delay 0 and is labelled the *immediate-processing
  assumption*; a separate experiment varies the delay.
* The demo cutoff partitions data by arrival: bootstrap = available before C, scoring = decided
  at or after C. (An earlier draft split by event time; a transaction that happened before C but
  arrived after it belongs to scoring.)
* The live service stores the feature vector it used; that record, not a recomputation, is the
  audit trail.

**Consequences.** Shared code gives logic parity only. Whether online decisions saw the same
history as the reconstruction depends on real worker lag, retention and concurrency; that is
measured later by comparing persisted vectors with offline reconstruction, not assumed.
