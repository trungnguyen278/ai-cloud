# SPDX-License-Identifier: MIT
"""What the tests share: a clean environment, the development settings, and PostgreSQL
and Redis in Docker (docs/decisions/0008-data-and-tokens.md, decision 5), as the
development Compose file runs them."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from docker.errors import DockerException
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from ai_cloud.__main__ import ENVIRONMENT_ENV
from ai_cloud.accounts.db.tables import tables as accounts_tables
from ai_cloud.entry.migrate import migrate
from ai_cloud.entry.settings import CloudSettings
from ai_cloud.shared.config import CONFIG_DIR_ENV, deep_merge, load_config
from ai_cloud.shared.database import DATABASE_URL_ENV, create_engine
from ai_cloud.shared.jose import SIGNING_KEYS_ENV, new_signing_key
from ai_cloud.shared.protocols import PLATFORM_DIR_ENV
from ai_cloud.shared.redis import REDIS_URL_ENV
from ai_cloud.shared.secrets import FILE_SUFFIX

COMPOSE = Path(__file__).resolve().parents[1] / "deploy" / "compose.development.yaml"
SECRETS = (DATABASE_URL_ENV, REDIS_URL_ENV, SIGNING_KEYS_ENV)

type MakeSettings = Callable[..., CloudSettings]


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own variables never change what the tests see."""
    names = (CONFIG_DIR_ENV, ENVIRONMENT_ENV, PLATFORM_DIR_ENV, *SECRETS)
    for name in (*names, *(secret + FILE_SUFFIX for secret in SECRETS)):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _every_record_made(caplog: pytest.LogCaptureFixture) -> None:
    """Every log record is made, whatever its level, so that a mistake in one shows in the
    tests: a field named as one of LogRecord's own, such as ``module``, raises."""
    caplog.set_level(logging.DEBUG)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _settings(**changes: Any) -> CloudSettings:
    return CloudSettings.from_config(deep_merge(load_config("development"), changes))


@pytest.fixture
def make_settings() -> MakeSettings:
    """The development environment's settings, with tables of changes merged in."""
    return _settings


@pytest.fixture
def jwk_set() -> Callable[[int], str]:
    """A JWK set of new signing keys."""
    return lambda count=1: json.dumps({"keys": [new_signing_key() for _ in range(count)]})


def _image(service: str) -> str:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return compose["services"][service]["image"]


def _start[C: (PostgresContainer, RedisContainer)](container: C) -> C:
    try:
        return container.start()
    except DockerException as error:
        pytest.fail(f"these tests need Docker, running (docs/decisions/0008): {error}")


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A PostgreSQL for the whole run, its tables migrated."""
    container = _start(PostgresContainer(_image("postgres"), driver=None))
    try:
        url = container.get_connection_url()
        asyncio.run(migrate(url, _settings().database))
        yield url
    finally:
        container.stop()


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    container = _start(RedisContainer(_image("redis")))
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
async def database(postgres_url: str) -> AsyncIterator[str]:
    """The database's URL; its tables are emptied after the test."""
    yield postgres_url
    engine = create_engine(postgres_url, _settings().database, "ai-cloud tests")
    try:
        names = ", ".join(f"{table.schema}.{table.name}" for table in accounts_tables.sorted_tables)
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {names} CASCADE"))
    finally:
        await engine.dispose()


@pytest.fixture
def redis_url(redis_container: RedisContainer) -> Iterator[str]:
    """Redis's URL; it is emptied after the test."""
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(redis_container.port)
    yield f"redis://{host}:{port}/0"
    client = redis_container.get_client()
    try:
        client.flushdb()
    finally:
        client.close()
