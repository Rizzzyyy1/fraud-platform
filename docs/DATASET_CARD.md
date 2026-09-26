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

## External benchmark data: ULB credit-card fraud (real, offline only)

284,807 real card transactions over 48 hours (September 2013), 492 frauds (0.17%). Features
V1–V28 are PCA components computed upstream by the data owner (the fitting scope of that PCA
cannot be verified from the released features), plus `Time` (elapsed seconds, not time of day)
and `Amount`; no customer or merchant identifiers; actual label-arrival times are unavailable; no
currency. It is used only for the offline benchmark in
`reports/external/ulb/benchmark.md` (split by time: train 0–24 h, validation 24–32 h, held-out
32–48 h) and never enters the platform, its features or its models. Provenance and licence:
`THIRD_PARTY_NOTICES.md`.

### Reproducing the frozen ULB benchmark

The held-out window was evaluated once. The reference results, `reports/external/ulb/benchmark.json`
and `benchmark.md`, are never regenerated by the commands below, and `benchmark.py` refuses to
overwrite them.

1. **Check the terms.** The owner-listed source is Kaggle `mlg-ulb/creditcardfraud` (ODbL v1.0
   for the database, DbCL v1.0 for its contents). Read the ULB section of
   `THIRD_PARTY_NOTICES.md`, cite the works the sources request, and keep the ODbL notice with
   anything you publish from the data.
2. **Fetch the data:** `make ulb-fetch`. This fetches the owner-listed Kaggle copy (version 3)
   through Kaggle's official download endpoint and refuses a `creditcard.csv` whose SHA-256 is not
   `76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89`. If Kaggle asks you to
   sign in, download `archive.zip` from
   <https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud> into `data/external/ulb-creditcard/`
   and run the command again. `make ulb-fetch-openml` fetches the OpenML 1597 v1 copy the original
   run used (MD5 `178bcf9bb1f31a3dfe12d0e577884add`). The two copies' data rows are identical
   (`reports/external/ulb/source_comparison.md`), and both routes produce the same Parquet file.
   `data/external/ulb-creditcard/source.json` records which route was used.
3. **Reproduce:** `make ulb-reproduce` (about 2 minutes). It uses the committed protocol code,
   windows and seeds, and the configurations the original run selected. There is no search and
   no new threshold. It writes models, schema, thresholds, environment, per-transaction scores
   and a checksum manifest to `artifacts/external/ulb/reproduction/<UTC time>/`, and compares
   every recorded value with the reference in `run.json`. Differences are reported, not fixed.
   `uv sync --frozen` gives the locked dependency versions. Other platforms or versions may
   differ in the last digits, and the comparison will show it.
4. **Record in MLflow (optional).** `make ulb-mlflow` imports the reference as a historical run;
   running it again changes nothing. Then
   `uv run python -m fraudplat.external.mlflow_import --reproduction artifacts/external/ulb/reproduction/<id>`
   logs the rerun as its own run, linked to the import. Neither step registers or deploys a
   model.

The committed record of the reproducibility rerun is `reports/external/ulb/reproduction.md`. In
the rerun environment recorded there, all 41 recorded values were reproduced identically, and two
reruns gave byte-identical saved models and predictions. The original run's environment was not
recorded. The rerun's row-level outputs stay local.
