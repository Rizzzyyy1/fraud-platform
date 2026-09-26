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

**Origin and citation.** Machine Learning Group, Université Libre de Bruxelles, with Worldline;
Dal Pozzolo, Caelen, Johnson and Bontempi, *Calibrating Probability with Undersampling for
Unbalanced Classification*, IEEE SSCI 2015.

**Licensing metadata as recorded by each distributor (recorded, not interpreted):**

| Source | Recorded licence | Accessed |
|---|---|---|
| OpenML dataset 1597, version 1 (the copy downloaded here) | licence field: "Public" | 2026-09-26 |
| Kaggle dataset "Credit Card Fraud Detection" (mlg-ulb) | Database Contents License (DbCL) v1.0 | from its dataset page; not re-verified here |
| Original data owners (ULB MLG / Worldline) | terms not independently verified | — |

A distributor's label is not treated as permission overriding the original owners' terms. The raw
data is **not redistributed**: `python -m fraudplat.external.ulb fetch` downloads it into the
git-ignored `data/` directory and checks OpenML's MD5 (`178bcf9bb1f31a3dfe12d0e577884add`). Only
derived aggregate metrics are committed (`reports/external/ulb/`).
