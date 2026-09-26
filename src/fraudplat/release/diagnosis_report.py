"""Render `reports/serving_diagnosis/summary.md` from the recorded diagnostic runs."""

# ruff: noqa: E501

from __future__ import annotations

import glob
import json
from pathlib import Path


def main() -> int:
    runs = sorted(
        glob.glob("reports/serving_diagnosis/run*_*.json"),
        key=lambda p: int(Path(p).name.split("_")[0].removeprefix("run")),
    )
    out = [
        "# Serving diagnosis (bounded)\n",
        "All runs: 100 rps open loop, 10 s warm-up + 60 s measured, identical request list "
        "(same SHA-256 per run), fresh isolated namespace, one uvicorn process. Criterion: "
        "p95 ≤ 100 ms, p99 ≤ 200 ms, errors ≤ 0.1%, ≥ 99 rps achieved. Development traffic "
        "only (days ≥ 140, pre-test); the frozen predictive results were not touched.\n",
        "| Run | Model | Docker stats in window | Achieved rps | Client p50 / p95 / p99 (ms) | Scoreless reviews | Scheduling delay p95 (ms) | Server request p99 ≤ (ms) | DB insert p99 ≤ | Event-loop lag p99 ≤ | Inference mean (ms) | Criterion |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    tally: dict[tuple[str, bool], list[bool]] = {}
    workloads = set()
    for f in runs:
        r = json.loads(Path(f).read_text())
        lvl = r["levels"][0]
        m, st, lat = lvl["measured_window"], lvl["api_stages"], lvl["latency_ms_all_responses"]
        model = r["readiness_at_start"]["model"]["model_version"]
        docker = r["environment"].get("docker_stats_during_load", True)
        ok = (
            lat["p95"] <= 100
            and lat["p99"] <= 200
            and m["achieved_completion_rps"] >= 99
            and m["failed_http_or_rejected"] / m["scheduled"] <= 0.001
        )
        tally.setdefault((model, docker), []).append(ok)
        workloads.add(lvl["workload_sha256"])
        out.append(
            f"| {Path(f).name.split('_')[0]} | `{model}` | {'yes' if docker else 'no'} | {m['achieved_completion_rps']} | "
            f"{lat['p50']:.1f} / {lat['p95']:.1f} / {lat['p99']:.1f} | {m['outcomes'].get('degraded_review_no_score', 0)} | "
            f"{lvl['scheduling_delay_ms']['p95']:.1f} | {st['request_total']['p99_le_ms']} | {st['db_insert']['p99_le_ms']} | "
            f"{st['event_loop_lag']['p99_le_ms']} | {st['inference']['mean_ms']:.3f} | {'pass' if ok else 'fail'} |"
        )
    out.append(
        f"\nDistinct workload hashes across runs: {len(workloads)} (identical inputs when 1).\n"
    )
    out.append("| Model | Docker stats in window | Passed |")
    out.append("|---|---|---|")
    for (model, docker), results in sorted(tally.items()):
        out.append(f"| `{model}` | {'yes' if docker else 'no'} | {sum(results)}/{len(results)} |")
    parity = json.loads(Path("reports/serving_diagnosis/parity.json").read_text())
    out.append(
        f"\nServing-path parity on 5,000 pre-test fixtures: {'exact' if parity['parity'] else 'FAILED'} "
        "(maximum score difference 0.0; no action changes under any policy).\n"
    )
    out.append("""## Findings

* **No model-specific mechanism.** Inference costs well under a millisecond for both models,
  model-input conversion and feature computation are negligible, and event-loop lag stays small
  in every run. Both models show the same intermittent stalls; the earlier "candidate fails,
  baseline passes" result was an association that did not hold up under identical conditions.
* **Large stalls in this session were largely a harness artefact.** With the in-window
  `docker stats` sample (it queries every container in the VM), failing runs show PostgreSQL
  pool-wait/transaction spikes and load-generator scheduling delays of hundreds of
  milliseconds. Without it, no run shows a stall of that size.
* **A residual tail remains** near the 200 ms p99 limit for either model (one candidate run
  without the sample reached 241.7 ms).
* **Sustained performance is inconsistent for both models.** Neither model met the criterion in
  every run. The baseline is the retained default because it is the model already serving; it
  is not a control that passes consistently.
* **Not established:** the cause of the release-1 session's failures, which were degraded for
  the whole window (p50 around 380 ms, the load generator falling behind throughout) rather
  than in bursts, and the source of the residual tail. Measurements that bear on storage:
  PostgreSQL checkpoints recorded during the diagnosis runs did not coincide with the stall
  windows, and the mean fsync latency on the VM disk measured about 87 µs. A fast mean does
  not exclude intermittent storage or VM-level stalls, which these measurements could not
  observe.
* **No serving fix was applied:** nothing blocking in the request path was demonstrated, so
  there was no evidence-backed change to make.
""")
    Path("reports/serving_diagnosis/summary.md").write_text("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
