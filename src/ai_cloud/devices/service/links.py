# SPDX-License-Identifier: MIT
"""The devices' control channels: each connection's session, from the device's hello to its
end, and what a replica of the api does for the sessions it holds (the cloud link 3.5,
platform/protocols/cloud/README.md; docs/decisions/0011-control-channels.md).

A connection opens once the device's access token verifies and the registry holds the
device, within what a replica takes from all devices and what it takes from each
(docs/decisions/0012-what-one-device-can-exhaust.md): an attempt without a valid token
spends none of it. Its session starts with the two
hellos: the device's says what it is, which the registry keeps, and the connection becomes
the device's newest in the presence, which every replica reads. A session then ends with a
``bye``, from either side, or when its connection is lost or falls silent.

Whatever may end a session elsewhere, the device's removal, its home's deletion or a newer
connection, sends a hint that reaches every replica, at most once. A replica checks the
connections it holds of that device against the registry and the presence, and does so for
all of them every ``check_interval``: so a lost hint delays a ``bye``, and never loses one.

A device leaves its home before its factory reset, in a session of 3.2: the cloud removes it
as removing it in the app does, then ends the session with ``bye`` ``left`` (leave; ai-platform
ADR-0015). The hint that the removal sends ends the device's other connections, not this one.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Collection, Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ai_cloud.devices.domain import link
from ai_cloud.devices.domain.errors import RateLimited, Unauthenticated, Unavailable
from ai_cloud.devices.domain.link import (
    Bye,
    Close,
    Closed,
    Code,
    DeviceHello,
    Message,
    Number,
    Unanswerable,
)
from ai_cloud.devices.domain.model import Opening
from ai_cloud.devices.ports import Channel, Hints, Presence, Store
from ai_cloud.devices.service.devices import DeviceTokens, hint
from ai_cloud.devices.settings import LinkSettings
from ai_cloud.shared.protocols import Schema, protocol_schema
from ai_cloud.shared.rates import RateLimiter, RatesUnavailable, TokenBucket

log = logging.getLogger(__name__)

# How a session ended, besides the reasons of the cloud's bye.
RECONNECT = "reconnect"  # the cloud went away, and asked the device to connect again
CLOSED = "closed"  # the connection was lost, or the device closed it
TOO_BIG = "too_big"  # a message too large closed the connection, as invalid_message would
DEVICE_BYE = "bye"  # the device ended the session
SILENT = "silent"  # nothing came for two ping intervals
STALLED = "stalled"  # nothing the cloud sent could leave for two ping intervals
FLOODED = "flooded"  # frames below the session broke the link's rules for them, and closed it
INVALID_TEXT = "invalid_text"  # text that was no UTF-8 closed the connection


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
        connection_rate: RateLimiter,
    ) -> None:
        self.settings = settings
        self.schema: Schema = protocol_schema(link.PROTOCOL, "from_device")
        self._leeway = leeway
        self._tokens = tokens
        self._store = store
        self._presence = presence
        self._hints = hints
        self._connection_rate = connection_rate
        # Connections this replica takes, from all devices together.
        self._connections = TokenBucket(settings.connect_rate, settings.connect_burst)
        # The sessions held here, by device: one, or two while a newer one replaces another.
        self._sessions: dict[UUID, set[Session]] = {}
        # What this replica takes from each device, kept until it is whole again.
        self._budgets: dict[UUID, Budget] = {}
        self._checks: set[asyncio.Task[None]] = set()
        self._stopping = False
        # Set while no session is held here, its presence forgotten.
        self._drained = asyncio.Event()
        self._drained.set()

    async def open(self, access_token: str | None) -> Opening:
        """A connection for the device whose access token this is, if the registry still
        holds it under the registration that the token was issued for. ``Unauthenticated``
        otherwise; ``RateLimited`` while the device connects too often; ``Unavailable`` once
        this replica goes away, also while the registry was read, while the device's count
        cannot be read, and with when to try again while it takes no more connections.

        The token's signature comes first, which needs no I/O: what the cloud counts, an
        attempt without a valid token never spends, so that no flood of them refuses a
        device. The device's own count comes before the replica's, which a device past its
        own then spends neither (ADR-0012)."""
        if self._stopping:
            raise Unavailable("this replica is going away")
        if access_token is None:
            raise Unauthenticated("a bearer access token is needed")
        holder = self._tokens.authenticate(access_token)
        try:
            wait = await self._connection_rate.wait(str(holder.device_id))
        except RatesUnavailable:
            raise Unavailable("the cloud cannot count connections now") from None
        if wait > 0:
            raise RateLimited(wait)
        wait = self._connections.take_now()
        if wait > 0:
            raise Unavailable("the cloud takes no more connections for now", wait)
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
        return Opening(device.id, device.home_id, device.create_time)

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
            session.finished = True
            try:
                if session.established:
                    await self._forget(session)
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

    async def remove(self, session: Session) -> bool:
        """Remove the device of ``session`` from its home, as removing it in the app does, if
        the registry still holds it under the registration the session opened with; whether
        it did. The cloud forgets its key, and the device's other connections hear of it."""
        async with self._store.transaction() as store:
            # The home first, then the device's first answers and the device itself, as the
            # app's removal and a home's deletion take them.
            await store.hold_home(session.home_id)
            device = await store.device(session.device_id)
            if device is None or device.create_time != session.registered:
                # Removed meanwhile, and maybe registered again: no longer this session's.
                return False
            # False if its home's deletion, which does not hold the home, came first.
            removed = await store.remove_device(session.home_id, session.device_id)
        if removed:
            log.info(
                "a device left its home",
                extra={"home": str(session.home_id), "device": str(session.device_id)},
            )
            await hint(self._hints, [session.device_id])
        return removed

    def budget(self, device_id: UUID) -> Budget:
        """What this replica takes from ``device_id``, which its sessions here share: one
        that connects again finds it as its last connection left it."""
        budget = self._budgets.get(device_id)
        if budget is None:
            budget = self._budgets[device_id] = Budget(self.settings)
        return budget

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
            self._sweep()
            try:
                await self._check(self._held())
            except Exception:
                log.warning("the control channels could not be checked", exc_info=True)

    def _sweep(self) -> None:
        """Forget the budgets of the devices that hold no session here, once they are whole:
        a new one would be the same."""
        for device_id, budget in list(self._budgets.items()):
            if device_id not in self._sessions and budget.full():
                del self._budgets[device_id]

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
                # One whose leave removes its device ends as the leave says, after it.
                if not session.leaving:
                    session.end(Bye.REVOKED)
            elif session.established:
                standing.append(session)
        replaced = await self._presence.keep(
            [(session.device_id, session.connection) for session in standing]
        )
        for session in standing:
            if (session.device_id, session.connection) in replaced:
                session.end(Bye.REPLACED)
            elif session.finished:
                # It closed while its presence was kept, which may have noted it again.
                await self._forget(session)

    async def _forget(self, session: Session) -> None:
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
    message, the hello and pings included, and the characters of its text, before it is
    read: reading costs as much as the text is long. It is the device's, which its sessions
    on this replica share. Up to the backlog wait their turn, past which the cloud drops what
    comes. A device that takes nothing for two ping intervals seems gone, as one that sends
    nothing does: what waits for it is then bounded by the rate.
    """

    def __init__(self, links: Links, channel: Channel, opening: Opening) -> None:
        self._links = links
        self._settings = links.settings
        self._channel = channel
        self.device_id = opening.device_id
        self.home_id = opening.home_id
        self.registered = opening.registered
        # This connection, as the presence names it: a session may outlive it.
        self.connection = uuid.uuid7()
        self.id: UUID | None = None
        self.hello: DeviceHello | None = None
        self.established = False
        # Set once the session is over, its connection closed.
        self.finished = False
        # Set while the device's leave removes it: the session then ends as the leave says.
        self.leaving = False
        # Why the cloud ends the session, once it does: its bye's reason, or reconnect; and
        # what its bye says of it, if anything.
        self.ending: str | None = None
        self._farewell: str | None = None
        self.closed = asyncio.Event()
        self.received = 0  # the number of the last message taken from the device
        self.sent = 0  # the number of the last message the cloud sent
        self._ended = asyncio.Event()
        self._outbox: asyncio.Queue[str | Close] = asyncio.Queue()
        self._inbox: asyncio.Queue[tuple[int, dict[str, Any]]] = asyncio.Queue(
            maxsize=self._settings.backlog
        )
        self._budget = links.budget(self.device_id)
        self._dropping = False
        self._noted: set[str] = set()

    def end(self, reason: str, message: str | None = None) -> None:
        """End the session from the cloud's side: with ``bye`` and ``reason``, and
        ``message`` for the device's logs, or with ``reconnect``."""
        if self.ending is None:
            self.ending = reason
            self._farewell = message
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
        # The connection may be what failed.
        await self._close(Close.INTERNAL, link.bye(Bye.ERROR), failed=True)
        return str(Bye.ERROR)

    async def _close(
        self, close: Close, bye: dict[str, Any] | None = None, *, failed: bool = False
    ) -> None:
        """Close the connection, after ``bye`` if there is one, within one ``close_timeout``
        for both, as every end of a session does: a device that reads nothing holds neither
        longer, and uvicorn cuts its connection once the application returns (ADR-0012).
        After the cloud's own failure, ``failed``, the close is tried whatever the ``bye``
        met."""
        deadline = asyncio.get_running_loop().time() + self._settings.close_timeout
        missed = Exception if failed else TimeoutError
        if bye is not None:
            with contextlib.suppress(missed):
                async with asyncio.timeout_at(deadline):
                    await self._channel.send(link.encode(bye))
        with contextlib.suppress(missed):
            async with asyncio.timeout_at(deadline):
                await self._channel.close(close)

    async def _run(self) -> str:
        first = await self._first()
        if isinstance(first, str):
            return first
        if first.type == "bye":
            await self._close(Close.NORMAL)
            return DEVICE_BYE
        hello = link.read_hello(first, self._links.schema)
        if isinstance(hello, Bye):
            return await self._refuse(hello, link.NOT_FIRST if hello is Bye.NO_HELLO else None)
        if isinstance(hello, Unanswerable):
            return await self._refuse(Bye.INVALID_MESSAGE, hello.why)
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
            await self._close(Close.RESTART)
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

    async def _first(self) -> Message | str:
        """The device's first message, which should be its hello; how the session ended if
        none came that the cloud can answer."""
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
                await self._close(Close.RESTART)
                return RECONNECT
            return await self._refuse(Bye(self.ending))
        if receiving not in done:
            return await self._refuse(Bye.NO_HELLO, link.NOT_IN_TIME)
        frame = receiving.result()
        if isinstance(frame, Closed):
            return _closed(frame)
        await self._budget.take(frame)
        first = link.read_message(frame)
        if isinstance(first, Unanswerable):
            return await self._refuse(Bye.INVALID_MESSAGE, first.why)
        return first

    async def _refuse(self, reason: Bye, message: str | None = None) -> str:
        """End the session before it is carried: a bye, then close."""
        await self._close(Close.NORMAL, link.bye(reason, message))
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
        deadline: float | None = None
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
                self._outbox.put_nowait(link.encode(link.bye(Bye(self.ending), self._farewell)))
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
            # What was sent before leaves first, then the close, all within close_timeout.
            deadline = asyncio.get_running_loop().time() + self._settings.close_timeout
            await asyncio.wait({writing}, timeout=self._settings.close_timeout)
            return ended
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Closed already, unless what waited did not leave: a device that reads nothing
            # would hold this close too.
            if deadline is None:
                deadline = asyncio.get_running_loop().time() + self._settings.close_timeout
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout_at(deadline):
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
            if isinstance(frame, Closed):
                return _closed(frame)
            await self._budget.take(frame)
            ended = self._take(frame)
            if ended is not None:
                return ended

    async def _handle_all(self) -> None:
        # What waits once the session ends is not handled: after a leave, for one.
        while self.ending is None:
            seq, message = await self._inbox.get()
            if self._inbox.empty():
                self._dropping = False
            try:
                await self._handle(seq, message)
            except Exception:
                log.exception("a device's message could not be handled", extra=self.about())
                self._error(seq, Code.INTERNAL, "the cloud failed to handle the message")

    # Reading.

    def _take(self, frame: str | bytes) -> str | None:
        """Take one frame from the device; how the session ends, if it does."""
        message = link.read_message(frame)
        if isinstance(message, Unanswerable):
            return self._unanswerable(message)
        if message.seq is None:
            return self._unnumbered(message)
        match link.number(self.received, message.seq):
            case Number.DUPLICATE:
                return None
            case Number.SKIPPED:
                self.end(Bye.OUT_OF_SEQUENCE)
                return str(Bye.OUT_OF_SEQUENCE)
        self.received = message.seq
        try:
            self._inbox.put_nowait((message.seq, message.body))
        except asyncio.QueueFull:
            self._drop(message.seq)
        return None

    def _unnumbered(self, message: Message) -> str | None:
        kind = message.type
        if kind == "bye":
            return DEVICE_BYE
        if (problem := self._links.schema.problem(message.body)) is not None:
            # A hello, a ping, a pong or a reconnect that fails has no number for an error to
            # answer (Messages, Validation).
            return self._unanswerable(Unanswerable(f"{kind}: {problem}"))
        if kind != "ping":
            # The hello came already, and the others are the cloud's to send.
            self._note(kind, f"a device sent {kind} messages in its session: they are ignored")
            return None
        self._outbox.put_nowait(link.encode({"type": "pong", "received": self.received}))
        return None

    def _unanswerable(self, frame: Unanswerable) -> str:
        """End the session over a frame that it cannot answer (Messages, Validation)."""
        self.end(Bye.INVALID_MESSAGE, frame.why)
        return str(Bye.INVALID_MESSAGE)

    def _drop(self, seq: int) -> None:
        """Drop the message ``seq``: as many as the backlog holds wait their turn. The device
        hears of it once until the backlog empties again."""
        if self._dropping:
            return
        self._dropping = True
        log.warning("a device sends more than the cloud takes: some messages are dropped")
        self._error(seq, Code.RATE_LIMITED, "too many messages; some were dropped")

    # Handling.

    async def _handle(self, seq: int, message: dict[str, Any]) -> None:
        problem = self._links.schema.problem(message)
        if problem is not None:
            self._error(seq, Code.INVALID_MESSAGE, problem)
            return
        kind = message["type"]
        if kind == "device.changed" and self.hello is not None:
            self.hello = self.hello.changed(message)
        elif kind == "conversation.request":
            self._error(seq, Code.UNAVAILABLE, "the cloud holds no conversations")
        elif (
            kind == "leave"
            and self.hello is not None
            and self.hello.session_minor >= link.LEAVE_MINOR
        ):
            await self._leave(seq)
        else:
            # Calls and events come with the modules that make and take them; a conversation
            # the cloud does not hold ended as its end crossed; and a type the cloud does not
            # know comes from a newer minor version (Messages, Unknown content), as leave
            # does in a session of 3.1. The first type is logged: the device chooses how many
            # come.
            self._note(
                "type", "a device sent messages of a type the cloud does not take", type=kind
            )

    async def _leave(self, seq: int) -> None:
        """Remove the device from its home, as its leave ``seq`` asks, then end the session
        with ``bye`` ``left``; with ``bye`` ``revoked`` if a removal in the app came first. A
        removal that fails is answered with ``error`` ``internal``, and the session goes on:
        the device may ask again (leave). The cloud holds no conversations, so none of the
        device's is left to end."""
        self.leaving = True
        try:
            left = await self._links.remove(self)
        except Exception:
            self.leaving = False
            log.exception(
                "a device could not leave its home",
                extra={"device": str(self.device_id), **self.about()},
            )
            self._error(seq, Code.INTERNAL, "the device could not be removed")
            return
        self.end(Bye.LEFT if left else Bye.REVOKED)

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


def _closed(closed: Closed) -> str:
    """How a session ended whose connection closed: a message too large, text that is no UTF-8
    and frames past their rules end it as ``invalid_message`` would, for their sender would
    send them again on a resumed one (Messages: Sizes, Validation since 3.5, and Frames)."""
    match closed.code:
        case Close.TOO_BIG:
            return TOO_BIG
        case Close.POLICY:
            return FLOODED
        case Close.INVALID_TEXT:
            return INVALID_TEXT
    return CLOSED


class Budget:
    """What a replica takes from one device: ``rate`` messages a second, in bursts of up to
    ``burst``, and ``text_rate`` characters of their text a second, in bursts of up to
    ``text_burst``."""

    def __init__(self, settings: LinkSettings) -> None:
        self._messages = TokenBucket(settings.rate, settings.burst)
        self._text = TokenBucket(settings.text_rate, settings.text_burst)

    async def take(self, frame: str | bytes) -> None:
        """Wait until ``frame`` may be read, then count it."""
        await self._messages.take()
        await self._text.take(len(frame))

    def full(self) -> bool:
        """Whether nothing taken from it is left to make up."""
        return self._messages.full() and self._text.full()
