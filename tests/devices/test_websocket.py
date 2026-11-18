# SPDX-License-Identifier: MIT
"""The control channel's WebSocket adapter, on Starlette's WebSocket, against an ASGI server
of the test's own: what a server does that a test cannot time, such as refusing a message
after a close that it sent itself, happens here when the test says."""

from typing import Any

import pytest
from starlette.websockets import WebSocket

from ai_cloud.devices.adapters.websocket import WebSocketChannel
from ai_cloud.devices.domain.link import Closed

pytestmark = pytest.mark.anyio


class Server:
    """The ASGI server's side: what the device sent, and what the application sends."""

    def __init__(self, *incoming: dict[str, Any]) -> None:
        self.incoming = [{"type": "websocket.connect"}, *incoming]
        self.sent: list[dict[str, Any]] = []
        self.refusing: BaseException | None = None

    async def receive(self) -> dict[str, Any]:
        return self.incoming.pop(0)

    async def send(self, message: dict[str, Any]) -> None:
        if self.refusing is not None and message["type"] != "websocket.accept":
            raise self.refusing
        self.sent.append(message)


async def accepted(server: Server) -> WebSocketChannel:
    websocket = WebSocket({"type": "websocket", "path": "/"}, server.receive, server.send)
    await websocket.accept()
    return WebSocketChannel(websocket)


async def test_frames_and_how_the_connection_closed_are_received() -> None:
    server = Server(
        {"type": "websocket.receive", "text": "{}"},
        {"type": "websocket.receive", "bytes": b"\x00"},
        {"type": "websocket.disconnect", "code": 1009},
    )
    channel = await accepted(server)
    assert await channel.receive() == "{}"
    assert await channel.receive() == b"\x00"
    assert await channel.receive() == Closed(1009)
    # Closed already: nothing more is received, or sent.
    assert await channel.receive() == Closed()
    await channel.send("{}")
    await channel.close(1000)
    assert server.sent == [{"type": "websocket.accept", "subprotocol": None, "headers": []}]


async def test_a_disconnect_without_a_code_is_a_close_without_one() -> None:
    channel = await accepted(Server({"type": "websocket.disconnect"}))
    assert await channel.receive() == Closed()


@pytest.mark.parametrize(
    "refusal",
    [
        # uvicorn's, after a close that it sent itself, until it sees the connection lost.
        RuntimeError("Unexpected ASGI message 'websocket.send', after sending 'websocket.close'."),
        # A connection lost: Starlette's WebSocketDisconnect.
        OSError(),
    ],
    ids=["closed by the server", "lost"],
)
async def test_what_cannot_be_sent_any_more_is_not_the_session_s_failure(
    refusal: BaseException,
) -> None:
    server = Server({"type": "websocket.disconnect", "code": 1009})
    channel = await accepted(server)
    server.refusing = refusal
    await channel.send("{}")
    await channel.close(1000)
    # Receiving learns how the connection closed.
    assert await channel.receive() == Closed(1009)
