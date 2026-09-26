# Provenance of this public copy

This repository is a **sanitized snapshot** of a private development repository. It was exported
from a single commit of that repository; the development history (commits, authors, dates) is
**not included**, and this copy does **not** preserve publicly verifiable Git history.

## Commit references

Reports, model cards and frozen artifacts cite commit hashes (for example `code_revision` inside
integrity-hashed JSON, and "commit `…`" in documents). They refer to the private development
history and were left unchanged so that frozen artifacts keep their integrity hashes. They cannot
be checked from this copy. The cited commits are:

* `39be6ea` — 2026-09-24 Checkpoint C code: Redis feature state, LR training, model scoring through the API
* `572eaae` — 2026-09-24 Held-out evaluation script for frozen releases (single run, guarded)
* `7f9508c` — 2026-09-24 Add handbook-scale sim-v2 dataset, pipeline benchmarks, faster window features
* `8c74a0b` — 2026-09-24 Log model comparison to the configured MLflow server, never the local default
* `9e33491` — 2026-09-24 Staleness limits with scoreless review; release freeze procedure
* `f2a33ed` — 2026-09-24 Freeze release-1: XGBoost refit on days 30-123, review-only policy, manifest
* `774ee9a` — 2026-09-25 Benchmark: optional in-window docker stats; parity check on development fixtures
* `b583f49` — 2026-09-25 Serving diagnosis harness: stage timing, event-loop lag, thread counts
* `e622374` — 2026-09-25 Serving diagnosis runs (ABBA), report corrections, baseline review-only policy tool

## Files omitted from this copy

* `reports/release-1/host_snapshots.txt`: machine log with local account paths
* `docs/APPLICATION_MATERIALS.md`: personal application notes
* `scripts/build_publication_copy.py`: private publication tooling

## Frozen artifacts

Everything under `releases/` is byte-identical to the private repository (checked when this copy
was built), so integrity hashes and identities such as `lr-f1-6f0ebad8fcc7`,
`pol-6164cb21826d` and the release-1 manifest verify unchanged.
