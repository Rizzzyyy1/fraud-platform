# Demo script (about 4½ minutes, real time)

The recorded video follows this script exactly (`dashboard/e2e/demo_recording.mjs`; real time,
no speed-up, production thresholds unchanged; captions are an overlay added for the video).
Timings below are from the recording.

**Before you start.** Setup and startup are in the README (*Running it*). Sign in *before*
recording so no credentials appear. Start when *Model & system health* shows all four
processes running and "Running deployment matches the alias target", and no demo job is
running.

## 0:00 · Introduce the platform (10 s)

* "A locally deployed fraud decisioning platform on synthetic payment data. Each transaction is
  scored in real time, stored as an auditable decision, and fed back into point-in-time features
  through Kafka."
* Point at the **Simulated data** and **Local deployment** badges.

## 0:10 · Start activity (20 s)

* *Live activity → Demo traffic*: Traffic, 10/s, 60 s → **Start**. "Pre-test simulated
  transactions through the real API; the held-out test period is never used here."
* "Each row is a stored decision. This release is review-only: no automatic declines."

## 0:30 · An approved decision (30 s)

* Filter *Action: Approve* and open the first row.
* Summary: "The policy rule applied: score below the review threshold. It compares the score with
  thresholds; it doesn't say which features drove the score."
* *From transaction to auditable decision*: received, decided within milliseconds, persisted with
  its outbox event in one transaction, published to Kafka.
* *Decided by*: model, policy and registry version stored with the decision.
* *Stored feature values*: exactly what the model received, computed only from events that had
  arrived before the decision.

## 1:00 · A reviewed decision and an analyst disposition (30 s)

* *Investigation*: the open queue, highest score first; open the top item ("score above the
  review threshold").
* "Reason codes are policy and data-quality flags, not an explanation of the score."
* Set *Closed*, disposition *Needs more information*, add a note, **Save review**. "Appended
  separately: the decision is unchanged, and a disposition is not a fraud label."

## 1:30 · The running model (15 s)

* *Model & system health*: the model and policy the API loaded at startup, next to the registry
  alias target. *Release candidate*: "Better ranking on held-out data, **not promoted**: it did
  not reproducibly meet the latency criterion."

## 1:45 · Controlled failure and recovery (2 min 15 s)

* *Live activity → Demo traffic*: Failure drill, 5/s → **Start**. The worker is stopped at 20 s
  for 90 s.
* ~10 s into the outage: badge **Degraded (WORKER_HEARTBEAT_STALE)**; decisions still scored but
  flagged.
* Past 60 s of outage: *Model & system health* shows **Scoring suppressed
  (PIPELINE_STALE_BEYOND_LIMIT)** and the worker *stopped by failure drill*. Filter *Score: Not
  scored* on the live view: scoreless reviews, never shown as a zero score.
* At 110 s the launcher restarts the worker; it applies the missed events and the pipeline returns
  to **Ready** (the view updates within its 10 s poll).

## 4:00 · One tradeoff, one limitation (15 s)

* Tradeoff: "After 60 s of staleness the system chooses a human review over a possibly wrong
  automatic decision — more reviews during an outage, no silent errors."
* Limitation: "Synthetic data only, and both models essentially miss the compromised-terminal
  scenario. Sustained 100 rps latency was inconsistent on this machine, so I don't claim it."

A separate recorded run of the same drill: 750 decisions at 5/s, 157 scoreless reviews, 0 HTTP
errors (`reports/dashboard/walkthrough.json`).
