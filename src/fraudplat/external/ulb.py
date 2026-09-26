"""ULB credit-card fraud dataset: download, verify, load (offline benchmark only).

Source: Machine Learning Group, Université Libre de Bruxelles (with Worldline). 284,807 European
card transactions over two days in September 2013, 492 frauds. Features V1 to V28 are PCA components
released by the data owner; `Time` is seconds since the first transaction; `Amount` is the
transaction amount. There are no customer or merchant identifiers, so this data cannot feed the
platform's streaming features; it is used only to check the modelling and evaluation method on real
data.

Two acquisition routes, both verified by checksum and both producing the same Parquet file:

* `kaggle` (preferred): the owner-listed source, Kaggle dataset `mlg-ulb/creditcardfraud`
  (version 3; ODbL v1.0 for the database, DbCL v1.0 for its contents), through Kaggle's official
  download endpoint. If Kaggle asks for sign-in, download `archive.zip` from the dataset page
  yourself into `data/external/ulb-creditcard/` and run the command again; it is never bypassed.
* `openml`: OpenML dataset 1597 v1, the copy the original benchmark was run on. A cell-by-cell
  comparison (`scripts/compare_ulb_sources.py`) found the two copies' data rows identical.

The data is never committed: it is written to the git-ignored `data/external/ulb-creditcard/`,
with `source.json` recording which route produced the local copy.

Run: python -m fraudplat.external.ulb fetch [--source kaggle|openml]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import polars as pl

URL = "https://openml.org/data/v1/download/1673544/creditcard.arff"
MD5 = "178bcf9bb1f31a3dfe12d0e577884add"
KAGGLE_PAGE = "https://www.kaggle.com/datasets/mlg-ulb/creditcardfraud"
KAGGLE_URL = "https://www.kaggle.com/api/v1/datasets/download/mlg-ulb/creditcardfraud"
KAGGLE_VERSION = 3
KAGGLE_CSV_SHA256 = "76274b691b16a6c49d3f159c883398e03ccd6d1ee12d9d8ee38f4b4b98551a89"
DIR = Path("data/external/ulb-creditcard")
FEATURES = [f"V{i}" for i in range(1, 29)] + ["Amount"]


def parse_arff(text: str) -> pl.DataFrame:
    names: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        low = line.strip().lower()
        if low.startswith("@attribute"):
            names.append(line.split()[1].strip("'\""))
        elif low.startswith("@data"):
            body = "\n".join(lines[i + 1 :]).replace("'", "")
            schema = {n: (pl.Utf8 if n == "Class" else pl.Float64) for n in names}
            frame = pl.read_csv(io.StringIO(body), has_header=False, schema=schema)
            return frame.with_columns(pl.col("Class").cast(pl.Int8))
    raise ValueError("no @DATA section")


def parse_csv(data: bytes) -> pl.DataFrame:
    header = data.split(b"\n", 1)[0].decode().replace('"', "").strip().split(",")
    schema = {n: (pl.Utf8 if n == "Class" else pl.Float64) for n in header}
    frame = pl.read_csv(io.BytesIO(data), schema=schema)
    return frame.with_columns(pl.col("Class").cast(pl.Int8))


def download(url: str, target: Path) -> None:
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as response:
        if response.status_code in {401, 403}:
            raise SystemExit(
                f"{url} requires sign-in ({response.status_code}). Download archive.zip from "
                f"{KAGGLE_PAGE} yourself into {target.parent}/ and run this command again."
            )
        response.raise_for_status()
        with target.open("wb") as sink:
            for chunk in response.iter_bytes():
                sink.write(chunk)


def fetch(directory: Path = DIR, source: str = "kaggle") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if source == "kaggle":
        archive = directory / "archive.zip"
        if not archive.exists():
            download(KAGGLE_URL, archive)
        with zipfile.ZipFile(archive) as bundle:
            raw = bundle.read("creditcard.csv")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != KAGGLE_CSV_SHA256:
            raise SystemExit(f"creditcard.csv SHA-256 {digest} != {KAGGLE_CSV_SHA256}")
        frame = parse_csv(raw)
        record = {"source": "kaggle", "dataset": f"mlg-ulb/creditcardfraud v{KAGGLE_VERSION}"}
        record |= {"url": KAGGLE_PAGE, "file": "creditcard.csv", "sha256": digest}
    else:
        arff = directory / "creditcard.arff"
        if not arff.exists():
            download(URL, arff)
        digest = hashlib.md5(arff.read_bytes(), usedforsecurity=False).hexdigest()
        if digest != MD5:
            raise SystemExit(f"checksum mismatch for {arff}: {digest} != {MD5}")
        frame = parse_arff(arff.read_text())
        record = {"source": "openml", "dataset": "OpenML 1597 v1", "url": URL}
        record |= {"file": "creditcard.arff", "md5": digest}
    out = directory / "creditcard.parquet"
    frame.write_parquet(out)
    record["fetched_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    (directory / "source.json").write_text(json.dumps(record, indent=2) + "\n")
    return out


def source(directory: Path = DIR) -> dict[str, str]:
    """Which verified route produced the local copy (written by `fetch`)."""
    path = directory / "source.json"
    if not path.exists():
        raise SystemExit("dataset source unknown: run `python -m fraudplat.external.ulb fetch`")
    record: dict[str, str] = json.loads(path.read_text())
    return record


def load(directory: Path = DIR) -> pl.DataFrame:
    path = directory / "creditcard.parquet"
    if not path.exists():
        raise SystemExit("dataset missing: run `python -m fraudplat.external.ulb fetch`")
    return pl.read_parquet(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["fetch"])
    parser.add_argument("--source", choices=["kaggle", "openml"], default="kaggle")
    args = parser.parse_args()
    out = fetch(source=args.source)
    frame = pl.read_parquet(out)
    print(f"{out}: {frame.height:,} rows, {int(frame['Class'].sum())} frauds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
