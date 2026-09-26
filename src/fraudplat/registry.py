"""MLflow model registry integration, used only at service startup and by release tooling.

A registered model version is a *bundle*: the validated model artifact (linear JSON or XGBoost
JSON + manifest), the policy derived for it, and `bundle.json` naming both versions. The
registry adds naming, version numbers and aliases; integrity still comes from the artifacts'
own hashes (ADR 0005, 0009), re-verified after every download.

Startup (`resolve`):
  1. `models:/<name>@<alias>` is resolved to an immutable version number;
  2. the bundle is downloaded into `<cache>/<name>/<version>/` (or reused if already there);
  3. model and policy are loaded and verified, including the policy/model pairing;
  4. a pin `<cache>/<name>/pins/<alias>.json` records the resolved version.
If the registry cannot be reached, the pinned version is used from the cache after the same
verification, and `source` says so. With no verified pin, startup fails (`RegistryUnavailable`).
Nothing in the request path calls MLflow.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fraudplat.policy import load_policy
from fraudplat.training.artifacts import artifact_file, load_any_model

BUNDLE_SCHEMA = "fraudplat.registry-bundle/v1"


class RegistryUnavailable(Exception):
    pass


@dataclass(frozen=True)
class ResolvedModel:
    model_path: Path
    policy_path: Path
    registry_ref: str  # e.g. "fraud-risk-f1/3"
    model_version: str
    policy_version: str
    source: str  # "registry" or "cache"


def parse_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("models:/") or "@" not in uri:
        raise ValueError("expected models:/<name>@<alias>")
    name, alias = uri.removeprefix("models:/").split("@", 1)
    return name, alias


def _client(tracking_uri: str):  # type: ignore[no-untyped-def]
    # Fail fast instead of MLflow's default multi-minute retry loop at startup.
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "0")
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "5")
    from mlflow.tracking import MlflowClient

    return MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)


def _verify_bundle(directory: Path) -> tuple[Path, Path, str, str]:
    bundle = json.loads((directory / "bundle.json").read_text())
    if bundle.get("schema") != BUNDLE_SCHEMA:
        raise ValueError(f"unsupported bundle schema in {directory}")
    model_path = artifact_file(directory / "model")
    policy_path = directory / "policy.json"
    model = load_any_model(model_path)
    policy = load_policy(policy_path)
    if model.model_version != bundle["model_version"] or policy.version != bundle["policy_version"]:
        raise ValueError("bundle contents do not match bundle.json")
    if policy.derived_for_model != model.model_version:
        raise ValueError("policy was derived for a different model")
    return model_path, policy_path, model.model_version, policy.version


def publish(
    tracking_uri: str,
    name: str,
    model_path: Path,
    policy_path: Path,
    run_id: str,
    description: str = "",
) -> str:
    """Register model + policy as a new version of `name`; returns the version number."""
    model = load_any_model(model_path)
    policy = load_policy(policy_path)
    if policy.derived_for_model != model.model_version:
        raise ValueError("policy was derived for a different model")
    client = _client(tracking_uri)
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / model.model_version
        source_model = artifact_file(model_path)
        (bundle / "model").mkdir(parents=True)
        for item in (
            source_model.parent.iterdir()
            if source_model.name == "manifest.json"
            else [source_model]
        ):
            shutil.copy2(item, bundle / "model" / item.name)
        shutil.copy2(policy_path, bundle / "policy.json")
        (bundle / "bundle.json").write_text(
            json.dumps(
                {
                    "schema": BUNDLE_SCHEMA,
                    "model_version": model.model_version,
                    "policy_version": policy.version,
                    "feature_version": model.feature_version,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        _verify_bundle(bundle)
        client.log_artifacts(run_id, str(bundle), f"registry_bundle/{model.model_version}")
    run = client.get_run(run_id)
    from mlflow.exceptions import MlflowException

    try:
        client.create_registered_model(name)
    except MlflowException as exc:
        if exc.error_code != "RESOURCE_ALREADY_EXISTS":
            raise
    version = client.create_model_version(
        name,
        f"{run.info.artifact_uri}/registry_bundle/{model.model_version}",
        run_id=run_id,
        tags={
            "model_version": model.model_version,
            "policy_version": policy.version,
            "feature_version": model.feature_version,
        },
        description=description,
    )
    return str(version.version)


def set_alias(tracking_uri: str, name: str, alias: str, version: str) -> None:
    _client(tracking_uri).set_registered_model_alias(name, alias, version)


def resolve(
    tracking_uri: str, uri: str, cache_dir: Path, allow_cache: bool = True
) -> ResolvedModel:
    name, alias = parse_uri(uri)
    pins = cache_dir / name / "pins"
    try:
        client = _client(tracking_uri)
        mv = client.get_model_version_by_alias(name, alias)
        target = cache_dir / name / str(mv.version)
        if not target.exists():
            from mlflow.artifacts import download_artifacts

            staging = Path(
                tempfile.mkdtemp(dir=cache_dir.parent if cache_dir.parent.exists() else None)
            )
            downloaded = Path(
                download_artifacts(
                    artifact_uri=mv.source, dst_path=str(staging), tracking_uri=tracking_uri
                )
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(downloaded), str(target))
            shutil.rmtree(staging, ignore_errors=True)
        model_path, policy_path, model_version, policy_version = _verify_bundle(target)
        if mv.tags.get("model_version") not in (None, model_version):
            raise ValueError("registry tag model_version disagrees with the downloaded artifact")
        pins.mkdir(parents=True, exist_ok=True)
        (pins / f"{alias}.json").write_text(
            json.dumps(
                {
                    "version": str(mv.version),
                    "model_version": model_version,
                    "policy_version": policy_version,
                    "resolved_at": datetime.now(UTC).isoformat(timespec="seconds"),
                },
                indent=2,
            )
            + "\n"
        )
        return ResolvedModel(
            model_path,
            policy_path,
            f"{name}/{mv.version}",
            model_version,
            policy_version,
            "registry",
        )
    except ValueError:
        raise  # a verification failure is never masked by the cache
    except Exception as exc:  # any registry or network failure
        pin_file = pins / f"{alias}.json"
        if not allow_cache or not pin_file.exists():
            raise RegistryUnavailable(f"cannot resolve {uri} and no cached pin: {exc}") from exc
        pin = json.loads(pin_file.read_text())
        model_path, policy_path, model_version, policy_version = _verify_bundle(
            cache_dir / name / pin["version"]
        )
        if model_version != pin["model_version"]:
            raise ValueError("cached bundle does not match its pin") from exc
        return ResolvedModel(
            model_path,
            policy_path,
            f"{name}/{pin['version']}",
            model_version,
            policy_version,
            "cache",
        )


@dataclass(frozen=True)
class AliasTarget:
    registry_ref: str
    model_version: str | None
    policy_version: str | None


def alias_target(tracking_uri: str, uri: str) -> AliasTarget:
    """Where an alias points *now*, from registry metadata only (no download, no verification).

    For display next to the running deployment, which resolved its alias once at startup; the
    two differ after an alias move until the service restarts. Raises on registry failure.
    """
    name, alias = parse_uri(uri)
    mv = _client(tracking_uri).get_model_version_by_alias(name, alias)
    return AliasTarget(
        f"{name}/{mv.version}", mv.tags.get("model_version"), mv.tags.get("policy_version")
    )
