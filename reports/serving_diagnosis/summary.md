# Serving diagnosis (bounded)

All runs: 100 rps open loop, 10 s warm-up + 60 s measured, identical request list (same SHA-256 per run), fresh isolated namespace, one uvicorn process. Criterion: p95 ≤ 100 ms, p99 ≤ 200 ms, errors ≤ 0.1%, ≥ 99 rps achieved. Development traffic only (days ≥ 140, pre-test); the frozen predictive results were not touched.

| Run | Model | Docker stats in window | Achieved rps | Client p50 / p95 / p99 (ms) | Scoreless reviews | Scheduling delay p95 (ms) | Server request p99 ≤ (ms) | DB insert p99 ≤ | Event-loop lag p99 ≤ | Inference mean (ms) | Criterion |
|---|---|---|---|---|---|---|---|---|---|---|---|
| run1 | `lr-f1-6f0ebad8fcc7` | yes | 100.0 | 8.8 / 67.8 / 245.6 | 13 | 8.7 | 100.0 | 50.0 | 25.0 | 0.094 | fail |
| run2 | `xgb-f1-1f168d371c42` | yes | 100.0 | 8.1 / 21.4 / 105.2 | 0 | 2.1 | 50.0 | 25.0 | 5.0 | 0.274 | pass |
| run3 | `xgb-f1-1f168d371c42` | yes | 100.0 | 8.0 / 21.9 / 125.1 | 2 | 2.0 | 50.0 | 25.0 | 2.5 | 0.282 | pass |
| run4 | `lr-f1-6f0ebad8fcc7` | yes | 99.6 | 8.2 / 608.5 / 1487.8 | 64 | 545.3 | 500.0 | 250.0 | 25.0 | 0.089 | fail |
| run5 | `xgb-f1-1f168d371c42` | yes | 100.0 | 7.9 / 24.4 / 80.7 | 0 | 2.0 | 100.0 | 50.0 | 10.0 | 0.291 | pass |
| run6 | `lr-f1-6f0ebad8fcc7` | yes | 100.0 | 7.8 / 17.5 / 103.6 | 2 | 1.8 | 100.0 | 25.0 | 2.5 | 0.087 | pass |
| run7 | `lr-f1-6f0ebad8fcc7` | yes | 100.0 | 7.7 / 19.0 / 173.9 | 0 | 1.9 | 100.0 | 25.0 | 2.5 | 0.087 | pass |
| run8 | `xgb-f1-1f168d371c42` | yes | 100.0 | 7.9 / 423.1 / 1345.4 | 4 | 296.8 | 250.0 | 50.0 | 5.0 | 0.257 | fail |
| run9 | `lr-f1-6f0ebad8fcc7` | no | 100.0 | 7.7 / 17.3 / 86.1 | 1 | 1.7 | 100.0 | 50.0 | 2.5 | 0.089 | pass |
| run10 | `xgb-f1-1f168d371c42` | no | 100.0 | 7.7 / 15.8 / 76.9 | 2 | 1.5 | 25.0 | 25.0 | 2.5 | 0.258 | pass |
| run11 | `xgb-f1-1f168d371c42` | no | 100.0 | 8.0 / 39.4 / 241.7 | 3 | 4.6 | 100.0 | 50.0 | 5.0 | 0.294 | fail |
| run12 | `lr-f1-6f0ebad8fcc7` | no | 100.0 | 7.8 / 18.1 / 118.1 | 0 | 2.0 | 100.0 | 50.0 | 2.5 | 0.088 | pass |

Distinct workload hashes across runs: 1 (identical inputs when 1).

| Model | Docker stats in window | Passed |
|---|---|---|
| `lr-f1-6f0ebad8fcc7` | no | 2/2 |
| `lr-f1-6f0ebad8fcc7` | yes | 2/4 |
| `xgb-f1-1f168d371c42` | no | 1/2 |
| `xgb-f1-1f168d371c42` | yes | 3/4 |

Serving-path parity on 5,000 pre-test fixtures: exact (maximum score difference 0.0; no action changes under any policy).

## Findings

* **No model-specific mechanism.** Inference costs well under a millisecond for both models,
  model-input conversion and feature computation are negligible, and event-loop lag stays small
  in every run. Both models show the same intermittent stalls; the earlier "candidate fails,
  baseline passes" result was an association that did not hold up under identical conditions.
* **Large stalls in this session were largely a harness artefact.** With the in-window
  `docker stats` sample (it queries every container in the VM), failing runs show PostgreSQL
  pool-wait/transaction spikes and load-generator scheduling delays of hundreds of
  milliseconds. Without it, no run shows a stall of that size.
* **A residual tail remains** near the 200 ms p99 limit for either model (one candidate run
  without the sample reached 241.7 ms).
* **Sustained performance is inconsistent for both models.** Neither model met the criterion in
  every run. The baseline is the retained default because it is the model already serving; it
  is not a control that passes consistently.
* **Not established:** the cause of the release-1 session's failures, which were degraded for
  the whole window (p50 around 380 ms, the load generator falling behind throughout) rather
  than in bursts, and the source of the residual tail. Measurements that bear on storage:
  PostgreSQL checkpoints recorded during the diagnosis runs did not coincide with the stall
  windows, and the mean fsync latency on the VM disk measured about 87 µs. A fast mean does
  not exclude intermittent storage or VM-level stalls, which these measurements could not
  observe.
* **No serving fix was applied:** nothing blocking in the request path was demonstrated, so
  there was no evidence-backed change to make.

