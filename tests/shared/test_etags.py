# SPDX-License-Identifier: MIT
"""The condition of a change, at each of the app API's four ``PATCH`` endpoints (the app API
1.7, Requests, ``etag``): the etag that its body brings, and the ``If-Match`` of apps of 1.6."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest
from app_api import Cloud, bearer, conforms, problem
from device import Device

pytestmark = pytest.mark.anyio

KINDS = ["account", "home", "person", "device"]


@dataclass
class Changeable:
    """One of the things that people change: where, by whom, two changes of it, and how its
    etag and what the changes change of it are read."""

    cloud: Cloud
    path: str
    tokens: dict[str, Any]
    definition: str
    first: dict[str, Any]
    second: dict[str, Any]
    read: Callable[[], Awaitable[dict[str, Any]]]

    async def change(
        self, change: dict[str, Any], etag: str | None = None, if_match: str | None = None
    ) -> Any:
        body = change if etag is None else {**change, "etag": etag}
        headers = bearer(self.tokens) | ({} if if_match is None else {"If-Match": if_match})
        return await self.cloud.http.patch(self.path, json=body, headers=headers)


async def changeable(cloud: Cloud, kind: str) -> Changeable:
    family = await cloud.family()
    owner = family.owner
    match kind:
        case "account":
            path, definition = "/app/v1/account", "account"
            first, second = {"languages": ["vi"]}, {"languages": ["en"]}

            async def read() -> dict[str, Any]:
                return await cloud.account(owner)

        case "home":
            path, definition = family.path, "home"
            first, second = {"name": "A"}, {"name": "B"}

            async def read() -> dict[str, Any]:
                return await cloud.home(owner, family.home)

        case "person":
            member = family.ids["member"]
            path, definition = f"{family.path}/members/{member}", "member"
            first, second = {"display_name": "A"}, {"display_name": "B"}

            async def read() -> dict[str, Any]:
                members = await cloud.members(owner, family.home)
                [person] = [each for each in members if each["id"] == member]
                return person

        case _:
            device = Device()
            await cloud.add_device(owner, family.home, device)
            path, definition = f"{family.path}/devices/{device.id}", "device"
            first, second = {"name": "A"}, {"name": "B"}

            async def read() -> dict[str, Any]:
                return (await cloud.http.get(path, headers=bearer(owner))).json()

    return Changeable(cloud, path, owner, definition, first, second, read)


def changed(thing: dict[str, Any], change: dict[str, Any]) -> bool:
    return all(thing[key] == value for key, value in change.items())


@pytest.mark.parametrize("kind", KINDS)
async def test_a_change_with_the_etag_it_read_is_made_only_on_it(cloud: Cloud, kind: str) -> None:
    thing = await changeable(cloud, kind)
    read = (await thing.read())["etag"]
    made = await thing.change(thing.first, read)
    assert made.status_code == 200, made.text
    now = conforms(made, thing.definition)["etag"]
    assert now != read
    # Another app, which read it before that change, changes nothing.
    problem(await thing.change(thing.second, read), 409, "etag_mismatch")
    assert changed(await thing.read(), thing.first)
    # Compared as written: neither a list nor "*", which If-Match has, matches.
    for etag in (f'{now}, W/"9"', "*"):
        problem(await thing.change(thing.second, etag), 409, "etag_mismatch")
    assert (await thing.change(thing.second, now)).status_code == 200
    assert changed(await thing.read(), thing.second)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    ("if_match", "etag", "status"),
    [
        ("current", "current", 200),
        ("*", "current", 200),
        # If-Match names a list, of which one is enough.
        ("list", "current", 200),
        ("current", "stale", 409),
        ("stale", "current", 412),
        # The precondition first, as RFC 9110 evaluates it (13.2.1).
        ("stale", "stale", 412),
    ],
)
async def test_a_change_that_brings_both_meets_each(
    cloud: Cloud, kind: str, if_match: str, etag: str, status: int
) -> None:
    thing = await changeable(cloud, kind)
    stale = (await thing.read())["etag"]
    assert (await thing.change(thing.first)).status_code == 200
    current = (await thing.read())["etag"]
    tags = {"current": current, "stale": stale, "*": "*", "list": f"{stale}, {current}"}
    response = await thing.change(thing.second, tags[etag], tags[if_match])
    if status == 200:
        assert response.status_code == 200, response.text
        assert changed(await thing.read(), thing.second)
    else:
        problem(response, status, "etag_mismatch" if status == 409 else None)
        assert changed(await thing.read(), thing.first)


@pytest.mark.parametrize("kind", KINDS)
async def test_an_etag_alone_changes_nothing(cloud: Cloud, kind: str) -> None:
    thing = await changeable(cloud, kind)
    before = await thing.read()
    problem(await thing.change({}, before["etag"]), 400)
    assert await thing.read() == before
