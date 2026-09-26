"""Checkpoint B demonstration: dataset facts, cutoff split, and the arrival-time leak it prevents.

Run: `python -m fraudplat.demos.checkpoint_b --dataset data/raw/sim-small-v1 --cutoff-day 45`
Everything printed is computed from the dataset; nothing is typed in.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from fraudplat.features.offline import point_in_time_features, split_at_cutoff
from fraudplat.features.spec import DAY, US
from fraudplat.simulator.dataset import load_transactions, to_historical, verify_dataset


def _iso(us: int) -> str:
    return datetime.fromtimestamp(us / US, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--cutoff-day", type=int, default=45)
    args = parser.parse_args(argv)

    problems = verify_dataset(args.dataset)
    manifest = json.loads((args.dataset / "manifest.json").read_text())
    print(f"dataset {manifest['dataset_id']}  (synthetic)  integrity: {problems or 'OK'}")
    print(f"  rows {manifest['rows']['transactions']:,}  seed {manifest['config']['seed']}")
    print(f"  content sha256 {manifest['tables']['transactions']['content_sha256'][:16]}…")
    prevalence = manifest["fraud_prevalence"]
    print(
        "  fraud prevalence  any {any:.3%}  s1 {s1:.3%}  s2 {s2:.3%}  s3 {s3:.3%}".format(
            **prevalence
        )
    )

    txns = to_historical(load_transactions(args.dataset))
    start = min(t.event.event_time_us for t in txns)
    first_day = start - start % DAY
    cutoff = first_day + args.cutoff_day * DAY
    split = split_at_cutoff(txns, cutoff)
    print(f"\ncutoff C = {_iso(cutoff)} UTC")
    print(f"  bootstrap (available < C): {len(split.bootstrap):,}")
    print(f"  scoring (decided >= C):    {len(split.scoring):,}")
    straddle = [t for t in split.scoring if t.event.event_time_us < cutoff]
    print(
        f"  of which event_time < C but arrived after C (scored, not bootstrapped): {len(straddle)}"
    )

    correct = point_in_time_features(txns)
    leaky = point_in_time_features(txns, availability="event_time")
    right = {(r.event_id, r.decision_time_us): r.features for r in correct.rows}
    differ = [
        (r.event_id, r.decision_time_us)
        for r in leaky.rows
        if r.features != right[(r.event_id, r.decision_time_us)]
    ]
    print("\nimmediate-processing assumption (processing delay = 0)")
    statuses = {status.value: count for status, count in correct.apply_statuses.items()}
    print(f"  decisions reconstructed: {len(correct.rows):,}  apply statuses: {statuses}")
    print(
        f"  decisions whose features would change if history were filtered by event time only: "
        f"{len(differ):,} ({len(differ) / len(correct.rows):.2%})"
    )

    if differ:
        event_id, decision_us = differ[0]
        by_id = {t.event.event_id: t for t in txns}
        subject = by_id[event_id]
        late = [
            t
            for t in txns
            if (
                t.event.customer_id == subject.event.customer_id
                or t.event.terminal_id == subject.event.terminal_id
            )
            and t.event.event_time_us < subject.event.event_time_us
            and t.received_at_us > decision_us
        ]
        print(
            f"\nexample: decision {event_id} (customer {subject.event.customer_id}, terminal "
            f"{subject.event.terminal_id}) at {_iso(decision_us)}"
        )
        for t in late[:3]:
            print(
                f"  earlier txn {t.event.event_id} (customer {t.event.customer_id}, terminal "
                f"{t.event.terminal_id}): event {_iso(t.event.event_time_us)}, "
                f"arrived {_iso(t.received_at_us)} -> not yet visible, excluded"
            )
        good, bad = (
            right[(event_id, decision_us)],
            next(
                r.features
                for r in leaky.rows
                if (r.event_id, r.decision_time_us) == (event_id, decision_us)
            ),
        )
        for name in good:
            if good[name] != bad[name]:
                print(f"  {name}: point-in-time {good[name]}  vs  leaky {bad[name]}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
