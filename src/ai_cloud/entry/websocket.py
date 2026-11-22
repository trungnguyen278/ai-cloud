# SPDX-License-Identifier: MIT
"""The WebSocket protocol that the api's server runs: uvicorn's own, but for the frames that a
connection may send below the application, and how long it may take to close
(docs/decisions/0012-what-one-device-can-exhaust.md)."""

import asyncio
import math
from typing import Any, cast

from uvicorn._types import ASGISendEvent
from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
from websockets.frames import CloseCode, Frame
from websockets.protocol import State

from ai_cloud.shared.rates import TokenBucket

# The cloud link's Frames (Messages, since 3.4): "A side sends a message in one frame, or in
# fragments of at least 1 KiB of payload each but the last, which may hold less, or nothing."
SMALLEST_FRAGMENT = 1024
# And its WebSocket Pings: "A side sends Pings and unsolicited Pongs together at most once a
# second", and "lets twice `ping_interval` of them through at once".
PINGS_A_SECOND = 1
PINGS_AT_ONCE = 2  # times ping_interval, in seconds
# Bytes that websockets' parser takes at once, after which the protocol looks again at whether
# a message waits for the application, or the connection closes. A piece holds at most 170 of
# the smallest frames, of 6 bytes (RFC 6455, 5.2), whose events uvicorn handles together; 1 MiB
# in one frame takes 1.7 ms to parse in such pieces, and 1.5 ms in asyncio's reads of 256 KiB
# (docs/decisions/0012, 2026-11-22).
PIECE = 1024


class WebSocketProtocol(WebSocketsSansIOProtocol):
    """uvicorn's protocol on websockets' sans-I/O implementation, with bounds of its own, and
    the peer's errors and refusals logged as no error of the cloud's.

    What comes below the application. A Ping, a Pong or a fragment is no message that the
    application sees, nor counts, and uvicorn 0.54 handles each as it comes. The cloud link
    bounds them (Messages, Frames, since 3.4), and past its bounds the connection closes with
    1008, "policy violation" (RFC 6455, 7.4.1): on a fragment under 1 KiB that is not its
    message's last, and on Pings and unsolicited Pongs past one a second, twice the link's
    ``ping_interval`` of them at once. So Envoy closes an HTTP/2 connection flooded with
    frames of its own, empty ones and Pings among them (its Http2ProtocolOptions, read
    2026-11-22), the floods of CVE-2019-9512 and CVE-2019-9518 (Netflix's advisory 2019-002).
    Fragments of 1 KiB or more are a message as it comes, which the session paces as it reads
    them. What came is parsed a piece at a time, and no further than the application has
    taken: uvicorn stops reading once a message waits for it, but would parse the rest of
    what it has read already.

    Pongs. websockets answers each Ping with a Pong, which uvicorn writes even while the
    transport takes no more: a peer that pings and reads nothing would grow the buffer for
    as long as the connection lasts. Only the latest Ping's Pong then waits, as RFC 6455
    lets an endpoint answer (5.5.3), with those that websockets wrote with it, from the same
    piece: a close's answer among them leaves before the connection closes.

    Closing. uvicorn closes a connection with its transport's ``close``, on each of its ways
    to close one, and asyncio's then waits for what is buffered to leave: "buffered data will
    be flushed asynchronously" (asyncio's Transports, docs.python.org/3.14). A peer that
    reads nothing lets nothing leave, and holds its connection for ever. The protocol has no
    other bound in uvicorn 0.54: its pull request 3230, which would add one, was open on
    2026-11-21. So each close here is cut short, with ``abort``, once ``close_timeout`` has
    passed without the connection ending, as Node's ws terminates a connection whose close
    took longer than its ``closeTimeout`` (its doc/ws.md, read 2026-11-12). The closing
    handshake waits as long for the peer's close frame, where uvicorn waits 10 s.

    The peer's errors. A frame that breaks WebSocket's rules before the handshake is answered
    has no close frame to answer it, where uvicorn 0.54 asserts that one was sent, and asyncio
    then logs the failure with its traceback: the connection closes here, as it would. Text
    that is no UTF-8 fails the connection with 1007 (RFC 6455, 8.1 and 7.4.1), as in uvicorn,
    which logs it as an error of its own, with a traceback: the session's end notes it here,
    as it notes a message too large.

    Refusals. uvicorn 0.54 leaves the handshake of a connection that the application refused
    unfinished, and so logs an error for each once the application returns, "ASGI callable
    returned without completing handshake": anyone could write the log full. A refusal sent
    whole finishes it here, as uvicorn's pull request 3138 does from the refusal's start
    (merged 2026-10-25, in no release on 2026-11-22)."""

    def __init__(
        self, *args: Any, close_timeout: float, link_ping_interval: float, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.close_timeout = close_timeout
        self._pings = TokenBucket(PINGS_A_SECOND, math.ceil(PINGS_AT_ONCE * link_ping_interval))
        # What came that is not parsed yet: some of one read of the transport, at most.
        self._unread = bytearray()
        # Whether the peer broke the link's rules for frames, and the connection closes.
        self._broken = False
        # The Pong that waits for the transport to take more.
        self._pong: list[bytes] | None = None
        self._bounded: _Transport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._bounded = _Transport(cast(asyncio.Transport, transport), self)
        super().connection_made(cast(asyncio.Transport, self._bounded))

    def connection_lost(self, exc: Exception | None) -> None:
        if self._bounded is not None:
            self._bounded.lost()
        self._unread.clear()
        super().connection_lost(exc)

    # Reading.

    def data_received(self, data: bytes) -> None:
        self._unread += data
        self._read()

    def read_on(self) -> bool:
        """Parse what came and waits; whether the transport may read more. What is left
        unparsed waits for the application, which has paused reading."""
        self._read()
        return not self.read_paused

    def _read(self) -> None:
        while (
            self._unread
            and not self.read_paused
            and not self._broken
            and self.conn.parser_exc is None
        ):
            piece = bytes(self._unread[:PIECE])
            del self._unread[:PIECE]
            super().data_received(piece)
        if self._broken or self.conn.parser_exc is not None:
            # Nothing more of it is parsed.
            self._unread.clear()

    def handle_text(self, event: Frame) -> None:
        if self._kept(event):
            super().handle_text(event)

    def handle_bytes(self, event: Frame) -> None:
        if self._kept(event):
            super().handle_bytes(event)

    def handle_cont(self, event: Frame) -> None:
        if self._kept(event):
            super().handle_cont(event)

    def handle_ping(self) -> None:
        if not self._allowed():
            return
        # websockets queued the Pong (RFC 6455, 5.5.2), with what answers the frames parsed
        # with this Ping, which the Pings among them then find taken already.
        pong = self.conn.data_to_send()
        if self.writable.is_set():
            self._pong = None
            self.transport.write(b"".join(pong))
        elif pong:
            # The latest supersedes any before it.
            self._pong = pong

    def handle_pong(self, event: Frame) -> None:
        # One that answers uvicorn's own Ping is solicited, and counts for nothing.
        solicited = self.pending_ping_payload is not None and (
            bytes(event.data) == self.pending_ping_payload
        )
        if solicited or self._allowed():
            super().handle_pong(event)

    def handle_close(self, event: Frame) -> None:
        # What waits may hold this close's answer, which goes before the connection closes.
        pong, self._pong = self._pong, None
        if pong and not self.close_sent and not self.transport.is_closing():
            self.transport.write(b"".join(pong))
        super().handle_close(event)

    def handle_parser_exception(self) -> None:
        if self.conn.close_sent is None:
            # Before the handshake is answered, with no close frame to send.
            self.transport.close()
            return
        super().handle_parser_exception()

    def send_receive_event_to_app(self) -> None:
        if self.curr_msg_data_type != "text":
            super().send_receive_event_to_app()
            return
        # uvicorn 0.54's, but for text that is no UTF-8, whose failure it logs: decoding the
        # text once more, to tell first, would cost about what reading its JSON costs, 0.5 ms
        # for 1 MiB (measured 2026-11-22).
        data = self.frames[0] if len(self.frames) == 1 else b"".join(self.frames)
        self.frames = []
        if self.close_sent:
            return
        try:
            text = data.decode()
        except UnicodeDecodeError:
            self.conn.fail(CloseCode.INVALID_DATA)
            self.handle_parser_exception()
            return
        self.queue.put_nowait({"type": "websocket.receive", "text": text})
        if not self.read_paused:
            self.read_paused = True
            self.transport.pause_reading()

    def _kept(self, event: Frame) -> bool:
        """Whether a frame of a message keeps the link's rule for fragments; False once the
        connection broke a rule, and closes."""
        if self._broken:
            return False
        if event.fin or len(event.data) >= SMALLEST_FRAGMENT:
            return True
        self._break("fragment too small")
        return False

    def _allowed(self) -> bool:
        """Count a Ping or an unsolicited Pong; False once past their allowance, or once the
        connection broke a rule, and closes."""
        if self._broken:
            return False
        if self._pings.take_now() == 0:
            return True
        self._break("too many pings")
        return False

    def _break(self, reason: str) -> None:
        """Fail the connection with 1008, for ``reason``: before the handshake is answered,
        with no close frame, and uvicorn then writes no answer into a connection that
        closes."""
        self._broken = True
        self.conn.fail(CloseCode.POLICY_VIOLATION, reason)
        self.handle_parser_exception()

    # Writing.

    async def send(self, message: ASGISendEvent) -> None:
        await super().send(message)
        if self.initial_response is not None and self.close_sent:
            # The refusal went whole, and the connection closes.
            self.handshake_complete = True

    def resume_writing(self) -> None:
        super().resume_writing()
        pong, self._pong = self._pong, None
        if pong and self.conn.state is State.OPEN:
            self.transport.write(b"".join(pong))


class _Transport:
    """``transport``, as ``protocol`` uses it: its ``close`` aborts it once the protocol's
    ``close_timeout`` has passed without the connection ending, and it reads again only once
    the protocol has parsed what came before. Everything else is the transport's own."""

    def __init__(self, transport: asyncio.Transport, protocol: WebSocketProtocol) -> None:
        self._transport = transport
        self._protocol = protocol
        self._cut: asyncio.TimerHandle | None = None
        self._resuming: asyncio.Handle | None = None
        self._lost = False

    def close(self) -> None:
        self._transport.close()
        if self._cut is None and not self._lost:
            loop = asyncio.get_running_loop()
            self._cut = loop.call_later(self._protocol.close_timeout, self._transport.abort)

    def resume_reading(self) -> None:
        # Later, as the transport would deliver what it reads: not within the call that
        # resumes it, which is the application's.
        if self._resuming is None and not self._lost:
            self._resuming = asyncio.get_running_loop().call_soon(self._resume)

    def _resume(self) -> None:
        self._resuming = None
        if self._protocol.read_on():
            self._transport.resume_reading()

    def lost(self) -> None:
        """The connection ended: nothing is left to cut, nor to read."""
        self._lost = True
        for handle in (self._cut, self._resuming):
            if handle is not None:
                handle.cancel()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._transport, name)
