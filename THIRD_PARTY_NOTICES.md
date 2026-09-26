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
de Bruxelles (ULB). Creators listed by OpenML: Andrea Dal Pozzolo, Olivier Caelen, Gianluca
Bontempi.

**Attribution requested by the sources.** Both distributors ask users to cite: Andrea Dal Pozzolo,
Olivier Caelen, Reid A. Johnson and Gianluca Bontempi, *Calibrating Probability with
Undersampling for Unbalanced Classification*, Symposium on Computational Intelligence and Data
Mining (CIDM), IEEE, 2015. The Kaggle page lists further MLG publications to cite; OpenML's terms
additionally ask users to credit the authors whose work they build on.

**Source terms, checked at the source on 2026-09-26 (recorded, not interpreted; not legal
advice):**

| Source (URL) | What the source states |
|---|---|
| OpenML dataset 1597 v1, the copy downloaded here: `https://www.openml.org/api/v1/json/data/1597` (file `https://openml.org/data/v1/download/1673544/creditcard.arff`, MD5 `178bcf9bb1f31a3dfe12d0e577884add`, uploaded 2015-06-25) | `licence` field: "Public". This is not a licence and is not treated as one. No `original_data_url` or licence text is given. |
| OpenML terms of use: `https://www.openml.org/terms` | OpenML's own data and metadata are CC-BY. Individual datasets may carry their own citation requests, which users are asked to honour. Uploaders grant users a non-exclusive licence to use content for their own research, subject to the uploader's licences. Anyone distributing content from OpenML affirms they hold the necessary rights. |
| Kaggle dataset `mlg-ulb/creditcardfraud`: `https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud` (API `https://www.kaggle.com/api/v1/datasets/view/mlg-ulb/creditcardfraud`; owner "Machine Learning Group - ULB", version 3, updated 2018-03-23) | Licence: Open Database License (ODbL) v1.0 for the database, and Database Contents License (DbCL) v1.0 for its contents. |
| DbCL v1.0: `https://opendatacommons.org/licenses/dbcl/1-0/` | A worldwide, royalty-free copyright licence to the contents, commercial use included. ODbL definitions are incorporated by reference. |
| MLG project pages cited in the dataset description (`http://mlg.ulb.ac.be/BruFence`, `http://mlg.ulb.ac.be/ARTML`) | Not reachable on 2026-09-26 (HTTP 404). No terms were obtained directly from ULB MLG or Worldline. |

**Unresolved, so licensing is not declared complete:**

1. **The terms of the copy used.** The data here came from OpenML, which states no licence. The
   ODbL/DbCL terms appear on the Kaggle listing, a separate distribution. Neither source says
   whether those terms cover the OpenML copy.
2. **No statement from the owners.** Nothing was obtained directly from ULB MLG or Worldline, so
   the distributors' terms could not be confirmed with the original owners.
3. **Obligations for derived artifacts, if ODbL governs.** ODbL attaches notice and share-alike
   conditions to publicly used derivative databases, and notice conditions to produced works.
   Whether these apply to the local rerun artifacts (per-transaction scores joined to labels, and
   the fitted models) needs a qualified legal reading. That reading has not been done.

**What is and is not published.** The raw data is **not redistributed**. `python -m
fraudplat.external.ulb fetch` downloads it into the git-ignored `data/` directory and verifies
OpenML's MD5. The committed files under `reports/external/ulb/` hold aggregate metrics, checksums
and environment details only, and carry the attribution above. The rerun artifacts stay local
and are not published: the per-transaction predictions (with labels) and the fitted model files,
under git-ignored `artifacts/`. Keeping these files out resolves only part of the question.
Items 1–3 remain open before any of them could be published.
