# SPDX-License-Identifier: MIT
"""Writing events in their changes' transactions, and relaying them to their stream, once
each, in their order, whatever is lost on the way."""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest
from events_module import EVENTS, OUTBOX, SCHEMA, Happened, stream, until, write
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_cloud.shared.events import Event, EventsSettings
from ai_cloud.shared.events.relay import Relay
from ai_cloud.shared.events.streams import RedisEventQueue

pytestmark = pytest.mark.anyio


def relay_of(engine: AsyncEngine, queue: Any, settings: EventsSettings) -> Relay:
    return Relay(engine, queue, [OUTBOX], settings)


async def outbox(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as connection:
        found = await connection.execute(
            text(f"SELECT subject, position FROM {SCHEMA}.outbox ORDER BY seq")
        )
        return list(found)


async def subjects(redis: Redis) -> list[str]:
    return [event.subject for _, event in await stream(redis)]


async def age(engine: AsyncEngine, seconds: float, *, but: str | None = None) -> None:
    """Make the outbox's events look ``seconds`` older, all but the one about ``but``."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE {SCHEMA}.outbox SET create_time = create_time"
                " - make_interval(secs => :seconds) WHERE subject IS DISTINCT FROM :but"
            ),
            {"seconds": seconds, "but": but},
        )


# Writing.


async def test_an_event_stands_or_falls_with_its_transaction(engine: AsyncEngine) -> None:
    await write(engine, "kept")
    with pytest.raises(LookupError):
        async with engine.begin() as connection:
            await OUTBOX.write(connection, Happened("lost"))
            raise LookupError("the change failed")
    assert [row.subject for row in await outbox(engine)] == ["kept"]


async def test_an_event_is_written_in_a_transaction(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        with pytest.raises(RuntimeError, match="transaction"):
            await OUTBOX.write(connection, Happened("a"))


@dataclass(frozen=True)
class Foreign(Happened):
    TYPE: ClassVar[str] = "accounts.thing.happened.v1"


async def test_a_module_writes_its_own_events_only(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        with pytest.raises(ValueError, match="not one of events_test's"):
            await OUTBOX.write(connection, Foreign("a"))


# Relaying.


async def test_each_event_goes_to_its_place_in_the_stream(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    await write(engine, "a", "b")
    await write(engine, "c")
    relay = relay_of(engine, queue, settings)
    assert await relay.relay() == 6  # three numbered, then three written
    entries = await stream(redis)
    assert [entry for entry, _ in entries] == ["1-0", "2-0", "3-0"]
    first = entries[0][1]
    assert (first.source, first.type, first.subject) == ("/events_test", Happened.TYPE, "a")
    assert first.data == {"thing": "a"}
    assert first.time.tzinfo is not None
    assert [row.position for row in await outbox(engine)] == [1, 2, 3]
    assert await relay.relay() == 0


@dataclass(frozen=True)
class Odd(Happened):
    def data(self) -> dict[str, Any]:
        # A lone surrogate, which no request lets in, and the json column takes as an escape.
        return {"thing": chr(0xD800)}


async def test_odd_text_in_an_event_holds_nothing_up(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    await write(engine, "odd", kind=Odd)
    await write(engine, "next")
    relay = relay_of(engine, queue, settings)
    while await relay.relay():
        pass
    [(_, odd), (_, following)] = await stream(redis)
    assert (odd.data, following.subject) == ({"thing": chr(0xD800)}, "next")


async def test_events_take_their_places_as_their_transactions_commit(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    relay = relay_of(engine, queue, settings)
    async with engine.begin() as connection:
        # Written first, committed last: nothing waits for it, and nothing is skipped.
        await OUTBOX.write(connection, Happened("early"))
        await write(engine, "late")
        await relay.relay()
        assert await subjects(redis) == ["late"]
    await relay.relay()
    assert await subjects(redis) == ["late", "early"]


async def test_a_large_outbox_is_relayed_in_batches(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, make_settings: Any
) -> None:
    settings = make_settings(events={**EVENTS, "batch_size": 2}).events
    await write(engine, *"abcde")
    relay = relay_of(engine, queue, settings)
    assert await relay.relay() == 4
    assert await subjects(redis) == ["a", "b"]
    while await relay.relay():
        pass
    assert await subjects(redis) == list("abcde")


async def test_a_stream_redis_lost_is_written_again(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    await write(engine, "a", "b", "c")
    relay = relay_of(engine, queue, settings)
    await relay.relay()
    before = await stream(redis)
    await redis.flushdb()
    assert await relay.relay() == 3
    assert await stream(redis) == before


async def test_a_stream_that_lost_its_end_is_filled_in(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    # As after a crash between numbering and writing, or Redis restored from a snapshot.
    await write(engine, "a", "b", "c")
    relay = relay_of(engine, queue, settings)
    await relay.relay()
    before = await stream(redis)
    [(entry, fields)] = await redis.xrange(OUTBOX.stream, count=1)
    await redis.delete(OUTBOX.stream)
    await redis.xadd(OUTBOX.stream, fields, id=entry)
    assert await relay.relay() == 2
    assert await stream(redis) == before


async def test_relays_at_once_write_each_event_once_in_order(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    things = [f"t{n}" for n in range(30)]
    for each in things:
        await write(engine, each)
    relays = [relay_of(engine, queue, settings) for _ in range(3)]
    while sum(await asyncio.gather(*(relay.relay() for relay in relays))):
        pass
    entries = await stream(redis)
    assert [entry for entry, _ in entries] == [f"{n}-0" for n in range(1, 31)]
    assert [event.subject for _, event in entries] == things


async def test_one_relay_at_a_time_numbers_or_sweeps_an_outbox(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    await write(engine, "a")
    relay = relay_of(engine, queue, settings)
    async with engine.begin() as connection:
        assert await OUTBOX.take_turn(connection)
        assert await relay.relay() == 0
        assert [row.position for row in await outbox(engine)] == [None]
    assert await relay.relay() == 2
    await age(engine, settings.retention + 1)
    async with engine.begin() as connection:
        assert await OUTBOX.take_turn(connection)
        await relay.sweep()
        assert [row.subject for row in await outbox(engine)] == ["a"]
    await relay.sweep()
    assert await outbox(engine) == []


async def test_a_stream_past_its_outbox_is_written_again(
    engine: AsyncEngine,
    queue: RedisEventQueue,
    redis: Redis,
    settings: EventsSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # As after PostgreSQL is restored from a backup: the stream holds places the outbox
    # never gave.
    await write(engine, "a")
    relay = relay_of(engine, queue, settings)
    await relay.relay()
    await redis.xadd(OUTBOX.stream, {"x": "y"}, id="100-0")
    await relay.relay()
    assert [entry for entry, _ in await stream(redis)] == ["1-0"]
    assert "a stream holds places its outbox has not given" in caplog.text


async def test_a_stream_holding_the_places_an_outbox_gives_next_is_written_again(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    # As after PostgreSQL is restored from a backup made before the outbox gave its first
    # place: the stream still holds an event at place 1, which the outbox gives again.
    lost = Event(
        id=uuid.uuid7(),
        source=OUTBOX.source,
        type=Happened.TYPE,
        subject="lost",
        time=datetime.now(UTC),
        data={"thing": "lost"},
    )
    await redis.xadd(OUTBOX.stream, lost.fields(), id="1-0")
    await write(engine, "a")
    await relay_of(engine, queue, settings).relay()
    assert await subjects(redis) == ["a"]


# The retention.


class Unwritten(RedisEventQueue):
    """A queue whose writes are lost on the way."""

    async def append(self, stream: str, events: Sequence[tuple[int, Event]]) -> None:
        pass


async def test_events_past_their_retention_go_once_relayed(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    await write(engine, "old", "older")
    relay = relay_of(engine, queue, settings)
    await relay.relay()
    await write(engine, "numbered")
    await relay_of(engine, Unwritten(redis), settings).relay()
    await write(engine, "new")
    await age(engine, settings.retention + 1, but="new")
    await relay.sweep()
    # What the stream holds goes, and what it does not hold yet stays.
    assert [row.subject for row in await outbox(engine)] == ["numbered", "new"]
    assert await stream(redis) == []
    await relay.relay()
    assert await subjects(redis) == ["numbered", "new"]


async def test_a_stream_keeps_what_its_outbox_keeps(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    await write(engine, "a", "b")
    relay = relay_of(engine, queue, settings)
    await relay.relay()
    await relay.sweep()
    assert await subjects(redis) == ["a", "b"]
    await redis.flushdb()
    await relay.sweep()  # a stream that is not there
    assert await redis.exists(OUTBOX.stream) == 0


# The loop.


async def test_the_relay_runs_until_it_is_stopped(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    stop = asyncio.Event()
    running = asyncio.create_task(relay_of(engine, queue, settings).run(stop))
    await write(engine, "a")

    async def relayed() -> bool:
        return await subjects(redis) == ["a"]

    await until(relayed)
    stop.set()
    await asyncio.wait_for(running, 1)


async def test_the_relay_rests_only_when_there_is_nothing_more(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, make_settings: Any
) -> None:
    # One event a round, and a rest far longer than the test waits.
    settings = make_settings(events={**EVENTS, "batch_size": 1, "poll_interval": 60}).events
    await write(engine, "a", "b", "c")
    stop = asyncio.Event()
    running = asyncio.create_task(relay_of(engine, queue, settings).run(stop))

    async def relayed() -> bool:
        return await subjects(redis) == ["a", "b", "c"]

    await until(relayed)
    stop.set()
    await asyncio.wait_for(running, 1)


class Flaky(RedisEventQueue):
    """A queue that fails the first time it is asked where a stream stops."""

    failed = False

    async def last(self, stream: str) -> int:
        if not self.failed:
            self.failed = True
            raise ConnectionError("Redis is not there")
        return await super().last(stream)


async def test_the_relay_rides_out_a_failure(
    engine: AsyncEngine, redis: Redis, settings: EventsSettings, caplog: pytest.LogCaptureFixture
) -> None:
    await write(engine, "a")
    stop = asyncio.Event()
    running = asyncio.create_task(relay_of(engine, Flaky(redis), settings).run(stop))

    async def relayed() -> bool:
        return await subjects(redis) == ["a"]

    await until(relayed)
    stop.set()
    await asyncio.wait_for(running, 1)
    [failure] = [
        record
        for record in caplog.records
        if record.name == "ai_cloud.shared.events.relay" and record.levelno == logging.WARNING
    ]
    assert failure.getMessage() == "relaying events failed: the relay tries again"
