# ADR 0009 — XGBoost artifacts: native JSON booster with a verified manifest

**Status:** accepted

**Context.** Tree models cannot be expressed in the linear-model JSON of ADR 0005, and pickled
XGBoost or scikit-learn objects execute code on load and tie serving to library internals.

**Decision.** An XGBoost artifact is a directory: `booster.json` (XGBoost's own JSON model
format) and `manifest.json` (schema `fraudplat.xgb-model/v1`, feature version, ordered feature
names, SHA-256 of the booster file, XGBoost version, training parameters, metadata). The version
is `xgb-<feature_version>-<12 hex of the manifest-core digest>`; the core includes the booster
hash. Loading verifies the manifest digest, the booster hash, and that the booster's own
feature names equal the manifest's. Missing values are passed as NaN. `load_any_model`
dispatches on the declared schema, so the scorer and registry handle either format.

**Consequences.** The booster JSON must be loaded by a compatible XGBoost version; the version
used is recorded. Serving uses `inplace_predict` with one thread per call.
