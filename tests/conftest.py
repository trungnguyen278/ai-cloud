# SPDX-License-Identifier: MIT
"""What the tests share: a clean environment, the development settings, PostgreSQL and
Redis in Docker (docs/decisions/0008-data-and-tokens.md, decision 5), as the development
Compose file runs them, and the api served in the tests' own process."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from app_api import ADDRESS, Cloud
from docker.errors import DockerException
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

from ai_cloud.__main__ import ENVIRONMENT_ENV
from ai_cloud.entry.api import ApiSecrets, create_app
from ai_cloud.entry.migrate import MODULES, migrate
from ai_cloud.entry.settings import CloudSettings
from ai_cloud.shared.config import CONFIG_DIR_ENV, deep_merge, load_config
from ai_cloud.shared.database import DATABASE_URL_ENV, create_engine
from ai_cloud.shared.jose import SIGNING_KEYS_ENV, SigningKeys, new_signing_key
from ai_cloud.shared.protocols import PLATFORM_DIR_ENV
from ai_cloud.shared.redis import REDIS_URL_ENV
from ai_cloud.shared.secrets import FILE_SUFFIX

COMPOSE = Path(__file__).resolve().parents[1] / "deploy" / "compose.development.yaml"
SECRETS = (DATABASE_URL_ENV, REDIS_URL_ENV, SIGNING_KEYS_ENV)

type MakeSettings = Callable[..., CloudSettings]
type MakeCloud = Callable[..., Awaitable[Cloud]]


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
        names = ", ".join(
            f"{table.schema}.{table.name}"
            for module in MODULES
            for table in module.metadata.sorted_tables
        )
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


@pytest.fixture
async def make_cloud(
    database: str, redis_url: str, make_settings: Any, jwk_set: Callable[[int], str]
) -> AsyncIterator[MakeCloud]:
    """Serve an api with these changes to the development settings, and give an app that
    reaches it."""
    opened: list[tuple[AbstractAsyncContextManager[None], httpx2.AsyncClient]] = []

    async def make(*, keys: SigningKeys | None = None, **changes: Any) -> Cloud:
        keys = keys or SigningKeys.from_jwk_set(jwk_set(1))
        settings = make_settings(**changes)
        app = create_app(settings, ApiSecrets(database, redis_url, keys))
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        transport = httpx2.ASGITransport(app=app, client=(ADDRESS, 50000))
        http = httpx2.AsyncClient(transport=transport, base_url="http://cloud.test")
        opened.append((lifespan, http))
        return Cloud(http, keys=keys, settings=settings, database=database, redis_url=redis_url)

    yield make
    for lifespan, http in reversed(opened):
        await http.aclose()
        await lifespan.__aexit__(None, None, None)


@pytest.fixture
async def cloud(make_cloud: MakeCloud) -> Cloud:
    return await make_cloud()
