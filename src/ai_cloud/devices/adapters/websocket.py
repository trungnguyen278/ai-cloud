# SPDX-License-Identifier: MIT
"""A device's control channel on a WebSocket (RFC 6455), as Starlette serves one: the cloud
link 3.4's transport (platform/protocols/cloud/README.md, The control channel)."""

import contextlib

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ai_cloud.devices.domain.link import Closed

# What sending raises once the connection is closed: Starlette's own, for a device that is
# gone; and uvicorn's RuntimeError for any message after a close that it sent itself, as past
# ws_max_size, until it sees the connection lost (uvicorn 0.54.0,
# protocols/websockets/websockets_sansio_impl.py, send). Starlette raises no RuntimeError of
# its own while the application's side is open, which _open checks first.
_GONE = (WebSocketDisconnect, RuntimeError)


class WebSocketChannel:
    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def receive(self) -> str | bytes | Closed:
        if self._websocket.client_state is not WebSocketState.CONNECTED:
            return Closed()
        message = await self._websocket.receive()
        if message["type"] == "websocket.disconnect":
            # The server's own close code, such as 1009 past ws_max_size, or the device's.
            return Closed(message.get("code"))
        text = message.get("text")
        if text is not None:
            return text
        return message.get("bytes") or b""

    async def send(self, text: str) -> None:
        if not self._open():
            return
        # A device that is gone, or a connection that the server closed: receiving learns how.
        with contextlib.suppress(*_GONE):
            await self._websocket.send_text(text)

    async def close(self, code: int) -> None:
        if not self._open():
            return
        with contextlib.suppress(*_GONE):
            await self._websocket.close(code)

    def _open(self) -> bool:
        """Whether the connection is still open both ways. Once the device's side is closed,
        the server may have closed it already, as uvicorn does past ws_max_size: it then
        takes nothing more."""
        return (
            self._websocket.application_state is WebSocketState.CONNECTED
            and self._websocket.client_state is not WebSocketState.DISCONNECTED
        )
