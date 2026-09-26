"""Bounded browser smoke test: the real console and dashboard against a small seeded database.

    python scripts/browser_smoke.py --admin-url postgresql://USER:PASS@HOST:PORT/postgres

What it exercises: the console service (FastAPI BFF) and the production dashboard build in Google
Chrome, reading and writing a disposable PostgreSQL database seeded with 64 deterministic
decisions. What it does **not** exercise: the scoring API, Kafka, Redis or the feature worker;
the health view is expected to report the scoring API as unavailable. The streaming path and the
failure drill are covered by the integration tests and the full walkthrough
(`dashboard/e2e/walkthrough.mjs`).

Steps: create `fraud_smoke_test` on the given server (guarded by `tests/safety.py`: never the
application's database or server), migrate it, seed it, write a temporary deployment record and a
neutral admin account (random password, passed only in the browser process's environment and
never printed), start the console on a free port, run `dashboard/e2e/smoke.mjs`, and always tear
everything down. On failure, screenshots, page HTML, failed checks and the console log are kept in
`run/smoke-evidence/` (no passwords, cookies or API keys are written there).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fraudplat.console.auth import Accounts  # noqa: E402
from fraudplat.features.spec import FEATURE_NAMES  # noqa: E402
from tests.safety import assert_disposable_database  # noqa: E402

DB_NAME = "fraud_smoke_test"
NAMESPACE = "replay:smoke"
EVIDENCE = ROOT / "run" / "smoke-evidence"
MODEL, POLICY, REGISTRY = "lr-f1-6f0ebad8fcc7", "pol-6164cb21826d", "fraud-risk-f1/1"
REVIEW_THRESHOLD = 0.0381


def stage(message: str) -> None:
    print(f"[smoke] {message}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def app_database_url() -> str | None:
    value = os.environ.get("FRAUD_DATABASE_URL")
    env_file = ROOT / ".env"
    if value is None and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("FRAUD_DATABASE_URL="):
                value = line.split("=", 1)[1].strip()
    return value or None


def features(i: int, review: bool) -> dict[str, float | int | None]:
    """Deterministic, plausible f1 feature values (minor units for amounts)."""
    base = {
        "amount_minor": 4_000 + 137 * i + (30_000 if review else 0),
        "day_of_week_utc": i % 7,
        "hour_of_day_utc": (8 + i) % 24,
        "cust_txn_count_1h": i % 2,
        "cust_txn_count_1d": 1 + i % 4,
        "cust_txn_count_7d": 5 + i % 9,
        "cust_txn_count_30d": 20 + i % 40,
        "term_txn_count_1d": i % 3,
        "term_txn_count_7d": 2 + i % 6,
        "cust_amount_mean_7d": 5_000.0 + 11 * i,
        "cust_amount_mean_30d": 5_200.0 + 7 * i,
        "cust_has_history_30d": 1,
        "amount_to_cust_mean_30d": round((4_000 + 137 * i + (30_000 if review else 0)) / 5_200, 4),
        "cust_terminal_is_new_30d": 1 if review else 0,
        "secs_since_prev_cust_txn": 3_600 + 97 * i,
    }
    return {name: base.get(name) for name in FEATURE_NAMES}


def seed(url: str) -> dict[str, int]:
    """64 decisions in the last few minutes: 32 approved, 28 scored reviews, 4 scoreless reviews."""
    now = datetime.now(UTC)
    counts = {"approve": 0, "review": 0, "scoreless": 0}
    with psycopg.connect(url, autocommit=True) as db:
        for i in range(64):
            kind = "scoreless" if i % 16 == 15 else ("review" if i % 2 else "approve")
            counts[kind] += 1
            txn = f"S-TX{i:06d}"
            decided = now - timedelta(seconds=5 * i + 5)
            if kind == "approve":
                score, action = 0.002 + 0.0005 * i, "approve"
                codes, feats = ["SCORE_BELOW_REVIEW_THRESHOLD"], features(i, False)
            elif kind == "review":
                score, action = min(0.05 + 0.013 * i, 0.95), "review"
                codes, feats = ["SCORE_AT_OR_ABOVE_REVIEW_THRESHOLD"], features(i, True)
            else:
                score, action, feats = None, "review", {}
                codes = ["PIPELINE_STALE_BEYOND_LIMIT", "WORKER_HEARTBEAT_STALE", "NO_SCORE"]
            db.execute(
                "INSERT INTO decisions (transaction_id, request_hash, hash_version, customer_id, "
                "terminal_id, amount_minor, currency, event_time, received_at, decision_time, "
                "score, action, reason_codes, features, feature_freshness, model_version, "
                "feature_version, policy_version, model_registry_ref) VALUES (%s, %s, 1, %s, %s, "
                "%s, 'USD', %s, %s, %s, %s, %s, %s, %s, '{}', %s, 'f1', %s, %s)",
                (
                    txn,
                    bytes(32),
                    f"C{i % 17:06d}",
                    f"T{i % 23:06d}",
                    feats.get("amount_minor") or 1_000,
                    datetime(2025, 5, 21, 12, tzinfo=UTC) + timedelta(minutes=i),
                    decided - timedelta(milliseconds=6),
                    decided,
                    score,
                    action,
                    codes,
                    Jsonb(feats),
                    None if score is None else MODEL,
                    POLICY,
                    REGISTRY,
                ),
            )
            db.execute(
                "INSERT INTO outbox (event_id, aggregate_id, event_type, schema_version, payload, "
                "stream, published_at) VALUES (%s, %s, 'decision.made', 1, '{}', %s, %s)",
                (uuid.uuid4(), txn, NAMESPACE, decided + timedelta(milliseconds=20)),
            )
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--admin-url", required=True, help="server URL used to create the database")
    args = parser.parse_args()
    parts = urlsplit(args.admin_url)
    if parts.scheme not in {"postgresql", "postgres"}:
        raise SystemExit("--admin-url must be a postgresql:// URL")
    smoke_url = urlunsplit(parts._replace(path=f"/{DB_NAME}"))
    assert_disposable_database(smoke_url, app_database_url())  # refuses before anything changes
    if not (ROOT / "dashboard/dist/index.html").exists():
        raise SystemExit("build the dashboard first: cd dashboard && npm ci && npm run build")

    stage(f"creating disposable database {DB_NAME}")
    shutil.rmtree(EVIDENCE, ignore_errors=True)
    EVIDENCE.mkdir(parents=True)
    with psycopg.connect(args.admin_url, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DB_NAME}")
        admin.execute(f"CREATE DATABASE {DB_NAME}")
    state = Path(tempfile.mkdtemp(prefix="fraud-smoke-"))
    console: subprocess.Popen[bytes] | None = None
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        config.cmd_opts = type("Opts", (), {"x": [f"url={smoke_url}"]})()
        stage("migrating and seeding")
        command.upgrade(config, "head")
        counts = seed(smoke_url)
        (state / "deployment.json").write_text(
            json.dumps(
                {
                    "namespace": NAMESPACE,
                    "id_prefix": "S-",
                    "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "requests": 0,
                    "cutoff_day": 140,
                }
            )
        )
        user = "smoke-admin"
        password = secrets.token_urlsafe(24)  # never printed; only in the browser env
        Accounts(state / "analysts.json").add(user, password, "admin")
        port = free_port()
        env = {
            **os.environ,
            "FRAUD_DATABASE_URL": smoke_url,
            "FRAUD_API_KEY": secrets.token_urlsafe(24),
            "FRAUD_CONSOLE_STATE_DIR": str(state),
            "FRAUD_CONSOLE_API_PORT": str(free_port()),  # nothing listens: API "unavailable"
            "FRAUD_CONSOLE_MODEL_URI": "",  # no registry
        }
        stage("starting the console")
        log = (EVIDENCE / "console.log").open("wb")
        console = subprocess.Popen(  # noqa: S603 - fixed argv
            [sys.executable, "-m", "uvicorn", "fraudplat.console.app:create_console", "--factory",
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )  # fmt: skip
        stage("waiting for the console to answer (up to 90 s)")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if console.poll() is not None:
                raise SystemExit("console exited at startup; see run/smoke-evidence/console.log")
            try:
                with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            raise SystemExit("console did not answer; see run/smoke-evidence/console.log")
        print(f"seeded {sum(counts.values())} decisions {counts}; console on port {port}")
        browser_env = {
            **os.environ,
            "CONSOLE_URL": f"http://127.0.0.1:{port}",
            "E2E_USER": user,
            "E2E_PASSWORD": password,
            "EVIDENCE_DIR": str(EVIDENCE),
        }
        stage("running the browser checks")
        result = subprocess.run(
            ["node", "e2e/smoke.mjs"],  # noqa: S607
            cwd=ROOT / "dashboard",
            env=browser_env,
            check=False,
        )
        return result.returncode
    finally:
        if console is not None:
            console.terminate()
            try:
                console.wait(10)
            except subprocess.TimeoutExpired:
                console.kill()
        shutil.rmtree(state, ignore_errors=True)
        with psycopg.connect(args.admin_url, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {DB_NAME} WITH (FORCE)")


if __name__ == "__main__":
    raise SystemExit(main())
