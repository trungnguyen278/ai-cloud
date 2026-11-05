# SPDX-License-Identifier: MIT
"""A module of the tests' own, with its outbox in its schema and its events, and what the
tests of events share."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_cloud.shared.database import metadata
from ai_cloud.shared.events import Event, Outbox

SCHEMA = "events_test"
TABLES = metadata(SCHEMA)
OUTBOX = Outbox(TABLES)

# Short times, so that nothing waits long. A retry is made due by ageing its entry.
EVENTS = {
    "poll_interval": 0.01,
    "batch_size": 100,
    "retention": 3600,
    "sweep_interval": 3600,
    "reconnect_delay": 0.01,
    "read_wait": 0.05,
    "handler_timeout": 0.2,
    "retry_delay": 1,
    "retry_delay_max": 4,
    "max_deliveries": 4,
}


@dataclass(frozen=True)
class Happened:
    """Something that happened to a thing, in the tests' module."""

    TYPE: ClassVar[str] = "events_test.thing.happened.v1"

    thing: str

    @property
    def subject(self) -> str:
        return self.thing

    def data(self) -> dict[str, Any]:
        return {"thing": self.thing}


@dataclass(frozen=True)
class Noticed(Happened):
    """Another type of the tests' module's events."""

    TYPE: ClassVar[str] = "events_test.thing.noticed.v1"


async def write(engine: AsyncEngine, *things: str, kind: type[Happened] = Happened) -> None:
    """Write an event for each of ``things``, in one transaction."""
    async with engine.begin() as connection:
        for thing in things:
            await OUTBOX.write(connection, kind(thing))


async def stream(redis: Redis, key: str = OUTBOX.stream) -> list[tuple[str, Event]]:
    """What a stream holds: each entry's identifier, and its event."""
    return [
        (entry.decode(), Event.from_fields(decoded(fields)))
        for entry, fields in await redis.xrange(key)
    ]


def decoded(fields: dict[bytes, bytes]) -> dict[str, str]:
    return {name.decode(): value.decode() for name, value in fields.items()}


async def until(condition: Callable[[], Awaitable[bool]], timeout: float = 5.0) -> None:
    """Wait for ``condition``, checking it often, for ``timeout`` seconds at most."""
    async with asyncio.timeout(timeout):
        while not await condition():
            await asyncio.sleep(0.01)
