# ADR 0005 — Integrity-checked JSON model and policy artifacts

**Status:** accepted

**Context.** The scoring service must load exactly the model that was evaluated, reject anything
else, and record which model and policy produced each decision. Pickled scikit-learn objects can
execute code when loaded and tie serving to the training library's version.

**Decision.**
* A linear model is stored as JSON: feature version, ordered feature names, preprocessing
  parameters (nullable columns, log1p columns, train medians, means, scales), coefficients,
  intercept, and metadata (dataset identity and content hash, timeline, training configuration,
  selected hyperparameters, code revision).
* `model_version = lr-<feature_version>-<first 12 hex of SHA-256 of the canonical JSON>`; the
  full digest is stored and verified on load, so any edit is rejected.
* Serving reuses `Preprocessing.transform` and computes a dot product and sigmoid in NumPy. At
  training time the artifact's scores are compared with scikit-learn's on every validation row;
  a difference above 1e-12 aborts training.
* A policy (review / decline thresholds) is a separate JSON artifact with its own content-derived
  version and a `derived_for_model` field. The app refuses to start with a policy derived for a
  different model; a model whose feature version or feature order differs from the running code
  makes `/readyz` fail and new decisions return 503.

**Consequences.** No MLflow registry yet; artifacts live under `artifacts/` (not committed) and
are reproducible with `make train`. Only linear models fit this format; a tree model will need
its own schema or a registry-backed format.
