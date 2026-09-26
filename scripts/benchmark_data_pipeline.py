"""Benchmark simulator generation and point-in-time reconstruction at increasing sizes.

Each phase runs in a fresh subprocess so its peak RSS is not inflated by earlier phases:
  generate     simulate + write Parquet           -> elapsed, peak RSS, output bytes
  reconstruct  read Parquet + events + features   -> elapsed (split), peak RSS

Sizes scale customers and terminals by a fraction of the sim-v2 config; the radius is scaled by
1/sqrt(fraction) so the expected number of terminals per customer stays constant.

Usage: python scripts/benchmark_data_pipeline.py --fractions 0.1 0.25 0.5 [--out reports/...]
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _peak_rss_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS and KiB on Linux.
    return peak / 2**20 if sys.platform == "darwin" else peak / 2**10


def _scaled_config(fraction: float) -> object:
    from fraudplat.simulator.config import load_config

    base = load_config(ROOT / "configs" / "sim-v2.toml")
    return base.model_copy(
        update={
            "dataset_id": f"bench-{fraction:g}",
            "n_customers": round(base.n_customers * fraction),
            "n_terminals": round(base.n_terminals * fraction),
            "radius": base.radius / math.sqrt(fraction),
        }
    )


def phase_generate(fraction: float, workdir: Path) -> dict[str, float]:
    from fraudplat.simulator.dataset import write_dataset
    from fraudplat.simulator.generate import generate

    config = _scaled_config(fraction)
    start = time.perf_counter()
    dataset = generate(config)  # type: ignore[arg-type]
    generated = time.perf_counter()
    directory = write_dataset(dataset, config, workdir)  # type: ignore[arg-type]
    written = time.perf_counter()
    return {
        "rows": dataset.transactions.height,
        "generate_s": generated - start,
        "write_and_hash_s": written - generated,
        "output_mib": sum(p.stat().st_size for p in directory.iterdir()) / 2**20,
        "peak_rss_mib": _peak_rss_mib(),
    }


def phase_reconstruct(fraction: float, workdir: Path) -> dict[str, float]:
    from fraudplat.features.offline import point_in_time_matrix
    from fraudplat.simulator.dataset import load_transactions, to_historical

    directory = workdir / f"bench-{fraction:g}"
    start = time.perf_counter()
    frame = load_transactions(directory)
    loaded = time.perf_counter()
    txns = to_historical(frame)
    converted = time.perf_counter()
    matrix = point_in_time_matrix(txns)
    done = time.perf_counter()
    return {
        "rows": len(txns),
        "read_s": loaded - start,
        "to_events_s": converted - loaded,
        "reconstruct_s": done - converted,
        "reconstruct_us_per_row": (done - converted) / len(txns) * 1e6,
        "matrix_mib": matrix.values.nbytes / 2**20,
        "peak_rss_mib": _peak_rss_mib(),
    }


def run_child(phase: str, fraction: float, workdir: Path) -> dict[str, float]:
    out = subprocess.run(  # noqa: S603 - re-invokes this script with fixed arguments
        [sys.executable, __file__, "--child", phase, str(fraction), str(workdir)],
        capture_output=True,
        text=True,
        check=True,
    )
    result: dict[str, float] = json.loads(out.stdout.strip().splitlines()[-1])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fractions", type=float, nargs="+", default=[0.1, 0.25, 0.5])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--child", nargs=3, metavar=("PHASE", "FRACTION", "WORKDIR"))
    args = parser.parse_args()

    if args.child:
        phase, fraction, workdir = args.child[0], float(args.child[1]), Path(args.child[2])
        fn = phase_generate if phase == "generate" else phase_reconstruct
        print(json.dumps(fn(fraction, workdir)))
        return 0

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        for fraction in args.fractions:
            gen = run_child("generate", fraction, Path(tmp))
            rec = run_child("reconstruct", fraction, Path(tmp))
            row = {"fraction": fraction, "generate": gen, "reconstruct": rec}
            results.append(row)
            print(json.dumps(row), flush=True)
    report = {
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": platform.python_version(),
        },
        "results": results,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
