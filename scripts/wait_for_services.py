"""Wait until PostgreSQL, Redis and Kafka accept requests, or fail after a deadline.

Used by CI before the integration tests (and usable locally). Reads FRAUD_TEST_DATABASE_URL,
FRAUD_TEST_REDIS_URL and FRAUD_TEST_KAFKA_BOOTSTRAP from the environment.

Usage: python scripts/wait_for_services.py --timeout 180
"""

from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable

import psycopg
import redis
from confluent_kafka.admin import AdminClient


def postgres() -> None:
    with psycopg.connect(os.environ["FRAUD_TEST_DATABASE_URL"], connect_timeout=3) as conn:
        conn.execute("SELECT 1")


def redis_ping() -> None:
    redis.Redis.from_url(os.environ["FRAUD_TEST_REDIS_URL"], socket_timeout=3).ping()


def kafka() -> None:
    admin = AdminClient({"bootstrap.servers": os.environ["FRAUD_TEST_KAFKA_BOOTSTRAP"]})
    metadata = admin.list_topics(timeout=5)
    if not metadata.brokers:
        raise RuntimeError("no brokers in metadata")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()
    deadline = time.monotonic() + args.timeout
    checks: dict[str, Callable[[], None]] = {
        "postgres": postgres,
        "redis": redis_ping,
        "kafka": kafka,
    }
    for name, check in checks.items():
        started = time.monotonic()
        while True:
            try:
                check()
                print(f"{name}: ready after {time.monotonic() - started:.1f}s")
                break
            except Exception as exc:
                if time.monotonic() > deadline:
                    print(f"{name}: not ready before the {args.timeout:.0f}s deadline: {exc}")
                    return 1
                time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
