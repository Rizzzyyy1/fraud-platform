"""Register the active model-policy bundle by artifact identity and point `production` at it.

    python -m fraudplat.release.setup_active            # register if needed, set the alias
    python -m fraudplat.release.setup_active --check    # report only; change nothing

The active bundle is committed under `releases/active/` (logistic regression `lr-f1-6f0ebad8fcc7`
and review-only policy `pol-6164cb21826d`; both files are integrity-checked JSON, ADR 0005), so a
fresh clone needs neither training nor a promotion/rollback demonstration to serve it. The
registry version *number* is whatever MLflow assigns: an existing version is reused when its tags
name the same model and policy and its files hash identically; otherwise a new version is
registered. `production` is then set to that version. Running it twice changes nothing.

A running API keeps the version it resolved at startup; restart it to pick up an alias change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from fraudplat.config import Settings
from fraudplat.policy import load_policy
from fraudplat.registry import _client, publish, set_alias
from fraudplat.training.artifacts import load_any_model

BUNDLE = Path("releases/active")
NAME = "fraud-risk-f1"
ALIAS = "production"
EXPECTED = {"model_version": "lr-f1-6f0ebad8fcc7", "policy_version": "pol-6164cb21826d"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_bundle(bundle: Path = BUNDLE) -> dict[str, str]:
    model = load_any_model(bundle / "model")
    policy = load_policy(bundle / "policy.json")
    if policy.derived_for_model != model.model_version:
        raise SystemExit("active policy was derived for a different model")
    found = {"model_version": model.model_version, "policy_version": policy.version}
    if found != EXPECTED:
        raise SystemExit(f"releases/active holds {found}, expected {EXPECTED}")
    return {
        **found,
        "model_sha256": _sha256(bundle / "model" / "model.json"),
        "policy_sha256": _sha256(bundle / "policy.json"),
    }


def matching_version(tracking_uri: str, identity: dict[str, str]) -> str | None:
    """A registered version whose tags name the same model and policy and whose downloaded
    files hash identically to the committed bundle."""
    from mlflow.artifacts import download_artifacts

    client = _client(tracking_uri)
    try:
        versions = client.search_model_versions(f"name='{NAME}'")
    except Exception as exc:
        if "RESOURCE_DOES_NOT_EXIST" in str(exc):
            return None
        raise
    for mv in sorted(versions, key=lambda v: int(v.version)):
        tags = mv.tags or {}
        if (
            tags.get("model_version") != identity["model_version"]
            or tags.get("policy_version") != identity["policy_version"]
        ):
            continue
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(
                download_artifacts(artifact_uri=mv.source, dst_path=tmp, tracking_uri=tracking_uri)
            )
            model_file = root / "model" / "model.json"
            if not model_file.exists() or not (root / "policy.json").exists():
                continue
            if (
                _sha256(model_file) == identity["model_sha256"]
                and _sha256(root / "policy.json") == identity["policy_sha256"]
            ):
                return str(mv.version)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="report only; change nothing")
    args = parser.parse_args()
    os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")
    settings = Settings()
    identity = verify_bundle()
    print(f"bundle: {identity['model_version']} + {identity['policy_version']} (verified)")
    try:
        version = matching_version(settings.mlflow_tracking_uri, identity)
    except Exception as exc:
        raise SystemExit(
            f"MLflow at {settings.mlflow_tracking_uri} is not reachable ({type(exc).__name__}); "
            "start it with `make mlflow`"
        ) from exc
    client = _client(settings.mlflow_tracking_uri)
    try:
        current = str(client.get_model_version_by_alias(NAME, ALIAS).version)
    except Exception:
        current = None
    if args.check:
        print(f"registered as: {NAME}/{version}" if version else "not registered yet")
        print(f"{ALIAS} -> {NAME}/{current}" if current else f"{ALIAS} alias not set")
        return 0 if version and current == version else 1
    if version is None:
        import mlflow

        mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
        mlflow.set_experiment("fraud-f1-releases")
        with mlflow.start_run(run_name="register-active-bundle") as run:
            mlflow.set_tags({**EXPECTED, "source": "releases/active"})
            version = publish(
                settings.mlflow_tracking_uri,
                NAME,
                BUNDLE / "model",
                BUNDLE / "policy.json",
                run.info.run_id,
                description="active bundle: baseline LR with the review-only policy",
            )
        print(f"registered {NAME}/{version}")
    else:
        print(f"already registered as {NAME}/{version}")
    if current != version:
        set_alias(settings.mlflow_tracking_uri, NAME, ALIAS, version)
        print(f"{ALIAS}: {current or 'unset'} -> {version} (restart a running API to apply)")
    else:
        print(f"{ALIAS} already points to {NAME}/{version}")
    json.dump({"registry_ref": f"{NAME}/{version}", **identity}, sys.stdout)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
