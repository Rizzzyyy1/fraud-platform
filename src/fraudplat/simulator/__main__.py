"""CLI: `python -m fraudplat.simulator generate|verify`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fraudplat.simulator.config import load_config
from fraudplat.simulator.dataset import DatasetExists, content_hash, verify_dataset, write_dataset
from fraudplat.simulator.generate import generate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fraudplat.simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="generate an immutable raw dataset")
    gen.add_argument("--config", type=Path, required=True)
    gen.add_argument("--out", type=Path, default=Path("data/raw"))
    ver = sub.add_parser("verify", help="check hashes and regenerate to confirm determinism")
    ver.add_argument("--config", type=Path, required=True)
    ver.add_argument("--out", type=Path, default=Path("data/raw"))
    args = parser.parse_args(argv)

    config = load_config(args.config)
    directory = args.out / config.dataset_id
    if args.command == "generate":
        try:
            path = write_dataset(generate(config), config, args.out)
        except DatasetExists as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"wrote {path}")
        return 0

    problems = verify_dataset(directory)
    regenerated = content_hash(generate(config).transactions, "transaction_id")
    manifest = json.loads((directory / "manifest.json").read_text())
    if regenerated != manifest["tables"]["transactions"]["content_sha256"]:
        problems.append("regeneration from config produced different transactions")
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if not problems:
        print(f"OK {directory}: hashes match and regeneration is identical")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
