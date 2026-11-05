# SPDX-License-Identifier: MIT
"""Handing events to their consumers: once to each consumer group, again after a failure,
later each time, and aside once they have failed too often."""

import asyncio
import logging
from typing import Any

import pytest
from events_module import EVENTS, OUTBOX, Happened, Noticed, decoded, until, write
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_cloud.shared.events import Consumer, Event, EventsSettings
from ai_cloud.shared.events.consumers import Subscription
from ai_cloud.shared.events.relay import Relay
from ai_cloud.shared.events.streams import RedisEventQueue

pytestmark = pytest.mark.anyio

GROUP = "events_test.seeing"


class Handling:
    """A handler that notes what it is given, and fails as it is told to."""

    def __init__(self, *, failures: int = 0, delay: float = 0.0) -> None:
        self.seen: list[str] = []
        self.failures = failures
        self.delay = delay

    async def __call__(self, event: Event) -> None:
        self.seen.append(event.subject)
        await asyncio.sleep(self.delay)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("the handler failed")


def consumer(handler: Handling, name: str = GROUP) -> Consumer:
    return Consumer(name, {Happened.TYPE: handler})


async def relayed(
    engine: AsyncEngine, queue: RedisEventQueue, settings: EventsSettings, *things: str
) -> None:
    await write(engine, *things)
    while await Relay(engine, queue, [OUTBOX], settings).relay():
        pass


async def drained(subscription: Subscription) -> None:
    while await subscription.step(0):
        pass


async def pending(redis: Redis, group: str = GROUP) -> list[dict[str, Any]]:
    return await redis.xpending_range(OUTBOX.stream, group, "-", "+", 100)


async def age(redis: Redis, seconds: float, group: str = GROUP) -> None:
    """Make the group's pending events look as if they had waited ``seconds`` more."""
    for each in await pending(redis, group):
        idle = each["time_since_delivered"] + round(seconds * 1000)
        await redis.xclaim(
            OUTBOX.stream, group, each["consumer"], 0, [each["message_id"]], idle=idle, justid=True
        )


async def letters(redis: Redis, key: str = consumer(Handling()).dead_letters) -> list[dict]:
    return [decoded(fields) for _, fields in await redis.xrange(key)]


def records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == "ai_cloud.shared.events.consumers" and record.levelno == level
    ]


async def test_a_consumer_handles_its_types_from_the_start_of_the_stream(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    handler = Handling()
    await relayed(engine, queue, settings, "a", "b")
    await write(engine, "c", kind=Noticed)
    await relayed(engine, queue, settings, "d")
    subscription = Subscription(consumer(handler), queue, settings, name="one")
    await subscription.join()
    await drained(subscription)
    # What came before it joined, and not the type it does not handle, which it is done with.
    assert handler.seen == ["a", "b", "d"]
    assert await pending(redis) == []


async def test_the_consumers_of_a_group_share_its_events(
    engine: AsyncEngine, queue: RedisEventQueue, settings: EventsSettings
) -> None:
    things = [f"t{n}" for n in range(10)]
    await relayed(engine, queue, settings, *things)
    first, second = Handling(), Handling()
    one = Subscription(consumer(first), queue, settings, name="one")
    two = Subscription(consumer(second), queue, settings, name="two")
    await one.join()
    while [await one.step(0), await two.step(0)] != [False, False]:
        pass
    assert first.seen and second.seen
    assert sorted(first.seen + second.seen) == sorted(things)


async def test_each_consumer_group_gets_every_event(
    engine: AsyncEngine, queue: RedisEventQueue, settings: EventsSettings
) -> None:
    await relayed(engine, queue, settings, "a", "b")
    seeing, counting = Handling(), Handling()
    for handler, name in ((seeing, GROUP), (counting, "events_test.counting")):
        subscription = Subscription(consumer(handler, name), queue, settings)
        await subscription.join()
        await drained(subscription)
    assert seeing.seen == counting.seen == ["a", "b"]


async def test_a_failed_event_is_tried_again_after_its_delay(
    engine: AsyncEngine,
    queue: RedisEventQueue,
    redis: Redis,
    settings: EventsSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Handling(failures=1)
    await relayed(engine, queue, settings, "a")
    subscription = Subscription(consumer(handler), queue, settings, name="one")
    await subscription.join()
    assert await subscription.step(0)
    [held] = await pending(redis)
    assert held["times_delivered"] == 1
    assert not await subscription.step(0)  # not yet
    await age(redis, settings.retry_delay)
    assert await subscription.step(0)
    assert handler.seen == ["a", "a"]
    assert await pending(redis) == []
    [failure] = records(caplog, logging.WARNING)
    assert failure.getMessage() == "an event's handler failed: it is tried again later"
    assert (failure.consumer, failure.type, failure.entry, failure.deliveries) == (
        GROUP,
        Happened.TYPE,
        "1-0",
        1,
    )
    assert failure.exc_info is not None


class Counting(RedisEventQueue):
    """A queue that counts the events a consumer asks to take over."""

    claims = 0

    async def claim(self, *args: Any, **kwargs: Any) -> Any:
        self.claims += 1
        return await super().claim(*args, **kwargs)


async def test_each_retry_waits_twice_as_long_as_the_one_before(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    handler = Handling(failures=100)
    await relayed(engine, queue, settings, "a")
    counting = Counting(redis)
    subscription = Subscription(consumer(handler), counting, settings, name="one")
    await subscription.join()
    await subscription.step(0)
    # 1 s, then 2, then 4, which is retry_delay_max: half of it is not enough.
    for wait in (1, 2, 4):
        await age(redis, wait / 2)
        assert not await subscription.step(0), wait
        await age(redis, wait / 2)
        assert await subscription.step(0), wait
    # It was asked for at its times only, not each time it was seen waiting.
    assert counting.claims == 3
    # The fourth delivery was its last: it is put aside at once.
    assert len(handler.seen) == settings.max_deliveries == 4
    assert await pending(redis) == []
    [letter] = await letters(redis)
    assert letter["deliveries"] == "4"


async def test_an_event_that_fails_each_time_becomes_a_dead_letter(
    engine: AsyncEngine,
    queue: RedisEventQueue,
    redis: Redis,
    make_settings: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = make_settings(events={**EVENTS, "max_deliveries": 2}).events
    handler = Handling(failures=100)
    await relayed(engine, queue, settings, "a")
    subscription = Subscription(consumer(handler), queue, settings, name="one")
    await subscription.join()
    await subscription.step(0)
    await age(redis, settings.retry_delay)
    await subscription.step(0)
    assert await pending(redis) == []
    [letter] = await letters(redis)
    assert {
        name: letter[name] for name in ("consumer", "stream", "entry", "deliveries", "reason")
    } == {
        "consumer": GROUP,
        "stream": OUTBOX.stream,
        "entry": "1-0",
        "deliveries": "2",
        "reason": "its handler failed on every delivery",
    }
    # The letter holds the event, for whoever looks into it.
    assert Event.from_fields(letter).subject == "a"
    [put_aside] = records(caplog, logging.ERROR)
    assert put_aside.getMessage() == "an event is put aside as a dead letter"
    assert (put_aside.consumer, put_aside.type, put_aside.deliveries) == (GROUP, Happened.TYPE, 2)
    assert [record.getMessage() for record in records(caplog, logging.WARNING)] == [
        "an event's handler failed: it is tried again later",
        "an event's handler failed: it is put aside",
    ]
    # Handled as many times as it was delivered, and no more.
    assert not await subscription.step(0)
    assert handler.seen == ["a", "a"]


async def test_a_handler_that_takes_too_long_has_failed(
    engine: AsyncEngine,
    queue: RedisEventQueue,
    redis: Redis,
    settings: EventsSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Handling(delay=settings.handler_timeout * 5)
    await relayed(engine, queue, settings, "a")
    subscription = Subscription(consumer(handler), queue, settings, name="one")
    await subscription.join()
    await subscription.step(0)
    [held] = await pending(redis)
    assert held["times_delivered"] == 1
    [failure] = records(caplog, logging.WARNING)
    assert failure.exc_info is not None
    assert failure.exc_info[0] is TimeoutError


async def test_an_entry_that_is_not_an_event_is_put_aside_at_once(
    queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    handler = Handling()
    subscription = Subscription(consumer(handler), queue, settings, name="one")
    await subscription.join()
    await redis.xadd(OUTBOX.stream, {"what": "not an event"}, id="5-0")
    assert await subscription.step(0)
    assert handler.seen == []
    assert await pending(redis) == []
    [letter] = await letters(redis)
    assert (letter["what"], letter["deliveries"]) == ("not an event", "1")
    assert letter["reason"] == "not an event: no specversion"


async def test_an_event_a_stopped_consumer_held_is_taken_over(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    handler = Handling()
    await relayed(engine, queue, settings, "a")
    subscription = Subscription(consumer(handler), queue, settings, name="one")
    await subscription.join()
    # Another consumer was given it, and stopped before it was done.
    assert await queue.read(GROUP, "gone", [OUTBOX.stream], 0) is not None
    assert not await subscription.step(0)
    await age(redis, settings.retry_delay)
    assert await subscription.step(0)
    assert handler.seen == ["a"]
    assert await pending(redis) == []


async def test_a_spent_event_a_stopped_consumer_held_is_put_aside(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, make_settings: Any
) -> None:
    settings = make_settings(events={**EVENTS, "max_deliveries": 1}).events
    handler = Handling()
    await relayed(engine, queue, settings, "a")
    subscription = Subscription(consumer(handler), queue, settings, name="one")
    await subscription.join()
    # Its one delivery went to a consumer that stopped while it handled it.
    await queue.read(GROUP, "gone", [OUTBOX.stream], 0)
    await age(redis, settings.retry_delay)
    assert await subscription.step(0)
    assert handler.seen == []
    [letter] = await letters(redis)
    assert (letter["reason"], letter["deliveries"]) == ("its last delivery never ended", "1")


async def test_a_consumer_joins_again_when_its_group_is_gone(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    handler = Handling()
    stop = asyncio.Event()
    subscription = Subscription(consumer(handler), queue, settings, name="one")
    running = asyncio.create_task(subscription.run(stop))
    await relayed(engine, queue, settings, "a")

    async def seen(count: int) -> bool:
        return handler.seen == ["a"] * count

    await until(lambda: seen(1))
    await redis.flushdb()
    # The relay writes the stream again, which the group reads again from its start.
    await relayed(engine, queue, settings)
    await until(lambda: seen(2))
    stop.set()
    await asyncio.wait_for(running, 2)


async def test_a_consumer_leaves_its_group_as_it_stops(
    engine: AsyncEngine, queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    done, failing = Handling(), Handling(failures=1)
    await relayed(engine, queue, settings, "a")
    for handler, name in ((done, GROUP), (failing, "events_test.failing")):
        stop = asyncio.Event()
        running = asyncio.create_task(
            Subscription(consumer(handler, name), queue, settings, name="one").run(stop)
        )

        async def handled(handler: Handling = handler) -> bool:
            return handler.seen == ["a"]

        await until(handled)
        stop.set()
        await asyncio.wait_for(running, 2)
    names = {
        group: [each["name"] for each in await redis.xinfo_consumers(OUTBOX.stream, group)]
        for group in (GROUP, "events_test.failing")
    }
    # One that holds an event to try again stays, for another to take it over in time.
    assert names == {GROUP: [], "events_test.failing": [b"one"]}


class Flaky(RedisEventQueue):
    """A queue that fails the first read."""

    failed = False

    async def read(self, *args: Any, **kwargs: Any) -> Any:
        if not self.failed:
            self.failed = True
            raise ConnectionError("Redis is not there")
        return await super().read(*args, **kwargs)


async def test_a_consumer_rides_out_a_failure(
    engine: AsyncEngine,
    queue: RedisEventQueue,
    redis: Redis,
    settings: EventsSettings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = Handling()
    await relayed(engine, queue, settings, "a")
    stop = asyncio.Event()
    running = asyncio.create_task(
        Subscription(consumer(handler), Flaky(redis), settings, name="one").run(stop)
    )

    async def handled() -> bool:
        return handler.seen == ["a"]

    await until(handled)
    stop.set()
    await asyncio.wait_for(running, 2)
    [failure] = records(caplog, logging.WARNING)
    assert failure.getMessage() == "a consumer failed: it tries again"


async def test_dead_letters_past_the_retention_go(
    queue: RedisEventQueue, redis: Redis, settings: EventsSettings
) -> None:
    subscription = Subscription(consumer(Handling()), queue, settings)
    key = subscription.consumer.dead_letters
    await redis.xadd(key, {"reason": "put aside long ago"}, id="1-0")
    await redis.xadd(key, {"reason": "put aside now"})
    await subscription.sweep()
    assert [letter["reason"] for letter in await letters(redis, key)] == ["put aside now"]
