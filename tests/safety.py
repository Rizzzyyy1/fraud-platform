"""Guards that run before any destructive integration-test action.

Integration fixtures migrate the test database down and up, truncate tables, flush a Redis
database, create and delete Kafka topics, and pause a Redis container. Each of those is allowed
only against an explicitly designated, disposable test target that is not the application's:

* PostgreSQL: the database name must end in `_test` and must not be a reserved name; it must not
  be the application's database *or live on the application's server* (`FRAUD_DATABASE_URL`) —
  integration tests run on isolated services (`docker-compose.test.yml`).
* Redis: database 15 only, on a server other than the application's (`FRAUD_REDIS_URL`).
* Kafka: a bootstrap address other than the application's (`FRAUD_KAFKA_BOOTSTRAP`).
* Outage tests: the container must be named explicitly (`FRAUD_TEST_REDIS_CONTAINER`) and must
  publish Redis on the test Redis port, which must not be the application's port.
Every check raises `UnsafeTestTarget` before anything is changed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from psycopg.conninfo import conninfo_to_dict

RESERVED_DATABASES = frozenset({"fraud", "postgres", "mlflow", "template0", "template1"})
_LOOPBACK = {"localhost", "127.0.0.1", "::1", ""}


class UnsafeTestTarget(RuntimeError):
    pass


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


def reset_database(test_url: str, app_url: str | None, reset: Callable[[str], None]) -> None:
    """Run `reset` (downgrade + upgrade) only after the target passed the guard."""
    assert_disposable_database(test_url, app_url)
    reset(test_url)
