# Provenance and third-party notices

## Scope of the MIT license

`LICENSE` (MIT) covers the original code, configuration and documentation written for this
repository. It does not cover third-party software installed as dependencies, container images,
or the external work cited below; those keep their own terms.

## Source consulted for the simulator

**Work:** Le Borgne, Siblini, Lebichot and Bontempi, *Reproducible Machine Learning for Credit Card
Fraud Detection — Practical Handbook* (2022),
https://github.com/Fraud-Detection-Handbook/fraud-detection-handbook. Its README states that the
notebook code is released under GPL-3.0 and the prose and pictures under CC BY-SA 4.0.

**What was consulted (2026-09-24):** the repository's `LICENSE` file and README licence section,
and the prose (markdown) cells of `Chapter_3_GettingStarted/SimulatedDataset.ipynb`. The notebook's
code cells were not read (recorded in `docs/DATASET_CARD.md` and ADR 0003).

**What this repository adapts from that prose (attributed, not copied):** the design of the
generative process and the example's parameter values —

* customer and terminal locations on a 100 × 100 grid; customer mean amount U(5, 100) with
  standard deviation = mean / 2; transactions per day ~ Poisson(U(0, 4)); time of day centred on
  noon; terminals available within a radius (example: 5,000 customers, 10,000 terminals,
  183 days, radius 5, used by `configs/sim-v2.toml`);
* the three fraud scenarios as described (amounts above a threshold; terminals compromised for a
  period; customers compromised for a period with some transaction amounts multiplied by 5), with
  parameters recorded in the configs.

These places are marked in `src/fraudplat/simulator/generate.py`, `configs/sim-*.toml` and the
dataset card ("handbook prose", "handbook example value").

**What was written independently for this repository:** all simulator code
(`src/fraudplat/simulator/`), every parameter the prose leaves open (listed in the dataset card),
and the additions the handbook does not describe (arrival delay, label availability, integer
minor-unit amounts, immutable versioned output).

**Quoted text:** one short phrase from the prose, "1/3 of their transactions", is quoted with
attribution in the scenario table of `docs/DATASET_CARD.md` to show which rule it supports.

**Not included:** apart from that attributed phrase, no handbook code, prose passages, figures or
published datasets are copied into this repository. Generated datasets are not distributed; they are regenerated locally.

This is a factual account of the process, not a legal opinion.

## Software dependencies

Python packages (`pyproject.toml`, `uv.lock`) and npm packages (`dashboard/package.json`,
`dashboard/package-lock.json`) are installed at setup time and are not redistributed here; each
keeps its own license. Container images (PostgreSQL, Redis, Apache Kafka) are pulled by Docker
Compose and are not redistributed. The demo recording uses Google Chrome and Playwright's FFmpeg
build (LGPL-2.1), downloaded to the user's cache; neither is included.

## Media

Screenshots and the demo video show only this project's own application and synthetic data.

## External dataset: ULB credit-card fraud (offline benchmark only)

**Origin.** Transactions by European cardholders in September 2013, collected and analysed in a
research collaboration of Worldline and the Machine Learning Group (MLG) of the Université Libre
de Bruxelles (ULB).

**Attribution.** Both distributors ask users to cite: Andrea Dal Pozzolo, Olivier Caelen, Reid A.
Johnson and Gianluca Bontempi, *Calibrating Probability with Undersampling for Unbalanced
Classification*, Symposium on Computational Intelligence and Data Mining (CIDM), IEEE, 2015. The
Kaggle page lists further MLG publications to cite.

**Sources, checked directly on 2026-09-26 (recorded, not legal advice):**

| Source | What it states |
|---|---|
| **Kaggle `mlg-ulb/creditcardfraud`, version 3** (updated 2018-03-23), <https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud>, listed by the owner account "Machine Learning Group - ULB" (`mlg-ulb`). Preferred acquisition route. | Licence: [ODbL v1.0](https://opendatacommons.org/licenses/odbl/1-0/) for the database and [DbCL v1.0](https://opendatacommons.org/licenses/dbcl/1-0/) for its contents. `creditcard.csv` SHA-256 `76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89`, downloaded 2026-09-26 through Kaggle's official download endpoint (no sign-in was requested). |
| **OpenML dataset 1597 v1** (uploaded 2015-06-25), <https://www.openml.org/api/v1/json/data/1597>, file `https://openml.org/data/v1/download/1673544/creditcard.arff`, MD5 `178bcf9bb1f31a3dfe12d0e577884add`. **The copy the original benchmark was run on.** | Licence field "Public". This is not a licence and is not relied on. OpenML's terms (<https://www.openml.org/terms>) ask users to honour citation requests and defer to the uploader's licences. |
| MLG project pages cited in the dataset description (`mlg.ulb.ac.be/BruFence`, `/ARTML`) | HTTP 404 on 2026-09-26. |

**Relationship between the copies.** A cell-by-cell comparison found the two data files identical:
the same columns in the same order, the same 284,807 rows in the same order, and all 8,829,017
cells equal as text and as parsed numbers. The only formatting difference is quoting. Details are
in `reports/external/ulb/source_comparison.md`. The data used is therefore the database the owner
licenses on Kaggle under ODbL/DbCL. The original benchmark's recorded provenance remains OpenML
1597 v1 (`benchmark.json` is unchanged).

**ODbL terms relevant to what this project publishes.** DbCL §2.2 says "You must comply with the
ODbL", so the terms below are ODbL v1.0 sections. The right-hand column records the approach the
project takes. It is the project's reading of the licence text, not legal advice and not a
determination of compliance.

| Item | Publication scope | Relevant ODbL text | Approach taken |
|---|---|---|---|
| Raw data | Not published. Users download it from the source; `data/` is git-ignored. | §4.2 sets notice conditions for Publicly Conveying the Database or a Derivative Database. | The data is not conveyed by this project. |
| Aggregate reports (`reports/external/ulb/*`), the dashboard entry and its screenshot, README figures | Published | "Produced Work" is defined as a work resulting from using the whole or a Substantial part of the Contents. §4.3 asks for a notice when a Produced Work is Publicly Used, and gives example wording. | Treated as Produced Works. The §4.3 example notice (below) is placed in this file, `reports/external/ulb/NOTICE.md`, the reproduction and comparison reports, the dashboard entry and the README. |
| Derived tables: time windows, and per-transaction scores joined to labels | Kept local, not published | "Derivative Database" includes extractions and modifications of the Contents. §4.4c treats a Derivative Database as Publicly Used when a Produced Work created from it is Publicly Used. §4.4a sets share-alike for Derivative Databases. §4.6 asks for an offer of the Derivative Database or "a file containing … the method of making the alterations … (such as an algorithm)". | The code that makes these tables is published free of charge (`src/fraudplat/external/ulb.py`, `benchmark.py`, `reproduce.py`), in line with §4.6(b). The tables are not released. |
| Fitted model files | Kept local, not published | The ODbL definitions do not name trained model parameters. | Not classified. Whether they would be treated as a Produced Work or a Derivative Database has not been determined. |

**§4.3 notice.** Contains information from [Credit Card Fraud
Detection](https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud), which is made available here
under the [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/).

**Rights facts not verified directly.** The licence shown on Kaggle is stated by the owner account
`mlg-ulb` ("Machine Learning Group - ULB"). No document was obtained that confirms who controls
that account, or that sets out Worldline's position as co-collector of the data. The project
relies on the licence as the owner-listed source states it, and records this gap.
