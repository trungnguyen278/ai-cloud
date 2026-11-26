# SPDX-License-Identifier: MIT
"""A control channel's session, and a replica's checks of the sessions it holds, against
ports of the tests' own: what timing decides elsewhere, such as an end that comes while a
session starts, happens here when a test says (docs/decisions/0011-control-channels.md)."""

import asyncio
import contextlib
import dataclasses
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from link import DEEP, hello
from records import Held, Records

from ai_cloud.devices.domain.errors import RateLimited, Unauthenticated, Unavailable
from ai_cloud.devices.domain.link import MINOR, NOT_FIRST, Claim, Closed, Record
from ai_cloud.devices.domain.model import Description, Device, Opening, TokenHolder
from ai_cloud.devices.service.links import RECONNECT, TAKEN_OVER, Links
from ai_cloud.devices.settings import LinkSettings
from ai_cloud.shared import rates
from ai_cloud.shared.protocols import platform_dir
from ai_cloud.shared.rates import RatesUnavailable

pytestmark = pytest.mark.anyio

REGISTERED = datetime(2026, 11, 11, tzinfo=UTC)
HOME = uuid.uuid7()
SETTINGS = LinkSettings(
    hello_timeout=5,
    ping_interval=20,
    rate=1000,
    burst=1000,
    backlog=10,
    text_rate=10_000_000,
    text_burst=1 << 20,
    connect_rate=1000,
    connect_burst=1000,
    check_interval=20,
    presence_ttl=60,
    close_timeout=1,
    resume_window=120,
    replay=1000,
    takeover_timeout=2,
)


class Channel:
    """A connection whose device's frames the test gives, one at a time."""

    def __init__(self) -> None:
        self.frames: asyncio.Queue[str | bytes | Closed] = asyncio.Queue()
        self.sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.closed: int | None = None

    async def receive(self) -> str | bytes | Closed:
        return await self.frames.get()

    async def send(self, text: str) -> None:
        if self.closed is None:
            self.sent.put_nowait(json.loads(text))

    async def close(self, code: int) -> None:
        if self.closed is None:
            self.closed = code

    def say(self, message: dict[str, Any] | str | bytes) -> None:
        self.frames.put_nowait(message if isinstance(message, str | bytes) else json.dumps(message))

    def gone(self, code: int | None = None) -> None:
        """The connection closes, with ``code`` if its close frame said one."""
        self.frames.put_nowait(Closed(code))

    async def hear(self) -> dict[str, Any]:
        async with asyncio.timeout(5):
            return await self.sent.get()


async def nothing() -> None:
    pass


class Store:
    """The registry: the devices it holds, under their registration, in ``HOME``. As a change
    elsewhere would, ``describing`` runs while a hello is kept, ``holding`` while a home's
    lock is waited for, and ``removing`` as a device is removed, before the registry holds it
    no more."""

    def __init__(self) -> None:
        self.held: dict[UUID, datetime] = {}
        self.describing: Callable[[], None] = lambda: None
        self.holding: Callable[[], Awaitable[None]] = nothing
        self.removing: Callable[[], Awaitable[None]] = nothing
        self.homes_held: list[UUID] = []

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Store]:
        yield self

    async def describe(self, device_id: UUID, registered: datetime, _: Description) -> bool:
        self.describing()
        return self.held.get(device_id) == registered

    async def registrations(self, device_ids: Collection[UUID]) -> dict[UUID, datetime]:
        return {each: self.held[each] for each in device_ids if each in self.held}

    async def hold_home(self, home_id: UUID) -> None:
        await self.holding()
        self.homes_held.append(home_id)

    async def device(self, device_id: UUID) -> Device | None:
        if device_id not in self.held:
            return None
        return Device(device_id, HOME, self.held[device_id], None, None, 1)

    async def remove_device(self, home_id: UUID, device_id: UUID) -> bool:
        await self.removing()
        return home_id == HOME and self.held.pop(device_id, None) is not None


class Presence:
    def __init__(self) -> None:
        self.newest: dict[UUID, UUID] = {}
        self.left: list[UUID] = []

    async def arrive(self, device_id: UUID, connection: UUID) -> UUID | None:
        replaced, self.newest[device_id] = self.newest.get(device_id), connection
        return replaced

    async def keep(self, held: Sequence[tuple[UUID, UUID]]) -> set[tuple[UUID, UUID]]:
        replaced = set()
        for device_id, connection in held:
            if self.newest.setdefault(device_id, connection) != connection:
                replaced.add((device_id, connection))
        return replaced

    async def leave(self, device_id: UUID, connection: UUID) -> None:
        self.left.append(connection)
        if self.newest.get(device_id) == connection:
            del self.newest[device_id]


class Rate:
    """How often each device may connect: as often as it likes, but for those in ``waits``,
    which wait as long as it says. ``asked`` names each device asked of."""

    def __init__(self) -> None:
        self.waits: dict[str, float] = {}
        self.asked: list[str] = []

    async def wait(self, subject: str) -> float:
        self.asked.append(subject)
        return self.waits.get(subject, 0)


class Hints:
    """``publishing`` runs as a hint is sent: as this replica would hear it, for one."""

    def __init__(self) -> None:
        self.publishing: Callable[[], Awaitable[None]] = nothing

    async def publish(self, device_ids: Collection[UUID]) -> None:
        await self.publishing()

    async def listen(self) -> AsyncIterator[UUID]:
        await asyncio.Event().wait()
        yield uuid.uuid7()


class Bus:
    """Hints that reach at once every replica that listens."""

    def __init__(self) -> None:
        self.listening: list[asyncio.Queue[UUID]] = []

    async def publish(self, device_ids: Collection[UUID]) -> None:
        for heard in self.listening:
            for device_id in device_ids:
                heard.put_nowait(device_id)

    def listen(self) -> AsyncIterator[UUID]:
        heard: asyncio.Queue[UUID] = asyncio.Queue()
        self.listening.append(heard)

        async def hearing() -> AsyncIterator[UUID]:
            while True:
                yield await heard.get()

        return hearing()


def links(
    store: Store,
    presence: Presence | None = None,
    hints: Hints | Bus | None = None,
    *,
    records: Records | None = None,
    **changes: Any,
) -> Links:
    settings = SETTINGS.model_copy(update=changes)
    return Links(
        settings=settings,
        leeway=30,
        tokens=None,  # type: ignore[arg-type]  # no handshake here
        store=store,  # type: ignore[arg-type]
        presence=presence or Presence(),
        sessions=records or Records(),
        hints=hints or Hints(),  # type: ignore[arg-type]
        connection_rate=Rate(),
    )


def held(store: Store) -> Opening:
    device_id = uuid.uuid7()
    store.held[device_id] = REGISTERED
    return Opening(device_id, HOME, REGISTERED)


async def started(replica: Links, opening: Opening, channel: Channel) -> asyncio.Task[None]:
    """Serve ``channel``, and wait for the cloud's hello."""
    serving = asyncio.create_task(replica.serve(channel, opening))  # type: ignore[arg-type]
    channel.say(hello())
    assert (await channel.hear())["type"] == "hello"
    return serving


async def test_device_changed_changes_what_the_session_knows_of_the_device() -> None:
    store = Store()
    replica = links(store)
    channel = Channel()
    serving = await started(replica, held(store), channel)
    [session] = replica._held()
    assert session.hello is not None
    assert session.hello.languages == ("vi", "en")
    channel.say({"type": "device.changed", "seq": 1, "languages": ["en", "vi"]})
    channel.say({"type": "device.changed", "seq": 2, "time_zone": "Europe/Paris"})
    channel.say({"type": "device.changed", "seq": 3, "allowed_classes": ["informational"]})
    async with asyncio.timeout(5):
        while session.hello.allowed_classes != ("informational",):
            await asyncio.sleep(0.01)
    # Handled, and so received.
    channel.say({"type": "ping", "received": 0})
    assert await channel.hear() == {"type": "pong", "received": 3}
    assert session.hello.languages == ("en", "vi")
    assert session.hello.time_zone == "Europe/Paris"
    channel.gone()
    await serving


async def test_drops_are_told_once_each_time_the_backlog_overflows() -> None:
    store = Store()
    replica = links(store, backlog=1)
    channel = Channel()
    serving = await started(replica, held(store), channel)
    # They come at once: the first waits its turn, and the backlog is full for the others.
    # Each that is handled gets its error, for it changes nothing.
    for seq in range(1, 6):
        channel.say({"type": "device.changed", "seq": seq})
    told = [await channel.hear() for _ in range(2)]
    assert [(each["code"], each["reply_to"]) for each in told] == [
        ("rate_limited", 2),
        ("invalid_message", 1),
    ]
    # The backlog emptied: the next overflow is told too.
    for seq in range(6, 10):
        channel.say({"type": "device.changed", "seq": seq})
    told = [await channel.hear() for _ in range(2)]
    assert [(each["code"], each["reply_to"]) for each in told] == [
        ("rate_limited", 7),
        ("invalid_message", 6),
    ]
    channel.gone()
    await serving


@pytest.mark.parametrize(
    ("ending", "said", "code"), [(RECONNECT, None, 1012), ("revoked", "revoked", 1000)]
)
async def test_an_end_that_comes_while_a_session_starts_ends_it(
    ending: str, said: str | None, code: int
) -> None:
    store = Store()
    replica = links(store)
    channel = Channel()
    opening = held(store)
    serving = asyncio.create_task(replica.serve(channel, opening))  # type: ignore[arg-type]
    store.describing = lambda: [session.end(ending) for session in replica._held()]
    channel.say(hello())
    if said is not None:
        assert await channel.hear() == {"type": "bye", "reason": said}
    await serving
    assert channel.sent.empty()
    assert channel.closed == code


async def test_a_session_that_has_not_said_hello_is_not_taken_for_replaced() -> None:
    store, presence = Store(), Presence()
    replica = links(store, presence)
    opening = held(store)
    # Its device's older connection, elsewhere.
    presence.newest[opening.device_id] = uuid.uuid7()
    channel = Channel()
    serving = asyncio.create_task(replica.serve(channel, opening))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert len(replica._held()) == 1  # held, before its hello
    await replica._check(replica._held())
    channel.say(hello())
    assert (await channel.hear())["type"] == "hello"
    channel.gone()
    await serving


async def test_a_check_forgets_again_the_presence_of_a_connection_that_closed_meanwhile() -> None:
    store, presence = Store(), Presence()
    replica = links(store, presence)
    channel = Channel()
    serving = await started(replica, held(store), channel)
    [session] = replica._held()
    # The connection closes while a check, which took it, keeps its presence.
    channel.gone()
    await serving
    assert session.finished
    presence.newest[session.device_id] = session.connection  # noted again by the check
    await replica._check([session])
    assert session.device_id not in presence.newest


async def test_messages_are_handled_at_the_rate_the_cloud_takes() -> None:
    store = Store()
    replica = links(store, rate=5, burst=1)
    channel = Channel()
    serving = await started(replica, held(store), channel)
    for seq in range(1, 4):
        channel.say({"type": "device.changed", "seq": seq})
    started_at = asyncio.get_running_loop().time()
    for _ in range(3):
        assert (await channel.hear())["code"] == "invalid_message"
    # One at once, then one each fifth of a second.
    assert asyncio.get_running_loop().time() - started_at >= 0.35
    channel.gone()
    await serving


class Deaf(Channel):
    """A connection whose device reads nothing: what the cloud sends waits for ever, its
    close too, as uvicorn's wait while the device's window is full."""

    async def send(self, text: str) -> None:
        await asyncio.Event().wait()

    async def close(self, code: int) -> None:
        self.closed = code
        await asyncio.Event().wait()


async def test_a_session_whose_device_reads_nothing_ends(caplog: pytest.LogCaptureFixture) -> None:
    """It stalls, and is kept for the device to resume (Resuming a session)."""
    store, records = Store(), Records()
    replica = links(store, records=records, ping_interval=0.1)
    channel = Deaf()
    opening = held(store)
    serving = asyncio.create_task(replica.serve(channel, opening))  # type: ignore[arg-type]
    channel.say(hello())
    # It keeps sending, so that it is not silent: it only takes nothing.
    async with asyncio.timeout(5):
        while not serving.done():
            channel.say({"type": "ping", "received": 0})
            await asyncio.sleep(0.02)
    await serving
    assert channel.closed == 1001
    assert replica._held() == []
    [closed] = [
        each for each in caplog.records if each.getMessage() == "a device's control channel closed"
    ]
    assert closed.ended == "stalled"  # type: ignore[attr-defined]
    assert records.held[opening.device_id].owner is None


async def test_pings_count_against_the_rate() -> None:
    store = Store()
    replica = links(store, rate=5, burst=1)
    channel = Channel()
    serving = await started(replica, held(store), channel)
    started_at = asyncio.get_running_loop().time()
    for _ in range(3):
        channel.say({"type": "ping", "received": 0})
    for _ in range(3):
        assert (await channel.hear())["type"] == "pong"
    assert asyncio.get_running_loop().time() - started_at >= 0.35
    channel.gone()
    await serving


def closed_as(caplog: pytest.LogCaptureFixture) -> list[str]:
    """How each session that the test served ended, as the cloud logs it."""
    return [
        each.ended  # type: ignore[attr-defined]
        for each in caplog.records
        if each.getMessage() == "a device's control channel closed"
    ]


async def ends_unanswered(channel: Channel, serving: asyncio.Task[None]) -> str:
    """Wait for the session to end over a frame that it cannot answer; what its bye says."""
    bye = await channel.hear()
    assert (bye["type"], bye["reason"]) == ("bye", "invalid_message")
    await serving
    assert channel.closed == 1000
    return bye["message"]


def device_twice(first: str) -> str:
    """A hello whose ``device`` comes twice: ``first``, then one that passes. Python's reader
    keeps the last, and would drop the first unread."""
    return json.dumps(hello()).replace('"device": ', f'"device": {first}, "device": ', 1)


# Frames that a session cannot answer, whenever they come: each ends it (Messages, Validation).
NOT_HELD = "not JSON that every side can hold: "
DEEPER = NOT_HELD + "its arrays and objects nest deeper than the cloud reads"
UNHELD = NOT_HELD + "it holds U+0000 or a lone surrogate, which no side holds"
REPEATED = NOT_HELD + "an object repeats a name"
NO_TYPE = "no type that is text"
NO_NUMBER = "a numbered type without its number"
UNANSWERABLE = [
    pytest.param(b'{"type": "ping", "received": 0}', "a binary frame", id="binary"),
    pytest.param("not JSON", NOT_HELD + "it is not JSON", id="no JSON"),
    pytest.param(DEEP, DEEPER, id="nested past Python's reader"),
    pytest.param("[" * 65 + "]" * 65, DEEPER, id="nested past the protocols' depth"),
    pytest.param('{"type": "ping", "received": 0, "received": 1}', REPEATED, id="a repeated name"),
    pytest.param(
        '{"type": "ping", "received": 0, "x": 1e400}',
        NOT_HELD + "a number is past a double's range",
        id="a number past a double",
    ),
    pytest.param(
        '{"type": "ping", "received": 0, "x": NaN}',
        NOT_HELD + "a number is not finite",
        id="NaN",
    ),
    pytest.param(json.dumps({"type": "ping", "received": 0, "x": chr(0)}), UNHELD, id="U+0000"),
    pytest.param(
        json.dumps({"type": "ping", "received": 0, chr(0xD800): 1}),
        UNHELD,
        id="a lone surrogate in a name",
    ),
    pytest.param('[{"type": "ping", "received": 0}]', "not a JSON object", id="no object"),
    pytest.param('{"received": 0}', NO_TYPE, id="no type"),
    pytest.param('{"type": ["ping"], "received": 0}', NO_TYPE, id="a type that is no text"),
    pytest.param('{"type": null, "seq": 1}', NO_TYPE, id="a type that is null"),
    pytest.param('{"type": "device.changed", "languages": ["en"]}', NO_NUMBER, id="no number"),
    pytest.param(
        '{"type": "device.changed", "seq": "1", "languages": ["en"]}',
        NO_NUMBER,
        id="a number in text",
    ),
    pytest.param(
        '{"type": "device.changed", "seq": true, "languages": ["en"]}',
        NO_NUMBER,
        id="a number that is true",
    ),
    pytest.param(
        '{"type": "device.changed", "seq": 0, "languages": ["en"]}', NO_NUMBER, id="the number 0"
    ),
    pytest.param(
        '{"type": "device.changed", "seq": 1.5, "languages": ["en"]}',
        NO_NUMBER,
        id="a number with a fraction",
    ),
    pytest.param(
        '{"type": "device.changed", "seq": 9007199254740992, "languages": ["en"]}',
        NO_NUMBER,
        id="a number past the largest",
    ),
    # Which rule fails, and where: the schema's words, which name no value.
    pytest.param('{"type": "ping"}', "ping: ", id="a ping that fails"),
    pytest.param('{"type": "pong", "seq": 0}', "pong: ", id="a pong that fails"),
    pytest.param('{"type": "reconnect", "seq": "1"}', "reconnect: ", id="a reconnect that fails"),
]


@pytest.mark.parametrize(
    ("first", "said"),
    [
        *UNANSWERABLE,
        pytest.param(json.dumps(hello(languages=[])), "hello: ", id="a hello that fails"),
        pytest.param(json.dumps(hello(protocol="3")), "hello: ", id="a hello without its minor"),
        # Items that jsonschema's uniqueItems would compare as deep as they nest.
        pytest.param(
            json.dumps(hello(languages=[json.loads("[" * 400 + "]" * 400)] * 2)),
            DEEPER,
            id="nested items to compare",
        ),
        pytest.param(
            device_twice('{"kind": "robot", "model": "m", "software": "\\u0000"}'),
            REPEATED,
            id="U+0000 in a device repeated",
        ),
        pytest.param(device_twice("[" * 200 + "]" * 200), DEEPER, id="nested in a device repeated"),
    ],
)
async def test_a_first_frame_the_cloud_cannot_answer_ends_the_session(
    first: str | bytes, said: str, caplog: pytest.LogCaptureFixture
) -> None:
    store = Store()
    channel = Channel()
    serving = asyncio.create_task(links(store).serve(channel, held(store)))  # type: ignore[arg-type]
    channel.say(first)
    why = await ends_unanswered(channel, serving)
    assert why.startswith(said)
    assert len(why) <= 256
    assert closed_as(caplog) == ["invalid_message"]


@pytest.mark.parametrize(
    "first",
    [
        {"type": "ping", "received": 0},
        {"type": "pong"},
        {"type": "device.changed", "seq": 1, "languages": ["en"]},
        # Numbered, it is answerable: but the hello has not come.
        {"type": "device.changed", "seq": 1},
        {"type": "something.new", "seq": 1},
    ],
)
async def test_a_first_message_that_is_no_hello_is_told(first: dict[str, Any]) -> None:
    store = Store()
    channel = Channel()
    serving = asyncio.create_task(links(store).serve(channel, held(store)))  # type: ignore[arg-type]
    channel.say(first)
    # Said at once, without waiting for hello_timeout (hello, since 3.3).
    async with asyncio.timeout(1):
        said = await channel.hear()
    assert said == {"type": "bye", "reason": "no_hello", "message": NOT_FIRST}
    await serving
    assert channel.closed == 1000


async def test_the_first_message_example_runs_as_it_says() -> None:
    """The cloud link's example of a first message that is not a hello (hello, since 3.3)."""
    example = json.loads(
        (platform_dir() / "cloud" / "examples" / "first-message.json").read_text(encoding="utf-8")
    )
    store = Store()
    channel = Channel()
    serving = asyncio.create_task(links(store).serve(channel, held(store)))  # type: ignore[arg-type]
    steps = [step for step in example["messages"] if "from" in step]
    assert [step["from"] for step in steps] == ["device", "cloud"]
    channel.say(steps[0]["message"])
    assert await channel.hear() == steps[1]["message"]
    await serving
    assert channel.closed == 1000


async def test_a_hello_that_holds_u0000_is_told_why(caplog: pytest.LogCaptureFixture) -> None:
    """No side holds U+0000, nor does PostgreSQL's text: the session ends before the cloud
    keeps anything of it, and its bye says why, naming no value (SEC-5)."""
    store = Store()
    described: list[None] = []
    store.describing = lambda: described.append(None)
    channel = Channel()
    serving = asyncio.create_task(links(store).serve(channel, held(store)))  # type: ignore[arg-type]
    software = "0.4.0" + chr(0) + "secret"
    channel.say(hello(device={"kind": "robot", "model": "concept-v0", "software": software}))
    assert await ends_unanswered(channel, serving) == UNHELD
    assert described == []
    assert closed_as(caplog) == ["invalid_message"]
    assert "secret" not in caplog.text


@pytest.mark.parametrize(
    ("frame", "said"),
    [*UNANSWERABLE, pytest.param('{"type": "hello"}', "hello: ", id="a hello that fails")],
)
async def test_a_frame_the_cloud_cannot_answer_ends_the_session(
    frame: str | bytes, said: str, caplog: pytest.LogCaptureFixture
) -> None:
    store = Store()
    replica = links(store)
    channel = Channel()
    serving = await started(replica, held(store), channel)
    channel.say(frame)
    why = await ends_unanswered(channel, serving)
    assert why.startswith(said)
    assert len(why) <= 256
    assert closed_as(caplog) == ["invalid_message"]


async def test_a_numbered_message_that_fails_gets_an_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """It has a number to answer, and the session goes on (REL-4)."""
    store = Store()
    replica = links(store)
    channel = Channel()
    serving = await started(replica, held(store), channel)
    channel.say(
        {"type": "device.changed", "seq": 1, "languages": [json.loads("[" * 60 + "]" * 60)] * 2}
    )
    error = await channel.hear()
    assert (error["reply_to"], error["code"]) == (1, "invalid_message")
    # A type longer than the protocol allows fails before the cloud would log it.
    channel.say({"type": "x" * 65, "seq": 2})
    error = await channel.hear()
    assert (error["reply_to"], error["code"]) == (2, "invalid_message")
    channel.say({"type": "ping", "received": 2})
    assert await channel.hear() == {"type": "pong", "received": 2}
    assert not [each for each in caplog.records if each.getMessage().startswith("a device sent")]
    channel.gone()
    await serving


async def test_a_number_whose_fraction_is_zero_is_the_integer() -> None:
    """JSON Schema's integer is a number without a fraction, however it is written."""
    store = Store()
    replica = links(store)
    channel = Channel()
    serving = await started(replica, held(store), channel)
    channel.say('{"type": "device.changed", "seq": 1.0, "languages": ["en"]}')
    # The same number: a duplicate.
    channel.say('{"type": "device.changed", "seq": 1, "languages": ["en"]}')
    channel.say('{"type": "device.changed", "seq": 2e0}')
    error = await channel.hear()
    assert (error["reply_to"], error["code"]) == (2, "invalid_message")
    channel.say({"type": "ping", "received": 0})
    assert await channel.hear() == {"type": "pong", "received": 2}
    channel.gone()
    await serving


def changed(seq: int, depth: int) -> str:
    """A device.changed that nests ``depth`` deep, its own object included."""
    languages = "[" * (depth - 1) + '"en"' + "]" * (depth - 1)
    return f'{{"type": "device.changed", "seq": {seq}, "languages": {languages}}}'


async def test_a_message_nests_as_deep_as_the_protocols_say() -> None:
    """64 levels, counting the message's own object (ai-platform's ADR-0014, decision 3)."""
    store = Store()
    channel = Channel()
    serving = await started(links(store), held(store), channel)
    channel.say(changed(1, 2))
    channel.say(changed(2, 64))
    # Read, and failing the schema: numbered, so answered.
    error = await channel.hear()
    assert (error["reply_to"], error["code"]) == (2, "invalid_message")
    channel.say(changed(3, 65))
    assert await ends_unanswered(channel, serving) == DEEPER


async def test_what_a_session_ignores_is_logged_a_bounded_number_of_times(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = Store()
    replica = links(store, backlog=100)
    channel = Channel()
    serving = await started(replica, held(store), channel)
    [session] = replica._held()
    # Each a type the cloud does not take, valid as a newer minor version's would be.
    for seq in range(1, 51):
        channel.say({"type": f"something.new{seq}", "seq": seq})
    # What the cloud sends, and a hello that came already.
    for seq in range(51, 101):
        channel.say({"type": "pong", "received": seq})
        channel.say({"type": "reconnect"})
        channel.say(hello())
    channel.say({"type": "device.changed", "seq": 51})
    assert (await channel.hear())["reply_to"] == 51  # after all the others
    noted = [each for each in caplog.records if each.getMessage().startswith("a device sent")]
    # Once each: the first type the cloud does not take, a pong, a reconnect and a hello.
    assert len(noted) == 4
    assert [each.type for each in noted if hasattr(each, "type")] == ["something.new1"]
    assert len(session._noted) == 4
    channel.gone()
    await serving


@pytest.mark.parametrize(
    ("code", "ended"), [(None, "closed"), (1000, "closed"), (1001, "closed"), (1009, "too_big")]
)
@pytest.mark.parametrize("hello_first", [False, True], ids=["before the hello", "in the session"])
async def test_a_session_ends_as_its_connection_closed(
    code: int | None, ended: str, hello_first: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A message too large closes the connection with 1009, whichever side received it: the
    session ends, as it would with invalid_message, though no bye can follow (Messages,
    Sizes)."""
    store = Store()
    replica = links(store)
    channel = Channel()
    if hello_first:
        serving = await started(replica, held(store), channel)
    else:
        serving = asyncio.create_task(replica.serve(channel, held(store)))  # type: ignore[arg-type]
    channel.gone(code)
    await serving
    assert channel.sent.empty()
    assert closed_as(caplog) == [ended]


# Leaving the home (leave, since 3.2).


async def test_the_leave_example_runs_as_it_says(caplog: pytest.LogCaptureFixture) -> None:
    """The cloud link's example of a leave: the first removal fails, and the second answers
    bye left, once the registry holds the device no more."""
    example = json.loads(
        (platform_dir() / "cloud" / "examples" / "leave.json").read_text(encoding="utf-8")
    )
    store = Store()
    failures = iter([RuntimeError("for the test")])

    async def removing() -> None:
        if (failure := next(failures, None)) is not None:
            raise failure

    store.removing = removing
    replica = links(store)
    channel = Channel()
    opening = held(store)
    serving = asyncio.create_task(replica.serve(channel, opening))  # type: ignore[arg-type]
    for step in example["messages"]:
        if step.get("from") == "device":
            channel.say(step["message"])
        elif step.get("from") == "cloud":
            said = await channel.hear()
            if said["type"] == "hello":
                # Its session and its time are the cloud's own.
                said |= {key: step["message"][key] for key in ("session", "time")}
            else:
                # Only then: the registry holds the device no more.
                assert (opening.device_id in store.held) is (said["type"] != "bye")
            assert said == step["message"]
    await serving
    assert channel.closed == 1000
    assert store.homes_held == [HOME, HOME]
    assert closed_as(caplog) == ["left"]
    left = [each for each in caplog.records if each.getMessage() == "a device left its home"]
    assert [(each.home, each.device) for each in left] == [  # type: ignore[attr-defined]
        (str(HOME), str(opening.device_id))
    ]


async def test_a_leave_ends_its_session_with_left_though_its_hint_comes_first() -> None:
    """The removal's hint reaches this replica too, and its check finds the device gone,
    before the leave ends the session."""
    store, hints = Store(), Hints()
    replica = links(store, hints=hints)
    channel = Channel()
    opening = held(store)
    serving = await started(replica, opening, channel)
    hinted: list[int] = []

    async def heard() -> None:
        await replica._check(replica._held())
        hinted.append(1)

    hints.publishing = heard
    channel.say({"type": "leave", "seq": 1})
    assert await channel.hear() == {"type": "bye", "reason": "left"}
    await serving
    assert hinted == [1]
    assert opening.device_id not in store.held


@pytest.mark.parametrize(
    "meanwhile",
    [
        "removed in the app",
        "registered again",
        "registered again as the leave waits for its home",
        "gone with its home",
    ],
)
async def test_a_device_removed_meanwhile_is_told_revoked(meanwhile: str) -> None:
    """A removal in the app, or the home's deletion, came before the leave's: the device is
    out of its home all the same (leave), and a registration made since stays."""
    store, hints = Store(), Hints()
    replica = links(store, hints=hints)
    channel = Channel()
    opening = held(store)
    serving = await started(replica, opening, channel)
    hinted: list[int] = []

    async def heard() -> None:
        hinted.append(1)

    hints.publishing = heard
    if meanwhile == "removed in the app":
        del store.held[opening.device_id]
    elif meanwhile == "registered again":
        store.held[opening.device_id] = datetime(2026, 11, 12, tzinfo=UTC)
    elif meanwhile == "registered again as the leave waits for its home":
        # A removal and a registration, in the app, which held the home first: the leave
        # reads the device only once it holds the home, and finds another registration.

        async def registered() -> None:
            store.held[opening.device_id] = datetime(2026, 11, 12, tzinfo=UTC)

        store.holding = registered
    else:

        async def deleted() -> None:
            del store.held[opening.device_id]

        store.removing = deleted
    channel.say({"type": "leave", "seq": 1})
    assert await channel.hear() == {"type": "bye", "reason": "revoked"}
    await serving
    # Its removal was another's, and so was its hint.
    assert hinted == []
    if meanwhile.startswith("registered again"):
        assert store.held[opening.device_id] == datetime(2026, 11, 12, tzinfo=UTC)


async def test_a_leave_that_failed_leaves_the_session_to_its_checks() -> None:
    store = Store()

    async def failing() -> None:
        raise RuntimeError("for the test")

    store.removing = failing
    replica = links(store)
    channel = Channel()
    opening = held(store)
    serving = await started(replica, opening, channel)
    channel.say({"type": "leave", "seq": 1})
    error = await channel.hear()
    assert (error["reply_to"], error["code"]) == (1, "internal")
    # Removed in the app since: the next check ends the session.
    del store.held[opening.device_id]
    await replica._check(replica._held())
    assert await channel.hear() == {"type": "bye", "reason": "revoked"}
    await serving


async def test_what_comes_after_a_leave_is_not_handled() -> None:
    store = Store()
    removing, removed = asyncio.Event(), asyncio.Event()

    async def held_up() -> None:
        removing.set()
        await removed.wait()

    store.removing = held_up
    replica = links(store)
    channel = Channel()
    serving = await started(replica, held(store), channel)
    channel.say({"type": "leave", "seq": 1})
    async with asyncio.timeout(5):
        await removing.wait()
    # Taken while the removal runs, and handled by nothing yet: the pong counts neither.
    channel.say({"type": "leave", "seq": 2})
    channel.say({"type": "ping", "received": 0})
    assert await channel.hear() == {"type": "pong", "received": 0}
    removed.set()
    assert await channel.hear() == {"type": "bye", "reason": "left"}
    await serving
    assert store.homes_held == [HOME]


async def test_a_session_of_3_1_ignores_leave(caplog: pytest.LogCaptureFixture) -> None:
    """A cloud of 3.1 ignores a type it does not know, and so does one of 3.2 in a session of
    3.1 (Messages, Unknown content)."""
    store = Store()
    replica = links(store)
    channel = Channel()
    opening = held(store)
    serving = asyncio.create_task(replica.serve(channel, opening))  # type: ignore[arg-type]
    channel.say(hello(protocol="3.1"))
    assert (await channel.hear())["protocol"] == "3.1"
    channel.say({"type": "leave", "seq": 1})
    channel.say({"type": "device.changed", "seq": 2})
    # Answered after the leave was handled.
    assert (await channel.hear())["reply_to"] == 2
    channel.say({"type": "ping", "received": 0})
    assert await channel.hear() == {"type": "pong", "received": 2}
    assert store.held[opening.device_id] == REGISTERED
    assert store.homes_held == []
    noted = [each for each in caplog.records if each.getMessage().startswith("a device sent")]
    assert [each.type for each in noted] == ["leave"]  # type: ignore[attr-defined]
    channel.gone()
    await serving


class Failing(Channel):
    """A connection whose frames cannot be read after ``good`` of them: the cloud's own
    failure, as a bug would be."""

    def __init__(self, good: int) -> None:
        super().__init__()
        self.good = good

    async def receive(self) -> str | bytes | Closed:
        if self.good == 0:
            raise RuntimeError("for the test")
        self.good -= 1
        return await super().receive()


@pytest.mark.parametrize("good", [0, 1], ids=["before the hello", "in the session"])
async def test_a_session_the_cloud_fails_ends_with_an_error(
    good: int, caplog: pytest.LogCaptureFixture
) -> None:
    store = Store()
    replica = links(store)
    channel = Failing(good)
    serving = asyncio.create_task(replica.serve(channel, held(store)))  # type: ignore[arg-type]
    if good:
        channel.say(hello())
        assert (await channel.hear())["type"] == "hello"
    assert await channel.hear() == {"type": "bye", "reason": "error"}
    await serving
    assert channel.closed == 1011
    assert replica._held() == []
    [failed] = [each for each in caplog.records if each.getMessage() == "a device's session failed"]
    assert failed.exc_info is not None


class FailingAndDeaf(Failing):
    """A failure, then a device that reads nothing, its close neither."""

    async def send(self, text: str) -> None:
        await asyncio.Event().wait()

    async def close(self, code: int) -> None:
        self.closed = code
        await asyncio.Event().wait()


class FailingAndGone(Failing):
    """A failure of the connection itself: nothing can be sent on it any more."""

    async def send(self, text: str) -> None:
        raise RuntimeError("for the test")


@pytest.mark.parametrize("failing", [FailingAndDeaf, FailingAndGone])
async def test_a_session_the_cloud_fails_ends_whatever_its_connection_does(
    failing: type[Failing],
) -> None:
    store = Store()
    replica = links(store, close_timeout=0.2)
    channel = failing(0)
    started_at = asyncio.get_running_loop().time()
    async with asyncio.timeout(2):
        await replica.serve(channel, held(store))  # type: ignore[arg-type]
    assert channel.closed == 1011
    assert replica._held() == []
    # Its bye and its close within one close_timeout together, not one each.
    assert asyncio.get_running_loop().time() - started_at < 0.3


class Deafened(Channel):
    """A connection whose device reads nothing once ``deaf`` is set, its close neither."""

    def __init__(self) -> None:
        super().__init__()
        self.deaf = False

    async def send(self, text: str) -> None:
        if self.deaf:
            await asyncio.Event().wait()
        await super().send(text)

    async def close(self, code: int) -> None:
        await super().close(code)
        if self.deaf:
            await asyncio.Event().wait()


class Unanswered(Records):
    """Records that Redis no longer answers once a session began, as when it is slow."""

    async def hand(self, record: Record, connection: UUID) -> Any:
        await asyncio.Event().wait()

    async def end(self, device_id: UUID, connection: UUID, session: UUID) -> None:
        await asyncio.Event().wait()


class Unleaving(Presence):
    """The presence, in a Redis that no longer answers its leave."""

    async def leave(self, device_id: UUID, connection: UUID) -> None:
        await asyncio.Event().wait()


@pytest.mark.parametrize("redis", ["answers", "slow"])
@pytest.mark.parametrize("in_hand", [False, True], ids=["idle", "in hand"])
async def test_a_session_that_ends_waits_one_close_timeout_for_a_device_that_reads_nothing(
    in_hand: bool, redis: str
) -> None:
    """The message in hand, giving the session up in Redis, the close and leaving its
    presence, together within one close_timeout, for uvicorn cuts the connection only once
    serve returns (ADR-0012, decision 2)."""
    store = Store()
    slow = redis == "slow"
    replica = links(
        store,
        Unleaving() if slow else Presence(),
        records=Unanswered() if slow else Records(),
        close_timeout=0.2,
    )
    channel = Deafened()
    serving = await started(replica, held(store), channel)
    [session] = replica._held()
    if in_hand:
        # Handled for longer than close_timeout: the session is torn.
        held_up = HeldUp(session)
        channel.say({"type": "device.changed", "seq": 1})
        async with asyncio.timeout(5):
            await held_up.handling.wait()
    channel.deaf = True
    started_at = asyncio.get_running_loop().time()
    session.end(RECONNECT)
    async with asyncio.timeout(2):
        await serving
    assert channel.closed == 1012
    # What it sent last and its close within one close_timeout together, not one each.
    assert asyncio.get_running_loop().time() - started_at < 0.3


def end_held(replica: Links, reason: str) -> None:
    for session in replica._held():
        session.end(reason)


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("no hello", 1000),
        ("bye first", 1000),
        ("another message first", 1000),
        ("a reconnect before the hello", 1012),
        ("an end before the hello", 1000),
        ("a reconnect as it starts", 1012),
        ("a device no longer registered", 1000),
    ],
)
async def test_an_end_before_the_session_is_carried_waits_one_close_timeout_too(
    case: str, code: int
) -> None:
    """A device that reads nothing, and says no hello or one that the cloud refuses, holds
    its connection no longer than one that the session ended."""
    store = Store()
    replica = links(store, hello_timeout=0.1, close_timeout=0.2)
    channel = Deaf()
    if case == "a device no longer registered":
        opening = Opening(uuid.uuid7(), HOME, REGISTERED)
    else:
        opening = held(store)
    started_at = asyncio.get_running_loop().time()
    serving = asyncio.create_task(replica.serve(channel, opening))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    match case:
        case "bye first":
            channel.say({"type": "bye", "reason": "error"})
        case "another message first":
            channel.say({"type": "ping", "received": 0})
        case "a reconnect before the hello":
            end_held(replica, RECONNECT)
        case "an end before the hello":
            end_held(replica, "revoked")
        case "a reconnect as it starts":
            store.describing = lambda: end_held(replica, RECONNECT)
            channel.say(hello())
        case "a device no longer registered":
            channel.say(hello())
    async with asyncio.timeout(2):
        await serving
    assert channel.closed == code
    assert replica._held() == []
    # Its bye and its close within one close_timeout together, after the hello_timeout of a
    # hello that never came.
    waited = 0.1 if case == "no hello" else 0
    assert asyncio.get_running_loop().time() - started_at < waited + 0.3


class Unhurried:
    """The registry, read once the test lets it."""

    def __init__(self) -> None:
        self.go = asyncio.Event()
        self.reading = asyncio.Event()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Unhurried]:
        yield self

    async def device(self, device_id: UUID) -> Any:
        self.reading.set()
        await self.go.wait()
        return type("Device", (), {"id": device_id, "create_time": REGISTERED})


class Tokens:
    """Tokens that are their device's id, and are valid if they are one."""

    def authenticate(self, access_token: str) -> TokenHolder:
        try:
            device_id = uuid.UUID(access_token)
        except ValueError:
            raise Unauthenticated("the token is invalid") from None
        return TokenHolder(device_id, int(REGISTERED.timestamp()))


async def test_a_handshake_that_the_replica_goes_away_during_is_refused() -> None:
    store = Unhurried()
    replica = Links(
        settings=SETTINGS,
        leeway=30,
        tokens=Tokens(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        presence=Presence(),
        sessions=Records(),
        hints=Hints(),  # type: ignore[arg-type]
        connection_rate=Rate(),
    )
    opening = asyncio.create_task(replica.open(str(uuid.uuid7())))
    await store.reading.wait()
    await replica.going_away()
    store.go.set()
    with pytest.raises(Unavailable):
        await opening


class Counted(Tokens):
    """Tokens, of which ``authenticated`` counts those checked."""

    def __init__(self) -> None:
        self.authenticated = 0

    def authenticate(self, access_token: str) -> TokenHolder:
        self.authenticated += 1
        return super().authenticate(access_token)


def handshakes(store: Any, **changes: Any) -> tuple[Links, Counted, Rate]:
    """A replica, with ``changes`` to its settings, that checks handshakes against ``store``;
    its tokens and its devices' rate."""
    tokens, rate = Counted(), Rate()
    replica = Links(
        settings=SETTINGS.model_copy(update=changes),
        leeway=30,
        tokens=tokens,  # type: ignore[arg-type]
        store=store,
        presence=Presence(),
        sessions=Records(),
        hints=Hints(),  # type: ignore[arg-type]
        connection_rate=rate,
    )
    return replica, tokens, rate


async def test_a_replica_takes_so_many_connections_then_says_when_to_try_again() -> None:
    store = Store()
    replica, tokens, rate = handshakes(store, connect_rate=0.5, connect_burst=2)
    opening = held(store)
    for _ in range(2):
        await replica.open(str(opening.device_id))
    with pytest.raises(Unavailable) as refused:
        await replica.open(str(opening.device_id))
    # Each came with a valid token, within its device's own rate.
    assert tokens.authenticated == 3
    assert len(rate.asked) == 3
    assert refused.value.retry_after is not None
    assert 1.9 < refused.value.retry_after <= 2


@pytest.mark.parametrize("access_token", [None, "not-a-token"])
async def test_attempts_without_a_valid_token_spend_nothing_the_cloud_counts(
    access_token: str | None,
) -> None:
    store = Store()
    replica, _, rate = handshakes(store, connect_rate=0.001, connect_burst=1)
    for _ in range(10):
        with pytest.raises(Unauthenticated):
            await replica.open(access_token)
    assert rate.asked == []
    # A device then connects as if none had come.
    await replica.open(str(held(store).device_id))


async def test_a_device_past_its_own_rate_spends_none_of_the_replicas() -> None:
    store = Store()
    replica, _, rate = handshakes(store, connect_rate=0.001, connect_burst=1)
    eager = str(uuid.uuid7())
    rate.waits[eager] = 5
    for _ in range(10):
        with pytest.raises(RateLimited):
            await replica.open(eager)
    await replica.open(str(held(store).device_id))


async def test_a_device_whose_count_cannot_be_read_is_refused_until_it_can() -> None:
    store = Unhurried()
    replica, _, rate = handshakes(store)

    async def unavailable(subject: str) -> float:
        raise RatesUnavailable("the rates cannot be counted now")

    rate.wait = unavailable  # type: ignore[method-assign]
    with pytest.raises(Unavailable) as refused:
        async with asyncio.timeout(5):
            await replica.open(str(uuid.uuid7()))
    # After its backoff, which no Retry-After overrides; and the registry was not read.
    assert refused.value.retry_after is None
    assert not store.reading.is_set()


async def test_a_device_that_connects_too_often_is_told_before_the_registry_is_read() -> None:
    store = Unhurried()
    replica, _, rate = handshakes(store)
    device_id = str(uuid.uuid7())
    rate.waits[device_id] = 7.5
    # The registry's read would wait for ever: refused before, the device does not.
    with pytest.raises(RateLimited) as refused:
        async with asyncio.timeout(5):
            await replica.open(device_id)
    assert refused.value.retry_after == 7.5
    # Counted for the device that the token names.
    assert rate.asked == [device_id]
    assert not store.reading.is_set()


def test_a_wait_is_told_in_whole_seconds_none_of_them_early() -> None:
    assert str(RateLimited(0.2)) == "over the rate: retry after 1 s"


async def test_what_a_device_sends_is_read_no_faster_than_its_text_rate() -> None:
    store = Store()
    replica = links(store, text_rate=100_000, text_burst=1000)
    channel = Channel()
    serving = await started(replica, held(store), channel)
    pad = "a" * 50_000
    start = time.monotonic()
    for _ in range(3):
        channel.say({"type": "ping", "received": 0, "pad": pad})
    for _ in range(3):
        assert (await channel.hear())["type"] == "pong"
    # The first takes the full bucket and owes the rest of its length, as the second does
    # then: each owes half a second.
    assert time.monotonic() - start >= 0.9
    channel.gone()
    await serving


async def test_a_device_that_connects_again_finds_what_it_spent_spent() -> None:
    store = Store()
    replica = links(store, text_rate=100_000, text_burst=1000)
    opening = held(store)
    channel = Channel()
    serving = await started(replica, opening, channel)
    # Its length past the burst is owed: 0.49 s.
    channel.say({"type": "ping", "received": 0, "pad": "a" * 50_000})
    assert (await channel.hear())["type"] == "pong"
    channel.gone()
    await serving
    # Its next connection's hello, as any message, waits for what the last one owes.
    start = time.monotonic()
    serving = await started(replica, opening, Channel())
    assert time.monotonic() - start >= 0.4
    serving.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await serving


async def test_a_budget_is_forgotten_once_whole_and_unused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(rates, "time", SimpleNamespace(monotonic=lambda: now[0]))
    replica = links(Store(), rate=10, burst=5)
    gone, connected = uuid.uuid7(), uuid.uuid7()
    for device_id in (gone, connected):
        await replica.budget(device_id).take("{}")
    replica._sessions[connected] = set()
    replica._sweep()
    # What one took is not made up yet: a new budget would forget it.
    assert set(replica._budgets) == {gone, connected}
    now[0] = 0.1
    replica._sweep()
    assert set(replica._budgets) == {connected}


async def test_a_replica_forgets_budgets_as_it_checks() -> None:
    replica = links(Store(), check_interval=0.01)
    replica.budget(uuid.uuid7())
    stop = asyncio.Event()
    running = asyncio.create_task(replica.run(stop))
    async with asyncio.timeout(5):
        while replica._budgets:
            await asyncio.sleep(0.01)
    stop.set()
    await running


async def test_a_session_that_begins_as_the_replica_goes_away_closes_before_its_hello() -> None:
    store = Store()
    replica = links(store)
    await replica.going_away()
    channel = Channel()
    serving = asyncio.create_task(replica.serve(channel, held(store)))  # type: ignore[arg-type]
    channel.say(hello())
    await serving
    # As one the replica held then: it closes, to be connected again, with nothing sent.
    assert channel.closed == 1012
    assert channel.sent.empty()
    assert replica._held() == []


class Leaving(Presence):
    """The presence, which forgets a connection once the test lets it."""

    def __init__(self) -> None:
        super().__init__()
        self.go = asyncio.Event()
        self.leaving = asyncio.Event()

    async def leave(self, device_id: UUID, connection: UUID) -> None:
        self.leaving.set()
        await self.go.wait()
        await super().leave(device_id, connection)


async def test_a_replica_that_stops_waits_for_its_sessions_to_forget_their_presence() -> None:
    store, presence = Store(), Leaving()
    replica = links(store, presence)
    stop = asyncio.Event()
    running = asyncio.create_task(replica.run(stop))
    channel = Channel()
    serving = await started(replica, held(store), channel)
    # The server cuts it short as it stops, then the replica's checks stop.
    serving.cancel()
    await presence.leaving.wait()
    stop.set()
    done, _ = await asyncio.wait({running}, timeout=SETTINGS.close_timeout / 4)
    assert not done
    presence.go.set()
    # Once they have, at once.
    async with asyncio.timeout(SETTINGS.close_timeout / 4):
        await running
    assert presence.newest == {}
    with pytest.raises(asyncio.CancelledError):
        await serving


async def test_a_replica_that_stops_waits_for_its_sessions_no_longer_than_close_timeout() -> None:
    store, presence = Store(), Leaving()
    replica = links(store, presence, close_timeout=0.2)
    stop = asyncio.Event()
    running = asyncio.create_task(replica.run(stop))
    channel = Channel()
    serving = await started(replica, held(store), channel)
    channel.gone()
    await presence.leaving.wait()
    stop.set()
    async with asyncio.timeout(1):
        await running
    presence.go.set()
    await serving


# Resuming a session.


def resuming(session: str, received: int, **changes: Any) -> dict[str, Any]:
    """A hello that resumes ``session``, whose device received up to ``received``."""
    return hello(resume={"session": session, "received": received}, **changes)


async def opened(
    replica: Links, opening: Opening, channel: Channel, first: dict[str, Any] | None = None
) -> tuple[asyncio.Task[None], dict[str, Any]]:
    """Serve ``channel``, say ``first`` or a hello, and hear the cloud's."""
    serving = asyncio.create_task(replica.serve(channel, opening))  # type: ignore[arg-type]
    channel.say(first or hello())
    answer = await channel.hear()
    assert answer["type"] == "hello", answer
    return serving, answer


@asynccontextmanager
async def running(*replicas: Links, heard: Bus) -> AsyncIterator[None]:
    """The replicas' hints, which ``heard`` carries, and their checks, while the block runs."""
    stop = asyncio.Event()
    tasks = [asyncio.create_task(replica.run(stop)) for replica in replicas]
    await until(lambda: len(heard.listening) == len(replicas))
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(*tasks)


async def until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not condition():
            await asyncio.sleep(0.01)


def session_of(replica: Links, channel: Channel) -> Any:
    """The session that ``replica`` holds on ``channel``."""
    [session] = [each for each in replica._held() if each._channel is channel]
    return session


class Example:
    """A cloud link example, run here: its sessions and its times are the cloud's own."""

    def __init__(self, name: str) -> None:
        path = platform_dir() / "cloud" / "examples" / name
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.steps = [step for step in loaded["messages"] if "from" in step]
        self.sessions: dict[str, str] = {}

    def device(self, index: int) -> dict[str, Any]:
        """The device's message ``index``, resuming the cloud's own session."""
        step = self.steps[index]
        assert step["from"] == "device"
        message = step["message"]
        if "resume" in message:
            resume = message["resume"]
            message = message | {"resume": resume | {"session": self.sessions[resume["session"]]}}
        return message

    def cloud(self, index: int) -> dict[str, Any]:
        """The cloud's message ``index``, as a session sends it, without its number."""
        step = self.steps[index]
        assert step["from"] == "cloud"
        return {name: value for name, value in step["message"].items() if name != "seq"}

    def check(self, said: dict[str, Any], index: int) -> None:
        """That the cloud said its message ``index``."""
        step = self.steps[index]
        assert step["from"] == "cloud"
        expected = step["message"]
        if said.get("type") == "hello":
            # One session of the example's is one of the cloud's.
            assert self.sessions.setdefault(expected["session"], said["session"]) == said["session"]
            said = said | {name: expected[name] for name in ("session", "time")}
        assert said == expected


async def test_the_takeover_example_runs_as_it_says() -> None:
    """The cloud link's example of a session that a newer connection resumes while the older
    holds it, which closes with 4000 and no bye; then of a new session, which ends the older's
    with bye replaced (Resuming a session, since 3.7)."""
    run = Example("takeover.json")
    store, bus = Store(), Bus()
    replica = links(store, hints=bus)
    opening = held(store)
    first, second, third = Channel(), Channel(), Channel()
    async with running(replica, heard=bus):
        older = asyncio.create_task(replica.serve(first, opening))  # type: ignore[arg-type]
        first.say(run.device(0))
        run.check(await first.hear(), 1)
        # A call of the cloud's own, as its MCP client would send it.
        session_of(replica, first)._send(run.cloud(2))
        run.check(await first.hear(), 2)
        # The device's answer, 3, is lost on the way.
        newer = asyncio.create_task(replica.serve(second, opening))  # type: ignore[arg-type]
        second.say(run.device(4))
        run.check(await second.hear(), 5)
        await older
        assert first.closed == 4000
        assert first.sent.empty()  # and no bye
        second.say(run.device(6))
        # Its pong counts what was handled, which the device's ping waits for here.
        resumed = session_of(replica, second)
        await until(lambda: resumed.received == 1)
        second.say(run.device(7))
        run.check(await second.hear(), 8)
        newest = asyncio.create_task(replica.serve(third, opening))  # type: ignore[arg-type]
        third.say(run.device(9))
        run.check(await second.hear(), 10)
        run.check(await third.hear(), 11)
        await newer
        assert second.closed == 1000
        third.gone()
        await newest


async def test_the_resume_example_runs_as_it_says() -> None:
    """The cloud link's example of a session resumed after a loss, then after the cloud asked
    its device to connect again, on another replica; then of a new session (Resuming a
    session)."""
    run = Example("resume.json")
    store, presence, bus, records = Store(), Presence(), Bus(), Records()
    here = links(store, presence, bus, records=records)
    there = links(store, presence, bus, records=records)
    opening = held(store)
    first, second, third, fourth = Channel(), Channel(), Channel(), Channel()
    async with running(here, there, heard=bus):
        serving = asyncio.create_task(here.serve(first, opening))  # type: ignore[arg-type]
        first.say(run.device(0))
        run.check(await first.hear(), 1)
        session = session_of(here, first)
        session._send(run.cloud(2))
        run.check(await first.hear(), 2)
        first.say(run.device(3))
        await until(lambda: session.received == 1)
        first.say(run.device(4))
        run.check(await first.hear(), 5)
        session._send(run.cloud(6))
        # Lost before the device receives it.
        first.gone()
        await serving
        serving = asyncio.create_task(here.serve(second, opening))  # type: ignore[arg-type]
        second.say(run.device(7))
        run.check(await second.hear(), 8)
        run.check(await second.hear(), 9)
        second.say(run.device(10))
        session = session_of(here, second)
        await until(lambda: session.received == 2)
        await here.going_away()
        run.check(await second.hear(), 11)
        await serving
        assert second.closed == 1012
        serving = asyncio.create_task(there.serve(third, opening))  # type: ignore[arg-type]
        third.say(run.device(12))
        run.check(await third.hear(), 13)
        third.say(run.device(14))
        await serving
        assert records.held == {}
        serving = asyncio.create_task(there.serve(fourth, opening))  # type: ignore[arg-type]
        fourth.say(run.device(15))
        run.check(await fourth.hear(), 16)
        fourth.gone()
        await serving


class HeldUp:
    """A session's handling, held up from ``handling`` until ``go``."""

    def __init__(self, session: Any) -> None:
        self.handling, self.go = asyncio.Event(), asyncio.Event()
        self._handle = session._handle
        session._handle = self.handle

    async def handle(self, seq: int, message: dict[str, Any]) -> None:
        self.handling.set()
        await self.go.wait()
        await self._handle(seq, message)


async def test_a_session_handed_over_handles_the_message_in_hand_and_nothing_more() -> None:
    store, records, bus = Store(), Records(), Bus()
    replica = links(store, hints=bus, records=records)
    opening = held(store)
    first, second = Channel(), Channel()
    async with running(replica, heard=bus):
        older, answer = await opened(replica, opening, first)
        session = session_of(replica, first)
        held_up = HeldUp(session)
        first.say({"type": "device.changed", "seq": 1})  # answered with an error
        first.say({"type": "device.changed", "seq": 2})
        await held_up.handling.wait()
        # Taken, and not handled: neither counts.
        first.say({"type": "ping", "received": 0})
        assert await first.hear() == {"type": "pong", "received": 0}
        newer = asyncio.create_task(replica.serve(second, opening))  # type: ignore[arg-type]
        second.say(resuming(answer["session"], 0))
        await until(lambda: session.ending == TAKEN_OVER)
        # The older connection has not handed it over: nothing answers yet.
        await asyncio.sleep(0.05)
        assert second.sent.empty()
        held_up.go.set()
        # Once handed over, at once: the hint of the hand-over wakes the newer connection,
        # well within takeover_timeout.
        async with asyncio.timeout(SETTINGS.takeover_timeout / 2):
            resumed = await second.hear()
        assert (resumed["session"], resumed["resumed"], resumed["received"]) == (
            answer["session"],
            True,
            1,
        )
        # What the message in hand sent, the newer connection sends.
        error = await second.hear()
        assert (error["seq"], error["reply_to"]) == (1, 1)
        await older
        assert first.closed == 4000
        # The device sends 2 again, which the newer one handles.
        second.say({"type": "device.changed", "seq": 2})
        assert (await second.hear())["reply_to"] == 2
        second.gone()
        await newer


class Gated(Channel):
    """A connection whose device reads what the cloud sends only while ``reading`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.reading = asyncio.Event()
        self.reading.set()

    async def send(self, text: str) -> None:
        await self.reading.wait()
        await super().send(text)


async def test_a_session_taken_over_drops_what_waits_to_be_sent() -> None:
    """The newer connection sends it again (Resuming a session): the older one closes with
    4000 without it."""
    store, records, bus = Store(), Records(), Bus()
    replica = links(store, hints=bus, records=records)
    opening = held(store)
    first, second = Gated(), Channel()
    async with running(replica, heard=bus):
        older, answer = await opened(replica, opening, first)
        session = session_of(replica, first)
        first.reading.clear()
        for seq in (1, 2, 3):
            first.say({"type": "device.changed", "seq": seq})  # each answered with an error
        # The first error leaves once the device reads, and the others wait behind it.
        await until(lambda: session._outbox.qsize() == 2)
        newer, resumed = await opened(replica, opening, second, resuming(answer["session"], 0))
        assert resumed["resumed"] is True
        # Only the close waits now.
        await until(lambda: session._outbox.qsize() == 1)
        first.reading.set()
        await older
        assert first.closed == 4000
        assert [first.sent.get_nowait()["reply_to"] for _ in range(first.sent.qsize())] == [1]
        assert [(await second.hear())["reply_to"] for _ in range(3)] == [1, 2, 3]
        second.gone()
        await newer


async def test_a_session_whose_message_in_hand_must_be_stopped_is_not_resumed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, records, bus = Store(), Records(), Bus()
    replica = links(store, hints=bus, records=records, close_timeout=0.2, takeover_timeout=0.4)
    opening = held(store)
    first, second = Channel(), Channel()
    async with running(replica, heard=bus):
        older, answer = await opened(replica, opening, first)
        held_up = HeldUp(session_of(replica, first))
        first.say({"type": "device.changed", "seq": 1})
        await held_up.handling.wait()
        newer = asyncio.create_task(replica.serve(second, opening))  # type: ignore[arg-type]
        second.say(resuming(answer["session"], 0))
        # Not handed over: the older connection's session ends, and the newer gets a new one.
        assert await first.hear() == {"type": "bye", "reason": "replaced"}
        await older
        assert first.closed == 1000
        resumed = await second.hear()
        assert resumed["resumed"] is False
        assert resumed["session"] != answer["session"]
        assert records.held[opening.device_id].record.session == UUID(resumed["session"])
        second.gone()
        await newer
    assert why_new(caplog) == ["gone"]


def why_new(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Why each resume that failed got a new session."""
    return [
        each.why  # type: ignore[attr-defined]
        for each in caplog.records
        if each.getMessage() == "a device's session could not be resumed: it gets a new one"
    ]


async def test_a_session_not_handed_over_in_time_is_not_resumed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, presence, records, bus = Store(), Presence(), Records(), Bus()
    # One replica hears no hints, as one cut off from Redis.
    deaf = links(store, presence, Hints(), records=records)
    replica = links(store, presence, bus, records=records, takeover_timeout=0.2)
    opening = held(store)
    first, second = Channel(), Channel()
    async with running(replica, heard=bus):
        older, answer = await opened(deaf, opening, first)
        newer, resumed = await opened(replica, opening, second, resuming(answer["session"], 0))
        assert resumed["resumed"] is False
        assert why_new(caplog) == ["not_handed"]
        # The new session replaced the older, which its connection no longer writes.
        first.gone()
        await older
        assert records.held[opening.device_id].record.session == UUID(resumed["session"])
        assert records.held[opening.device_id].owner == session_of(replica, second).connection
        second.gone()
        await newer


class Arriving(Presence):
    """The presence, which runs ``arriving`` as a connection becomes its device's newest."""

    def __init__(self) -> None:
        super().__init__()
        self.arriving: Callable[[], None] = lambda: None

    async def arrive(self, device_id: UUID, connection: UUID) -> UUID | None:
        self.arriving()
        return await super().arrive(device_id, connection)


@pytest.mark.parametrize("claimed", [True, False])
async def test_a_session_taken_over_as_it_starts_hands_it_over(claimed: bool) -> None:
    """A newer connection that resumes the session as the older starts it: the older closes
    with 4000 once it handed the session over, and ends it with bye replaced otherwise."""
    store, presence, records = Store(), Arriving(), Records()
    replica = links(store, presence, records=records)
    opening = held(store)
    newer = uuid.uuid7()

    def arriving() -> None:
        if claimed:
            records.held[opening.device_id].taker = newer
        for session in replica._held():
            session.end(TAKEN_OVER)

    presence.arriving = arriving
    channel = Channel()
    serving = asyncio.create_task(replica.serve(channel, opening))  # type: ignore[arg-type]
    channel.say(hello())
    if claimed:
        await serving
        assert channel.sent.empty()
        assert channel.closed == 4000
        assert records.held[opening.device_id].owner == newer
    else:
        assert await channel.hear() == {"type": "bye", "reason": "replaced"}
        await serving
        assert channel.closed == 1000


class Starting(Records):
    """The sessions' records, in which ``meanwhile`` runs as a session starts, before its
    connection holds it."""

    def __init__(self) -> None:
        super().__init__()
        self.meanwhile: Callable[[UUID], None] = lambda _: None

    async def start(self, record: Record, connection: UUID) -> None:
        await super().start(record, connection)
        self.meanwhile(record.device_id)


@pytest.mark.parametrize("meanwhile", ["claimed", "replaced"])
async def test_a_session_claimed_before_its_connection_holds_it_is_found_so(
    meanwhile: str,
) -> None:
    """A newer connection's claim, or its new session, that comes as the session starts finds
    no session held to hint: the connection finds it once it holds the session."""
    store, records = Store(), Starting()
    replica = links(store, records=records)
    opening = held(store)
    newer = uuid.uuid7()

    def starting(device_id: UUID) -> None:
        if meanwhile == "claimed":
            records.held[device_id].taker = newer
        else:
            replacing = Record(uuid.uuid7(), device_id, REGISTERED, "{}", MINOR)
            records.held[device_id] = Held(replacing, newer)

    records.meanwhile = starting
    channel = Channel()
    serving = asyncio.create_task(replica.serve(channel, opening))  # type: ignore[arg-type]
    channel.say(hello())
    if meanwhile == "claimed":
        await serving
        assert channel.sent.empty()
        assert channel.closed == 4000
    else:
        assert await channel.hear() == {"type": "bye", "reason": "replaced"}
        await serving
        assert channel.closed == 1000
    assert records.held[opening.device_id].owner == newer


async def test_a_resume_that_a_newer_one_supersedes_takes_nothing_and_closes_with_4000(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The session goes on on the newer connection, which claimed it while this one waited:
    this one starts no new session in its place."""
    store, presence, records, bus = Store(), Presence(), Records(), Bus()
    deaf = links(store, presence, Hints(), records=records)
    replica = links(store, presence, bus, records=records, takeover_timeout=0.2)
    opening = held(store)
    first, second = Channel(), Channel()
    async with running(replica, heard=bus):
        older, answer = await opened(deaf, opening, first)
        newer = asyncio.create_task(replica.serve(second, opening))  # type: ignore[arg-type]
        second.say(resuming(answer["session"], 0))
        kept = records.held[opening.device_id]
        await until(lambda: kept.taker is not None)
        newest = uuid.uuid7()
        claimed = await records.claim(
            opening.device_id, newest, kept.record.session, REGISTERED, kept.record.terms, MINOR
        )
        assert claimed is Claim.WAITING
        await newer
        assert second.sent.empty()
        assert second.closed == 4000
        assert records.held[opening.device_id] is kept
        assert (kept.record.session, kept.taker) == (UUID(answer["session"]), newest)
        first.gone()
        await older
    assert why_new(caplog) == []


ENDS = [
    pytest.param(lambda channel, _: channel.gone(), True, id="lost"),
    pytest.param(lambda channel, _: channel.gone(1000), True, id="closed"),
    pytest.param(lambda channel, _: channel.gone(1001), True, id="going away"),
    pytest.param(lambda _, session: session.end(RECONNECT), True, id="reconnect"),
    pytest.param(lambda channel, _: channel.gone(1002), False, id="protocol error"),
    pytest.param(lambda channel, _: channel.gone(1007), False, id="invalid text"),
    pytest.param(lambda channel, _: channel.gone(1008), False, id="policy"),
    pytest.param(lambda channel, _: channel.gone(1009), False, id="too big"),
    pytest.param(
        lambda channel, _: channel.say({"type": "bye", "reason": "stopping"}),
        False,
        id="the device's bye",
    ),
    pytest.param(lambda _, session: session.end("revoked"), False, id="the cloud's bye"),
    pytest.param(lambda channel, _: channel.say("not JSON"), False, id="invalid message"),
]


@pytest.mark.parametrize(("ending", "kept"), ENDS)
async def test_a_session_is_kept_after_a_pause_and_forgotten_once_it_ends(
    ending: Callable[[Channel, Any], None], kept: bool
) -> None:
    """A loss, a close but for 1002, 1007, 1008 and 1009, and a reconnect leave the session to
    resume; any bye ends it for good (Resuming a session)."""
    store, records = Store(), Records()
    replica = links(store, records=records)
    opening = held(store)
    channel = Channel()
    serving = await started(replica, opening, channel)
    ending(channel, session_of(replica, channel))
    await serving
    assert (opening.device_id in records.held) is kept
    if kept:
        assert records.held[opening.device_id].owner is None


async def test_a_session_whose_record_a_newer_one_replaced_ends_at_the_next_check() -> None:
    """A newer session that started but has not arrived yet replaces the record before the
    presence: the older ends as replaced, and leaves the newer's record as it is."""
    store, records = Store(), Records()
    replica = links(store, records=records)
    opening = held(store)
    channel = Channel()
    serving = await started(replica, opening, channel)
    newer = Held(Record(uuid.uuid7(), opening.device_id, REGISTERED, "{}", MINOR), uuid.uuid7())
    records.held[opening.device_id] = newer
    await replica._check(replica._held())
    assert await channel.hear() == {"type": "bye", "reason": "replaced"}
    await serving
    assert records.held[opening.device_id] is newer


async def test_a_silent_session_is_kept_to_resume() -> None:
    store, records = Store(), Records()
    replica = links(store, records=records, ping_interval=0.05)
    opening = held(store)
    channel = Channel()
    serving = await started(replica, opening, channel)
    await serving
    assert channel.closed == 1001
    assert records.held[opening.device_id].owner is None


async def test_a_dropped_message_counts_as_handled() -> None:
    store = Store()
    replica = links(store, backlog=1)
    channel = Channel()
    serving = await started(replica, held(store), channel)
    # The first waits its turn, and the others are dropped.
    for seq in range(1, 6):
        channel.say({"type": "device.changed", "seq": seq})
    told = [await channel.hear() for _ in range(2)]
    assert [each["reply_to"] for each in told] == [2, 1]
    channel.say({"type": "ping", "received": 0})
    assert await channel.hear() == {"type": "pong", "received": 5}
    channel.gone()
    await serving


@pytest.mark.parametrize(
    ("replay", "ping", "received", "again"),
    [
        # What the device said it received, the cloud keeps no more.
        (1000, 2, 1, None),
        (1000, 2, 2, [3]),
        (1000, 2, 3, []),
        (1000, None, 0, [1, 2, 3]),
        # And it keeps no more than replay.
        (2, None, 0, None),
        (2, None, 1, [2, 3]),
        # What it did not send, the device cannot have received.
        (1000, None, 4, None),
    ],
)
async def test_a_resumed_session_sends_again_what_its_device_did_not_receive(
    caplog: pytest.LogCaptureFixture,
    replay: int,
    ping: int | None,
    received: int,
    again: list[int] | None,
) -> None:
    store, records = Store(), Records()
    replica = links(store, records=records, replay=replay)
    opening = held(store)
    first, second = Channel(), Channel()
    older, answer = await opened(replica, opening, first)
    for seq in (1, 2, 3):
        first.say({"type": "device.changed", "seq": seq})
    assert [(await first.hear())["seq"] for _ in range(3)] == [1, 2, 3]
    if ping is not None:
        first.say({"type": "ping", "received": ping})
        assert await first.hear() == {"type": "pong", "received": 3}
    first.gone()
    await older
    newer, resumed = await opened(replica, opening, second, resuming(answer["session"], received))
    if again is None:
        assert resumed["resumed"] is False
        assert why_new(caplog) == ["received"]
    else:
        assert (resumed["session"], resumed["resumed"], resumed["received"]) == (
            answer["session"],
            True,
            3,
        )
        sent = [await second.hear() for _ in again]
        assert [(each["seq"], each["reply_to"]) for each in sent] == [(n, n) for n in again]
    second.say({"type": "ping", "received": 0})
    assert (await second.hear())["type"] == "pong"
    second.gone()
    await newer


async def test_a_resumed_session_takes_what_its_hello_says_now() -> None:
    """The device's time zone, languages and allowed classes, and the conversation it names,
    which the cloud ends after what it sends again (The session, hello: A resuming hello)."""
    store = Store()
    replica = links(store)
    opening = held(store)
    first, second = Channel(), Channel()
    older, answer = await opened(replica, opening, first)
    first.say({"type": "device.changed", "seq": 1})
    assert (await first.hear())["seq"] == 1
    first.gone()
    await older
    conversation = str(uuid.uuid7())
    newer, resumed = await opened(
        replica,
        opening,
        second,
        resuming(
            answer["session"],
            0,
            time_zone="Europe/Paris",
            languages=["en"],
            allowed_classes=["informational"],
            conversation=conversation,
        ),
    )
    assert resumed["resumed"] is True
    assert (await second.hear())["seq"] == 1
    assert await second.hear() == {
        "type": "conversation.end",
        "seq": 2,
        "conversation": conversation,
        "reason": "lost",
    }
    session = session_of(replica, second)
    assert (session.hello.time_zone, session.hello.languages) == ("Europe/Paris", ("en",))
    assert session.hello.allowed_classes == ("informational",)
    second.gone()
    await newer


def unregistered(records: Records, opening: Opening) -> None:
    held = records.held[opening.device_id]
    held.record = dataclasses.replace(held.record, registered=REGISTERED + timedelta(seconds=1))


def newer_version(records: Records, opening: Opening) -> None:
    held = records.held[opening.device_id]
    held.record = dataclasses.replace(held.record, minor=MINOR + 1)


@pytest.mark.parametrize(
    ("changes", "meanwhile", "why"),
    [
        ({"session": str(uuid.uuid7())}, None, "gone"),
        ({}, unregistered, "registration"),
        ({}, newer_version, "version"),
        ({"protocol": "3.8"}, None, "terms"),
        ({"device": {"kind": "robot", "model": "concept-v0", "software": "0.5.0"}}, None, "terms"),
        ({"conversations": False}, None, "terms"),
        ({"media": []}, None, "terms"),
    ],
)
async def test_a_resume_the_cloud_cannot_take_gets_a_new_session(
    caplog: pytest.LogCaptureFixture,
    changes: dict[str, Any],
    meanwhile: Callable[[Records, Opening], None] | None,
    why: str,
) -> None:
    """Only the device's session, under the registration of its connection (SEC-6), on the
    terms of its first hello, of a version the cloud speaks (Resuming a session)."""
    store, records = Store(), Records()
    replica = links(store, records=records)
    opening = held(store)
    first, second = Channel(), Channel()
    older, answer = await opened(replica, opening, first)
    first.gone()
    await older
    if meanwhile is not None:
        meanwhile(records, opening)
    asked = {"session": answer["session"]} | changes
    resume = hello(**{name: value for name, value in asked.items() if name != "session"})
    resume["resume"] = {"session": asked["session"], "received": 0}
    newer, resumed = await opened(replica, opening, second, resume)
    assert resumed["resumed"] is False
    assert resumed["session"] != answer["session"]
    assert why_new(caplog) == [why]
    second.gone()
    await newer


async def test_a_resume_after_a_new_session_takes_the_new_one_only() -> None:
    store, records = Store(), Records()
    replica = links(store, records=records)
    opening = held(store)
    first, second, third = Channel(), Channel(), Channel()
    older, answer = await opened(replica, opening, first)
    first.gone()
    await older
    newer, _ = await opened(replica, opening, second)
    second.gone()
    await newer
    newest, resumed = await opened(replica, opening, third, resuming(answer["session"], 0))
    assert resumed["resumed"] is False
    third.gone()
    await newest


@pytest.mark.parametrize(("last", "resumed"), [("x", True), ("y", False)])
async def test_a_session_s_record_keeps_a_digest_of_its_terms(last: str, resumed: bool) -> None:
    """What a device adds to its terms costs the record 64 characters, whatever its size, and a
    resume still compares all of them (ADR-0013, decision 1)."""
    store, records = Store(), Records()
    replica = links(store, records=records)
    opening = held(store)
    first, second = Channel(), Channel()
    device = hello()["device"]
    big = hello(device=device | {"notes": "x" * 1_000_000})
    older, answer = await opened(replica, opening, first, big)
    assert len(records.held[opening.device_id].record.terms) == 64
    first.gone()
    await older
    again = resuming(answer["session"], 0, device=device | {"notes": "x" * 999_999 + last})
    newer, resume = await opened(replica, opening, second, again)
    assert resume["resumed"] is resumed
    second.gone()
    await newer


async def test_a_resume_keeps_the_events_its_session_began_with() -> None:
    """A resuming hello's events are not read: those declared since wait for a new session
    (hello: A resuming hello)."""
    store, records = Store(), Records()
    replica = links(store, records=records)
    opening = held(store)
    first, second = Channel(), Channel()
    older, answer = await opened(replica, opening, first)
    first.gone()
    await older
    event = {"name": "button.pressed", "description": "A button.", "schema": {"type": "object"}}
    resume = resuming(answer["session"], 0, events=[event])
    newer, resumed = await opened(replica, opening, second, resume)
    assert (resumed["session"], resumed["resumed"]) == (answer["session"], True)
    assert resumed["events"] == answer["events"]
    second.gone()
    await newer


class Unreliable(Records):
    """Records whose call ``failing`` fails, as when Redis is unavailable."""

    def __init__(self, failing: str) -> None:
        super().__init__()
        self.failing = failing

    async def forget(self, device_ids: Collection[UUID]) -> None:
        if self.failing == "forget":
            raise ConnectionError("Redis is unavailable")
        await super().forget(device_ids)

    async def end(self, device_id: UUID, connection: UUID, session: UUID) -> None:
        if self.failing == "end":
            raise ConnectionError("Redis is unavailable")
        await super().end(device_id, connection, session)


@pytest.mark.parametrize("failing", ["forget", "end"])
@pytest.mark.parametrize(
    "pausing",
    [
        pytest.param(lambda channel, _: channel.gone(), id="lost"),
        pytest.param(lambda _, session: session.end(RECONNECT), id="reconnect"),
    ],
)
async def test_a_leave_that_completes_as_its_session_pauses_ends_it_for_good(
    failing: str, pausing: Callable[[Channel, Any], None]
) -> None:
    """Its device left, so nothing resumes the session, whether its connection was lost or
    the cloud paused it: both its removal and its end forget it, so that either does should
    the other fail."""
    store, records = Store(), Unreliable(failing)
    removing, removed = asyncio.Event(), asyncio.Event()

    async def held_up() -> None:
        removing.set()
        await removed.wait()

    store.removing = held_up
    replica = links(store, records=records)
    opening = held(store)
    channel = Channel()
    serving = await started(replica, opening, channel)
    [session] = replica._held()
    channel.say({"type": "leave", "seq": 1})
    async with asyncio.timeout(5):
        await removing.wait()
    # The session pauses while the leave is in hand, waiting for it.
    pausing(channel, session)
    await until(lambda: session._halted)
    removed.set()
    async with asyncio.timeout(5):
        await serving
    assert session.ending == "left"
    assert opening.device_id not in records.held


class HeldBack:
    """The replicas' records, but for one replica's first keep, which waits for ``go``, as
    the next step of a busy replica, or a slow round trip to Redis, would."""

    def __init__(self, records: Records) -> None:
        self._records = records
        self.go, self.waiting = asyncio.Event(), asyncio.Event()
        self._first = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self._records, name)

    async def keep(self, held: Sequence[tuple[UUID, UUID, UUID]]) -> Any:
        if self._first:
            self._first = False
            self.waiting.set()
            await self.go.wait()
        return await self._records.keep(held)


class Dropped(Channel):
    """A connection that the device dropped: its close is never answered, as on a TCP
    connection that is gone."""

    async def close(self, code: int) -> None:
        self.closed = code
        await asyncio.Event().wait()


async def test_a_new_session_replaced_as_it_begins_leaves_the_device_to_the_newer() -> None:
    """Two new sessions of one device begin at once, on two replicas: the one that the record
    no longer names ends with bye replaced, and takes nothing from the newer one."""
    store, presence, records, bus = Store(), Presence(), Records(), Bus()
    slow = HeldBack(records)
    busy = links(store, presence, bus, records=slow, close_timeout=0.2)  # type: ignore[arg-type]
    other = links(store, presence, bus, records=records)
    opening = held(store)
    older, newer = Dropped(), Channel()
    async with running(busy, other, heard=bus):
        serving_older = asyncio.create_task(busy.serve(older, opening))  # type: ignore[arg-type]
        older.say(hello())
        # The older connection's new session is in the record, and its keep waits.
        async with asyncio.timeout(5):
            await slow.waiting.wait()
        serving_newer, _ = await opened(other, opening, newer)
        slow.go.set()
        assert await older.hear() == {"type": "bye", "reason": "replaced"}
        await serving_older
        session = session_of(other, newer)
        assert session.ending is None
        assert presence.newest[opening.device_id] == session.connection
        newer.gone()
        await serving_newer


class Muted(Bus):
    """Hints that, once ``left`` more were published, wait for ever, as Redis that no longer
    answers would."""

    def __init__(self) -> None:
        super().__init__()
        self.left: int | None = None

    async def publish(self, device_ids: Collection[UUID]) -> None:
        if self.left is not None:
            if self.left == 0:
                await asyncio.Event().wait()
            self.left -= 1
        await super().publish(device_ids)


async def test_a_hand_over_whose_hint_does_not_leave_ends_within_one_close_timeout() -> None:
    """Handed over all the same: the older connection closes with 4000 within close_timeout,
    and the newer one finds the session at its last try."""
    store, records, bus = Store(), Records(), Muted()
    replica = links(store, hints=bus, records=records, close_timeout=0.2, takeover_timeout=0.4)
    opening = held(store)
    first, second = Channel(), Channel()
    async with running(replica, heard=bus):
        older, answer = await opened(replica, opening, first)
        # The claim's hint leaves, the hand-over's does not.
        bus.left = 1
        started_at = asyncio.get_running_loop().time()
        newer = asyncio.create_task(replica.serve(second, opening))  # type: ignore[arg-type]
        second.say(resuming(answer["session"], 0))
        async with asyncio.timeout(2):
            await older
        assert asyncio.get_running_loop().time() - started_at < 0.3
        assert first.closed == 4000
        resumed = await second.hear()
        assert (resumed["session"], resumed["resumed"]) == (answer["session"], True)
        second.gone()
        await newer


class TakenAtOnce(Unanswered):
    """Records whose session a newer connection claims as soon as it starts, and that Redis
    no longer answers then."""

    async def keep(self, held: Sequence[tuple[UUID, UUID, UUID]]) -> Any:
        for device_id, _, _ in held:
            self.held[device_id].taker = uuid.uuid7()
        return await super().keep(held)


async def test_a_session_taken_over_as_it_begins_ends_within_one_close_timeout() -> None:
    """Giving it up, its bye and its close together (ADR-0012, decision 2)."""
    store = Store()
    replica = links(store, records=TakenAtOnce(), close_timeout=0.2)
    channel = Deaf()
    started_at = asyncio.get_running_loop().time()
    serving = asyncio.create_task(replica.serve(channel, held(store)))  # type: ignore[arg-type]
    channel.say(hello())
    async with asyncio.timeout(2):
        await serving
    assert channel.closed is not None
    assert asyncio.get_running_loop().time() - started_at < 0.3
