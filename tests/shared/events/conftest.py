# SPDX-License-Identifier: MIT
"""The tests' module's outbox in the tests' database, and the queue on the tests' Redis."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from events_module import EVENTS, SCHEMA, TABLES
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.schema import CreateSchema

from ai_cloud.entry.settings import CloudSettings
from ai_cloud.shared.config import load_config
from ai_cloud.shared.database import create_engine
from ai_cloud.shared.events import EventsSettings
from ai_cloud.shared.events.streams import RedisEventQueue
from ai_cloud.shared.redis import create_redis


@pytest.fixture(scope="session")
def outbox_database(postgres_url: str) -> str:
    """The tests' database, with the tests' module's outbox."""
    database = CloudSettings.from_config(load_config("development")).database

    async def create() -> None:
        engine = create_engine(postgres_url, database, "ai-cloud tests")
        try:
            async with engine.begin() as connection:
                await connection.execute(CreateSchema(SCHEMA, if_not_exists=True))
                await connection.run_sync(TABLES.create_all)
        finally:
            await engine.dispose()

    asyncio.run(create())
    return postgres_url


@pytest.fixture
def settings(make_settings: Any) -> EventsSettings:
    return make_settings(events=EVENTS).events


@pytest.fixture
async def engine(outbox_database: str, make_settings: Any) -> AsyncIterator[AsyncEngine]:
    """An engine on the tests' database; the outbox is emptied, and its places start
    again, after the test."""
    engine = create_engine(outbox_database, make_settings().database, "ai-cloud tests")
    yield engine
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE {SCHEMA}.outbox RESTART IDENTITY"))
            await connection.execute(text(f"ALTER SEQUENCE {SCHEMA}.outbox_position RESTART"))
    finally:
        await engine.dispose()


@pytest.fixture
async def redis(redis_url: str, make_settings: Any) -> AsyncIterator[Redis]:
    client = create_redis(redis_url, make_settings().redis)
    yield client
    await client.aclose()


@pytest.fixture
def queue(redis: Redis) -> RedisEventQueue:
    return RedisEventQueue(redis)
