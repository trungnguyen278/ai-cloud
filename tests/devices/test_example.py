# SPDX-License-Identifier: MIT
"""The app API's example of a home's devices (platform/protocols/app/examples/devices.json),
from beginning to end: the app's requests as it makes them, and a robot of the tests' own
that connects to the control channel, and is told when it is removed, where the example
says."""

import asyncio
import json
import uuid
from contextlib import AsyncExitStack
from typing import Any

import pytest
from app_api import Cloud, bearer
from device import Device
from link import Link, linked, served, token

from ai_cloud.devices.adapters.redis import PRESENCE_PREFIX
from ai_cloud.shared.protocols import platform_dir

pytestmark = pytest.mark.anyio

# The example's own: the robot it sets up, and the server its challenges name.
ROBOT = uuid.UUID("4a4ce4bd-2853-43d8-98dc-3dc06daec6e5")
SERVER = "https://cloud.example"


class Robot:
    """The example's robot: its identifier, a key of the tests' own, and its connection to
    the control channel when it has one."""

    def __init__(self, cloud: Cloud, url: str, stack: AsyncExitStack) -> None:
        self.device = Device(ROBOT)
        self._cloud = cloud
        self._url = url
        self._stack = stack
        self.link: Link | None = None
        self._signed: dict[str, str] = {}

    def sign(self, theirs: str, challenge: str) -> str:
        """Its registration in place of the example's ``theirs``, for ``challenge``: the same
        one each time the example sends ``theirs`` again."""
        if theirs not in self._signed:
            self._signed[theirs] = self.device.registration(challenge, SERVER)
        return self._signed[theirs]

    async def connect(self) -> None:
        access_token = await token(self._cloud, self.device)
        self.link = await self._stack.enter_async_context(linked(self._url, access_token))
        await self.link.open()

    async def lose_what_it_kept(self) -> None:
        """It stops, its connection with it."""
        assert self.link is not None
        await self.link.ws.close()
        self.link = None
        # Once the cloud has seen it go, as the app reading it later would.
        async with asyncio.timeout(5):
            while await self._cloud.redis("EXISTS", f"{PRESENCE_PREFIX}{ROBOT}"):
                await asyncio.sleep(0.02)


async def test_the_example_runs_as_it_says(make_cloud) -> None:
    cloud = await make_cloud(devices={"server": SERVER})
    example = json.loads(
        (platform_dir() / "app" / "examples" / "devices.json").read_text(encoding="utf-8")
    )
    linh = await cloud.sign_in("linh@example.com", "Linh")
    minh = await cloud.sign_in("minh@example.com", "Minh")
    first = await cloud.create_home(linh)
    await cloud.join(minh, first, linh)
    second = await cloud.create_home(linh, name="Nhà Đà Lạt")
    # What the cloud made up, for what the example made up: its homes.
    known = {
        "01a1a592-7000-71fb-9722-9e6fccd5bf7b": first["id"],
        "01a1a5c9-5e80-70e4-9678-2fc9ea0d06c7": second["id"],
    }
    tokens = {"linh": linh, "minh": minh}

    def ours(text: str) -> str:
        for theirs, mine in known.items():
            text = text.replace(theirs, mine)
        return text

    def matches(expected: Any, answered: Any) -> None:
        if isinstance(expected, dict):
            assert isinstance(answered, dict), answered
            # What the cloud makes up anew each time, and what is for logs only.
            for key in set(expected) - {"challenge", "create_time", "detail"}:
                assert key in answered, (key, answered)
                matches(expected[key], answered[key])
            assert set(answered) <= set(expected) | {"detail"}, answered
        elif isinstance(expected, list):
            assert len(expected) == len(answered), answered
            for each, other in zip(expected, answered, strict=True):
                matches(each, other)
        else:
            assert expected == answered, (expected, answered)

    async with served(cloud) as url, AsyncExitStack() as stack:
        robot = Robot(cloud, url, stack)
        who, challenge = "linh", ""
        messages = iter(example["messages"])
        for message in messages:
            if "note" in message:
                note = message["note"]
                who = "minh" if note.startswith("Minh") else "linh"
                if "loses what it kept" in note:
                    await robot.lose_what_it_kept()
                if "connects again" in note:
                    await robot.connect()
                continue
            method, target = message["request"].split(" ", 1)
            body = message.get("message")
            if body is not None and "registration" in body:
                body = {**body, "registration": robot.sign(body["registration"], challenge)}
            answer = next(messages)
            expected = answer.get("message", {})
            if expected.get("connected") is True and robot.link is None:
                # The app reads the device until it has connected: it connects meanwhile.
                await robot.connect()
            headers = {**bearer(tokens[who]), **message.get("headers", {})}
            response = await cloud.http.request(method, ours(target), json=body, headers=headers)
            assert response.status_code == answer["status"], (target, response.text)
            if "message" in answer:
                answered = response.json()
                matches(expected, answered)
                challenge = answered.get("challenge", challenge)
            if method == "DELETE" and robot.link is not None:
                # The cloud ends its session, and the robot offers set-up.
                assert await robot.link.hear() == {"type": "bye", "reason": "revoked"}
                assert await robot.link.ended() == 1000
                robot.link = None
