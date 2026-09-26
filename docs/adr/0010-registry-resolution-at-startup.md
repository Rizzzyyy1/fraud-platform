# ADR 0010 — MLflow registry resolved once at startup, never per request

**Status:** accepted

**Context.** Model versions need names, promotion and rollback. The scoring path must not depend
on MLflow being reachable, and the integrity guarantees of ADR 0005/0009 must survive the move.

**Decision.**
* MLflow runs locally with metadata in a separate PostgreSQL database (`mlflow`) and artifacts
  on disk (`mlartifacts/`).
* A registered model version is a bundle: validated model artifact + the policy derived for it +
  `bundle.json`. Model and policy keep their own content-derived versions.
* With `FRAUD_MODEL_URI=models:/<name>@<alias>`, the API resolves the alias to an immutable
  version number at startup, downloads the bundle to a local cache, verifies both artifacts and
  their pairing, writes a pin file, and records `<name>/<version>` in every decision
  (`decisions.model_registry_ref`).
* Registry unreachable at startup: start from the pinned cached version after the same
  verification (`/readyz` reports `source: cache`); with no verified pin, refuse to start. A
  verification failure is never masked by the cache. MLflow is not called after startup.
* Promotion and rollback move the alias; a restart picks the change up.

**Consequences.** An alias change takes effect only on restart, which keeps every process on one
known version. The cache is local to the host; a new host needs one successful resolution.
