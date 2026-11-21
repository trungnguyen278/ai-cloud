# SPDX-License-Identifier: MIT
"""What a WebSocket connection may send below the application, and how long it may take to
close, whatever its peer reads (docs/decisions/0012-what-one-device-can-exhaust.md, and the
cloud link's Frames, since 3.4). tests/devices/test_link.py meets the same bounds in the real
server; these count what the protocol parses and writes, through a transport of their own."""

import asyncio
import logging
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from uvicorn import Config
from uvicorn.server import ServerState
from websockets.frames import Frame, Opcode

from ai_cloud.entry.websocket import PIECE, PINGS_A_SECOND, SMALLEST_FRAGMENT, WebSocketProtocol
from ai_cloud.shared import rates

pytestmark = pytest.mark.anyio

TIMEOUT = 0.01
# Long enough for a cut that should not come, at Windows' timer resolution of some 16 ms.
PAST = 10 * TIMEOUT
# The link's ping interval, and the Pings that it lets through at once: twice as many,
# rounded up.
PING_INTERVAL = 3.75
ALLOWANCE = 8
# The smallest fragment that is not its message's last.
KIB = b"a" * SMALLEST_FRAGMENT

HANDSHAKE = (
    b"GET /link HTTP/1.1\r\n"
    b"Host: cloud.test\r\n"
    b"Upgrade: websocket\r\n"
    b"Connection: Upgrade\r\n"
    b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
    b"Sec-WebSocket-Version: 13\r\n"
    b"\r\n"
)
# The close frames that the server sends, unmasked: 1008 with its reasons, and 1007.
TOO_MANY_PINGS = b"\x88\x10\x03\xf0too many pings"
FRAGMENT_TOO_SMALL = b"\x88\x14\x03\xf0fragment too small"
INVALID_TEXT = b"\x88\x02\x03\xef"


class Transport:
    """What the protocol asks of its transport: what it wrote, whether it reads, and how
    often it closed it or cut it."""

    def __init__(self) -> None:
        self.written = bytearray()
        self.reading = True
        self.closed = 0
        self.aborted = 0

    def get_extra_info(self, name: str, default: object = None) -> object:
        return default

    def write(self, data: bytes) -> None:
        self.written += data

    def is_closing(self) -> bool:
        return self.closed > 0

    def pause_reading(self) -> None:
        self.reading = False

    def resume_reading(self) -> None:
        self.reading = True

    def close(self) -> None:
        self.closed += 1

    def abort(self) -> None:
        self.aborted += 1


class Application:
    """An ASGI application that accepts the connection, then takes each message once told
    to, and keeps what it received."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.take = asyncio.Event()

    async def __call__(self, scope: object, receive: Any, send: Any) -> None:
        if (await receive())["type"] != "websocket.connect":
            return
        await send({"type": "websocket.accept"})
        while True:
            await self.take.wait()
            message = await receive()
            self.received.append(message)
            if message["type"] == "websocket.disconnect":
                return


async def nothing(scope: object, receive: object, send: object) -> None:
    pass


def connected(transport: Transport, app: Any = nothing) -> WebSocketProtocol:
    # The process's logging stays as it is.
    config = Config(app=app, ws_ping_interval=None, log_config=None)
    config.load()
    protocol = WebSocketProtocol(
        config=config,
        server_state=ServerState(),
        app_state={},
        close_timeout=TIMEOUT,
        link_ping_interval=PING_INTERVAL,
    )
    protocol.connection_made(transport)  # type: ignore[arg-type]
    return protocol


async def opened(transport: Transport, app: Application) -> WebSocketProtocol:
    protocol = connected(transport, app)
    protocol.data_received(HANDSHAKE)
    async with asyncio.timeout(5):
        while b"101" not in transport.written:
            await asyncio.sleep(0)
    transport.written.clear()
    return protocol


def frame(opcode: Opcode, data: bytes = b"", *, fin: bool = True) -> bytes:
    return Frame(opcode, data, fin=fin).serialize(mask=True)


def frozen(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The clock of the protocol's buckets, which moves only when told to."""
    now = [0.0]
    monkeypatch.setattr(rates, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


async def received(app: Application, count: int = 1) -> list[dict[str, Any]]:
    app.take.set()
    async with asyncio.timeout(5):
        while len(app.received) < count:
            await asyncio.sleep(0)
    return app.received


async def disconnected(app: Application) -> dict[str, Any]:
    app.take.set()
    async with asyncio.timeout(5):
        while not app.received or app.received[-1]["type"] != "websocket.disconnect":
            await asyncio.sleep(0)
    return app.received[-1]


@pytest.fixture
def server_errors(caplog: pytest.LogCaptureFixture) -> Iterator[Callable[[], list[str]]]:
    """What uvicorn's own logger and asyncio's log as errors, whatever propagates from them."""
    loggers = [logging.getLogger("uvicorn.error"), logging.getLogger("asyncio")]
    for each in loggers:
        each.addHandler(caplog.handler)
    try:
        yield lambda: [
            each.getMessage() for each in caplog.records if each.levelno >= logging.ERROR
        ]
    finally:
        for each in loggers:
            each.removeHandler(caplog.handler)


# Pings and Pongs.


@pytest.mark.parametrize(
    ("allowed", "past"),
    [
        pytest.param(frame(Opcode.PONG) * ALLOWANCE, frame(Opcode.PONG), id="pongs"),
        pytest.param(frame(Opcode.PING) * ALLOWANCE, frame(Opcode.PING), id="pings"),
        pytest.param(
            (frame(Opcode.PING) + frame(Opcode.PONG)) * (ALLOWANCE // 2),
            frame(Opcode.PONG),
            id="together",
        ),
    ],
)
async def test_pings_and_pongs_close_the_connection_past_their_allowance(
    monkeypatch: pytest.MonkeyPatch, allowed: bytes, past: bytes
) -> None:
    frozen(monkeypatch)
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    protocol.data_received(allowed)
    assert not transport.closed
    protocol.data_received(past)
    assert transport.written.endswith(TOO_MANY_PINGS)
    assert transport.closed == 1
    assert (await disconnected(app))["code"] == 1008
    # Nothing more is read, nor answered.
    transport.written.clear()
    protocol.data_received(frame(Opcode.PING))
    assert not transport.written


async def test_the_pings_rate_comes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    now = frozen(monkeypatch)
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    protocol.data_received(frame(Opcode.PONG) * ALLOWANCE)
    now[0] = 1 / PINGS_A_SECOND
    protocol.data_received(frame(Opcode.PONG))
    assert not transport.closed
    protocol.data_received(frame(Opcode.PONG))
    assert transport.closed == 1


async def test_a_pong_that_answers_the_servers_ping_counts_for_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen(monkeypatch)
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    # As uvicorn's own Ping leaves it.
    protocol.pending_ping_payload = b"p"
    protocol.data_received(frame(Opcode.PONG, b"q") * ALLOWANCE)
    protocol.data_received(frame(Opcode.PONG, b"p"))
    assert not transport.closed
    # The Ping is answered: the same Pong once more is unsolicited.
    protocol.data_received(frame(Opcode.PONG, b"p"))
    assert transport.closed == 1


async def test_messages_cost_none_of_the_pings_allowance(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen(monkeypatch)
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    app.take.set()
    for n in range(3 * ALLOWANCE):
        protocol.data_received(frame(Opcode.TEXT, str(n).encode()))
        await asyncio.sleep(0)
    await received(app, 3 * ALLOWANCE)
    assert not transport.closed
    assert [message["text"] for message in app.received] == [str(n) for n in range(3 * ALLOWANCE)]


# Fragments.


@pytest.mark.parametrize(
    "fragments",
    [
        pytest.param(frame(Opcode.TEXT, KIB[1:], fin=False), id="the first"),
        pytest.param(frame(Opcode.BINARY, KIB[1:], fin=False), id="the first of bytes"),
        pytest.param(
            frame(Opcode.TEXT, KIB, fin=False) + frame(Opcode.CONT, KIB[1:], fin=False),
            id="a continuation",
        ),
        pytest.param(
            frame(Opcode.TEXT, KIB, fin=False) + frame(Opcode.CONT, fin=False) * 1000,
            id="a flood of empty continuations",
        ),
    ],
)
async def test_a_fragment_under_1_kib_that_is_not_the_last_closes_the_connection(
    fragments: bytes,
) -> None:
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    protocol.data_received(fragments)
    assert transport.written.endswith(FRAGMENT_TOO_SMALL)
    assert transport.closed == 1
    assert (await disconnected(app))["code"] == 1008
    assert [message["type"] for message in app.received] == ["websocket.disconnect"]


@pytest.mark.parametrize(
    ("opcode", "last"),
    [
        pytest.param(Opcode.TEXT, b"", id="an empty last"),
        pytest.param(Opcode.TEXT, b"z", id="a short last"),
        pytest.param(Opcode.BINARY, KIB, id="bytes"),
    ],
)
async def test_fragments_of_1_kib_or_more_but_the_last_are_a_message(
    opcode: Opcode, last: bytes
) -> None:
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    fragments = [KIB, KIB + b"b", last]
    protocol.data_received(
        frame(opcode, fragments[0], fin=False)
        + frame(Opcode.CONT, fragments[1], fin=False)
        + frame(Opcode.CONT, fragments[2])
    )
    whole = b"".join(fragments)
    expected = {"text": whole.decode()} if opcode is Opcode.TEXT else {"bytes": whole}
    assert await received(app) == [{"type": "websocket.receive", **expected}]
    assert not transport.closed


# What is parsed.


async def test_a_broken_rule_is_parsed_no_further_than_its_piece(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen(monkeypatch)
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    parsed: list[int] = []
    receive = protocol.conn.receive_data
    monkeypatch.setattr(
        protocol.conn, "receive_data", lambda data: (parsed.append(len(data)), receive(data))
    )
    pongs = frame(Opcode.PONG) * 1000
    protocol.data_received(pongs)
    assert transport.closed == 1
    # The piece that holds the Pong past the allowance, and nothing of what came after it,
    # which is let go.
    assert len(frame(Opcode.PONG)) * (ALLOWANCE + 1) <= PIECE < len(pongs)
    assert parsed == [PIECE]
    assert not protocol._unread


async def test_what_waits_for_the_application_is_not_parsed() -> None:
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    message = frame(Opcode.TEXT, b"{}")
    sent = 1000
    protocol.data_received(message * sent)
    # uvicorn stops reading once a message waits for the application; the rest of the piece
    # that held it was parsed with it, and no more.
    assert protocol.queue.qsize() <= PIECE // len(message) + 1
    assert not transport.reading
    assert [message["text"] for message in await received(app, sent)] == ["{}"] * sent
    # It reads again once all that came is taken.
    async with asyncio.timeout(5):
        while not transport.reading:
            await asyncio.sleep(0)
    assert not transport.closed


# A message that ends where the second piece ends, after 2 bytes of header, 2 of length and 4
# of mask (RFC 6455, 5.2).
LONG = frame(Opcode.TEXT, b"a" * (2 * PIECE - 8))


async def first_taken(transport: Transport) -> WebSocketProtocol:
    """A connection that read a message of two pieces and one after it, of which the
    application took the first, and so resumed reading."""
    protocol = await opened(transport, Application())
    assert len(LONG) == 2 * PIECE
    protocol.data_received(LONG + frame(Opcode.TEXT, b"{}"))
    assert protocol.queue.qsize() == 1
    assert not transport.reading
    protocol.queue.get_nowait()
    # As uvicorn's receive resumes it, once nothing waits.
    protocol.read_paused = False
    protocol.transport.resume_reading()
    return protocol


async def test_what_came_is_parsed_after_the_call_that_resumes_reading() -> None:
    transport = Transport()
    protocol = await first_taken(transport)
    # Not within the application's own call.
    assert protocol.queue.empty()
    await asyncio.sleep(0)
    assert protocol.queue.get_nowait()["text"] == "{}"
    # It waits for the application in turn, and the transport reads no more until then.
    assert not transport.reading


@pytest.mark.parametrize("resuming", [True, False], ids=["as reading resumes", "before"])
async def test_nothing_more_is_parsed_once_the_connection_is_lost(resuming: bool) -> None:
    transport = Transport()
    if resuming:
        protocol = await first_taken(transport)
    else:
        protocol = await opened(transport, Application())
        protocol.data_received(LONG + frame(Opcode.TEXT, b"{}"))
    protocol.connection_lost(None)
    # What came is let go, and reading resumes no more.
    assert not protocol._unread
    if not resuming:
        protocol.queue.get_nowait()
        protocol.read_paused = False
    protocol.transport.resume_reading()
    await asyncio.sleep(0)
    assert not transport.reading
    assert protocol.queue.get_nowait()["type"] == "websocket.disconnect"
    assert protocol.queue.empty()


# The peer's errors.


@pytest.mark.parametrize(
    "broken",
    [
        # Unmasked, as no client may send it (RFC 6455, 5.1).
        pytest.param(Frame(Opcode.TEXT, b"{}").serialize(mask=False), id="unmasked"),
        pytest.param(frame(Opcode.TEXT, b"\xff"), id="no UTF-8"),
        pytest.param(frame(Opcode.PONG) * (ALLOWANCE + 1), id="too many pongs"),
        pytest.param(frame(Opcode.TEXT, b"{", fin=False), id="a fragment too small"),
    ],
)
@pytest.mark.parametrize("apart", [False, True], ids=["with the request", "after it"])
async def test_a_frame_that_breaks_a_rule_before_the_handshake_is_answered_closes_it(
    monkeypatch: pytest.MonkeyPatch,
    server_errors: Callable[[], list[str]],
    broken: bytes,
    apart: bool,
) -> None:
    frozen(monkeypatch)
    transport, app = Transport(), Application()
    protocol = connected(transport, app)
    app.take.set()
    if apart:
        protocol.data_received(HANDSHAKE)
        protocol.data_received(broken)
    else:
        protocol.data_received(HANDSHAKE + broken)
    assert transport.closed == 1
    # The application, if it started, takes what came before the connection is lost.
    async with asyncio.timeout(5):
        while not protocol.queue.empty():
            await asyncio.sleep(0)
    protocol.connection_lost(None)
    async with asyncio.timeout(5):
        while protocol.tasks:
            await asyncio.sleep(0)
    # With no close frame to send, and no answer to the handshake once it is closed.
    assert not transport.written
    assert server_errors() == []


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(frame(Opcode.TEXT, b"\xff"), id="one frame"),
        pytest.param(
            frame(Opcode.TEXT, KIB, fin=False) + frame(Opcode.CONT, b"\xc3"), id="fragments"
        ),
    ],
)
async def test_text_that_is_no_utf_8_closes_the_connection_with_1007(
    server_errors: Callable[[], list[str]], message: bytes
) -> None:
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    protocol.data_received(message)
    assert transport.written.endswith(INVALID_TEXT)
    assert transport.closed == 1
    assert (await disconnected(app))["code"] == 1007
    assert [message["type"] for message in app.received] == ["websocket.disconnect"]
    # uvicorn would log it as an error of its own, with a traceback.
    assert server_errors() == []


async def test_a_character_split_between_fragments_is_text() -> None:
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    protocol.data_received(
        frame(Opcode.TEXT, KIB[1:] + b"\xc3", fin=False) + frame(Opcode.CONT, b"\xa9")
    )
    text = (KIB[1:] + b"\xc3\xa9").decode()
    assert await received(app) == [{"type": "websocket.receive", "text": text}]
    assert not transport.closed
    # Nothing of it is kept once it is delivered.
    assert protocol.frames == []


async def test_a_message_after_the_application_closed_is_let_go() -> None:
    """As in uvicorn: reading goes on, for the peer's answer to the close."""
    transport = Transport()
    protocol = await opened(transport, Application())
    await protocol.send({"type": "websocket.close"})
    protocol.data_received(frame(Opcode.TEXT, b"{}"))
    protocol.data_received(frame(Opcode.CLOSE, b"\x03\xe8"))
    assert transport.closed == 1
    assert protocol.queue.get_nowait()["type"] == "websocket.disconnect"
    assert protocol.queue.empty()


# Pongs.


async def test_only_the_latest_pong_waits_for_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen(monkeypatch)
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    protocol.data_received(frame(Opcode.PING, b"1"))
    assert transport.written == b"\x8a\x011"
    transport.written.clear()
    protocol.pause_writing()
    protocol.data_received(frame(Opcode.PING, b"2") + frame(Opcode.PING, b"3"))
    protocol.data_received(frame(Opcode.PING, b"4"))
    assert not transport.written
    protocol.resume_writing()
    assert transport.written == b"\x8a\x014"
    # Once.
    protocol.pause_writing()
    protocol.resume_writing()
    assert transport.written == b"\x8a\x014"


async def test_the_latest_of_the_pings_parsed_together_is_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen(monkeypatch)
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    protocol.pause_writing()
    protocol.data_received(frame(Opcode.PING, b"1") + frame(Opcode.PING, b"2"))
    protocol.resume_writing()
    assert transport.written.endswith(b"\x8a\x012")


async def test_a_close_is_answered_after_the_pong_that_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen(monkeypatch)
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    protocol.pause_writing()
    protocol.data_received(frame(Opcode.PING, b"1") + frame(Opcode.CLOSE, b"\x03\xe8"))
    assert transport.written == b"\x8a\x011\x88\x02\x03\xe8"
    assert transport.closed


async def test_no_pong_follows_a_close(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen(monkeypatch)
    transport, app = Transport(), Application()
    protocol = await opened(transport, app)
    protocol.pause_writing()
    protocol.data_received(frame(Opcode.PING, b"1"))
    protocol.resume_writing()
    transport.written.clear()
    protocol.pause_writing()
    protocol.data_received(frame(Opcode.PING, b"2"))
    protocol.conn.send_close(1000)
    protocol.resume_writing()
    assert b"\x8a" not in transport.written


# Refusals.


async def refusing(scope: object, receive: Any, send: Any) -> None:
    await receive()
    await send({"type": "websocket.http.response.start", "status": 401, "headers": []})
    await send({"type": "websocket.http.response.body", "body": b"refused"})


async def test_a_refusal_is_no_error(server_errors: Callable[[], list[str]]) -> None:
    transport = Transport()
    protocol = connected(transport, refusing)
    protocol.data_received(HANDSHAKE)
    async with asyncio.timeout(5):
        while not transport.closed or protocol.tasks:
            await asyncio.sleep(0)
    assert transport.written.startswith(b"HTTP/1.1 401 ")
    assert transport.written.endswith(b"refused")
    assert server_errors() == []
    # Once the connection ends, nothing more is sent.
    protocol.shutdown()
    assert transport.written.endswith(b"refused")


# Closing.


async def test_a_close_is_cut_once_its_timeout_passes() -> None:
    transport = Transport()
    protocol = connected(transport)
    protocol.transport.close()
    protocol.transport.close()
    async with asyncio.timeout(5):
        while not transport.aborted:
            await asyncio.sleep(TIMEOUT)
    await asyncio.sleep(PAST)
    assert transport.closed == 2
    # Once, however often it was closed.
    assert transport.aborted == 1


async def test_a_connection_that_ended_is_not_cut() -> None:
    transport = Transport()
    protocol = connected(transport)
    protocol.transport.close()
    protocol.connection_lost(None)
    await asyncio.sleep(PAST)
    # uvicorn closes it once more as it is lost.
    assert transport.closed == 2
    assert transport.aborted == 0


async def test_a_connection_lost_before_it_was_closed_is_not_cut() -> None:
    transport = Transport()
    protocol = connected(transport)
    protocol.connection_lost(None)
    await asyncio.sleep(PAST)
    assert transport.closed == 1
    assert transport.aborted == 0
