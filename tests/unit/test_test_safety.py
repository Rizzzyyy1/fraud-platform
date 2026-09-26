"""Integration-test guards refuse unsafe targets before any destructive action runs."""

from __future__ import annotations

import pytest

from ..safety import (
    UnsafeTestTarget,
    assert_disposable_database,
    assert_disposable_kafka,
    assert_disposable_redis,
    assert_outage_container,
    reset_database,
)

APP_DB = "postgresql://fraud:pw@localhost:5433/fraud"
ISOLATED_DB = "postgresql://fraud:pw@127.0.0.1:5543/fraud_test"


@pytest.mark.parametrize(
    "url",
    [
        APP_DB,  # the application's database
        "postgresql://fraud:pw@127.0.0.1:5433/fraud",  # same, spelled differently
        "postgresql://fraud:pw@localhost:5433/fraud_test",  # a test db on the app's server
        "postgresql://fraud:pw@127.0.0.1:5543/fraud",  # reserved name elsewhere
        "postgresql://fraud:pw@127.0.0.1:5543/postgres",
        "postgresql://fraud:pw@127.0.0.1:5543/analytics",  # not designated *_test
    ],
)
def test_unsafe_databases_are_rejected_before_reset(url: str) -> None:
    calls: list[str] = []
    with pytest.raises(UnsafeTestTarget):
        reset_database(url, APP_DB, calls.append)
    assert calls == []  # the downgrade/upgrade never ran


def test_isolated_test_database_is_reset() -> None:
    calls: list[str] = []
    reset_database(ISOLATED_DB, APP_DB, calls.append)
    assert calls == [ISOLATED_DB]
    assert_disposable_database(ISOLATED_DB, None)  # CI: no application database configured


@pytest.mark.parametrize(
    ("test_url", "app_url"),
    [
        ("redis://localhost:6380/15", "redis://localhost:6380/0"),  # app server, other db
        ("redis://127.0.0.1:6480/0", None),  # not database 15
    ],
)
def test_unsafe_redis_is_rejected(test_url: str, app_url: str | None) -> None:
    with pytest.raises(UnsafeTestTarget):
        assert_disposable_redis(test_url, app_url)


def test_isolated_redis_and_kafka_are_accepted_and_shared_kafka_rejected() -> None:
    assert_disposable_redis("redis://127.0.0.1:6480/15", "redis://localhost:6380/0")
    assert_disposable_kafka("127.0.0.1:9394", "127.0.0.1:9094")
    with pytest.raises(UnsafeTestTarget):
        assert_disposable_kafka("localhost:9094", "127.0.0.1:9094")


@pytest.mark.parametrize(
    ("container", "published"),
    [
        (None, ""),  # not named explicitly
        ("", ""),
        ("abc123", "127.0.0.1:6380\n"),  # publishes the application's Redis port
        ("abc123", "127.0.0.1:7000\n"),  # not the test Redis
        ("abc123", ""),  # no such container / not published
    ],
)
def test_outage_container_must_be_the_explicit_test_redis(
    container: str | None, published: str
) -> None:
    with pytest.raises(UnsafeTestTarget):
        assert_outage_container(
            container, published, "redis://127.0.0.1:6480/15", "redis://localhost:6380/0"
        )


def test_outage_container_for_isolated_redis_is_accepted() -> None:
    assert_outage_container(
        "abc123", "127.0.0.1:6480\n[::1]:6480\n", "redis://127.0.0.1:6480/15", "redis://x:6380/0"
    )
