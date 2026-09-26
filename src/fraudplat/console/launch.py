"""Start and supervise the local dashboard deployment.

    python -m fraudplat.console.launch            # start (bootstraps on first run)
    python -m fraudplat.console.launch --reset    # new deployment namespace, then start

First run: creates `replay:dashboard-<ts>`, its Kafka topics, the demo request list, and the
Redis feature state bootstrapped from history available before day 140. Later runs reuse it;
if its Redis state has gone (e.g. the Redis volume was removed) the launcher refuses to start
rather than scoring on empty history, and `--reset` creates a new deployment. Earlier
deployments' decisions and reviews stay in PostgreSQL (the audit trail is not deleted); their
Redis keys, topics and consumer group are removed on reset.

Processes (children of this launcher, logs in `run/dashboard/logs/`):
  api        scoring API on 127.0.0.1:8110 (FRAUD_CONSOLE_API_PORT), model from
             `models:/fraud-risk-f1@production` resolved once at startup
  publisher  outbox → Kafka for the deployment namespace
  worker     Kafka → Redis feature state
  console    analyst console on 127.0.0.1:8200 (FRAUD_CONSOLE_PORT; serves the built dashboard)
A child that exits unexpectedly is restarted (at most 5 times in 5 minutes), except the worker
while a failure drill holds it (see `console/jobs.py`). Ctrl-C stops everything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from redis.asyncio import Redis

from fraudplat.config import Settings
from fraudplat.console.deployment import (
    STATE_DIR,
    Deployment,
    demo_requests,
    load_deployment,
    new_deployment,
    pre_test_history,
    save_deployment,
)
from fraudplat.console.jobs import read_hold
from fraudplat.demos.stack import purge_namespace
from fraudplat.features.redis_state import RedisFeatureState, bootstrap_redis
from fraudplat.features.spec import DAY, RETENTION_HORIZON
from fraudplat.pipeline.topics import delete_consumer_group, delete_topics, ensure_topics

API_PORT = int(os.environ.get("FRAUD_CONSOLE_API_PORT", "8110"))
CONSOLE_PORT = int(os.environ.get("FRAUD_CONSOLE_PORT", "8200"))
MODEL_URI = "models:/fraud-risk-f1@production"
BOOTSTRAP_MARKER = "console:bootstrapped"


async def bootstrap(settings: Settings, state_dir: Path) -> Deployment:
    txns, pre_test, cutoff = pre_test_history()
    deployment = new_deployment(0)
    requests = demo_requests(txns, pre_test, cutoff, deployment.id_prefix)
    deployment = Deployment(**{**deployment.__dict__, "requests": len(requests)})
    ensure_topics(settings.kafka_bootstrap, deployment.stream)
    assert settings.redis_url is not None
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    try:
        started = time.perf_counter()
        await bootstrap_redis(
            RedisFeatureState(redis, deployment.namespace),
            txns,
            cutoff,
            history_start_us=cutoff - RETENTION_HORIZON - DAY,
        )
        await redis.set(f"{deployment.namespace}:{BOOTSTRAP_MARKER}", deployment.created_at)
        print(f"bootstrapped {deployment.namespace} in {time.perf_counter() - started:.1f}s")
    finally:
        await redis.aclose()
    write_state(state_dir, deployment, requests)
    return deployment


def write_state(state_dir: Path, deployment: Deployment, requests: list[dict[str, Any]]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "requests.json").write_text(json.dumps(requests))
    save_deployment(deployment, state_dir)


async def retire(settings: Settings, old: Deployment) -> None:
    """Remove an old deployment's Redis keys, topics and consumer group (rows are kept)."""
    assert settings.redis_url is not None
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    try:
        removed = await purge_namespace(redis, old.namespace)
    finally:
        await redis.aclose()
    delete_topics(settings.kafka_bootstrap, old.stream)
    delete_consumer_group(settings.kafka_bootstrap, old.stream)
    print(f"retired {old.namespace}: {removed} Redis keys, topics and consumer group removed")


async def bootstrapped(settings: Settings, deployment: Deployment) -> bool:
    assert settings.redis_url is not None
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    try:
        return bool(await redis.exists(f"{deployment.namespace}:{BOOTSTRAP_MARKER}"))
    finally:
        await redis.aclose()


class Supervisor:
    def __init__(self, deployment: Deployment, state_dir: Path) -> None:
        self.deployment = deployment
        self.state_dir = state_dir
        self.logs = state_dir / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, subprocess.Popen[bytes]] = {}
        self.restarts: dict[str, list[float]] = {}
        self.stopping = False

    def env(self) -> dict[str, str]:
        env = {
            **os.environ,
            "FRAUD_FEATURE_NAMESPACE": self.deployment.namespace,
            "FRAUD_MODEL_URI": MODEL_URI,
            "FRAUD_CONSOLE_API_PORT": str(API_PORT),
            "FRAUD_CONSOLE_MODEL_URI": MODEL_URI,
            "MLFLOW_DISABLE_AGENT_HINT": "1",
        }
        env.pop("FRAUD_MODEL_PATH", None)
        env.pop("FRAUD_POLICY_PATH", None)
        return env

    def argv(self, name: str) -> list[str]:
        ns = self.deployment.namespace
        uvicorn = ["-m", "uvicorn", "--factory", "--host", "127.0.0.1", "--log-level", "warning"]
        return [sys.executable] + {
            "api": [*uvicorn, "fraudplat.api.app:create_app", "--port", str(API_PORT),
                    "--no-access-log"],
            "publisher": ["-m", "fraudplat.pipeline.publisher", "--namespace", ns,
                          "--metrics-port", "0"],
            "worker": ["-m", "fraudplat.pipeline.worker", "--namespace", ns, "--metrics-port", "0"],
            "console": [*uvicorn, "fraudplat.console.app:create_console", "--port",
                        str(CONSOLE_PORT)],
        }[name]  # fmt: skip

    def spawn(self, name: str) -> None:
        log = (self.logs / f"{name}.log").open("ab")
        log.write(f"\n--- start {time.strftime('%Y-%m-%dT%H:%M:%S')} ---\n".encode())
        log.flush()
        self.procs[name] = subprocess.Popen(  # noqa: S603 - fixed argv
            self.argv(name), env=self.env(), stdout=log, stderr=subprocess.STDOUT
        )
        self.write_pids()

    def write_pids(self) -> None:
        pids = {n: p.pid for n, p in self.procs.items() if p.poll() is None}
        (self.state_dir / "pids.json").write_text(json.dumps(pids) + "\n")

    def tick(self) -> None:
        for name, proc in list(self.procs.items()):
            code = proc.poll()
            if code is None:
                continue
            if name == "worker" and read_hold(self.state_dir) is not None:
                self.write_pids()
                continue  # stopped by a failure drill; restarted once the hold is released
            now = time.monotonic()
            recent = [t for t in self.restarts.get(name, []) if now - t < 300]
            if len(recent) >= 5:
                print(f"{name} exited ({code}) 5 times in 5 minutes; not restarting")
                del self.procs[name]
                self.write_pids()
                continue
            self.restarts[name] = [*recent, now]
            print(f"{name} exited with {code}; restarting")
            self.spawn(name)

    def stop_all(self) -> None:
        self.stopping = True
        for proc in self.procs.values():
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in self.procs.values():
            try:
                proc.wait(15)
            except subprocess.TimeoutExpired:
                proc.kill()
        (self.state_dir / "pids.json").unlink(missing_ok=True)


def _interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reset", action="store_true", help="create a new deployment namespace")
    parser.add_argument("--state-dir", type=Path, default=STATE_DIR)
    args = parser.parse_args()
    settings = Settings()
    deployment = load_deployment(args.state_dir)
    if deployment is not None and args.reset:
        asyncio.run(retire(settings, deployment))
        deployment = None
    if deployment is None:
        deployment = asyncio.run(bootstrap(settings, args.state_dir))
    elif not asyncio.run(bootstrapped(settings, deployment)):
        raise SystemExit(
            f"Redis state for {deployment.namespace} is missing; run `make dashboard-reset`"
        )
    if not Path("dashboard/dist/index.html").exists():
        print("note: dashboard/dist is not built; run `make dashboard-build` for the UI")
    sup = Supervisor(deployment, args.state_dir)
    for name in ("api", "publisher", "worker", "console"):
        sup.spawn(name)
    print(
        f"namespace {deployment.namespace}\n"
        f"scoring API  http://127.0.0.1:{API_PORT}  (model {MODEL_URI}, resolved at startup)\n"
        f"console      http://127.0.0.1:{CONSOLE_PORT}\n"
        "Ctrl-C stops all processes."
    )
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        while True:
            time.sleep(1)
            sup.tick()
    except KeyboardInterrupt:
        pass
    finally:
        sup.stop_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
