# Data pipeline benchmark

Source: `reports/benchmarks/data_pipeline.json`, produced by
`python scripts/benchmark_data_pipeline.py --fractions 0.1 0.25 0.5 1.0 --out …` on 2026-09-24.

**Machine:** Apple M1 (8 cores), 16 GB RAM, macOS 15.2, Python 3.14.0, single process, other
applications running. One run per size; timings on this laptop varied by up to ~25% between
repeated runs, so treat differences smaller than that as noise.

**Workload:** sizes scale sim-v2's customers and terminals by the fraction, with the radius scaled
by 1/√fraction to keep terminals per customer constant; 183 days. Each phase runs in a fresh
subprocess, so peak RSS is per phase. Reconstruction = `point_in_time_matrix` (immediate-processing
assumption, feature version f1) after the binary-search window optimisation.

| fraction | rows | generate s | write+hash s | generate peak RSS MiB | output MiB | to-events s | reconstruct s | µs/row | reconstruct peak RSS MiB |
|---|---|---|---|---|---|---|---|---|---|
| 0.1 | 179,330 | 0.7 | 0.1 | 253 | 3.1 | 1.5 | 5.2 | 29.1 | 284 |
| 0.25 | 460,234 | 2.2 | 0.2 | 457 | 7.7 | 4.0 | 17.3 | 37.7 | 560 |
| 0.5 | 922,328 | 4.2 | 0.5 | 881 | 15.1 | 8.3 | 45.5 | 49.3 | 994 |
| 1.0 | 1,834,753 | 9.1 | 0.9 | 1,378 | 29.3 | 16.8 | 97.0 | 52.9 | 1,637 |

**Reading.**
* Generation and output size scale linearly.
* Reconstruction is close to linear in total time; per-row cost rises about 1.8× from 10% to
  100%. Profiling at 10% and 50% showed the same per-row cost in each hot function (no
  algorithmic growth); the increase is consistent with memory effects of larger working sets.
  Freezing the garbage collector did not help.
* Before the optimisation, per-row time at 10% / 50% was 44 / 67 µs in the first run; the
  window-count rewrite (binary search over sorted history) reduced it by roughly 40%.
* Peak memory at full size (1.6 GiB) fits comfortably in 16 GB.
