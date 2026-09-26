"""The local dashboard deployment: its namespace, its demo traffic, and its processes.

The dashboard runs its own stack (API, publisher, worker) in a dedicated `replay:dashboard-<ts>`
namespace, never `live`. Its Redis state is bootstrapped once with sim-v2 history available
before day 140; demo traffic is the pre-test transactions decided on days [140, 153), in decision
order, sent with transaction ids prefixed by the deployment (`D<ts>-...`) so they never collide
with benchmark or live rows. Customer and terminal ids are unchanged, so features are computed
from the same history. The held-out test period (decision time on day >= 153) is never read.

State files (git-ignored) under `run/dashboard/`:
  deployment.json   namespace, id prefix, creation time, request count
  requests.json     the demo request list (derived from the immutable dataset)
  pids.json         child process ids written by the launcher
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import polars as pl

from fraudplat.features.offline import HistoricalTxn, split_at_cutoff
from fraudplat.features.spec import DAY
from fraudplat.hashing import format_utc
from fraudplat.pipeline.streams import Stream
from fraudplat.simulator.dataset import to_historical

DATASET = Path("data/raw/sim-v2")
CUTOFF_DAY = 140
TEST_START_DAY = 153
STATE_DIR = Path("run/dashboard")


@dataclass(frozen=True)
class Deployment:
    namespace: str
    id_prefix: str
    created_at: str
    requests: int
    cutoff_day: int = CUTOFF_DAY

    @property
    def stream(self) -> Stream:
        return Stream(self.namespace)


def load_deployment(state_dir: Path = STATE_DIR) -> Deployment | None:
    path = state_dir / "deployment.json"
    if not path.exists():
        return None
    return Deployment(**json.loads(path.read_text()))


def save_deployment(deployment: Deployment, state_dir: Path = STATE_DIR) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "deployment.json").write_text(json.dumps(asdict(deployment), indent=2) + "\n")


def day_bounds(raw: pl.DataFrame) -> tuple[int, int, int]:
    day0 = int(raw["event_time"].dt.epoch("us").min())  # type: ignore[arg-type]
    day0 -= day0 % DAY
    return day0, day0 + CUTOFF_DAY * DAY, day0 + TEST_START_DAY * DAY


def pre_test_history(dataset: Path = DATASET) -> tuple[list[HistoricalTxn], pl.DataFrame, int]:
    """All transactions *received* before the test period, and the bootstrap cutoff."""
    raw = pl.read_parquet(dataset / "transactions.parquet")
    _, cutoff, test_start = day_bounds(raw)
    pre_test = raw.filter(pl.col("received_at").dt.epoch("us") < test_start)
    return to_historical(pre_test), pre_test, cutoff


def demo_requests(
    txns: list[HistoricalTxn], pre_test: pl.DataFrame, cutoff_us: int, id_prefix: str
) -> list[dict[str, Any]]:
    scoring = sorted(
        split_at_cutoff(txns, cutoff_us).scoring,
        key=lambda t: (t.decision_time_us, t.event.event_id),
    )
    rows = {r["transaction_id"]: r for r in pre_test.iter_rows(named=True)}
    return [
        {
            "transaction_id": f"{id_prefix}{t.event.event_id}",
            "customer_id": t.event.customer_id,
            "terminal_id": t.event.terminal_id,
            "amount_minor": t.event.amount_minor,
            "currency": rows[t.event.event_id]["currency"],
            "event_time": format_utc(rows[t.event.event_id]["event_time"]),
        }
        for t in scoring
    ]


def new_deployment(requests: int, now: float | None = None) -> Deployment:
    ts = int(time.time() if now is None else now)
    return Deployment(
        namespace=f"replay:dashboard-{ts}",
        id_prefix=f"D{ts}-",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        requests=requests,
    )


def load_requests(state_dir: Path = STATE_DIR) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads((state_dir / "requests.json").read_text())
    return data


def read_pids(state_dir: Path = STATE_DIR) -> dict[str, int]:
    path = state_dir / "pids.json"
    if not path.exists():
        return {}
    data: dict[str, int] = json.loads(path.read_text())
    return data


def process_command(pid: int) -> str | None:
    """The command line of a live process, or None. Used to confirm a pid before signalling."""
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    command = out.stdout.strip()
    return command or None


def is_worker(pid: int, namespace: str) -> bool:
    command = process_command(pid)
    return (
        command is not None
        and "fraudplat.pipeline.worker" in command
        and f"--namespace {namespace}" in command
    )


def process_state(pid: int) -> str | None:
    """`ps` state letters (e.g. 'S', 'R', 'T' for stopped), or None if the process is gone."""
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/bin/ps", "-p", str(pid), "-o", "state="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None
