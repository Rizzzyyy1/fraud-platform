"""ULB credit-card fraud dataset: download, verify, load (offline benchmark only).

Source: Machine Learning Group, Université Libre de Bruxelles, via OpenML dataset 1597
("creditcard", version 1). 284,807 European card transactions over two days in September 2013,
492 frauds. Features V1 to V28 are PCA components released by the data owner; `Time` is seconds
since the first transaction; `Amount` is the transaction amount. There are no customer or merchant
identifiers, so this data cannot feed the platform's streaming features; it is used only to check
the modelling and evaluation method on real data.

The data is never committed: `fetch` downloads it into `data/external/ulb-creditcard/`, checks
the MD5 checksum published by OpenML, and writes a Parquet copy.

Run: python -m fraudplat.external.ulb fetch
"""

from __future__ import annotations

import argparse
import hashlib
import io
from pathlib import Path

import httpx
import polars as pl

URL = "https://openml.org/data/v1/download/1673544/creditcard.arff"
MD5 = "178bcf9bb1f31a3dfe12d0e577884add"
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


def fetch(directory: Path = DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    arff = directory / "creditcard.arff"
    if not arff.exists():
        with httpx.stream("GET", URL, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            with arff.open("wb") as sink:
                for chunk in response.iter_bytes():
                    sink.write(chunk)
    digest = hashlib.md5(arff.read_bytes(), usedforsecurity=False).hexdigest()
    if digest != MD5:
        raise SystemExit(f"checksum mismatch for {arff}: {digest} != {MD5}")
    out = directory / "creditcard.parquet"
    parse_arff(arff.read_text()).write_parquet(out)
    return out


def load(directory: Path = DIR) -> pl.DataFrame:
    path = directory / "creditcard.parquet"
    if not path.exists():
        raise SystemExit("dataset missing: run `python -m fraudplat.external.ulb fetch`")
    return pl.read_parquet(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["fetch"])
    parser.parse_args()
    out = fetch()
    frame = pl.read_parquet(out)
    print(f"{out}: {frame.height:,} rows, {int(frame['Class'].sum())} frauds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
