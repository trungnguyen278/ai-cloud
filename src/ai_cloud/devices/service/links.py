# SPDX-License-Identifier: MIT
"""The devices' control channels: each connection's session, from the device's hello to its
end, and what a replica of the api does for the sessions it holds (the cloud link 3.0,
platform/protocols/cloud/README.md; docs/decisions/0011-control-channels.md).

A connection opens once the device's access token verifies and the registry holds the
device. Its session starts with the two hellos: the device's says what it is, which the
registry keeps, and the connection becomes the device's newest in the presence, which every
replica reads. A session then ends with a ``bye``, from either side, or when its connection
is lost or falls silent.

Whatever may end a session elsewhere, the device's removal, its home's deletion or a newer
connection, sends a hint that reaches every replica, at most once. A replica checks the
connections it holds of that device against the registry and the presence, and does so for
all of them every ``check_interval``: so a lost hint delays a ``bye``, and never loses one.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Collection, Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ai_cloud.devices.domain import link
from ai_cloud.devices.domain.errors import Unauthenticated, Unavailable
from ai_cloud.devices.domain.link import Bye, Close, Code, DeviceHello, Number
from ai_cloud.devices.domain.model import Opening
from ai_cloud.devices.ports import Channel, Hints, Presence, Store
from ai_cloud.devices.service.devices import DeviceTokens, hint
from ai_cloud.devices.settings import LinkSettings
from ai_cloud.shared.protocols import Schema, protocol_schema, read_json

log = logging.getLogger(__name__)

# How a session ended, besides the reasons of the cloud's bye.
RECONNECT = "reconnect"  # the cloud went away, and asked the device to connect again
CLOSED = "closed"  # the connection was lost, or the device closed it
DEVICE_BYE = "bye"  # the device ended the session
SILENT = "silent"  # nothing came for two ping intervals
STALLED = "stalled"  # nothing the cloud sent could leave for two ping intervals


class Links:
    """The control channels that this replica of the api holds."""

    def __init__(
        self,
        *,
        settings: LinkSettings,
        leeway: float,
        tokens: DeviceTokens,
        store: Store,
        presence: Presence,
        hints: Hints,
    ) -> None:
        self.settings = settings
        self.schema: Schema = protocol_schema(link.PROTOCOL, "from_device")
        self._leeway = leeway
        self._tokens = tokens
        self._store = store
        self._presence = presence
        self._hints = hints
        # The sessions held here, by device: one, or two while a newer one replaces another.
        self._sessions: dict[UUID, set[Session]] = {}
        self._checks: set[asyncio.Task[None]] = set()
        self._stopping = False
        # Set while no session is held here, its presence forgotten.
        self._drained = asyncio.Event()
        self._drained.set()

    async def open(self, access_token: str) -> Opening:
        """A connection for the device whose access token this is, if the registry still
        holds it under the registration that the token was issued for. ``Unauthenticated``
        otherwise; ``Unavailable`` once this replica goes away, also while the registry was
        read."""
        if self._stopping:
            raise Unavailable("this replica is going away")
        holder = self._tokens.authenticate(access_token)
        async with self._store.transaction() as store:
            device = await store.device(holder.device_id)
        if device is None:
            raise Unauthenticated("the device is not registered")
        # A token issued before the device's registration was for the one removed before it.
        # Replicas' clocks may differ by the leeway, as they may for any token.
        if holder.issued + self._leeway < device.create_time.timestamp():
            raise Unauthenticated("the token is older than the device's registration")
        if self._stopping:
            raise Unavailable("this replica is going away")
        return Opening(device.id, device.create_time)

    async def serve(self, channel: Channel, opening: Opening) -> None:
        """Hold the session of a connection that ``open`` let open, until it ends. One that
        begins once this replica goes away closes before its hello, as those it held then do.
        Nothing the device sends raises here: what the session cannot carry ends it."""
        session = Session(self, channel, opening)
        held = self._sessions.setdefault(opening.device_id, set())
        held.add(session)
        self._drained.clear()
        if self._stopping:
            session.end(RECONNECT)
        ended = CLOSED
        try:
            ended = await session.run()
        finally:
            session.left = True
            try:
                if session.established:
                    await self._leave(session)
            finally:
                held.discard(session)
                if not held and self._sessions.get(opening.device_id) is held:
                    del self._sessions[opening.device_id]
                if not self._sessions:
                    self._drained.set()
                log.info(
                    "a device's control channel closed",
                    extra={"device": str(session.device_id), "ended": ended, **session.about()},
                )

    async def establish(self, session: Session, hello: DeviceHello) -> bool:
        """Keep what ``hello`` says the device is, if the registry still holds it under the
        registration its connection opened with, then note the connection as the device's
        newest; whether the registry still holds it. A connection that this one replaces
        hears of it."""
        async with self._store.transaction() as store:
            if not await store.describe(session.device_id, session.registered, hello.description):
                return False
        replaced = await self._presence.arrive(session.device_id, session.connection)
        session.established = True
        if replaced is not None:
            await hint(self._hints, [session.device_id])
        return True

    async def run(self, stop: asyncio.Event) -> None:
        """Hear the hints, and check the connections held here every ``check_interval``,
        until ``stop`` is set. Then wait, up to ``close_timeout``, for the sessions still
        ending to forget their presence, such as those the server cut short as it stopped:
        what is closed after this, such as Redis, would otherwise leave it to lapse."""
        try:
            async with asyncio.TaskGroup() as tasks:
                listening = tasks.create_task(self._listen())
                checking = tasks.create_task(self._check_every())
                await stop.wait()
                listening.cancel()
                checking.cancel()
        finally:
            for check in list(self._checks):
                check.cancel()
            await asyncio.gather(*self._checks, return_exceptions=True)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.settings.close_timeout):
                    await self._drained.wait()

    async def going_away(self) -> None:
        """Refuse new connections, and ask each device connected here to connect again, to
        another replica or to this one restarted (the cloud link, Staying connected): its
        session sends ``reconnect`` and nothing more."""
        self._stopping = True
        sessions = self._held()
        for session in sessions:
            session.end(RECONNECT)
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self.settings.close_timeout):
                for session in sessions:
                    await session.closed.wait()

    # Checking the connections held here.

    async def _listen(self) -> None:
        while True:
            try:
                async for device_id in self._hints.listen():
                    held = self._sessions.get(device_id)
                    if held:
                        self._spawn(self._check(list(held)))
            except Exception:
                log.warning(
                    "the devices' hints cannot be heard: their checks find what they would say",
                    exc_info=True,
                )
            await asyncio.sleep(self.settings.check_interval)

    async def _check_every(self) -> None:
        while True:
            await asyncio.sleep(self.settings.check_interval)
            try:
                await self._check(self._held())
            except Exception:
                log.warning("the control channels could not be checked", exc_info=True)

    async def _check(self, sessions: Collection[Session]) -> None:
        """End those of ``sessions`` whose device the registry no longer holds under the
        registration their connection opened with, with ``bye`` ``revoked``, and those whose
        connection a newer one of the device replaced, with ``bye`` ``replaced``."""
        if not sessions:
            return
        async with self._store.transaction() as store:
            registered = await store.registrations({session.device_id for session in sessions})
        standing: list[Session] = []
        for session in sessions:
            if registered.get(session.device_id) != session.registered:
                session.end(Bye.REVOKED)
            elif session.established:
                standing.append(session)
        replaced = await self._presence.keep(
            [(session.device_id, session.connection) for session in standing]
        )
        for session in standing:
            if (session.device_id, session.connection) in replaced:
                session.end(Bye.REPLACED)
            elif session.left:
                # It closed while its presence was kept, which may have noted it again.
                await self._leave(session)

    async def _leave(self, session: Session) -> None:
        try:
            await self._presence.leave(session.device_id, session.connection)
        except Exception:
            # Its presence lapses after presence_ttl, as a stopped replica's does.
            log.warning("a closed connection's presence stays until it lapses", exc_info=True)

    def _held(self) -> list[Session]:
        return [session for held in self._sessions.values() for session in held]

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._checks.add(task)
        task.add_done_callback(self._checked)

    def _checked(self, task: asyncio.Task[None]) -> None:
        self._checks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning(
                "a device's connections could not be checked: the next check will",
                exc_info=task.exception(),
            )


class Session:
    """One connection's session: the device's hello and the cloud's, then what each sends,
    until one side ends it or the connection is lost.

    Three tasks carry it once open, as the robot's do on its side: one reads, at the rate the
    cloud takes, numbers each message, answers pings at once and queues the rest; one handles
    what is queued; one writes, in order, what the session sends. The rate counts every
    message, pings included, before it is read; up to the backlog wait their turn, past which
    the cloud drops what comes. A device that takes nothing for two ping intervals seems gone,
    as one that sends nothing does: what waits for it is then bounded by the rate.
    """

    def __init__(self, links: Links, channel: Channel, opening: Opening) -> None:
        self._links = links
        self._settings = links.settings
        self._channel = channel
        self.device_id = opening.device_id
        self.registered = opening.registered
        # This connection, as the presence names it: a session may outlive it.
        self.connection = uuid.uuid7()
        self.id: UUID | None = None
        self.hello: DeviceHello | None = None
        self.established = False
        self.left = False
        # Why the cloud ends the session, once it does: its bye's reason, or reconnect.
        self.ending: str | None = None
        self.closed = asyncio.Event()
        self.received = 0  # the number of the last message taken from the device
        self.sent = 0  # the number of the last message the cloud sent
        self._ended = asyncio.Event()
        self._outbox: asyncio.Queue[str | Close] = asyncio.Queue()
        self._inbox: asyncio.Queue[tuple[int, dict[str, Any]]] = asyncio.Queue(
            maxsize=self._settings.backlog
        )
        self._bucket = Bucket(self._settings.rate, self._settings.burst)
        self._dropping = False
        self._noted: set[str] = set()

    def end(self, reason: str) -> None:
        """End the session from the cloud's side: with ``bye`` and ``reason``, or with
        ``reconnect``."""
        if self.ending is None:
            self.ending = reason
            self._ended.set()

    def about(self) -> dict[str, str]:
        """What the logs say of the session."""
        return {} if self.id is None else {"session": str(self.id)}

    async def run(self) -> str:
        """Hold the session until it ends; how it ended."""
        try:
            return await self._run()
        except Exception:
            return await self._fail()
        finally:
            self.closed.set()

    async def _fail(self) -> str:
        """End the session after the cloud's own failure: ``bye`` ``error``, then close with
        1011, and the device connects again (The session, bye)."""
        log.exception(
            "a device's session failed", extra={"device": str(self.device_id), **self.about()}
        )
        # Within close_timeout, as any close: the connection may be what failed, or its
        # device may read nothing.
        with contextlib.suppress(Exception):
            async with asyncio.timeout(self._settings.close_timeout):
                await self._channel.send(link.encode(link.bye(Bye.ERROR)))
        with contextlib.suppress(Exception):
            async with asyncio.timeout(self._settings.close_timeout):
                await self._channel.close(Close.INTERNAL)
        return str(Bye.ERROR)

    async def _run(self) -> str:
        first = await self._first()
        if isinstance(first, str):
            return first
        if first.get("type") == "bye":
            await self._channel.close(Close.NORMAL)
            return DEVICE_BYE
        hello = link.read_hello(first, self._links.schema)
        if isinstance(hello, Bye):
            return await self._refuse(hello)
        try:
            registered = await self._links.establish(self, hello)
        except Exception:
            log.warning("a device's session could not start", exc_info=True)
            return await self._refuse(Bye.ERROR)
        if not registered:
            return await self._refuse(Bye.REVOKED)
        self.hello = hello
        self.id = uuid.uuid7()
        if self.ending == RECONNECT:
            await self._channel.close(Close.RESTART)
            return RECONNECT
        if self.ending is not None:
            return await self._refuse(Bye(self.ending))
        log.info(
            "a device's control channel opened",
            extra={"device": str(self.device_id), **self.about()},
        )
        hello_back = link.cloud_hello(
            self.id, hello, datetime.now(UTC), self._settings.ping_interval
        )
        self._outbox.put_nowait(link.encode(hello_back))
        if hello.conversation is not None:
            # The cloud holds none: the device ends it.
            ended = {"type": "conversation.end", "conversation": str(hello.conversation)}
            self._send({**ended, "reason": "lost"})
        return await self._carry()

    async def _first(self) -> dict[str, Any] | str:
        """The device's first message, which should be its hello; how the session ended if
        none came."""
        receiving = asyncio.create_task(self._channel.receive())
        ending = asyncio.create_task(self._ended.wait())
        try:
            done, _ = await asyncio.wait(
                {receiving, ending},
                timeout=self._settings.hello_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (receiving, ending):
                task.cancel()
            await asyncio.gather(receiving, ending, return_exceptions=True)
        if ending in done:
            if self.ending == RECONNECT:
                await self._channel.close(Close.RESTART)
                return RECONNECT
            return await self._refuse(Bye(self.ending))
        if receiving not in done:
            return await self._refuse(Bye.NO_HELLO)
        frame = receiving.result()
        if frame is None:
            return CLOSED
        try:
            message = (
                read_json(frame, self._settings.max_json_depth) if isinstance(frame, str) else None
            )
        except ValueError:
            message = None
        if not isinstance(message, dict):
            return await self._refuse(Bye.NO_HELLO)
        return message

    async def _refuse(self, reason: Bye) -> str:
        """End the session before it is carried: a bye, then close."""
        await self._channel.send(link.encode(link.bye(reason)))
        await self._channel.close(Close.NORMAL)
        return str(reason)

    async def _carry(self) -> str:
        """Carry the session until one side ends it, or its connection is lost or falls
        silent; how it ended."""
        writing = asyncio.create_task(self._write())
        handling = asyncio.create_task(self._handle_all())
        reading = asyncio.create_task(self._read())
        ending = asyncio.create_task(self._ended.wait())
        tasks = {writing, handling, reading, ending}
        close = Close.NORMAL
        try:
            await asyncio.wait({reading, ending}, return_when=asyncio.FIRST_COMPLETED)
            handling.cancel()
            reading.cancel()
            if self.ending == RECONNECT:
                ended, close = RECONNECT, Close.RESTART
                self._outbox.put_nowait(link.encode({"type": "reconnect"}))
            elif self.ending == STALLED:
                # What waits would not leave.
                ended, close = STALLED, Close.GOING_AWAY
            elif self.ending is not None:
                ended = self.ending
                self._outbox.put_nowait(link.encode(link.bye(Bye(self.ending))))
            elif (failure := reading.exception()) is not None:
                log.error(
                    "a device's session failed",
                    exc_info=failure,
                    extra={"device": str(self.device_id), **self.about()},
                )
                ended, close = str(Bye.ERROR), Close.INTERNAL
                self._outbox.put_nowait(link.encode(link.bye(Bye.ERROR)))
            else:
                ended = reading.result()
                if ended == SILENT:
                    close = Close.GOING_AWAY
            self._outbox.put_nowait(close)
            # What was sent before leaves first, within close_timeout.
            await asyncio.wait({writing}, timeout=self._settings.close_timeout)
            return ended
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Closed already, unless what waited did not leave: a device that reads nothing
            # would hold this close too.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._settings.close_timeout):
                    await self._channel.close(close)

    # The three tasks.

    async def _write(self) -> None:
        patience = link.SILENT_PINGS * self._settings.ping_interval
        while True:
            item = await self._outbox.get()
            if isinstance(item, Close):
                await self._channel.close(item)
                return
            try:
                async with asyncio.timeout(patience):
                    await self._channel.send(item)
            except TimeoutError:
                self.end(STALLED)
                return

    async def _read(self) -> str:
        silence = link.SILENT_PINGS * self._settings.ping_interval
        while True:
            try:
                async with asyncio.timeout(silence):
                    frame = await self._channel.receive()
            except TimeoutError:
                return SILENT
            if frame is None:
                return CLOSED
            await self._bucket.take()
            if isinstance(frame, bytes):
                self._note("binary", "a device sent binary frames: they are ignored")
                continue
            ended = self._take(frame)
            if ended is not None:
                return ended

    async def _handle_all(self) -> None:
        while True:
            seq, message = await self._inbox.get()
            if self._inbox.empty():
                self._dropping = False
            try:
                self._handle(seq, message)
            except Exception:
                log.exception("a device's message could not be handled", extra=self.about())
                self._error(seq, Code.INTERNAL, "the cloud failed to handle the message")

    # Reading.

    def _take(self, text: str) -> str | None:
        """Take one message from the device; how the session ends, if it does."""
        try:
            message = read_json(text, self._settings.max_json_depth)
        except ValueError:
            self._note(
                "unreadable", "a device sent messages the cloud cannot read: they are ignored"
            )
            return None
        kind = message.get("type") if isinstance(message, dict) else None
        # A type that is no text, such as a list, is a numbered message's: the schema fails it.
        if isinstance(kind, str) and kind in link.UNNUMBERED:
            return self._unnumbered(kind, message)
        seq = link.seq_of(message)
        if seq is None:
            # No number for an error to answer (Errors).
            self._note("seq", "a device sent messages without a number: they are ignored")
            return None
        match link.number(self.received, seq):
            case Number.DUPLICATE:
                return None
            case Number.SKIPPED:
                self.end(Bye.OUT_OF_SEQUENCE)
                return str(Bye.OUT_OF_SEQUENCE)
        self.received = seq
        try:
            self._inbox.put_nowait((seq, message))
        except asyncio.QueueFull:
            self._drop(seq)
        return None

    def _unnumbered(self, kind: str, message: dict[str, Any]) -> str | None:
        if kind == "bye":
            return DEVICE_BYE
        if kind != "ping":
            self._note(kind, f"a device sent {kind} messages in its session: they are ignored")
            return None
        if self._links.schema.problem(message) is not None:
            # A side that cannot handle a ping ends the session (Errors).
            self.end(Bye.ERROR)
            return str(Bye.ERROR)
        self._outbox.put_nowait(link.encode({"type": "pong", "received": self.received}))
        return None

    def _drop(self, seq: int) -> None:
        """Drop the message ``seq``: as many as the backlog holds wait their turn. The device
        hears of it once until the backlog empties again."""
        if self._dropping:
            return
        self._dropping = True
        log.warning("a device sends more than the cloud takes: some messages are dropped")
        self._error(seq, Code.RATE_LIMITED, "too many messages; some were dropped")

    # Handling.

    def _handle(self, seq: int, message: dict[str, Any]) -> None:
        problem = self._links.schema.problem(message)
        if problem is not None:
            self._error(seq, Code.INVALID_MESSAGE, problem)
            return
        kind = message["type"]
        if kind == "device.changed" and self.hello is not None:
            self.hello = self.hello.changed(message)
        elif kind == "conversation.request":
            self._error(seq, Code.UNAVAILABLE, "the cloud holds no conversations")
        else:
            # Calls and events come with the modules that make and take them; a conversation
            # the cloud does not hold ended as its end crossed; and a type the cloud does not
            # know comes from a newer minor version (Messages, Unknown content). The first
            # type is logged: the device chooses how many come.
            self._note(
                "type", "a device sent messages of a type the cloud does not take", type=kind
            )

    # Sending.

    def _send(self, message: dict[str, Any]) -> None:
        """Number ``message``, and send it after what was sent before."""
        self.sent += 1
        self._outbox.put_nowait(link.encode({"type": message["type"], "seq": self.sent, **message}))

    def _error(self, reply_to: int, code: Code, text: str) -> None:
        self._send(link.error(reply_to, code, text))

    def _note(self, key: str, text: str, **about: str) -> None:
        """Log ``text``, with ``about``, once in the session for each ``key``. The keys are
        the cloud's own, or the unnumbered types: what one session logs and keeps of them is
        bounded, whatever its device sends."""
        if key not in self._noted:
            self._noted.add(key)
            log.info(text, extra={"device": str(self.device_id), **self.about(), **about})


class Bucket:
    """A token bucket: ``rate`` messages a second, in bursts of up to ``burst``."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._at = time.monotonic()

    async def take(self) -> None:
        """Wait for a token, then use it."""
        while True:
            now = time.monotonic()
            self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
            self._at = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
            await asyncio.sleep((1 - self._tokens) / self.rate)
