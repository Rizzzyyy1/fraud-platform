"""Integration fixtures: disposable PostgreSQL, Redis and Kafka test services.

Every destructive step (migrating down, truncating, flushing, creating or deleting topics, pausing
a container) is preceded by a guard from `tests/safety.py` that rejects the application's
services. Run with isolated services: `make test-integration` (docker-compose.test.yml).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis

from fraudplat.api.app import create_app
from fraudplat.config import Settings

from ..safety import (
    assert_disposable_kafka,
    assert_disposable_redis,
    reset_database,
)

ROOT = Path(__file__).resolve().parents[2]
API_KEY = "integration-test-key"


def _setting(name: str) -> str:
    url = os.environ.get(name)
    if url is None:
        env_file = ROOT / ".env"
        for line in env_file.read_text().splitlines() if env_file.exists() else []:
            if line.startswith(f"{name}="):
                url = line.split("=", 1)[1].strip()
    if not url:
        pytest.fail(f"{name} is not set (run `make env`)")
    return url


def _optional_setting(name: str) -> str | None:
    """The application's own setting (env or .env), used only to refuse it as a test target."""
    value = os.environ.get(name)
    if value is None:
        env_file = ROOT / ".env"
        for line in env_file.read_text().splitlines() if env_file.exists() else []:
            if line.startswith(f"{name}="):
                value = line.split("=", 1)[1].strip()
    return value or None


def _test_database_url() -> str:
    return _setting("FRAUD_TEST_DATABASE_URL")


def redis_test_url() -> str:
    url = _setting("FRAUD_TEST_REDIS_URL")
    assert_disposable_redis(url, _optional_setting("FRAUD_REDIS_URL"))
    return url


def kafka_test_bootstrap() -> str:
    bootstrap = _setting("FRAUD_TEST_KAFKA_BOOTSTRAP")
    assert_disposable_kafka(bootstrap, _optional_setting("FRAUD_KAFKA_BOOTSTRAP"))
    return bootstrap


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """Redis database 15, flushed before each test. Never the live database."""
    url = redis_test_url()  # guard runs before the flush below
    client = Redis.from_url(url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = _test_database_url()

    def reset(target: str) -> None:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "migrations"))
        config.cmd_opts = type("Opts", (), {"x": [f"url={target}"]})()
        command.downgrade(config, "base")
        command.upgrade(config, "head")

    reset_database(url, _optional_setting("FRAUD_DATABASE_URL"), reset)
    yield url


@pytest.fixture
def db(database_url: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("TRUNCATE decisions, outbox, reviews, console_jobs")
        yield conn


def make_settings(database_url: str, **overrides: object) -> Settings:
    """Settings pointing only at test services, whatever `.env` says for the application."""
    overrides.setdefault("redis_url", SecretStr(redis_test_url()))
    overrides.setdefault("kafka_bootstrap", kafka_test_bootstrap())
    return Settings(
        database_url=SecretStr(database_url),
        api_key=SecretStr(API_KEY),
        **overrides,  # type: ignore[arg-type]
    )


@pytest.fixture
async def app(
    database_url: str, db: psycopg.Connection[tuple[object, ...]]
) -> AsyncIterator[FastAPI]:
    application = create_app(make_settings(database_url))
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers={"X-API-Key": API_KEY}
    ) as http:
        yield http
