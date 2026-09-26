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

**Which licence conditions apply, and to what.** DbCL §2.2 requires compliance with the ODbL, so
the conditions below are the ODbL's:

| What this project does | ODbL term | Applies? | How it is met |
|---|---|---|---|
| Raw data | §4.2 notices (on Publicly Conveying the Database or a Derivative Database) | No: the data is not conveyed. Users download it themselves from the source. | Not redistributed; `data/` is git-ignored. |
| Committed aggregate reports (`reports/external/ulb/*`), the dashboard entry and its screenshot, and the README figures | These are **Produced Works** (works resulting from using the whole of the Contents). §4.3 requires a notice when a Produced Work is Publicly Used. | Yes | The §4.3 notice below is given here, in `reports/external/ulb/NOTICE.md`, in the reproduction and comparison reports, in the dashboard entry and in the README. |
| Local derived tables: time windows, and per-transaction scores joined to labels | These are **Derivative Databases**. Publicly using a Produced Work made from one counts as publicly using it (§4.4c), which brings in share-alike (§4.4a) and access (§4.6). | Yes, because the aggregate metrics are computed from them | §4.6 is met by option (b): the method of making the alterations is published free of charge as the committed code (`src/fraudplat/external/benchmark.py`, `reproduce.py`, `ulb.py`). If the derived tables themselves are ever released, they must be released under the ODbL (§4.4a). They are not released. |
| Fitted model files (local only) | Not addressed by the ODbL's definitions. It is unclear whether trained parameters are a Produced Work (notice only) or a Derivative Database (share-alike). | Unresolved; no current effect | They are not published. Deciding needs a legal reading of that classification, which has not been obtained. |

**§4.3 notice.** Contains information from [Credit Card Fraud
Detection](https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud), which is made available here
under the [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/).

**Residual evidence gap (not blocking the publication recommended here).** The licence grant rests
on Kaggle's owner account `mlg-ulb`. No document was obtained showing that this account is
controlled by ULB MLG, or that Worldline, as co-collector, agreed to the licence. These are the
only rights facts not verified directly. They would matter before any release of the data, the
derived tables or the models, and none of those is released.
