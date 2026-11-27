# SPDX-License-Identifier: MIT
"""The devices' control channels: each connection's session, from the device's hello to its
end, and what a replica of the api does for the sessions it holds (the cloud link 3.7,
platform/protocols/cloud/README.md; docs/decisions/0011-control-channels.md and
docs/decisions/0013-resuming-sessions.md).

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

A session outlives its connection (Resuming a session). Its connection keeps its numbers and
what it sent, and holds its record in Redis, which every replica reads. A connection lost, or
gone silent, gives it up for ``resume_window``; a ``bye``, or a close that its sender would
cause again, ends it for good. A newer connection whose hello resumes it claims it: the older
one, wherever it is held, hears of it as of any newer connection, handles nothing more, hands
the session over and closes with 4000, "taken over", and no ``bye``. Only then does the newer
one answer, with the last number handled, and send again what the device has not received.
"""

import asyncio
import contextlib
import dataclasses
import logging
import uuid
from collections import deque
from collections.abc import Collection, Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from ai_cloud.devices.domain import link
from ai_cloud.devices.domain.errors import RateLimited, Unauthenticated, Unavailable
from ai_cloud.devices.domain.link import (
    Bye,
    Claim,
    Close,
    Closed,
    Code,
    DeviceHello,
    Handing,
    Keeping,
    Message,
    Number,
    Record,
    Unanswerable,
)
from ai_cloud.devices.domain.model import Opening
from ai_cloud.devices.ports import Channel, Hints, Presence, Sessions, Store
from ai_cloud.devices.service.devices import DeviceTokens, forget_sessions, hint
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
INVALID_FRAME = "invalid_frame"  # a frame past WebSocket's own rules closed the connection
TAKEN_OVER = "taken_over"  # a newer connection of the device resumed the session
# The ends that leave the session to resume: the others end it for good (Resuming a session).
PAUSES = frozenset({CLOSED, SILENT, STALLED, RECONNECT, TAKEN_OVER})
# Why a resume got a new session, besides those that its claim says (Claim).
OUT_OF_RANGE = "received"  # the device received what the cloud did not send, or kept no more
NOT_HANDED = "not_handed"  # the connection that held it did not hand it over in time


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
        sessions: Sessions,
        hints: Hints,
        connection_rate: RateLimiter,
    ) -> None:
        self.settings = settings
        self.schema: Schema = protocol_schema(link.PROTOCOL, "from_device")
        self._leeway = leeway
        self._tokens = tokens
        self._store = store
        self._presence = presence
        self.records = sessions
        self._hints = hints
        self._connection_rate = connection_rate
        # Connections this replica takes, from all devices together.
        self._connections = TokenBucket(settings.connect_rate, settings.connect_burst)
        # The sessions held here, by device: one, or two while a newer one replaces another.
        self._sessions: dict[UUID, set[Session]] = {}
        # What this replica takes from each device, kept until it is whole again.
        self._budgets: dict[UUID, Budget] = {}
        # The hellos waiting for a session of their device to be handed over, woken by hints.
        self._waiting: dict[UUID, set[asyncio.Event]] = {}
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
                    # Within its end's close_timeout too: uvicorn cuts the connection
                    # only once this returns.
                    await self._forget(session, by=session.closing_by())
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
        registration its connection opened with; then give the connection its session, the
        one that ``hello`` resumes if the cloud can resume it, or a new one, and note the
        connection as the device's newest. Whether the registry still holds the device. A
        connection that this one replaces hears of it."""
        async with self._store.transaction() as store:
            if not await store.describe(session.device_id, session.registered, hello.description):
                return False
        resumed = None if hello.resume is None else await self._resume(session, hello)
        if resumed is Claim.SUPERSEDED:
            # A newer connection of the device claimed the session meanwhile: it goes on there.
            session.end(TAKEN_OVER)
            return True
        record = resumed or Record(
            uuid.uuid7(), session.device_id, session.registered, hello.terms, hello.session_minor
        )
        if resumed is None:
            await self.records.start(record, session.connection)
        session.begin(record, hello, resumed=resumed is not None)
        # A newer connection's claim may have come before this one held the session, and its
        # hint with it: what it would have found, this finds.
        kept = await self.records.keep([(session.device_id, session.connection, record.session)])
        match kept[session.connection]:
            case Keeping.TAKEN:
                session.end(TAKEN_OVER)
            case Keeping.REPLACED:
                # A newer session of the device began meanwhile: the presence is its own.
                session.end(Bye.REPLACED)
                return True
        replaced = await self._presence.arrive(session.device_id, session.connection)
        session.established = True
        if replaced is not None:
            await hint(self._hints, [session.device_id])
        return True

    async def _resume(self, session: Session, hello: DeviceHello) -> Record | Claim | None:
        """The session that ``hello`` resumes, if the cloud can resume it, once the connection
        that held it, if one still does, handed it over; None for a new one (Resuming a
        session). Only a session of the device, under the registration its connection opened
        with (SEC-6), with the terms of its first hello and a version the cloud still speaks,
        that kept all that the device has not received. ``SUPERSEDED`` if a newer connection of
        the device claimed it meanwhile."""
        resume = hello.resume
        assert resume is not None
        woken = asyncio.Event()
        waiting = self._waiting.setdefault(session.device_id, set())
        waiting.add(woken)
        try:
            claimed = await self.records.claim(
                session.device_id,
                session.connection,
                resume.session,
                session.registered,
                hello.terms,
                link.MINOR,
            )
            if claimed is Claim.WAITING:
                # The connection that holds it hears of it as of any newer one.
                await hint(self._hints, [session.device_id])
                claimed = await self._taken(session, hello, woken)
        finally:
            waiting.discard(woken)
            if not waiting and self._waiting.get(session.device_id) is waiting:
                del self._waiting[session.device_id]
        if claimed is Claim.SUPERSEDED:
            return claimed
        if isinstance(claimed, Record):
            if claimed.sent - len(claimed.kept) <= resume.received <= claimed.sent:
                return claimed
            claimed = OUT_OF_RANGE
        log.info(
            "a device's session could not be resumed: it gets a new one",
            extra={"device": str(session.device_id), "why": str(claimed)},
        )
        return None

    async def _taken(
        self, session: Session, hello: DeviceHello, woken: asyncio.Event
    ) -> Record | Claim | str:
        """The session that ``hello`` claimed, once the connection that held it handed it
        over, as a hint about the device says, within ``takeover_timeout``: a lost hint is
        made up for at its end, whose try withdraws the claim. Why not otherwise."""
        assert hello.resume is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.takeover_timeout
        while True:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout_at(deadline):
                    await woken.wait()
            woken.clear()
            last = loop.time() >= deadline
            taken = await self.records.take(
                session.device_id,
                session.connection,
                hello.resume.session,
                session.registered,
                hello.terms,
                last=last,
            )
            if taken is not Claim.WAITING:
                return taken
            if last:
                return NOT_HANDED

    async def give_up(
        self, session: Session, record: Record, *, pause: bool, by: float
    ) -> Handing | None:
        """Give up ``record``, the session of ``session`` as it ends, by the loop's time
        ``by``: after a pause, to the newer connection that claimed it, which hears of it, or
        for ``resume_window``; for good otherwise. How it went, after a pause. What Redis has
        not answered by then raises ``TimeoutError``, and the record lapses."""
        async with asyncio.timeout_at(by):
            if not pause:
                await self.records.end(record.device_id, session.connection, record.session)
                return None
            handing = await self.records.hand(record, session.connection)
        if handing is Handing.HANDED:
            # Handed all the same: a hint that does not leave in time leaves the newer
            # connection to find it at its last try.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout_at(by):
                    await hint(self._hints, [record.device_id])
        return handing

    async def remove(self, session: Session) -> bool:
        """Remove the device of ``session`` from its home, as removing it in the app does, if
        the registry still holds it under the registration the session opened with; whether
        it did. The cloud forgets its key and its session, and the device's other connections
        hear of it."""
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
            await forget_sessions(self.records, [session.device_id])
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
                    # A hello that resumes the device's session may wait for its hand-over.
                    for woken in self._waiting.get(device_id, ()):
                        woken.set()
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
        registration their connection opened with, with ``bye`` ``revoked``; those whose
        session a newer connection of the device resumes, which hand it over and close with
        4000; and those whose connection a newer one of the device replaced, with ``bye``
        ``replaced``. The sessions that their connections hold last another lease."""
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
        holding = [session for session in standing if session.holding]
        kept = await self.records.keep(
            [(session.device_id, session.connection, session.id) for session in holding]
        )
        for session in holding:
            match kept[session.connection]:
                case Keeping.TAKEN:
                    session.end(TAKEN_OVER)
                case Keeping.REPLACED:
                    session.end(Bye.REPLACED)
        replaced = await self._presence.keep(
            [(session.device_id, session.connection) for session in standing]
        )
        for session in standing:
            if (session.device_id, session.connection) in replaced:
                session.end(Bye.REPLACED)
            elif session.finished:
                # It closed while its presence was kept, which may have noted it again.
                await self._forget(session)

    async def _forget(self, session: Session, by: float | None = None) -> None:
        """Leave the presence of ``session``, which closed, by the loop's time ``by`` if
        given."""
        try:
            async with asyncio.timeout_at(by):
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

    The connection holds its session, a new one or the one its hello resumes, until it ends:
    the last number taken from the device, and the last one handled, which its pongs say; and
    what it sent that the device has not said it received, up to ``replay``. A session that
    pauses, its connection lost, silent or taken over, handles the message in hand, if any,
    and nothing more, then gives up all that for the device to resume it. A dropped message
    counts as handled: the cloud answered it, or the burst it came in, with ``rate_limited``.
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
        # The session's version, which its first hello set, and whether this hello resumed it.
        self.minor = 0
        self.resumed = False
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
        self.taken = 0  # the number of the last message taken from the device
        self.sent = 0  # the number of the last message the cloud sent
        # What the cloud sent that the device has not said it received, as it was sent; and the
        # numbers of the messages taken that wait their turn, the first while it is handled.
        self._kept: deque[str] = deque(maxlen=self._settings.replay)
        self._queued: deque[int] = deque()
        # The session, as this connection holds it once it does, until it gives it up.
        self._record: Record | None = None
        self._settled = False
        # Set once the session pauses, which handles nothing more; while a message is handled;
        # and once one had to be stopped, after which nothing resumes the session.
        self._halted = False
        self._busy = False
        self._torn = False
        # When the session's end must be over, once it began (closing_by).
        self._deadline: float | None = None
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
        elif self.ending in PAUSES and reason not in PAUSES:
            # Ended for good as it pauses, by the leave in hand or a check: nothing resumes it.
            self.ending, self._farewell = reason, message

    def about(self) -> dict[str, str]:
        """What the logs say of the session."""
        return {} if self.id is None else {"session": str(self.id)}

    @property
    def received(self) -> int:
        """The number of the last message from the device that the cloud handled, as all
        before it: what its pongs, and a hello that resumes the session, say."""
        return self._queued[0] - 1 if self._queued else self.taken

    @property
    def holding(self) -> bool:
        """Whether this connection holds its session, which it keeps until it gives it up."""
        return self._record is not None and not self._settled

    def begin(self, record: Record, hello: DeviceHello, *, resumed: bool) -> None:
        """Hold ``record`` as the session that ``hello`` gets: the one it resumes, with what
        was kept of it, or a new one. What the device says it received, it needs no more."""
        self._record = record
        self.id = record.session
        self.minor = record.minor
        self.hello = hello
        self.resumed = resumed
        self.taken = record.received
        self.sent = record.sent
        kept = record.kept
        if resumed and hello.resume is not None:
            kept = kept[len(kept) - (record.sent - hello.resume.received) :]
        self._kept.extend(kept)

    async def run(self) -> str:
        """Hold the session until it ends; how it ended. A session that its connection holds
        is given up as it ends, if nothing did before."""
        try:
            try:
                ended = await self._run()
            except Exception:
                ended = await self._fail()
            await self._settle(ended)
            return ended
        finally:
            self.closed.set()

    async def _settle(self, ended: str) -> Handing | None:
        """Give the session up as it ends, once: for its device to resume it, after a pause
        that stopped no message, and for good otherwise; how it was handed over, after a
        pause. One that cannot be given up lapses, and its device gets a new one."""
        if self._record is None or self._settled:
            return None
        self._settled = True
        record = dataclasses.replace(
            self._record, received=self.received, sent=self.sent, kept=tuple(self._kept)
        )
        try:
            return await self._links.give_up(
                self, record, pause=ended in PAUSES and not self._torn, by=self.closing_by()
            )
        except Exception:
            log.warning(
                "a device's session could not be given up: it lapses",
                exc_info=True,
                extra={"device": str(self.device_id), **self.about()},
            )
            return None

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
        deadline = self.closing_by()
        missed = Exception if failed else TimeoutError
        if bye is not None:
            with contextlib.suppress(missed):
                async with asyncio.timeout_at(deadline):
                    await self._channel.send(link.encode(bye))
        with contextlib.suppress(missed):
            async with asyncio.timeout_at(deadline):
                await self._channel.close(close)

    def closing_by(self) -> float:
        """When the session's end must be over: one ``close_timeout`` from when it began,
        for the message in hand, giving the session up, the close and leaving its presence
        together, so that a device that reads nothing holds its connection no longer
        (ADR-0012, decision 2)."""
        if self._deadline is None:
            self._deadline = asyncio.get_running_loop().time() + self._settings.close_timeout
        return self._deadline

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
        if self.ending == RECONNECT:
            await self._close(Close.RESTART)
            return RECONNECT
        if self.ending == TAKEN_OVER:
            # Handed over, or claimed by a newer connection before this one held it.
            if self._record is None or await self._settle(TAKEN_OVER) is Handing.HANDED:
                await self._close(Close.TAKEN_OVER)
                return TAKEN_OVER
            return await self._refuse(Bye.REPLACED)
        if self.ending is not None:
            return await self._refuse(Bye(self.ending))
        assert self.id is not None
        log.info(
            "a device's control channel opened",
            extra={"device": str(self.device_id), **self.about(), "resumed": self.resumed},
        )
        hello_back = link.cloud_hello(
            self.id,
            self.minor,
            datetime.now(UTC),
            self._settings.ping_interval,
            self.received if self.resumed else None,
        )
        self._outbox.put_nowait(link.encode(hello_back))
        # What the device did not receive, again, in order and with its numbers.
        for text in self._kept:
            self._outbox.put_nowait(text)
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
        silent; how it ended. The session is given up before the close: one taken over closes
        with 4000 only once its newer connection has it, and with ``bye`` ``replaced``
        otherwise, for that one then gets a new session."""
        writing = asyncio.create_task(self._write())
        handling = asyncio.create_task(self._handle_all())
        reading = asyncio.create_task(self._read())
        ending = asyncio.create_task(self._ended.wait())
        tasks = {writing, handling, reading, ending}
        close = Close.NORMAL
        try:
            await asyncio.wait({reading, ending}, return_when=asyncio.FIRST_COMPLETED)
            # The message in hand, giving the session up and the close, all within it.
            deadline = self.closing_by()
            reading.cancel()
            bye: Bye | None = None
            if self.ending is not None:
                ended = self.ending
                if ended not in (RECONNECT, STALLED, TAKEN_OVER):
                    bye = Bye(ended)
            elif (failure := reading.exception()) is not None:
                log.error(
                    "a device's session failed",
                    exc_info=failure,
                    extra={"device": str(self.device_id), **self.about()},
                )
                ended, bye, close = str(Bye.ERROR), Bye.ERROR, Close.INTERNAL
            else:
                ended = reading.result()
            if ended in PAUSES:
                await self._halt(handling)
                if self.ending is not None and self.ending not in PAUSES:
                    # The leave in hand, or a check, ended it for good meanwhile.
                    ended, bye = self.ending, Bye(self.ending)
            else:
                handling.cancel()
            if await self._settle(ended) is not Handing.HANDED and ended == TAKEN_OVER:
                ended, bye = str(Bye.REPLACED), Bye.REPLACED
            if ended == RECONNECT:
                close = Close.RESTART
                self._outbox.put_nowait(link.encode({"type": "reconnect"}))
            elif ended == TAKEN_OVER:
                # The newer connection sends again what this one did not.
                close = Close.TAKEN_OVER
                while not self._outbox.empty():
                    self._outbox.get_nowait()
            elif ended in (STALLED, SILENT):
                # What waits would not leave, or the device seems gone.
                close = Close.GOING_AWAY
            elif bye is not None:
                self._outbox.put_nowait(link.encode(link.bye(bye, self._farewell)))
            self._outbox.put_nowait(close)
            # What was sent before leaves first, then the close.
            await asyncio.wait(
                {writing}, timeout=max(0, deadline - asyncio.get_running_loop().time())
            )
            return ended
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Closed already, unless what waited did not leave: a device that reads nothing
            # would hold this close too.
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout_at(self.closing_by()):
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
        # What waits once the session ends is not handled: after a leave, for one, or once it
        # pauses.
        while self.ending is None and not self._halted:
            seq, message = await self._inbox.get()
            if self.ending is not None or self._halted:
                return
            if self._inbox.empty():
                self._dropping = False
            self._busy = True
            try:
                await self._handle(seq, message)
            except Exception:
                log.exception("a device's message could not be handled", extra=self.about())
                self._error(seq, Code.INTERNAL, "the cloud failed to handle the message")
            finally:
                self._busy = False
            self._queued.popleft()

    async def _halt(self, handling: asyncio.Task[None]) -> None:
        """Stop handling as the session pauses, once the message in hand, if any, is handled,
        within the end's ``close_timeout``: one stopped in its middle tears the session, which
        nothing then resumes."""
        self._halted = True
        if not self._busy:
            handling.cancel()
        left = self.closing_by() - asyncio.get_running_loop().time()
        await asyncio.wait({handling}, timeout=max(0, left))
        if not handling.done():
            handling.cancel()
            self._torn = True

    # Reading.

    def _take(self, frame: str | bytes) -> str | None:
        """Take one frame from the device; how the session ends, if it does."""
        message = link.read_message(frame)
        if isinstance(message, Unanswerable):
            return self._unanswerable(message)
        if message.seq is None:
            return self._unnumbered(message)
        match link.number(self.taken, message.seq):
            case Number.DUPLICATE:
                return None
            case Number.SKIPPED:
                self.end(Bye.OUT_OF_SEQUENCE)
                return str(Bye.OUT_OF_SEQUENCE)
        self.taken = message.seq
        try:
            self._inbox.put_nowait((message.seq, message.body))
        except asyncio.QueueFull:
            self._drop(message.seq)
        else:
            self._queued.append(message.seq)
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
        # What the device received, it needs no more.
        while self._kept and self.sent - len(self._kept) < message.body["received"]:
            self._kept.popleft()
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
        elif kind == "leave" and self.minor >= link.LEAVE_MINOR:
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
        """Number ``message``, and send it after what was sent before, keeping it until the
        device says it received it."""
        self.sent += 1
        text = link.encode({"type": message["type"], "seq": self.sent, **message})
        self._kept.append(text)
        self._outbox.put_nowait(text)

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
    """How a session ended whose connection closed: a message too large, text that is no UTF-8,
    frames past the link's rules and a frame past WebSocket's own end it as ``invalid_message``
    would, whichever side closed, for their sender would send them again on a resumed one
    (Messages: Sizes, Validation since 3.5 and 3.6, and Frames). Any other close leaves the
    session to resume (Resuming a session)."""
    match closed.code:
        case Close.PROTOCOL_ERROR:
            return INVALID_FRAME
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
