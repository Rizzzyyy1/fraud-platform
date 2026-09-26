# Dataset card — `sim-small-v1` and `sim-v2`

**Everything in these datasets is simulated.** No real customers, merchants, cards, or payments.
Metrics computed on them describe the simulated distribution only; they are not evidence of
effectiveness on real payment data.

## Provenance

* **Source material consulted (2026-09-24):** the handbook repository's `LICENSE` file and README
  licence section, and the *prose* (markdown) cells of `Chapter_3_GettingStarted/SimulatedDataset.ipynb`
  in Le Borgne, Siblini, Lebichot, Bontempi, *Reproducible Machine Learning for Credit Card Fraud
  Detection — Practical Handbook* (2022), https://github.com/Fraud-Detection-Handbook/fraud-detection-handbook.
  The notebook's code cells were not read.
* **Stated licences of the source:** the README states the notebook code is released under GPL-3.0
  and the prose and pictures under CC BY-SA 4.0.
* **What was implemented here:** `src/fraudplat/simulator/` was written for this project from the
  prose description of the generative process. No handbook code was copied or translated.
  Parameters the prose does not fix are this project's choices and are listed below.
* **Attribution:** the handbook is cited in this card, in ADR 0003, and in the simulator
  package docstring.

## Generation and integrity

`make data` / `make data-v2` write `data/raw/<dataset_id>/` (read-only files; the writer refuses to
overwrite). `make verify-data` / `make verify-data-v2` re-hash the files and regenerate from the
config to confirm determinism. `manifest.json` records seed, full config, generator version,
code revision, library versions, row counts, prevalence, and content and file SHA-256 per table.

| Setting | sim-small-v1 | sim-v2 | Origin |
|---|---|---|---|
| seed | 20260924 | 20260924 | ours |
| customers / terminals | 500 / 1,000 | 5,000 / 10,000 | sim-v2 uses the handbook example's values |
| days (from 2025-01-01 UTC) | 60 | 183 | sim-v2 uses the handbook example's value |
| radius | 10 | 5 | sim-v2 uses the handbook example's value |
| customer/terminal location | uniform, 100 × 100 grid | same | handbook prose |
| mean amount; std | U(5, 100); mean / 2 | same | handbook prose |
| transactions per day | Poisson(λ), λ ~ U(0, 4) | same | handbook prose |
| time of day | normal, mean noon UTC, std 5 h, truncated to the day, whole seconds | same | prose: "centred on noon"; std and truncation ours |
| negative amounts | redrawn (truncated normal) | same | ours |
| customers with no terminal in radius | no transactions | same | ours |
| Scenario 1 | amount > 220 → fraud, judged before scenario 3 scaling | same | handbook prose |
| Scenario 2 | each day 2 random terminals; their transactions in days [d, d+28) → fraud | same | prose; window boundaries ours |
| Scenario 3 | each day 3 random customers; in days [d, d+14) each transaction independently with p = 1/3 has amount × 5 → fraud; scaled at most once | same | prose: "1/3 of their transactions"; Bernoulli selection ours |
| Arrival delay (synthetic) | 98%: log-normal, median 200 ms, σ = 1; 2%: uniform 1 min–6 h | same | ours |
| Label availability (synthetic) | fraud: event + U(1, 30) days; legitimate: event + 30 days | same | ours |
| Amounts | × 100 as integer minor units, labelled synthetic USD | same | ours |

Random streams for profiles, transactions, fraud, arrival and labels are spawned separately from
the seed, so changing arrival or label settings does not change the transactions (tested).

## Fields (`transactions.parquet`)

`transaction_id`, `customer_id`, `terminal_id`, `amount_minor` (int), `currency`, `event_time`,
`received_at`, `label_available_at` (UTC, µs), `is_fraud`, `fraud_s1`, `fraud_s2`, `fraud_s3`,
`arrival_delayed`. Profiles are in `customers.parquet` and `terminals.parquet`.

## Measured facts

| | sim-small-v1 | sim-v2 |
|---|---|---|
| transactions | 58,643 | 1,834,753 |
| content SHA-256 (transactions) | `a8112706b5412305…` | `97481ae593458ff0…` |
| fraud, any scenario | 6.430% | 0.835% |
| scenario 1 / 2 / 3 | 0.061% / 3.869% / 2.648% | 0.060% / 0.513% / 0.264% |
| arrival delayed (> 1 min) | 2.0% | 1.98% |

Scenario percentages overlap (a transaction can match several), so they sum to more than "any".

Prevalence differs between the two datasets because scenarios 2 and 3 compromise a fixed number
of terminals and customers per day regardless of population size. Neither value was tuned; each
is what the stated configuration produces. Metrics on sim-small-v1 describe its distribution
(about 6% fraud), not a lower-prevalence one. sim-v2's population matches the handbook example,
which does not by itself guarantee the handbook's published prevalence.

**Time-semantics measurement (sim-small-v1, immediate-processing assumption):** 650 of 58,643
decisions (1.11%) had a different feature vector when history was filtered by event time only
instead of arrival time. Whether those differences change scores or actions was not measured.

## Known limitations

* Scenario 1 is trivially learnable by design; metrics are reported per scenario.
* Scenario 2 fraud depends on terminal identity. The model trained on f1 detects it poorly on
  validation (observed); the cause has not been established.
* Arrival and label delays are simple parametric assumptions, not measurements.
* Content hashes depend on Polars' CSV rendering; library versions are in each manifest.
