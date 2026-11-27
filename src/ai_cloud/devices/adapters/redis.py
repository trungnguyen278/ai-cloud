# SPDX-License-Identifier: MIT
"""What the devices keep in Redis, which may lose it (docs/decisions/0008-data-and-tokens.md,
decision 3): the assertions that devices have used, until each has expired, the presence of
their open control channels, their sessions, for them to resume, and the hints that reach the
replicas holding them."""

import hashlib
import json
import math
from collections.abc import AsyncIterator, Collection, Sequence
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis

from ai_cloud.devices.domain.link import Claim, Handing, Keeping, Record

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


# Each device's session, for it to resume on any replica (docs/decisions/0013-resuming-
# sessions.md): a hash of the session's identifier, the registration it holds under, the
# digest of its terms (DeviceHello.terms), its version, the connection that holds it,
# ``owner``, empty once given up, and the newer one that claimed it, ``taker``; then what its
# holder wrote as it gave it up: the last number handled, the last one sent, and the texts
# kept, a JSON array. One hash, so that each script reads and writes all of it as one.
SESSION_PREFIX = "devices:session:"

# A new session, in place of any other of the device's: each field written anew, so the older
# one's connection finds it replaced.
_START = """
redis.call('HSET', KEYS[1], 'id', ARGV[1], 'registered', ARGV[2], 'terms', ARGV[3],
  'minor', ARGV[4], 'owner', ARGV[5], 'taker', '', 'received', ARGV[6], 'sent', ARGV[7],
  'kept', ARGV[8])
redis.call('PEXPIRE', KEYS[1], ARGV[9])
return 1
"""
# Claims the session for a connection: taken at once once given up, or asked of the
# connection that holds it; why not otherwise. Nothing changes unless the session is the
# device's under the same registration, with the same terms, of a version the cloud speaks.
_CLAIM = """
local record = redis.call('HMGET', KEYS[1], 'id', 'registered', 'terms', 'minor', 'owner')
if record[1] ~= ARGV[1] then
  return {'gone'}
end
if record[2] ~= ARGV[2] then
  return {'registration'}
end
if record[3] ~= ARGV[3] then
  return {'terms'}
end
if tonumber(record[4]) > tonumber(ARGV[4]) then
  return {'version'}
end
if record[5] ~= '' and record[5] ~= ARGV[5] then
  redis.call('HSET', KEYS[1], 'taker', ARGV[5])
  return {'waiting'}
end
redis.call('HSET', KEYS[1], 'owner', ARGV[5], 'taker', '')
redis.call('PEXPIRE', KEYS[1], ARGV[6])
local kept = redis.call('HMGET', KEYS[1], 'minor', 'received', 'sent', 'kept')
return {'taken', kept[1], kept[2], kept[3], kept[4]}
"""
# What was kept of a session claimed, once handed over to the connection that claimed it.
# The last try withdraws the claim if it was not: a hand-over that comes after it parks the
# session, which the connection that claimed it then replaces with a new one. A newer claim,
# or a newer connection that took the session, supersedes this one's.
_TAKE = """
local record = redis.call('HMGET', KEYS[1], 'id', 'owner', 'taker', 'minor', 'received',
  'sent', 'kept')
if record[1] ~= ARGV[1] then
  return {'gone'}
end
if record[2] == ARGV[2] then
  return {'taken', record[4], record[5], record[6], record[7]}
end
if record[3] == ARGV[2] then
  if ARGV[3] == '1' then
    redis.call('HSET', KEYS[1], 'taker', '')
  end
  return {'waiting'}
end
return {'superseded'}
"""
# Keeps a session that its connection holds for another lease, unless a newer connection
# took it or replaced it.
_HOLD = """
local record = redis.call('HMGET', KEYS[1], 'id', 'owner', 'taker')
if not record[1] then
  return 'missing'
end
if record[1] ~= ARGV[1] then
  return 'replaced'
end
if record[2] ~= ARGV[2] or record[3] ~= '' then
  return 'taken'
end
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return 'held'
"""
# Gives up a session that its connection holds, with what it kept: to the connection that
# claimed it, for a lease, or for resume_window.
_HAND = """
local record = redis.call('HMGET', KEYS[1], 'id', 'owner', 'taker')
if record[1] ~= ARGV[1] or record[2] ~= ARGV[2] then
  return 'lost'
end
redis.call('HSET', KEYS[1], 'received', ARGV[3], 'sent', ARGV[4], 'kept', ARGV[5])
if record[3] ~= '' then
  redis.call('HSET', KEYS[1], 'owner', record[3], 'taker', '')
  redis.call('PEXPIRE', KEYS[1], ARGV[6])
  return 'handed'
end
redis.call('HSET', KEYS[1], 'owner', '')
redis.call('PEXPIRE', KEYS[1], ARGV[7])
return 'parked'
"""
# Forgets a session that its connection still holds.
_END = """
local record = redis.call('HMGET', KEYS[1], 'id', 'owner')
if record[1] == ARGV[1] and record[2] == ARGV[2] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisSessions:
    """A session that its connection holds lasts ``lease`` seconds unless its replica keeps
    it, as the connection's presence does: a replica that stops without giving its sessions
    up leaves them to lapse, and their devices get new ones. One given up lasts ``window``.
    Losing one gives its device a new session when it next connects."""

    def __init__(self, redis: Redis, lease: float, window: float) -> None:
        self._redis = redis
        self._lease = math.ceil(lease * 1000)
        self._window = math.ceil(window * 1000)
        self._start = redis.register_script(_START)
        self._claim = redis.register_script(_CLAIM)
        self._take = redis.register_script(_TAKE)
        self._hold = redis.register_script(_HOLD)
        self._hand = redis.register_script(_HAND)
        self._end = redis.register_script(_END)

    async def start(self, record: Record, connection: UUID) -> None:
        await self._start(
            keys=[_session(record.device_id)],
            args=[
                str(record.session),
                _registration(record.registered),
                record.terms,
                record.minor,
                str(connection),
                record.received,
                record.sent,
                json.dumps(list(record.kept)),
                self._lease,
            ],
        )

    async def claim(
        self,
        device_id: UUID,
        connection: UUID,
        session: UUID,
        registered: datetime,
        terms: str,
        minor: int,
    ) -> Record | Claim:
        said = await self._claim(
            keys=[_session(device_id)],
            args=[
                str(session),
                _registration(registered),
                terms,
                minor,
                str(connection),
                self._lease,
            ],
        )
        return _claimed(said, Record(session, device_id, registered, terms, minor))

    async def take(
        self,
        device_id: UUID,
        connection: UUID,
        session: UUID,
        registered: datetime,
        terms: str,
        *,
        last: bool,
    ) -> Record | Claim:
        # The claim compared the registration and the terms.
        said = await self._take(
            keys=[_session(device_id)], args=[str(session), str(connection), int(last)]
        )
        return _claimed(said, Record(session, device_id, registered, terms, 0))

    async def keep(self, held: Sequence[tuple[UUID, UUID, UUID]]) -> dict[UUID, Keeping]:
        if not held:
            return {}
        async with self._redis.pipeline(transaction=False) as pipe:
            for device_id, connection, session in held:
                await self._hold(
                    keys=[_session(device_id)],
                    args=[str(session), str(connection), self._lease],
                    client=pipe,
                )
            found = await pipe.execute()
        return {
            connection: Keeping(each.decode())
            for (_, connection, _), each in zip(held, found, strict=True)
        }

    async def hand(self, record: Record, connection: UUID) -> Handing:
        handed = await self._hand(
            keys=[_session(record.device_id)],
            args=[
                str(record.session),
                str(connection),
                record.received,
                record.sent,
                json.dumps(list(record.kept)),
                self._lease,
                self._window,
            ],
        )
        return Handing(handed.decode())

    async def end(self, device_id: UUID, connection: UUID, session: UUID) -> None:
        await self._end(keys=[_session(device_id)], args=[str(session), str(connection)])

    async def forget(self, device_ids: Collection[UUID]) -> None:
        if device_ids:
            await self._redis.delete(*(_session(device_id) for device_id in device_ids))


def _session(device_id: UUID) -> str:
    return f"{SESSION_PREFIX}{device_id}"


def _registration(registered: datetime) -> str:
    """A registration as a session holds it: one text for one instant."""
    return registered.astimezone(UTC).isoformat()


def _claimed(said: list[bytes], claimed: Record) -> Record | Claim:
    """The record that a claim, or a take, found, with ``claimed``'s identity; or why not."""
    if said[0] != b"taken":
        return Claim(said[0].decode())
    minor, received, sent, kept = said[1:]
    return Record(
        claimed.session,
        claimed.device_id,
        claimed.registered,
        claimed.terms,
        int(minor),
        int(received),
        int(sent),
        tuple(json.loads(kept)),
    )
