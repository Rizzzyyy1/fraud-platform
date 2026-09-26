"""Reproducibility rerun of the frozen ULB benchmark, with saved artifacts and a comparison.

    python -m fraudplat.external.reproduce [--out DIR]

The original run (`fraudplat.external.benchmark`, results in `reports/external/ulb/benchmark.json`)
saved metrics only. This rerun repeats it with the committed protocol code, split boundaries, seeds
and the configurations the original run selected (read from the reference JSON; no grid search,
no new thresholds, no model choice), and this time keeps what the original did not:

* the fitted models: logistic regression as plain JSON parameters (scaler mean and scale,
  coefficients, intercept) and XGBoost in its native JSON format;
* the feature order and schema, the selected configurations and validation thresholds;
* the dataset checksum, the code revision and the dependency versions;
* per-transaction scores and labels for the validation and test windows (Parquet);
* the reproduced metrics, a metric-by-metric comparison with the reference JSON, and a manifest
  of SHA-256 checksums for every file.

Everything is written to a git-ignored directory under `artifacts/` (default
`artifacts/external/ulb/reproduction/<UTC time>/`) and is not published: row-level data,
predictions and model files stay local until their publication suitability is checked. The
reference report is read, never written; `--out` refuses any path under `reports/`. Differences
from the reference are reported as measured, not reconciled.

`--summarize DIR` writes the committed, aggregate-only record of a rerun:
`reports/external/ulb/reproduction.json` (metrics, comparison, environment, file checksums; no
row-level data) and `reproduction.md`, rendered from it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import subprocess
import sys
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

from fraudplat.external import benchmark as bm
from fraudplat.external.ulb import FEATURES, load, source

REFERENCE = bm.OUT
ARTIFACTS = Path("artifacts/external/ulb/reproduction")
PACKAGES = ("numpy", "polars", "scikit-learn", "xgboost", "scipy")
# Differences at or below this are floating-point noise, not a changed result.
TOLERANCE = 1e-9


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git(*args: str) -> str:
    try:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def environment() -> dict[str, Any]:
    record = source()  # written by `ulb fetch` after verifying the file's checksum
    lock = Path("uv.lock")
    return {
        "dataset_source": record,
        "dataset_md5": record.get("md5"),  # OpenML route; the Kaggle route records its SHA-256
        "code_revision": git("rev-parse", "HEAD"),
        "code_dirty": bool(git("status", "--porcelain", "--", "src", "uv.lock")),
        "protocol_sha256": sha256(Path(bm.__file__)),
        "uv_lock_sha256": sha256(lock) if lock.exists() else None,
        "python": sys.version.split()[0],
        "platform": platform.platform(terse=True),
        "machine": platform.machine(),
        "packages": {p: version(p) for p in PACKAGES},
    }


def lr_params(model: Any) -> dict[str, Any]:
    scaler, lr = model.steps[0][1], model.steps[1][1]
    return {
        "format": "standardise then logistic: p = 1 / (1 + exp(-(((x - mean) / scale) . coef "
        "+ intercept)))",
        "features": FEATURES,
        "mean": scaler.mean_.tolist(),
        "scale": scaler.scale_.tolist(),
        "coef": lr.coef_[0].tolist(),
        "intercept": float(lr.intercept_[0]),
    }


def lr_score(params: dict[str, Any], x: np.ndarray) -> np.ndarray:
    z = ((x - np.array(params["mean"])) / np.array(params["scale"])) @ np.array(params["coef"])
    return np.asarray(1.0 / (1.0 + np.exp(-(z + params["intercept"]))))


def evaluate(family: str, config: dict[str, Any], data: pl.DataFrame) -> dict[str, Any]:
    """The committed protocol for one fixed configuration (bm.main, minus the grid)."""
    train, valid, test = (bm.window(data, b) for b in (bm.TRAIN, bm.VALID, bm.TEST))
    (xt, yt), (xv, yv), (xs, ys) = bm.xy(train), bm.xy(valid), bm.xy(test)
    model = bm.build(family, config).fit(xt, yt)
    pv = model.predict_proba(xv)[:, 1]
    threshold = float(np.sort(pv)[::-1][math.ceil(bm.BUDGET * len(pv)) - 1])
    ps = model.predict_proba(xs)[:, 1]
    hours = (test["Time"].to_numpy() // bm.HOUR).astype(int)
    xa, ya = bm.xy(data)
    xr_tr, xr_te, yr_tr, yr_te = train_test_split(
        xa, ya, test_size=0.3, stratify=ya, random_state=bm.SEED
    )
    random_ap = float(
        average_precision_score(
            yr_te, bm.build(family, config).fit(xr_tr, yr_tr).predict_proba(xr_te)[:, 1]
        )
    )
    return {
        "model": model,
        "validation_scores": pv,
        "test_scores": ps,
        "metrics": {
            "selected": config,
            "validation_ap": float(average_precision_score(yv, pv)),
            "threshold_from_validation": threshold,
            "test": {
                "average_precision": float(average_precision_score(ys, ps)),
                "roc_auc": float(roc_auc_score(ys, ps)),
                "at_fixed_threshold": bm.at_threshold(ys, ps, threshold),
                "ece_raw": bm.ece(ys, ps),
                "mean_score": float(ps.mean()),
                "fraud_rate": float(ys.mean()),
                "ci95": bm.hour_block_ci(ys, ps, hours, threshold),
            },
            "random_split_contrast_ap": random_ap,
        },
    }


def flatten(value: Any, prefix: str = "") -> dict[str, float]:
    if isinstance(value, dict):
        out: dict[str, float] = {}
        for k, v in value.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(value, list):
        return {f"{prefix}[{i}]": float(v) for i, v in enumerate(value)}
    return {prefix: float(value)}


def compare(reference: dict[str, Any], reproduced: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for family, rep in reproduced.items():
        ref = {k: v for k, v in reference["families"][family].items() if k != "grid"}
        a, b = flatten(ref), flatten(rep)
        for key in sorted(a.keys() | b.keys()):
            ra, rb = a.get(key), b.get(key)
            diff = None if ra is None or rb is None else abs(ra - rb)
            rows.append(
                {
                    "family": family,
                    "metric": key,
                    "reference": ra,
                    "reproduced": rb,
                    "abs_diff": diff,
                    "within_tolerance": diff is not None and diff <= TOLERANCE,
                }
            )
    return {
        "tolerance": TOLERANCE,
        "all_within_tolerance": all(r["within_tolerance"] for r in rows),
        "max_abs_diff": max((r["abs_diff"] or 0.0) for r in rows),
        "rows": rows,
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def reproduce(out: Path) -> dict[str, Any]:
    reports = Path("reports").resolve()
    if out.resolve() == reports or reports in out.resolve().parents:
        raise SystemExit("refusing to write under reports/: the reference report is not replaced")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty")
    reference: dict[str, Any] = json.loads(REFERENCE.read_text())
    started = datetime.now(UTC)
    env = environment()
    data = load()
    out.mkdir(parents=True, exist_ok=True)
    reproduced: dict[str, Any] = {}
    columns: dict[str, Any] = {}
    parity: dict[str, float] = {}
    for family, ref in reference["families"].items():
        config = ref["selected"]
        best = max(ref["grid"], key=lambda t: t["validation_ap"])["config"]
        if best != config:
            raise SystemExit(f"{family}: reference selection is inconsistent with its grid")
        result = evaluate(family, config, data)
        reproduced[family] = result["metrics"]
        columns[f"{family}_score"] = np.concatenate(
            [result["validation_scores"], result["test_scores"]]
        )
        (out / family).mkdir()
        xs = bm.xy(bm.window(data, bm.TEST))[0]
        if family == "lr":
            params = lr_params(result["model"])
            write_json(out / family / "model.json", params)
            reloaded = lr_score(json.loads((out / family / "model.json").read_text()), xs)
        else:
            result["model"].save_model(out / family / "model.json")
            clf = XGBClassifier()
            clf.load_model(out / family / "model.json")
            reloaded = clf.predict_proba(xs)[:, 1]
        parity[family] = float(np.max(np.abs(reloaded - result["test_scores"])))
    rows = pl.concat(
        [
            bm.window(data.with_row_index("row"), bm.VALID).with_columns(
                pl.lit("validation").alias("window")
            ),
            bm.window(data.with_row_index("row"), bm.TEST).with_columns(
                pl.lit("test").alias("window")
            ),
        ]
    ).select("row", "window", "Time", "Class")
    rows.with_columns(**{k: pl.Series(v) for k, v in columns.items()}).write_parquet(
        out / "predictions.parquet"
    )
    finished = datetime.now(UTC)
    write_json(
        out / "schema.json",
        {
            "features": FEATURES,
            "feature_dtype": "float64",
            "label": "Class (int, 1 = fraud)",
            "windows_seconds": {"train": bm.TRAIN, "validation": bm.VALID, "test": bm.TEST},
            "predictions_columns": {
                "row": "0-based row position in the OpenML ARFF",
                "window": "validation or test",
                "Time": "seconds since the first transaction",
                "Class": "label",
                "lr_score": "logistic regression probability",
                "xgb_score": "XGBoost probability",
            },
        },
    )
    write_json(
        out / "config.json",
        {
            "selected": {f: m["selected"] for f, m in reproduced.items()},
            "thresholds_from_validation": {
                f: m["threshold_from_validation"] for f, m in reproduced.items()
            },
            "review_budget": bm.BUDGET,
            "seed": bm.SEED,
            "bootstrap": bm.BOOTSTRAP,
            "selection_source": f"{REFERENCE} (original run's grid); no search in this rerun",
        },
    )
    comparison = compare(reference, reproduced)
    run = {
        "kind": "reproducibility rerun",
        "reference": {
            "report": str(REFERENCE),
            "created_at": reference["created_at"],
            "sha256": sha256(REFERENCE),
            "saved_artifacts": "none (metrics only)",
        },
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "environment": env,
        "reload_parity_max_abs_diff": parity,
        "metrics": reproduced,
        "comparison": comparison,
    }
    write_json(out / "run.json", run)
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "manifest.json")
    write_json(
        out / "manifest.json",
        {
            "files": {
                str(p.relative_to(out)): {"sha256": sha256(p), "bytes": p.stat().st_size}
                for p in files
            },
            "publication": "local only; row-level data, predictions and models are not published",
        },
    )
    return run


SUMMARY = REFERENCE.with_name("reproduction.json")
ODBL_NOTICE = (
    "Contains information from [Credit Card Fraud Detection]"
    "(https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud), which is made available here "
    "under the [Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/)."
)


def summarize(run_dir: Path, imported_at: str | None) -> dict[str, Any]:
    run: dict[str, Any] = json.loads((run_dir / "run.json").read_text())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    return {
        "rerun_id": run_dir.name,
        "records": {
            "original_benchmark": run["reference"]["created_at"],
            "mlflow_import": imported_at,
            "reproducibility_rerun": [run["started_at"], run["finished_at"]],
        },
        **{k: v for k, v in run.items() if k not in {"kind"}},
        "local_artifacts": manifest["files"],
    }


def render(s: dict[str, Any]) -> str:
    env, cmp, rec = s["environment"], s["comparison"], s["records"]
    lines = [
        "# ULB benchmark: reproducibility rerun",
        "",
        "Generated from `reproduction.json` by `python -m fraudplat.external.reproduce "
        "--summarize`. The reference results (`benchmark.json`, `benchmark.md`) are unchanged.",
        "",
        "## Three records, kept apart",
        "",
        "| Record | When (UTC) | What it is |",
        "|---|---|---|",
        f"| Original benchmark | {rec['original_benchmark']} | The one pre-registered evaluation. "
        "Metrics only; models and predictions were not saved. |",
        f"| MLflow import | {rec['mlflow_import'] or 'not recorded'} | The original's committed "
        "results recorded afterwards as an imported historical run (start time set to the "
        "original evaluation). Nothing recomputed. |",
        f"| Reproducibility rerun | {rec['reproducibility_rerun'][0]} to "
        f"{rec['reproducibility_rerun'][1]} | Same protocol code, windows, seeds and the "
        "original's selected configurations; no search, no new thresholds. Saves artifacts "
        "locally. Logged as its own MLflow run linked to the import. |",
        "",
        "## Result",
        "",
        f"{len(cmp['rows'])} recorded values compared (validation AP, threshold, every test "
        "metric and interval bound, random-split contrast, per family); maximum absolute "
        f"difference {cmp['max_abs_diff']:.3g}; all within {cmp['tolerance']:g}: "
        f"{'yes' if cmp['all_within_tolerance'] else 'no'}.",
        "",
        "Scope of this result: the values were reproduced identically in the rerun environment "
        "recorded below. The original run's environment was not recorded, so this does not show "
        "that the original ran in the same environment, nor that other platforms or library "
        "versions give the same digits.",
        "",
        "| Family | Selected | Test AP reference | Test AP rerun | Threshold reference | "
        "Threshold rerun |",
        "|---|---|---|---|---|---|",
    ]
    by = {(r["family"], r["metric"]): r for r in cmp["rows"]}
    for family, m in s["metrics"].items():
        ap, th = by[(family, "test.average_precision")], by[(family, "threshold_from_validation")]
        lines.append(
            f"| {family} | {', '.join(f'{k}={v}' for k, v in m['selected'].items())} | "
            f"{ap['reference']:.6f} | {ap['reproduced']:.6f} | {th['reference']:.6g} | "
            f"{th['reproduced']:.6g} |"
        )
    if not cmp["all_within_tolerance"]:
        lines += ["", "Differences:", ""]
        lines += [
            f"* {r['family']} {r['metric']}: {r['reference']} vs {r['reproduced']}"
            for r in cmp["rows"]
            if not r["within_tolerance"]
        ]
    parity = ", ".join(f"{f} {v:.3g}" for f, v in s["reload_parity_max_abs_diff"].items())
    lines += [
        "",
        "Saved models reloaded from disk give the same test scores "
        f"(max abs difference: {parity}).",
        "",
        "## Environment",
        "",
        f"* Dataset MD5 `{env['dataset_md5']}`; code revision `{env['code_revision'][:12]}` "
        f"(uncommitted changes: {'yes' if env['code_dirty'] else 'no'}); protocol SHA-256 "
        f"`{env['protocol_sha256'][:16]}…`; `uv.lock` SHA-256 `{str(env['uv_lock_sha256'])[:16]}…`",
        f"* Python {env['python']}, {env['platform']} {env.get('machine', '')}; "
        + ", ".join(f"{p} {v}" for p, v in env["packages"].items()),
        "* The original run did not record its environment. `uv.lock` and the protocol code are "
        "unchanged in the repository since the results commit; which versions were installed "
        "when the original ran is not known.",
        "",
        "## Artifacts (local only, not published)",
        "",
        "Written to `artifacts/external/ulb/reproduction/<rerun id>/` (git-ignored). Row-level "
        "predictions and model files stay local until their publication suitability is checked "
        "(see `THIRD_PARTY_NOTICES.md`).",
        "",
        "| File | Bytes | SHA-256 |",
        "|---|---|---|",
    ]
    lines += [
        f"| `{name}` | {f['bytes']:,} | `{f['sha256']}` |"
        for name, f in sorted(s["local_artifacts"].items())
    ]
    lines += ["", ODBL_NOTICE]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    parser.add_argument("--out", type=Path, default=ARTIFACTS / stamp)
    parser.add_argument("--summarize", type=Path, help="write the committed summary of this rerun")
    parser.add_argument("--imported-at", help="UTC time of the MLflow import, for the summary")
    args = parser.parse_args()
    if args.summarize:
        summary = summarize(args.summarize, args.imported_at)
        write_json(SUMMARY, summary)
        SUMMARY.with_suffix(".md").write_text(render(summary))
        print(f"wrote {SUMMARY} and {SUMMARY.with_suffix('.md')}")
        return 0
    run = reproduce(args.out)
    c = run["comparison"]
    print(
        f"{args.out}: {len(c['rows'])} metrics compared, max abs diff {c['max_abs_diff']:.3g}, "
        f"all within {c['tolerance']:g}: {c['all_within_tolerance']}; "
        f"reload parity {run['reload_parity_max_abs_diff']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
