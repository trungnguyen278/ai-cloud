# SPDX-License-Identifier: MIT
"""A device's side of the cloud link's control channel (platform/protocols/cloud/README.md),
against the api served by uvicorn in the tests' own process, as the api process serves it:
its WebSocket's handshake and frames are the real ones."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from app_api import Cloud
from device import Device
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from ai_cloud.entry.api import ApiServer, server_config
from ai_cloud.shared.protocols import protocol_schema

PATH = "/device/v3/link"
WAIT = 5.0  # seconds a test waits for what should come at once
# JSON nested deeper than the cloud reads, in a frame the protocol allows.
DEEP = "[" * 500_000 + "]" * 500_000


@asynccontextmanager
async def served(cloud: Cloud) -> AsyncIterator[str]:
    """Serve ``cloud``'s api on a port of this machine; the control channel's URL there. The
    server goes away when the block ends, as a process told to stop does."""
    config = server_config(cloud.app, cloud.settings, host="127.0.0.1", port=0, lifespan="off")
    server = ApiServer(config, cloud.app.state.going_away)
    serving = asyncio.create_task(server.serve())
    async with asyncio.timeout(WAIT):
        while not server.started:
            assert not serving.done(), "the server stopped"
            await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}{PATH}"
    finally:
        server.should_exit = True
        await serving


def hello(**changes: Any) -> dict[str, Any]:
    """A robot's hello, as the cloud link's example has it."""
    return {
        "type": "hello",
        "protocol": "3.0",
        "device": {"kind": "robot", "model": "concept-v0", "software": "0.4.0"},
        "time": "2026-10-28T03:20:00.250Z",
        "time_zone": "Asia/Ho_Chi_Minh",
        "languages": ["vi", "en"],
        "allowed_classes": ["informational", "physical"],
        "conversations": True,
        "media": ["livekit"],
        "events": [],
        **changes,
    }


async def token(cloud: Cloud, device: Device) -> str:
    """An access token for ``device``, from the token endpoint."""
    response = await cloud.device_token(device.assertion(cloud.token_endpoint))
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@dataclass
class Refusal:
    """The handshake's answer, when the cloud refuses the connection."""

    status: int
    headers: dict[str, str]
    body: dict[str, Any]


class Link:
    """A device's connection to the control channel."""

    def __init__(self, ws: ClientConnection) -> None:
        self.ws = ws

    async def say(self, message: dict[str, Any] | str | bytes) -> None:
        await self.ws.send(message if isinstance(message, str | bytes) else json.dumps(message))

    async def hear(self) -> dict[str, Any]:
        """The cloud's next message, once it matches the protocol's schema."""
        async with asyncio.timeout(WAIT):
            text = await self.ws.recv()
        assert isinstance(text, str)
        message = json.loads(text)
        assert protocol_schema("cloud", "from_cloud").problem(message) is None, message
        return message

    async def ended(self) -> int | None:
        """How the cloud closed the connection: its close code; anything it sent before is
        taken for nothing."""
        async with asyncio.timeout(WAIT):
            try:
                while True:
                    await self.ws.recv()
            except ConnectionClosed as closed:
                return None if closed.rcvd is None else closed.rcvd.code

    async def open(self, **changes: Any) -> dict[str, Any]:
        """Say hello; the cloud's."""
        await self.say(hello(**changes))
        answer = await self.hear()
        assert answer["type"] == "hello", answer
        return answer


@asynccontextmanager
async def linked(url: str, access_token: str | None) -> AsyncIterator[Link]:
    """A connection to the control channel at ``url``, with ``access_token``."""
    headers = {} if access_token is None else {"Authorization": f"Bearer {access_token}"}
    async with connect(
        url, additional_headers=headers, proxy=None, ping_interval=None, open_timeout=WAIT
    ) as ws:
        yield Link(ws)


async def refused(url: str, access_token: str | None) -> Refusal:
    """The cloud's answer to a handshake it refuses."""
    try:
        async with linked(url, access_token):
            raise AssertionError("the cloud took the connection")
    except InvalidStatus as error:
        response = error.response
        body = json.loads(response.body) if response.body else {}
        headers = {name.lower(): value for name, value in response.headers.raw_items()}
        return Refusal(response.status_code, headers, body)
