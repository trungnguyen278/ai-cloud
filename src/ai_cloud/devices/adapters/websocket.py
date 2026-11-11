# SPDX-License-Identifier: MIT
"""A device's control channel on a WebSocket (RFC 6455), as Starlette serves one: the cloud
link 3.0's transport (platform/protocols/cloud/README.md, The control channel)."""

import contextlib

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState


class WebSocketChannel:
    def __init__(self, websocket: WebSocket) -> None:
        self._websocket = websocket

    async def receive(self) -> str | bytes | None:
        if self._websocket.client_state is not WebSocketState.CONNECTED:
            return None
        message = await self._websocket.receive()
        if message["type"] == "websocket.disconnect":
            return None
        text = message.get("text")
        if text is not None:
            return text
        return message.get("bytes") or b""

    async def send(self, text: str) -> None:
        if self._websocket.application_state is not WebSocketState.CONNECTED:
            return
        # A device that is gone: receiving learns it.
        with contextlib.suppress(WebSocketDisconnect):
            await self._websocket.send_text(text)

    async def close(self, code: int) -> None:
        if self._websocket.application_state is not WebSocketState.CONNECTED:
            return
        with contextlib.suppress(WebSocketDisconnect):
            await self._websocket.close(code)
