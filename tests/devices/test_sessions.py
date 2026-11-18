# SPDX-License-Identifier: MIT
"""A control channel's session, and a replica's checks of the sessions it holds, against
ports of the tests' own: what timing decides elsewhere, such as an end that comes while a
session starts, happens here when a test says (docs/decisions/0011-control-channels.md)."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from link import DEEP, hello

from ai_cloud.devices.domain.errors import Unavailable
from ai_cloud.devices.domain.link import Closed
from ai_cloud.devices.domain.model import Description, Opening, TokenHolder
from ai_cloud.devices.service.links import RECONNECT, Links
from ai_cloud.devices.settings import LinkSettings

pytestmark = pytest.mark.anyio

REGISTERED = datetime(2026, 11, 11, tzinfo=UTC)
SETTINGS = LinkSettings(
    hello_timeout=5,
    ping_interval=20,
    rate=1000,
    burst=1000,
    backlog=10,
    check_interval=20,
    presence_ttl=60,
    close_timeout=1,
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


class Store:
    """The registry: the devices it holds, under their registration. ``describing`` runs while
    a hello is kept, as a change elsewhere would."""

    def __init__(self) -> None:
        self.held: dict[UUID, datetime] = {}
        self.describing: Callable[[], None] = lambda: None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Store]:
        yield self

    async def describe(self, device_id: UUID, registered: datetime, _: Description) -> bool:
        self.describing()
        return self.held.get(device_id) == registered

    async def registrations(self, device_ids: Collection[UUID]) -> dict[UUID, datetime]:
        return {each: self.held[each] for each in device_ids if each in self.held}


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


class Hints:
    async def publish(self, device_ids: Collection[UUID]) -> None:
        pass

    async def listen(self) -> AsyncIterator[UUID]:
        await asyncio.Event().wait()
        yield uuid.uuid7()


def links(store: Store, presence: Presence | None = None, **changes: Any) -> Links:
    settings = SETTINGS.model_copy(update=changes)
    return Links(
        settings=settings,
        leeway=30,
        tokens=None,  # type: ignore[arg-type]  # no handshake here
        store=store,  # type: ignore[arg-type]
        presence=presence or Presence(),
        hints=Hints(),  # type: ignore[arg-type]
    )


def held(store: Store) -> Opening:
    device_id = uuid.uuid7()
    store.held[device_id] = REGISTERED
    return Opening(device_id, REGISTERED)


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
    channel.say({"type": "ping", "received": 0})
    assert await channel.hear() == {"type": "pong", "received": 3}
    async with asyncio.timeout(5):
        while session.hello.allowed_classes != ("informational",):
            await asyncio.sleep(0.01)
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
    assert session.left
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
    store = Store()
    replica = links(store, ping_interval=0.1)
    channel = Deaf()
    serving = asyncio.create_task(replica.serve(channel, held(store)))  # type: ignore[arg-type]
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
    assert await channel.hear() == {"type": "bye", "reason": "no_hello"}
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
    replica = links(store, close_timeout=0.1)
    channel = failing(0)
    async with asyncio.timeout(2):
        await replica.serve(channel, held(store))  # type: ignore[arg-type]
    assert channel.closed == 1011
    assert replica._held() == []


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
    def authenticate(self, access_token: str) -> TokenHolder:
        return TokenHolder(uuid.UUID(access_token), int(REGISTERED.timestamp()))


async def test_a_handshake_that_the_replica_goes_away_during_is_refused() -> None:
    store = Unhurried()
    replica = Links(
        settings=SETTINGS,
        leeway=30,
        tokens=Tokens(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        presence=Presence(),
        hints=Hints(),  # type: ignore[arg-type]
    )
    opening = asyncio.create_task(replica.open(str(uuid.uuid7())))
    await store.reading.wait()
    await replica.going_away()
    store.go.set()
    with pytest.raises(Unavailable):
        await opening


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
