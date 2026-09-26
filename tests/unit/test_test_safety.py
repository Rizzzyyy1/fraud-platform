"""Integration-test guards refuse unsafe targets before any destructive action runs."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from ..safety import (
    OWNERSHIP_LABEL,
    OWNERSHIP_VALUE,
    TEST_COMPOSE_PROJECT,
    OutageCleanupError,
    UnsafeTestTarget,
    assert_disposable_database,
    assert_disposable_kafka,
    assert_disposable_redis,
    assert_outage_container,
    recover_redis,
    redis_outage,
    reset_database,
    verify_outage_target,
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


# --- Redis outage helper: verify before pausing, clean up by recorded ID ---------------------

TEST_URL, APP_URL = "redis://127.0.0.1:6480/15", "redis://localhost:6420/0"
REDIS_ID = "6f72865cdba4" + "0" * 52


class FakeDocker:
    """Docker's container API as the outage helper sees it, including Engine 29.x behaviour:
    a paused container's live port map (`NetworkSettings.Ports`) is empty and `docker port`
    prints nothing, while its configured `HostConfig.PortBindings` are kept."""

    def __init__(self) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []
        self.fail_unpause = False

    def add(self, cid: str, name: str, labels: dict[str, str], host_port: str) -> None:
        bind = {"6379/tcp": [{"HostIp": "127.0.0.1", "HostPort": host_port}]}
        self.containers[cid] = {
            "Id": cid,
            "Name": f"/{name}",
            "Config": {"Labels": dict(labels)},
            "State": {"Status": "running", "Paused": False},
            "NetworkSettings": {"Ports": bind},
            "HostConfig": {"PortBindings": bind},
        }

    def _find(self, ref: str) -> dict[str, Any] | None:
        for info in self.containers.values():
            if ref in (info["Id"], info["Name"].lstrip("/")):
                return info
        return None

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        ref, info = args[-1], self._find(args[-1])
        if info is None:
            return subprocess.CompletedProcess(args, 1, "", f"No such container: {ref}")
        if args[0] == "inspect":
            return subprocess.CompletedProcess(args, 0, json.dumps([info]), "")
        if args[0] == "port":
            ports = info["NetworkSettings"]["Ports"].get("6379/tcp") or []
            out = "".join(f"{p['HostIp']}:{p['HostPort']}\n" for p in ports)
            return subprocess.CompletedProcess(args, 0, out, "")
        if args[0] == "pause":
            info["State"] = {"Status": "paused", "Paused": True}
            info["NetworkSettings"]["Ports"] = {}  # what the real engine reports while paused
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "unpause":
            if self.fail_unpause:
                return subprocess.CompletedProcess(args, 1, "", "daemon error")
            info["State"] = {"Status": "running", "Paused": False}
            info["NetworkSettings"]["Ports"] = info["HostConfig"]["PortBindings"]
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected docker call {args}")

    def paused(self, cid: str = REDIS_ID) -> bool:
        return bool(self.containers[cid]["State"]["Paused"])

    def actions(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] in {"pause", "unpause"}]


OWNED = {OWNERSHIP_LABEL: OWNERSHIP_VALUE}
COMPOSE_TEST = {
    **OWNED,
    "com.docker.compose.project": TEST_COMPOSE_PROJECT,
    "com.docker.compose.service": "redis",
}


def fake_test_redis() -> FakeDocker:
    fake = FakeDocker()
    fake.add(REDIS_ID, "fraud-itest-redis-1", COMPOSE_TEST, "6480")
    return fake


def test_cleanup_works_when_port_information_disappears_while_paused() -> None:
    fake = fake_test_redis()
    target = verify_outage_target("fraud-itest-redis-1", TEST_URL, APP_URL, fake)
    assert target.container_id == REDIS_ID and target.test_port == 6480
    with redis_outage(target, fake):
        assert fake.paused()
        assert fake(["port", REDIS_ID, "6379/tcp"]).stdout == ""  # the regression's trigger
        # The old helper re-derived ownership from `docker port` here and refused to unpause.
        with pytest.raises(UnsafeTestTarget):
            assert_outage_container(REDIS_ID, "", TEST_URL, APP_URL)
    assert not fake.paused()
    assert fake.actions() == [["pause", REDIS_ID], ["unpause", REDIS_ID]]  # the exact ID only
    helper_calls = [c for c in fake.calls if c != ["port", REDIS_ID, "6379/tcp"]]
    assert {c[0] for c in helper_calls} == {"inspect", "pause", "unpause"}  # never `docker port`


def test_exception_during_outage_still_unpauses_and_propagates() -> None:
    fake = fake_test_redis()
    target = verify_outage_target(REDIS_ID, TEST_URL, APP_URL, fake)
    with pytest.raises(ZeroDivisionError), redis_outage(target, fake):
        _ = 1 / 0
    assert not fake.paused()


def test_failed_assertion_and_failed_cleanup_keep_the_original_failure_visible(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = fake_test_redis()
    target = verify_outage_target(REDIS_ID, TEST_URL, APP_URL, fake)
    fake.fail_unpause = True
    with pytest.raises(AssertionError, match="original failure") as caught:
        with redis_outage(target, fake):
            raise AssertionError("original failure")
    notes = getattr(caught.value, "__notes__", [])
    assert any("cleanup also failed" in n and "make test-redis-recover" in n for n in notes)
    assert "cleanup also failed" in capsys.readouterr().err
    assert fake.paused()  # reported, not hidden


def test_cleanup_failure_alone_is_raised() -> None:
    fake = fake_test_redis()
    target = verify_outage_target(REDIS_ID, TEST_URL, APP_URL, fake)
    fake.fail_unpause = True
    with pytest.raises(OutageCleanupError, match="docker unpause"), redis_outage(target, fake):
        pass


APP_COMPOSE = {
    "com.docker.compose.project": "fraud-platform",
    "com.docker.compose.service": "redis",
}


@pytest.mark.parametrize(
    ("labels", "host_port", "reason"),
    [
        (APP_COMPOSE, "6420", "application Compose container, no ownership label"),
        ({**APP_COMPOSE, **OWNED}, "6480", "ownership label copied onto an application container"),
        ({"com.docker.compose.project": "fraud-review"}, "6480", "other project, test port"),
        ({}, "6380", "unlabelled container on the CI test port (unrelated service)"),
        ({OWNERSHIP_LABEL: "something-else"}, "6480", "wrong ownership value"),
        (COMPOSE_TEST, "6420", "owned, but publishes the application's Redis port"),
        (OWNED, "7000", "owned, but not on the test Redis port"),
    ],
)
def test_refuses_to_pause_unowned_or_application_containers(
    labels: dict[str, str], host_port: str, reason: str
) -> None:
    fake = FakeDocker()
    fake.add("a" * 64, "some-redis", labels, host_port)
    with pytest.raises(UnsafeTestTarget):
        verify_outage_target("some-redis", TEST_URL, APP_URL, fake)
    assert fake.actions() == [], reason


def test_ci_service_container_with_the_ownership_label_is_supported() -> None:
    """GitHub Actions service containers have no Compose labels; the CI workflow sets the
    ownership label through `options`, and the job passes `job.services.redis.id`."""
    ci_id = "d" * 64
    fake = FakeDocker()
    fake.add(ci_id, "redis-service", OWNED, "6380")
    ci_url = "redis://127.0.0.1:6380/15"
    target = verify_outage_target(ci_id, ci_url, None, fake)  # CI: no application Redis
    assert target.container_id == ci_id and target.provisioner.startswith("unmanaged")
    with pytest.raises(AssertionError, match="test failed"), redis_outage(target, fake):
        raise AssertionError("test failed")
    assert not fake.paused(ci_id)
    assert fake.actions() == [["pause", ci_id], ["unpause", ci_id]]


def _service_block(text: str, header: str, next_header: str) -> str:
    start = text.index(header)
    return text[start : text.index(next_header, start)]


def test_ownership_label_is_set_only_by_the_disposable_test_provisioners() -> None:
    root = Path(__file__).resolve().parents[2]
    ci = (root / ".github/workflows/ci.yml").read_text()
    ci_redis = _service_block(ci, "\n      redis:\n", "\n      kafka:\n")
    assert f"--label {OWNERSHIP_LABEL}={OWNERSHIP_VALUE}" in ci_redis
    assert "FRAUD_TEST_REDIS_CONTAINER: ${{ job.services.redis.id }}" in ci
    compose = (root / "docker-compose.test.yml").read_text()
    test_redis = _service_block(compose, "\n  redis:\n", "\n  kafka:\n")
    assert f"{OWNERSHIP_LABEL}: {OWNERSHIP_VALUE}" in test_redis
    assert OWNERSHIP_LABEL not in (root / "docker-compose.yml").read_text()  # the application's


def test_refuses_an_already_paused_or_missing_container() -> None:
    fake = fake_test_redis()
    fake(["pause", REDIS_ID])
    fake.calls.clear()
    with pytest.raises(UnsafeTestTarget, match="test-redis-recover"):
        verify_outage_target(REDIS_ID, TEST_URL, APP_URL, fake)
    with pytest.raises(UnsafeTestTarget):
        verify_outage_target("no-such-container", TEST_URL, APP_URL, fake)
    with pytest.raises(UnsafeTestTarget):
        verify_outage_target(None, TEST_URL, APP_URL, fake)
    assert fake.actions() == []


def test_cleanup_never_unpauses_a_different_container() -> None:
    fake = fake_test_redis()
    target = verify_outage_target(REDIS_ID, TEST_URL, APP_URL, fake)
    with pytest.raises(OutageCleanupError, match="refused"), redis_outage(target, fake):
        # The verified container disappears and an unrelated one takes its name.
        del fake.containers[REDIS_ID]
        fake.add("b" * 64, "fraud-itest-redis-1", APP_COMPOSE, "6420")
    assert fake.actions() == [["pause", REDIS_ID]]


def test_recovery_unpauses_only_the_owned_test_redis() -> None:
    fake = fake_test_redis()
    fake(["pause", REDIS_ID])  # left paused by a killed run
    assert "unpaused" in recover_redis(REDIS_ID, TEST_URL, APP_URL, fake)
    assert not fake.paused()
    assert "was not paused" in recover_redis(REDIS_ID, TEST_URL, APP_URL, fake)
    app = FakeDocker()
    app.add("c" * 64, "fraud-redis-1", APP_COMPOSE, "6420")
    app(["pause", "c" * 64])
    with pytest.raises(UnsafeTestTarget):
        recover_redis("fraud-redis-1", TEST_URL, APP_URL, app)
    assert app.paused("c" * 64)


def test_makefile_test_project_matches_the_guard() -> None:
    makefile = Path(__file__).resolve().parents[2] / "Makefile"
    assert f"docker compose -p {TEST_COMPOSE_PROJECT} " in makefile.read_text()
