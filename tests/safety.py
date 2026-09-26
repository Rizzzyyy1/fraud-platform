"""Guards that run before any destructive integration-test action.

Integration fixtures migrate the test database down and up, truncate tables, flush a Redis
database, create and delete Kafka topics, and pause a Redis container. Each of those is allowed
only against an explicitly designated, disposable test target that is not the application's:

* PostgreSQL: the database name must end in `_test` and must not be a reserved name; it must not
  be the application's database *or live on the application's server* (`FRAUD_DATABASE_URL`) —
  integration tests run on isolated services (`docker-compose.test.yml`).
* Redis: database 15 only, on a server other than the application's (`FRAUD_REDIS_URL`).
* Kafka: a bootstrap address other than the application's (`FRAUD_KAFKA_BOOTSTRAP`).
* Outage tests: the container must be named explicitly (`FRAUD_TEST_REDIS_CONTAINER`), carry the
  ownership label `fraudplat.test-resource=disposable-redis`, be running, and publish Redis on the
  test Redis port, which must not be the application's port. The label is the ownership contract.
  Only the disposable test provisioners set it: `docker-compose.test.yml` (project `fraud-itest`)
  and the CI workflow's Redis service container. The application's Compose file must never set
  it, and a unit test enforces all three. A container that also carries a Compose project label
  must belong to `fraud-itest`. Its immutable container ID is
  recorded at that point, and the pause and the cleanup act on that exact ID only. Cleanup does not
  re-derive ownership from `docker port`, which reports nothing for a paused container on some
  Docker Engine versions.
Every check raises `UnsafeTestTarget` before anything is changed.

Cleanup runs in a `finally` clause, so it is attempted after assertion failures, exceptions and
cancellation. It cannot run if the test process is killed (SIGKILL, a crashed interpreter, a lost
machine). In that case the disposable Redis can be left paused; recover it with
`make test-redis-recover`, which unpauses only a container that passes the same ownership checks
(`python -m tests.safety recover-redis`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from psycopg.conninfo import conninfo_to_dict

TEST_COMPOSE_PROJECT = "fraud-itest"  # the Makefile's ITEST project; a unit test keeps them equal
# Ownership contract for containers the outage tests may pause (set by docker-compose.test.yml and
# the CI Redis service; never by docker-compose.yml). Unit tests check all three files.
OWNERSHIP_LABEL = "fraudplat.test-resource"
OWNERSHIP_VALUE = "disposable-redis"
RESERVED_DATABASES = frozenset({"fraud", "postgres", "mlflow", "template0", "template1"})
_LOOPBACK = {"localhost", "127.0.0.1", "::1", ""}


class UnsafeTestTarget(RuntimeError):
    pass


class OutageCleanupError(RuntimeError):
    """The disposable Redis could not be confirmed unpaused after a simulated outage."""


@dataclass(frozen=True)
class Server:
    host: str
    port: int


def _host(host: str | None) -> str:
    host = (host or "").strip("[]").lower()
    return "127.0.0.1" if host in _LOOPBACK else host


def pg_target(url: str) -> tuple[Server, str]:
    info = conninfo_to_dict(url)
    return Server(_host(str(info.get("host") or "")), int(info.get("port") or 5432)), str(
        info.get("dbname") or ""
    )


def redis_target(url: str) -> tuple[Server, int]:
    parts = urlsplit(url)
    db = int((parts.path or "/0").lstrip("/") or 0)
    return Server(_host(parts.hostname), parts.port or 6379), db


def kafka_target(bootstrap: str) -> set[Server]:
    servers = set()
    for item in bootstrap.split(","):
        host, _, port = item.strip().rpartition(":")
        servers.add(Server(_host(host), int(port)))
    return servers


def assert_disposable_database(test_url: str, app_url: str | None) -> None:
    server, name = pg_target(test_url)
    if not name.endswith("_test") or name in RESERVED_DATABASES:
        raise UnsafeTestTarget(f"test database must be a designated *_test database, got {name!r}")
    if app_url:
        app_server, app_name = pg_target(app_url)
        if (server, name) == (app_server, app_name):
            raise UnsafeTestTarget("the test database is the application's database")
        if server == app_server:
            raise UnsafeTestTarget(
                "the test database is on the application's PostgreSQL server; run integration "
                "tests on isolated services (make test-integration)"
            )


def assert_disposable_redis(test_url: str, app_url: str | None) -> None:
    server, db = redis_target(test_url)
    if db != 15:
        raise UnsafeTestTarget("test Redis must use database 15")
    if app_url and redis_target(app_url)[0] == server:
        raise UnsafeTestTarget(
            "test Redis is the application's Redis server; run integration tests on isolated "
            "services (make test-integration)"
        )


def assert_disposable_kafka(test_bootstrap: str, app_bootstrap: str | None) -> None:
    if app_bootstrap and kafka_target(test_bootstrap) & kafka_target(app_bootstrap):
        raise UnsafeTestTarget("test Kafka is the application's broker; use isolated services")


def assert_outage_container(
    container: str | None, published: str, test_redis_url: str, app_redis_url: str | None
) -> None:
    """`published` is the output of `docker port <container> 6379/tcp`."""
    if not container:
        raise UnsafeTestTarget("set FRAUD_TEST_REDIS_CONTAINER to the disposable test Redis")
    test_server, _ = redis_target(test_redis_url)
    ports = {int(line.rsplit(":", 1)[1]) for line in published.split() if ":" in line}
    if test_server.port not in ports:
        raise UnsafeTestTarget(
            f"container {container!r} does not publish Redis on the test port {test_server.port}"
        )
    if app_redis_url and redis_target(app_redis_url)[0].port in ports:
        raise UnsafeTestTarget(f"container {container!r} publishes the application's Redis port")


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def docker(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run `docker <args>` with fixed arguments (no shell), capturing output."""
    return subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )


@dataclass(frozen=True)
class OutageTarget:
    """A container verified, while running, as the disposable test Redis."""

    container_id: str  # immutable full ID; every later action uses this, never the name
    name: str
    provisioner: str  # "compose project fraud-itest" or "unmanaged (e.g. CI service container)"
    test_port: int


def _inspect(ref: str, run: Runner) -> dict[str, Any]:
    found = run(["inspect", "--type", "container", ref])
    if found.returncode != 0:
        raise UnsafeTestTarget(f"cannot inspect container {ref!r}: {found.stderr.strip()}")
    info: dict[str, Any] = json.loads(found.stdout)[0]
    return info


def _published(ports: dict[str, Any] | None) -> str:
    """Docker's port map for 6379/tcp in `docker port` form ("host:port" per line)."""
    bindings = (ports or {}).get("6379/tcp") or []
    return "".join(f"{b.get('HostIp', '')}:{b.get('HostPort', '')}\n" for b in bindings)


def _assert_owned(ref: str, info: dict[str, Any]) -> str:
    """Check the ownership label; return how the container was provisioned."""
    labels = (info.get("Config") or {}).get("Labels") or {}
    if labels.get(OWNERSHIP_LABEL) != OWNERSHIP_VALUE:
        raise UnsafeTestTarget(
            f"container {ref!r} is not a disposable test Redis: label {OWNERSHIP_LABEL!r} is "
            f"{labels.get(OWNERSHIP_LABEL)!r}, expected {OWNERSHIP_VALUE!r}"
        )
    project = labels.get("com.docker.compose.project")
    if project is not None and project != TEST_COMPOSE_PROJECT:
        raise UnsafeTestTarget(
            f"container {ref!r} carries the test label but belongs to Compose project {project!r}"
            f", not {TEST_COMPOSE_PROJECT!r}"
        )
    return f"compose project {project}" if project else "unmanaged (e.g. CI service container)"


def verify_outage_target(
    container: str | None, test_redis_url: str, app_redis_url: str | None, run: Runner = docker
) -> OutageTarget:
    """Check the container before it is paused, and record its immutable ID."""
    if not container:
        raise UnsafeTestTarget("set FRAUD_TEST_REDIS_CONTAINER to the disposable test Redis")
    info = _inspect(container, run)
    provisioner = _assert_owned(container, info)
    state = info.get("State") or {}
    if state.get("Paused") or state.get("Status") != "running":
        raise UnsafeTestTarget(
            f"container {container!r} is not running (status {state.get('Status')!r}, paused "
            f"{state.get('Paused')!r}); if an earlier run was interrupted, run "
            "`make test-redis-recover`"
        )
    live = _published((info.get("NetworkSettings") or {}).get("Ports"))
    configured = _published((info.get("HostConfig") or {}).get("PortBindings"))
    assert_outage_container(container, live, test_redis_url, app_redis_url)
    assert_outage_container(container, configured, test_redis_url, app_redis_url)
    return OutageTarget(
        container_id=str(info["Id"]),
        name=str(info.get("Name", "")).lstrip("/"),
        provisioner=provisioner,
        test_port=redis_target(test_redis_url)[0].port,
    )


def _confirm_identity(target: OutageTarget, run: Runner) -> dict[str, Any]:
    """Re-inspect by the recorded ID (not the name) and re-check ownership labels."""
    info = _inspect(target.container_id, run)
    if info.get("Id") != target.container_id:
        raise UnsafeTestTarget(f"container ID changed: expected {target.container_id}")
    _assert_owned(target.container_id, info)
    return info


def restore(target: OutageTarget, run: Runner = docker) -> None:
    """Unpause the recorded container and confirm it; raise OutageCleanupError otherwise."""
    short = target.container_id[:12]
    try:
        if not (_confirm_identity(target, run).get("State") or {}).get("Paused"):
            return
        result = run(["unpause", target.container_id])
        if result.returncode != 0:
            raise OutageCleanupError(
                f"docker unpause {short} ({target.name}) failed: {result.stderr.strip()}"
            )
        if (_confirm_identity(target, run).get("State") or {}).get("Paused"):
            raise OutageCleanupError(f"container {short} ({target.name}) is still paused")
    except UnsafeTestTarget as refused:
        raise OutageCleanupError(f"cleanup of {short} refused: {refused}") from refused
    except OutageCleanupError as failed:
        raise OutageCleanupError(
            f"{failed}. The disposable test Redis may still be paused; recover it with "
            "`make test-redis-recover`."
        ) from failed


@contextmanager
def redis_outage(target: OutageTarget, run: Runner = docker) -> Iterator[None]:
    """Pause the verified test Redis for the duration of the block.

    Cleanup is attempted in `finally` whatever happens inside the block. If the block failed and
    cleanup also fails, the original exception propagates with the cleanup failure attached as a
    note (and printed to stderr); if only cleanup fails, `OutageCleanupError` is raised.
    """
    _confirm_identity(target, run)  # still the verified container; refuse before pausing
    pending: BaseException | None = None
    try:
        paused = run(["pause", target.container_id])
        if paused.returncode != 0:
            raise RuntimeError(f"docker pause {target.container_id[:12]} failed: {paused.stderr}")
        yield
    except BaseException as exc:
        pending = exc
        raise
    finally:
        try:
            restore(target, run)
        except OutageCleanupError as cleanup:
            if pending is None:
                raise
            message = f"Redis outage cleanup also failed: {cleanup}"
            pending.add_note(message)
            print(message, file=sys.stderr)


def recover_redis(
    container: str | None, test_redis_url: str, app_redis_url: str | None, run: Runner = docker
) -> str:
    """Unpause a disposable test Redis left paused by an interrupted run (owned containers only).

    Ownership is checked on the Compose labels and the configured port bindings, which Docker
    keeps while a container is paused (the live port map may be empty then).
    """
    if not container:
        raise UnsafeTestTarget("no disposable test Redis container found")
    info = _inspect(container, run)
    provisioner = _assert_owned(container, info)
    configured = _published((info.get("HostConfig") or {}).get("PortBindings"))
    assert_outage_container(container, configured, test_redis_url, app_redis_url)
    target = OutageTarget(
        str(info["Id"]), str(info.get("Name", "")).lstrip("/"), provisioner,
        redis_target(test_redis_url)[0].port,
    )  # fmt: skip
    was_paused = bool((info.get("State") or {}).get("Paused"))
    restore(target, run)
    outcome = "unpaused" if was_paused else "was not paused"
    return f"{target.name} ({target.container_id[:12]}): {outcome}"


def _app_setting(name: str) -> str | None:
    value = os.environ.get(name)
    env_file = Path(".env")
    if value is None and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith(f"{name}="):
                value = line.split("=", 1)[1].strip()
    return value or None


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["recover-redis"]:
        print("usage: python -m tests.safety recover-redis", file=sys.stderr)
        return 2
    test_url = os.environ.get("FRAUD_TEST_REDIS_URL", "redis://127.0.0.1:6480/15")
    assert_disposable_redis(test_url, _app_setting("FRAUD_REDIS_URL"))
    container = os.environ.get("FRAUD_TEST_REDIS_CONTAINER")
    print(recover_redis(container, test_url, _app_setting("FRAUD_REDIS_URL")))
    return 0


def reset_database(test_url: str, app_url: str | None, reset: Callable[[str], None]) -> None:
    """Run `reset` (downgrade + upgrade) only after the target passed the guard."""
    assert_disposable_database(test_url, app_url)
    reset(test_url)


if __name__ == "__main__":
    raise SystemExit(main())
