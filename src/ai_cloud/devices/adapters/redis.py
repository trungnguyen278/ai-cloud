# SPDX-License-Identifier: MIT
"""What the devices keep in Redis, which may lose it (docs/decisions/0008-data-and-tokens.md,
decision 3): the assertions that devices have used, until each has expired, the presence of
their open control channels, and the hints that reach the replicas holding them."""

import hashlib
import math
from collections.abc import AsyncIterator, Collection, Sequence
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis

PREFIX = "devices:assertion:"


class RedisAssertions:
    """Losing them reopens, for a few minutes at most, the replay of an assertion that someone
    copied in that time."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def first_use(self, device_id: UUID, jti: str, until: datetime) -> bool:
        # Each device's identifiers are its own (RFC 7519, 4.1.7). Their SHA-256 bounds the
        # key whatever the device sent, a lone surrogate included, which JSON can spell.
        digest = hashlib.sha256(jti.encode("utf-8", "surrogatepass")).hexdigest()
        lasts = math.ceil((until - datetime.now(UTC)).total_seconds() * 1000)
        # SET NX: of two requests with one assertion, one only sets it.
        return bool(
            await self._redis.set(f"{PREFIX}{device_id}:{digest}", b"", nx=True, px=max(lasts, 1))
        )


# The presence of each device's newest open connection, which every replica of the api reads
# (docs/decisions/0011-control-channels.md): its identifier, until the replica that holds it
# stops keeping it.
PRESENCE_PREFIX = "devices:presence:"
# The channel of the hints that a device's standing may have changed. Every replica hears
# each hint, at most once, and checks the connections it holds of that device.
HINTS = "devices:hints"

# Keeps a connection's presence, and notes it again if it lapsed, unless a newer connection
# of the device replaced it: 1 if kept, 0 if replaced. A script, so that the reading and the
# writing are one, as Redis's documentation releases a lock.
_KEEP = """
local held = redis.call('GET', KEYS[1])
if held and held ~= ARGV[1] then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
return 1
"""
# Forgets a connection's presence if it is still the device's newest.
_LEAVE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisPresence:
    def __init__(self, redis: Redis, ttl: float) -> None:
        self._redis = redis
        self._ttl = math.ceil(ttl * 1000)
        self._keep = redis.register_script(_KEEP)
        self._leave = redis.register_script(_LEAVE)

    async def arrive(self, device_id: UUID, connection: UUID) -> UUID | None:
        replaced = await self._redis.set(
            _presence(device_id), str(connection), px=self._ttl, get=True
        )
        return None if replaced is None else UUID(replaced.decode())

    async def keep(self, held: Sequence[tuple[UUID, UUID]]) -> set[tuple[UUID, UUID]]:
        if not held:
            return set()
        async with self._redis.pipeline(transaction=False) as pipe:
            for device_id, connection in held:
                await self._keep(
                    keys=[_presence(device_id)], args=[str(connection), self._ttl], client=pipe
                )
            kept = await pipe.execute()
        return {pair for pair, still in zip(held, kept, strict=True) if not still}

    async def leave(self, device_id: UUID, connection: UUID) -> None:
        await self._leave(keys=[_presence(device_id)], args=[str(connection)])

    async def connected(self, device_ids: Collection[UUID]) -> set[UUID]:
        ordered = list(device_ids)
        if not ordered:
            return set()
        found = await self._redis.mget([_presence(device_id) for device_id in ordered])
        return {device_id for device_id, held in zip(ordered, found, strict=True) if held}


class RedisHints:
    """Hints on Redis's publish and subscribe, whose messages reach the replicas listening at
    the time, once at most: a hint lost is found by the replica's next check."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, device_ids: Collection[UUID]) -> None:
        if not device_ids:
            return
        async with self._redis.pipeline(transaction=False) as pipe:
            for device_id in device_ids:
                pipe.publish(HINTS, str(device_id))
            await pipe.execute()

    async def listen(self) -> AsyncIterator[UUID]:
        async with self._redis.pubsub(ignore_subscribe_messages=True) as pubsub:
            await pubsub.subscribe(HINTS)
            while True:
                # Without a timeout, the read waits for the next hint however long it takes,
                # rather than the client's timeout for an answer.
                message = await pubsub.get_message(timeout=None)
                if message is None:
                    continue  # its subscription, which the client confirms
                try:
                    device_id = UUID(message["data"].decode())
                except AttributeError, UnicodeDecodeError, ValueError:
                    continue  # only the cloud publishes here: nothing else to do with it
                yield device_id


def _presence(device_id: UUID) -> str:
    return f"{PRESENCE_PREFIX}{device_id}"
