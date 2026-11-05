# SPDX-License-Identifier: MIT
"""The queue, on Redis Streams (docs/decisions/0009-domain-events.md, decisions 3 to 6).

An event's entry in its stream is ``<position>-0``: its place, which its outbox gave it.
Redis refuses an entry whose identifier is not greater than the stream's last, so an event
is never written twice, and the stream's last identifier says where it stops: after a
crash, or after Redis lost what it held, the relay goes on from there. A consumer group is
Redis's own, with its list of pending entries and their delivery counts. Dead letters are a
stream of their own, as Redis's documentation advises.
"""

from collections.abc import Sequence
from datetime import datetime

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from ai_cloud.shared.events.model import Event
from ai_cloud.shared.events.queue import Delivery, GroupLost, Pending

# What Redis answers an XADD whose identifier is not past the stream's last one.
_NOT_PAST_LAST = "equal or smaller than the target stream top item"
# What it answers when a stream, or a group, is not there.
_NO_SUCH_KEY = "no such key"
_NO_GROUP = "NOGROUP"
_GROUP_EXISTS = "BUSYGROUP"

_MILLISECONDS = 1000


class RedisEventQueue:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # The relay's side.

    async def last(self, stream: str) -> int:
        try:
            info = await self._redis.xinfo_stream(stream)
        except ResponseError as error:
            if _NO_SUCH_KEY in str(error):
                return 0
            raise
        return _position(_text(info["last-generated-id"]))

    async def append(self, stream: str, events: Sequence[tuple[int, Event]]) -> None:
        if not events:
            return
        async with self._redis.pipeline(transaction=False) as pipeline:
            for position, event in events:
                pipeline.xadd(stream, event.fields(), id=f"{position}-0")
            results = await pipeline.execute(raise_on_error=False)
        for result in results:
            # Another relay wrote it first.
            if isinstance(result, ResponseError) and _NOT_PAST_LAST in str(result):
                continue
            if isinstance(result, Exception):
                raise result

    async def drop(self, stream: str) -> None:
        await self._redis.delete(stream)

    async def trim(self, stream: str, *, before: int) -> None:
        await self._redis.xtrim(stream, minid=f"{before}-0", approximate=False)

    # The consumers' side.

    async def join(self, stream: str, group: str) -> None:
        try:
            await self._redis.xgroup_create(stream, group, id="0", mkstream=True)
        except ResponseError as error:
            if not str(error).startswith(_GROUP_EXISTS):
                raise

    async def read(
        self, group: str, consumer: str, streams: Sequence[str], wait: float
    ) -> Delivery | None:
        # BLOCK 0 would wait for ever: no BLOCK at all reads without waiting.
        block = max(1, round(wait * _MILLISECONDS)) if wait > 0 else None
        try:
            # One at a time: an event waits in a consumer only while its own handler runs,
            # which handler_timeout bounds, so that no other consumer takes it over meanwhile.
            found = await self._redis.xreadgroup(
                group, consumer, dict.fromkeys(streams, ">"), count=1, block=block
            )
        except ResponseError as error:
            raise _lost(error) from None
        for stream, entries in found or ():
            for entry, fields in entries:
                return Delivery(_text(stream), _text(entry), _fields(fields), deliveries=1)
        return None

    async def pending(
        self, stream: str, group: str, *, idle: float, after: str | None, limit: int
    ) -> list[Pending]:
        try:
            found = await self._redis.xpending_range(
                stream,
                group,
                min="-" if after is None else f"({after}",
                max="+",
                count=limit,
                idle=round(idle * _MILLISECONDS),
            )
        except ResponseError as error:
            raise _lost(error) from None
        return [
            Pending(
                entry=_text(each["message_id"]),
                idle=each["time_since_delivered"] / _MILLISECONDS,
                deliveries=each["times_delivered"],
            )
            for each in found
        ]

    async def claim(
        self, stream: str, group: str, consumer: str, pending: Pending, *, idle: float
    ) -> Delivery | None:
        try:
            # Of two consumers, the second finds the entry no longer idle. One the stream
            # has let go of comes back as nothing, and leaves the pending list.
            found = await self._redis.xclaim(
                stream, group, consumer, round(idle * _MILLISECONDS), [pending.entry]
            )
        except ResponseError as error:
            raise _lost(error) from None
        for entry, fields in found:
            if fields is not None:
                # Claiming counts as a delivery.
                return Delivery(stream, _text(entry), _fields(fields), pending.deliveries + 1)
        return None

    async def ack(self, stream: str, group: str, entry: str) -> None:
        await self._redis.xack(stream, group, entry)

    async def bury(self, delivery: Delivery, group: str, dead_letters: str, reason: str) -> None:
        letter = {
            **delivery.fields,
            "consumer": group,
            "stream": delivery.stream,
            "entry": delivery.entry,
            "deliveries": str(delivery.deliveries),
            "reason": reason,
        }
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.xadd(dead_letters, letter)
            pipeline.xack(delivery.stream, group, delivery.entry)
            await pipeline.execute()

    async def trim_dead_letters(self, dead_letters: str, *, before: datetime) -> None:
        # A dead letter's identifier is Redis's time when it was put aside.
        await self._redis.xtrim(
            dead_letters, minid=f"{int(before.timestamp() * _MILLISECONDS)}-0", approximate=False
        )

    async def leave(self, stream: str, group: str, consumer: str) -> None:
        try:
            held = await self._redis.xpending_range(
                stream, group, min="-", max="+", count=1, consumername=consumer
            )
            if not held:
                await self._redis.xgroup_delconsumer(stream, group, consumer)
        except ResponseError as error:
            if not str(error).startswith(_NO_GROUP):
                raise


def _lost(error: ResponseError) -> Exception:
    return GroupLost(str(error)) if str(error).startswith(_NO_GROUP) else error


def _position(entry: str) -> int:
    return int(entry.partition("-")[0])


def _text(value: bytes | str) -> str:
    # What is not UTF-8 makes no event, and is put aside as such.
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _fields(fields: dict[bytes | str, bytes | str]) -> dict[str, str]:
    return {_text(name): _text(value) for name, value in fields.items()}
