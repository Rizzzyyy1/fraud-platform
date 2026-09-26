# End-to-end benchmark

> **Later finding:** these runs were not reproduced consistently. In the bounded serving
> diagnosis (12 runs at 100 rps, `reports/serving_diagnosis/summary.md`) the latency criterion
> was met in 4/6 runs for each model; the cause of the failures is not established. Read the
> numbers below as one session's results, not as sustained capacity.

Sources: `reports/benchmarks/e2e.json` (25/50/100 rps) and `e2e_repeat_100rps.json` (a second
100 rps run), produced by `scripts/benchmark_e2e.py` on 2026-09-24. Local runs only.

## Conditions

* **Hardware:** Apple M1, 8 cores, 16 GB; macOS 15.2; Python 3.14.0. Load generator, API,
  publisher, worker and Colima VM share the machine.
* **Containers (Colima VM: 6 CPU, 8 GiB):** PostgreSQL 17 (512 MiB limit), Redis 8
  (512 MiB limit, `maxmemory` 384 MB, `noeviction`), Kafka 4.0 single broker (1 GiB, heap 512 MiB).
* **Processes:** 1 uvicorn API process (model `lr-f1-6f0ebad8fcc7`, policy `pol-82d9e16668ae`,
  pipeline monitor on), 1 publisher, 1 worker, in namespace `replay:bench-<ts>`.
* **Data:** sim-v2 (synthetic). Redis bootstrapped with 321,167 events available in the 32 days
  before day 140; requests are distinct transactions decided after day 140, in decision order.
* **Load:** open loop; request *i* scheduled at *t0 + i / rate*; ≤ 64 requests in flight; client
  timeout 5 s. Each level: 10 s warm-up (excluded), 60 s measured. Levels run one after another,
  each drained before the next.
* **Latency boundary:** from immediately before `http.post` (after obtaining an in-flight slot)
  until the full response is read. **Scheduling delay:** actual start minus scheduled start.
* **Pipeline delay:** `decision_time` (API) → `applied_at` (worker), both host clock. The
  database clock was measured 64–68 ms ahead of the host, so database timestamps are only used
  for database-clock durations (outbox publish delay).

## Results (measured window)

| Run | Target | Achieved | Scheduled / completed | Outcomes | Timeouts / errors / rejections | Late starts > 50 ms |
|---|---|---|---|---|---|---|
| main | 25 rps | 25.0 | 1,500 / 1,500 | 1,500 model-scored | 0 / 0 / 0 | 0 |
| main | 50 rps | 50.0 | 3,000 / 3,000 | 3,000 model-scored | 0 / 0 / 0 | 0 |
| main | 100 rps | 100.0 | 6,000 / 6,000 | 6,000 model-scored | 0 / 0 / 0 | 0 |
| repeat | 100 rps | 100.0 | 6,000 / 6,000 | 6,000 model-scored | 0 / 0 / 0 | 33 |

No request was answered as a degraded review (`FEATURES_UNAVAILABLE`), none carried
`PIPELINE_DEGRADED`, and none was rejected for backlog, so the latencies below are for
model-served decisions.

| Run | Target | p50 | p95 | p99 | max | Decision → applied p50 / p95 / p99 / max | Outbox publish p95 | Max backlog | Unapplied at load end | Drain |
|---|---|---|---|---|---|---|---|---|---|---|
| main | 25 | 9.3 ms | 12.4 ms | 17.3 ms | 25.8 ms | 39 / 71 / 77 / 121 ms | 65 ms | 3 rows | 1 | 0.28 s |
| main | 50 | 8.3 ms | 11.6 ms | 16.1 ms | 61.4 ms | 46 / 74 / 81 / 119 ms | 61 ms | 4 rows | 3 | 0.27 s |
| main | 100 | 7.2 ms | 12.1 ms | 17.7 ms | 48.7 ms | 53 / 87 / 100 / 135 ms | 60 ms | 6 rows | 4 | 0.29 s |
| repeat | 100 | 7.2 ms | 14.9 ms | 43.8 ms | 387.6 ms | 54 / 95 / 341 / 476 ms | — | — | — | 0.30 s |

Every measured decision was applied to Redis after draining (0 unapplied after drain).
Maximum worker lag reported during load: 0–2 events. Peak CPU (sampled, % of one core) at 100
rps: API 29%, worker 8.5%, publisher 2.1%.

## Memory

| | Idle (after bootstrap) | During 100 rps |
|---|---|---|
| Kafka container | 538 MiB | 575 MiB |
| Redis container | 272 MiB | 273 MiB |
| PostgreSQL container | 106 MiB | 129 MiB |
| API / publisher / worker RSS | 73 / 53 / 58 MiB | 80 / 29 / 35 MiB |

Redis `used_memory`: 108 MB before bootstrap (the `live` namespace from the Checkpoint C demo),
251 MB after bootstrapping the benchmark namespace (+142 MB for 321k events), peak sampled
during load 252 MB, leaving ~132 MB (34%) of the 384 MB `maxmemory`. The server's lifetime peak
is 384 MB, from a development incident in which three populated namespaces exhausted
`maxmemory` and writes were refused.

## Targets

| Target | Result |
|---|---|
| 100 rps sustained | Met in both complete runs (100.0 rps achieved, no errors) |
| p95 < 100 ms | Met in both complete runs (12.1 and 14.9 ms) |
| p99 < 200 ms | Met in both complete runs (17.7 and 43.8 ms) |

## Caveats

* **Run-to-run variance is large in the tail.** The repeat run's p99 was 2.5× the main run's,
  with 33 late starts showing the load generator itself fell behind. An earlier, interrupted run
  (before the harness fixes described below) measured p95 314 ms and p99 1,295 ms at 100 rps; that
  tail was not reproduced and its cause was not established.
* Harness fixes made during this checkpoint: memory sampling had blocked the request scheduler
  (moved off the send path); the delay metric originally mixed the database and host clocks.
* Single uvicorn process; higher rates, longer runs and CPU saturation were not tested.
* The load generator shares the machine; results are not comparable to a separate client.
