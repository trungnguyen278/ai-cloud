# SPDX-License-Identifier: MIT
"""The devices' control channel (platform/protocols/cloud/README.md: The control channel,
Staying connected, The session, Errors), on a WebSocket that uvicorn serves, against
PostgreSQL and Redis."""

import asyncio
import base64
import contextlib
import json
import logging
import socket
import time
import uuid
from datetime import UTC, datetime
from random import Random
from string import ascii_letters
from typing import Any

import httptools
import pytest
import redis.asyncio
from app_api import Cloud, bearer, conforms
from device import Device
from link import DEEP, PATH, WAIT, hello, linked, port, refused, running, served, token, url
from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from ai_cloud.accounts.api import HomeDeleted
from ai_cloud.devices.adapters.postgres import PostgresStore, PostgresTransaction
from ai_cloud.devices.adapters.redis import PRESENCE_PREFIX, RedisPresence
from ai_cloud.devices.api import CONNECTION_RATE_PREFIX, MAX_MESSAGE, consumers
from ai_cloud.entry import websocket
from ai_cloud.entry.api import ApiServer, server_config
from ai_cloud.shared.database import create_engine
from ai_cloud.shared.events import Event

pytestmark = pytest.mark.anyio

# Short times, so that what waits for them is quick.
QUICK = {"devices": {"link": {"check_interval": 0.2, "presence_ttl": 2, "close_timeout": 1}}}
# The cloud answers each ping at once, for the window of a device that reads nothing to fill
# soon.
MANY = {"rate": 100_000, "burst": 100_000, "backlog": 100_000}


async def registered(cloud: Cloud) -> tuple[dict[str, Any], dict[str, Any], Device]:
    """An owner, her home, and a device registered into it."""
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    device = Device()
    await cloud.add_device(tokens, home, device)
    return tokens, home, device


async def shown(cloud: Cloud, tokens: dict[str, Any], home: dict[str, Any], device: Device) -> Any:
    response = await cloud.http.get(
        f"/app/v1/homes/{home['id']}/devices/{device.id}", headers=bearer(tokens)
    )
    assert response.status_code == 200, response.text
    return conforms(response, "device")


async def until(condition: Any) -> None:
    """Wait for ``condition``, a coroutine function, to hold."""
    async with asyncio.timeout(5):
        while not await condition():
            await asyncio.sleep(0.02)


# The handshake.


async def test_a_device_without_a_valid_token_is_refused(cloud) -> None:
    tokens, _, device = await registered(cloud)
    async with served(cloud) as url:
        for access_token in (None, "", "not-a-token", tokens["access_token"]):
            refusal = await refused(url, access_token)
            assert refusal.status == 401
            assert refusal.headers["content-type"] == "application/problem+json"
            assert refusal.headers["www-authenticate"] == 'Bearer error="invalid_token"'
            assert refusal.body["status"] == 401
        # With a token of its own, it connects.
        async with linked(url, await token(cloud, device)) as link:
            await link.open()


def signed(cloud: Cloud, device: Device, **changes: Any) -> str:
    """An access token that the cloud's key signs for ``device``, as the token endpoint's,
    with ``changes``."""
    issued = int(time.time())
    claims = {
        "iss": cloud.settings.tokens.issuer,
        "sub": str(device.id),
        "aud": "device",
        "client_id": str(device.id),
        "iat": issued,
        "exp": issued + 600,
        "jti": str(uuid.uuid7()),
        **changes,
    }
    return cloud.keys.sign(
        {key: value for key, value in claims.items() if value is not None}, typ="at+jwt"
    )


async def test_a_token_must_be_a_device_s_own(cloud) -> None:
    _, _, device = await registered(cloud)
    other = str(uuid.uuid7())
    async with served(cloud) as url:
        for changes in (
            {"exp": int(time.time()) - 100},  # expired, past the leeway
            {"aud": "app"},
            {"iss": "someone"},
            {"client_id": other},  # another device's
            {"sub": "not a device"},
            {"client_id": None},
            {"iat": "1700000000"},
        ):
            refusal = await refused(url, signed(cloud, device, **changes))
            assert refusal.status == 401, changes
        async with linked(url, signed(cloud, device)) as link:
            await link.open()


async def test_a_removed_device_is_refused_and_its_old_tokens_with_it(cloud) -> None:
    tokens, home, device = await registered(cloud)
    before = signed(cloud, device, iat=int(time.time()) - 60)
    path = f"/app/v1/homes/{home['id']}/devices/{device.id}"
    async with served(cloud) as url:
        assert (await cloud.http.delete(path, headers=bearer(tokens))).status_code == 204
        assert (await refused(url, before)).status == 401
        # Set up again, it is a new registration: a token from before it opens nothing.
        await cloud.add_device(tokens, home, device)
        refusal = await refused(url, before)
        assert refusal.status == 401
        assert "older than" in refusal.body["detail"]
        async with linked(url, await token(cloud, device)) as link:
            await link.open()


# The hellos.


async def test_the_hellos_open_a_session(cloud) -> None:
    tokens, home, device = await registered(cloud)
    assert "kind" not in await shown(cloud, tokens, home, device)
    async with served(cloud) as url:
        async with linked(url, await token(cloud, device)) as link:
            answer = await link.open(protocol="3.7")
            assert answer["protocol"] == "3.6"  # the lower of the two minors
            assert answer["resumed"] is False
            assert uuid.UUID(answer["session"]).version == 7
            assert answer["ping_interval"] == cloud.settings.devices.link.ping_interval
            assert answer["conversations"] is False
            assert answer["events"] == []
            assert abs(datetime.fromisoformat(answer["time"]) - datetime.now(UTC)).seconds < 5
            seen = await shown(cloud, tokens, home, device)
            assert seen["connected"] is True
            assert (seen["kind"], seen["model"], seen["software"]) == (
                "robot",
                "concept-v0",
                "0.4.0",
            )
            await link.say({"type": "bye", "reason": "stopping"})
            assert await link.ended() == 1000
        await until(lambda: _disconnected(cloud, tokens, home, device))
        # What its latest hello said stays.
        async with linked(url, await token(cloud, device)) as link:
            await link.open(device={"kind": "robot", "model": "concept-v1", "software": "0.5.0"})
            seen = await shown(cloud, tokens, home, device)
            assert (seen["model"], seen["software"]) == ("concept-v1", "0.5.0")


async def _disconnected(cloud: Cloud, tokens: Any, home: Any, device: Device) -> bool:
    return (await shown(cloud, tokens, home, device))["connected"] is False


@pytest.mark.parametrize(
    ("first", "reason"),
    [
        pytest.param(
            json.dumps(hello(protocol="4.0", device="a new shape")),
            "unsupported_protocol",
            id="another major",
        ),
        pytest.param(json.dumps(hello(protocol="03.1")), None, id="the major written 03"),
        pytest.param(json.dumps(hello(protocol="3.0")), None, id="an older minor"),
        pytest.param(
            json.dumps(hello(protocol="2.9")), "unsupported_protocol", id="an older major"
        ),
        pytest.param(json.dumps({"type": "ping", "received": 0}), "no_hello", id="a ping"),
        pytest.param(json.dumps(hello(languages=[])), "invalid_message", id="invalid"),
        pytest.param(
            json.dumps(hello(device={"kind": "robot", "model": "m", "software": "\ud800"})),
            "invalid_message",
            id="a lone surrogate where the schema takes any text",
        ),
        # PostgreSQL's text holds no U+0000: the hello fails before the cloud keeps it.
        pytest.param(
            json.dumps(hello(device={"kind": "robot", "model": "m", "software": "0.4" + chr(0)})),
            "invalid_message",
            id="U+0000 where the schema takes any text",
        ),
        pytest.param(
            json.dumps(
                hello(events=[{"name": "x", "description": "x", "schema": {"maximum": 0}}])
            ).replace('"maximum": 0', '"maximum": NaN'),
            "invalid_message",
            id="NaN where the schema takes anything",
        ),
        pytest.param(json.dumps(hello(protocol="3")), "invalid_message", id="no minor"),
        pytest.param(json.dumps({"type": "ping"}), "invalid_message", id="a ping that fails"),
        pytest.param("[1, 2]", "invalid_message", id="no object"),
        pytest.param("not JSON", "invalid_message", id="no JSON"),
        pytest.param('{"type": "hello", "x": NaN}', "invalid_message", id="NaN"),
        pytest.param('{"type": "hello", "x": "\\ud800"}', "invalid_message", id="a lone surrogate"),
        pytest.param(b"\x00binary", "invalid_message", id="binary"),
        pytest.param(json.dumps(hello()).encode(), "invalid_message", id="a hello in binary"),
        pytest.param(DEEP, "invalid_message", id="nested too deep"),
        pytest.param(
            json.dumps(hello())[:-1] + ', "x": 1e400}',
            "invalid_message",
            id="a number past a double",
        ),
        # The schema's uniqueItems compares what it holds; JSON Schema's own way takes a time
        # that grows as the square of their number, and recurses as deep as they nest.
        pytest.param(
            json.dumps(hello(languages=[{"n": n} for n in range(6000)])),
            "invalid_message",
            id="many items to compare",
        ),
        pytest.param(
            json.dumps(hello(languages=[json.loads("[" * 400 + "]" * 400)] * 2)),
            "invalid_message",
            id="nested items to compare",
        ),
    ],
)
async def test_a_first_message_that_is_no_hello_ends_the_session(
    cloud, first: Any, reason: str | None
) -> None:
    _, _, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.say(first)
        answer = await link.hear()
        if reason is None:
            assert answer["type"] == "hello"  # 03 is the major 3
            return
        # What it could not answer, the bye says, for the device's logs (Messages, Validation).
        if reason == "invalid_message":
            assert answer.pop("message")
        if reason == "no_hello":
            assert answer.pop("message") == "the first message was not a hello"
        assert answer == {"type": "bye", "reason": reason}
        assert await link.ended() == 1000


async def test_a_device_that_says_no_hello_in_time_is_told(make_cloud) -> None:
    cloud = await make_cloud(devices={"link": {"hello_timeout": 0.2}})
    _, _, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        assert await link.hear() == {
            "type": "bye",
            "reason": "no_hello",
            "message": "the hello did not come in time",
        }
        assert await link.ended() == 1000


async def test_a_device_may_end_before_its_hello(cloud) -> None:
    tokens, home, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.say({"type": "bye", "reason": "stopping"})
        # Nothing comes back but the close.
        with pytest.raises(ConnectionClosed) as closed:
            async with asyncio.timeout(5):
                await link.ws.recv()
        assert closed.value.rcvd is not None and closed.value.rcvd.code == 1000
    assert (await shown(cloud, tokens, home, device))["connected"] is False


async def test_a_conversation_the_cloud_does_not_hold_ends(cloud) -> None:
    _, _, device = await registered(cloud)
    conversation = str(uuid.uuid7())
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.open(conversation=conversation)
        assert await link.hear() == {
            "type": "conversation.end",
            "seq": 1,
            "conversation": conversation,
            "reason": "lost",
        }


# The session.


async def test_pings_say_what_was_received(cloud) -> None:
    _, _, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.open()
        await link.say({"type": "ping", "received": 0})
        assert await link.hear() == {"type": "pong", "received": 0}
        await link.say({"type": "device.changed", "seq": 1, "languages": ["en"]})
        await link.say({"type": "device.changed", "seq": 2, "time_zone": "Europe/Paris"})
        # Sent again on a new connection, and taken already: ignored (SES-6).
        await link.say({"type": "device.changed", "seq": 2, "languages": ["vi"]})
        await link.say({"type": "device.changed", "seq": 1, "languages": ["vi"]})
        await link.say({"type": "ping", "received": 0})
        assert await link.hear() == {"type": "pong", "received": 2}
        # A message that skips a number ends the session.
        await link.say(
            {"type": "event", "seq": 4, "name": "x", "time": "2026-11-11T00:00:00Z", "data": {}}
        )
        assert await link.hear() == {"type": "bye", "reason": "out_of_sequence"}
        assert await link.ended() == 1000


async def test_a_message_the_cloud_cannot_take_gets_an_error(cloud) -> None:
    _, _, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.open()
        await link.say({"type": "device.changed", "seq": 1})
        error = await link.hear()
        assert (error["type"], error["seq"], error["reply_to"]) == ("error", 1, 1)
        assert error["code"] == "invalid_message"
        await link.say(
            {
                "type": "conversation.request",
                "seq": 2,
                "conversation": str(uuid.uuid7()),
                "trigger": "wake_word",
                "context": {"battery": None, "charging": None},
            }
        )
        error = await link.hear()
        assert (error["seq"], error["reply_to"], error["code"]) == (2, 2, "unavailable")
        # What the cloud does not take yet, or the cloud sends, or came already, it ignores.
        for ignored in (
            {"type": "mcp", "seq": 3, "payload": {"jsonrpc": "2.0", "method": "x"}},
            {"type": "something.new", "seq": 4},
            {"type": "error", "seq": 5, "reply_to": 1, "code": "busy", "message": "busy"},
            {"type": "pong", "received": 0},
            {"type": "reconnect"},
            hello(),
        ):
            await link.say(ignored)
        await link.say({"type": "ping", "received": 2})
        assert await link.hear() == {"type": "pong", "received": 5}


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param({"type": "ping"}, id="a ping that fails"),
        pytest.param("not JSON", id="no JSON"),
        # One message per text frame: a binary frame is no message, whatever it holds.
        pytest.param(
            json.dumps({"type": "device.changed", "seq": 1, "languages": ["en"]}).encode(),
            id="binary",
        ),
        pytest.param({"type": "event", "name": "x"}, id="no number"),
        pytest.param({"type": ["device.changed"], "seq": 1}, id="a type that is no text"),
        pytest.param('{"type": "device.changed", "seq": 1, "x": 1e400}', id="past a double"),
    ],
)
async def test_a_frame_the_cloud_cannot_answer_ends_the_session(cloud, frame: Any) -> None:
    _, _, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.open()
        await link.say(frame)
        bye = await link.hear()
        assert (bye["type"], bye["reason"]) == ("bye", "invalid_message")
        assert bye["message"]
        assert await link.ended() == 1000


async def test_a_silent_connection_is_closed(make_cloud) -> None:
    cloud = await make_cloud(devices={"link": {"ping_interval": 0.2}})
    tokens, home, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.open()
        started = time.monotonic()
        assert await link.ended() == 1001
        # Two intervals, at least.
        assert time.monotonic() - started >= 0.35
    await until(lambda: _disconnected(cloud, tokens, home, device))


async def test_too_many_messages_are_dropped_and_said_so(make_cloud) -> None:
    # They come at once, within a burst: the first is handled, the next waits its turn, and
    # the backlog is full for the others.
    cloud = await make_cloud(devices={"link": {"rate": 2, "burst": 10, "backlog": 1}})
    _, _, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.open()
        for seq in range(1, 11):
            await link.say({"type": "device.changed", "seq": seq, "languages": ["en"]})
        error = await link.hear()
        assert (error["type"], error["code"], error["seq"]) == ("error", "rate_limited", 1)
        assert 2 <= error["reply_to"] <= 10
        # Said once, until what waits has been handled.
        await link.say({"type": "ping", "received": 1})
        assert await link.hear() == {"type": "pong", "received": 10}


async def test_a_message_larger_than_the_protocol_allows_closes_the_connection(
    cloud, caplog: pytest.LogCaptureFixture
) -> None:
    """Whichever side received it, the session ends (Messages, Sizes)."""
    _, _, device = await registered(cloud)
    async with served(cloud) as url:
        async with linked(url, await token(cloud, device)) as link:
            await link.open()
            text = json.dumps({"type": "event", "seq": 1, "pad": "x" * (1 << 20)})
            await link.say(text)
            assert await link.ended() == 1009
        await until(lambda: _ended_as(caplog, ["too_big"]))
        # The device's own 1009, over a message from the cloud.
        async with linked(url, await token(cloud, device)) as link:
            await link.open()
            await link.ws.close(1009)
        await until(lambda: _ended_as(caplog, ["too_big", "too_big"]))


# The smallest fragment that a side sends, but its message's last (Frames, since 3.4).
KIB = 1024


@pytest.mark.parametrize("compression", ["deflate", None])
async def test_a_message_of_1_mib_is_read_and_one_byte_more_is_not(
    cloud, compression: str | None
) -> None:
    """A side receives every message up to 1 MiB, one of exactly 1 MiB included (Messages,
    Sizes, since 3.3): uvicorn's limit counts as the protocol does, in one frame or in
    fragments of 1 KiB, the smallest that a side sends (Frames, since 3.4), back to back.
    The cloud declines compression, with which websockets refuses some messages of 1 MiB."""
    _, _, device = await registered(cloud)
    random = Random(3)
    access_token = await token(cloud, device)
    async with served(cloud) as url, linked(url, access_token, compression=compression) as link:
        assert link.ws.protocol.extensions == []
        await link.open()
        for pad in ("x" * MAX_MESSAGE, "".join(random.choices(ascii_letters, k=MAX_MESSAGE))):
            text = _ping(MAX_MESSAGE, pad)
            await link.say(text)
            assert await link.hear() == {"type": "pong", "received": 0}
            # websockets sends what it is given in pieces as one message, in fragments.
            fragments = [text[n : n + KIB] for n in range(0, len(text), KIB)]
            await link.ws.send(fragments)
            await link.ws.send(fragments)
            assert await link.hear() == {"type": "pong", "received": 0}
            assert await link.hear() == {"type": "pong", "received": 0}
        await link.say(_ping(MAX_MESSAGE + 1, pad))
        assert await link.ended() == 1009


def _ping(size: int, pad: str) -> str:
    """A ping of ``size`` bytes of UTF-8, with a field of ``pad``'s ASCII that the schema
    allows."""
    bare = json.dumps({"type": "ping", "received": 0, "pad": ""})
    text = bare[:-2] + pad[: size - len(bare)] + bare[-2:]
    assert len(text.encode()) == size
    return text


async def _ended_as(caplog: pytest.LogCaptureFixture, ended: list[str]) -> bool:
    return [
        each.ended  # type: ignore[attr-defined]
        for each in caplog.records
        if each.getMessage() == "a device's control channel closed"
    ] == ended


# Replaced and revoked.


async def test_a_newer_connection_replaces_the_older(make_cloud) -> None:
    first = await make_cloud()
    second = await make_cloud(keys=first.keys)  # replicas share their keys
    tokens, home, device = await registered(first)
    async with (
        served(first) as here,
        served(second) as there,
        linked(here, await token(first, device)) as older,
    ):
        await older.open()
        async with linked(here, await token(first, device)) as newer:
            await newer.open()
            assert await older.hear() == {"type": "bye", "reason": "replaced"}
            assert await older.ended() == 1000
            # On another replica too.
            async with linked(there, await token(first, device)) as newest:
                await newest.open()
                assert await newer.hear() == {"type": "bye", "reason": "replaced"}
                assert (await shown(first, tokens, home, device))["connected"] is True
                await newest.say({"type": "ping", "received": 0})
                assert await newest.hear() == {"type": "pong", "received": 0}
    await until(lambda: _disconnected(first, tokens, home, device))


async def test_a_removed_device_is_told_wherever_it_is_connected(make_cloud) -> None:
    first = await make_cloud()
    second = await make_cloud(keys=first.keys)  # replicas share their keys
    tokens, home, device = await registered(first)
    async with served(first) as here, linked(here, await token(first, device)) as link:
        await link.open()
        removed = await second.http.delete(
            f"/app/v1/homes/{home['id']}/devices/{device.id}", headers=bearer(tokens)
        )
        assert removed.status_code == 204
        assert await link.hear() == {"type": "bye", "reason": "revoked"}
        assert await link.ended() == 1000
    assert await first.redis("EXISTS", f"{PRESENCE_PREFIX}{device.id}") == 0


async def test_a_device_that_leaves_its_home_is_removed_then_told(cloud) -> None:
    """As removing it in the app does (leave, since 3.2)."""
    tokens, home, device = await registered(cloud)
    devices = f"/app/v1/homes/{home['id']}/devices"
    access_token = await token(cloud, device)
    async with served(cloud) as url:
        async with linked(url, access_token) as link:
            assert (await link.open())["protocol"] == "3.6"
            await link.say({"type": "leave", "seq": 1})
            assert await link.hear() == {"type": "bye", "reason": "left"}
            assert await link.ended() == 1000
        # The app no longer lists it, and its tokens and its assertions are refused.
        listed = await cloud.http.get(devices, headers=bearer(tokens))
        assert listed.json()["devices"] == []
        read = await cloud.http.get(f"{devices}/{device.id}", headers=bearer(tokens))
        assert read.status_code == 404
        assert (await refused(url, access_token)).status == 401
        asked = await cloud.device_token(device.assertion(cloud.token_endpoint))
        assert (asked.status_code, asked.json()["error"]) == (401, "invalid_client")
    assert await cloud.redis("EXISTS", f"{PRESENCE_PREFIX}{device.id}") == 0


async def test_a_deleted_home_s_devices_are_told(cloud) -> None:
    _, home, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.open()
        engine = create_engine(cloud.database, cloud.settings.database, "ai-cloud tests")
        client = redis.asyncio.Redis.from_url(cloud.redis_url)
        try:
            # As the worker hands the accounts' event to the devices' consumer.
            [handler] = [
                each.handlers[HomeDeleted.TYPE]
                for each in consumers(engine, client)
                if HomeDeleted.TYPE in each.handlers
            ]
            data = {"home_id": home["id"]}
            await handler(
                Event(uuid.uuid7(), "/accounts", HomeDeleted.TYPE, "s", datetime.now(UTC), data)
            )
        finally:
            await client.aclose()
            await engine.dispose()
        assert await link.hear() == {"type": "bye", "reason": "revoked"}


async def test_a_lost_hint_is_found_by_the_next_check(make_cloud) -> None:
    cloud = await make_cloud(**QUICK)
    _, _, device = await registered(cloud)
    async with served(cloud) as url:
        async with linked(url, await token(cloud, device)) as link:
            await link.open()
            # Another connection took over, and its hint was lost.
            await cloud.redis("SET", f"{PRESENCE_PREFIX}{device.id}", str(uuid.uuid7()))
            assert await link.hear() == {"type": "bye", "reason": "replaced"}
        async with linked(url, await token(cloud, device)) as link:
            await link.open()
            # The device was removed and registered again, and the hints lost.
            bumped = "UPDATE devices.devices SET create_time = create_time + interval '1 second'"
            await cloud.sql(f"{bumped} WHERE id = :id", id=device.id)
            assert await link.hear() == {"type": "bye", "reason": "revoked"}
        async with linked(url, await token(cloud, device)) as link:
            await link.open()
            # The device was removed, and the hint lost.
            await cloud.sql("DELETE FROM devices.devices WHERE id = :id", id=device.id)
            assert await link.hear() == {"type": "bye", "reason": "revoked"}


async def test_the_presence_of_a_connection_is_kept_while_it_is_open(make_cloud) -> None:
    cloud = await make_cloud(**QUICK)
    tokens, home, device = await registered(cloud)
    key = f"{PRESENCE_PREFIX}{device.id}"
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.open()
        held = await cloud.redis("GET", key)
        # Past its first time to live, kept by the checks.
        await asyncio.sleep(2.5)
        assert await cloud.redis("GET", key) == held
        assert 0 < await cloud.redis("PTTL", key) <= 2000
        # Lost, as Redis may lose it: the next check notes it again.
        await cloud.redis("DEL", key)
        await until(lambda: _connected(cloud, tokens, home, device))
        await link.say({"type": "ping", "received": 0})
        assert await link.hear() == {"type": "pong", "received": 0}


async def _connected(cloud: Cloud, tokens: Any, home: Any, device: Device) -> bool:
    return (await shown(cloud, tokens, home, device))["connected"] is True


async def test_presence_names_a_device_s_newest_connection(cloud) -> None:
    client = redis.asyncio.Redis.from_url(cloud.redis_url)
    presence = RedisPresence(client, 60)
    device, older, newer = uuid.uuid7(), uuid.uuid7(), uuid.uuid7()
    try:
        assert await presence.arrive(device, older) is None
        assert await presence.connected([device, uuid.uuid7()]) == {device}
        assert await presence.arrive(device, newer) == older
        assert await presence.keep([(device, older), (device, newer)]) == {(device, older)}
        await presence.leave(device, older)  # replaced: it leaves nothing
        assert await presence.connected([device]) == {device}
        await presence.leave(device, newer)
        assert await presence.connected([device]) == set()
        assert await presence.keep([]) == set()
        assert await presence.connected([]) == set()
    finally:
        await client.aclose()


# Going away.


async def test_a_replica_that_goes_away_asks_its_devices_to_connect_again(cloud) -> None:
    _, _, device = await registered(cloud)
    access_token = await token(cloud, device)
    async with served(cloud) as url, linked(url, access_token) as link:
        await link.open()
        server_going = asyncio.create_task(cloud.app.state.going_away())
        assert await link.hear() == {"type": "reconnect"}
        assert await link.ended() == 1012
        await server_going
        # It takes no new connection meanwhile.
        assert (await refused(url, access_token)).status == 503


async def test_a_server_that_stops_asks_its_devices_to_connect_again(cloud) -> None:
    _, _, device = await registered(cloud)
    serving = contextlib.AsyncExitStack()
    url = await serving.enter_async_context(served(cloud))
    try:
        async with linked(url, await token(cloud, device)) as link:
            await link.open()
            stopping = asyncio.create_task(serving.aclose())
            assert await link.hear() == {"type": "reconnect"}
            assert await link.ended() == 1012
            await stopping
    finally:
        await serving.aclose()


async def test_hints_are_heard_after_a_long_silence(make_cloud) -> None:
    # Redis's client gives up on an answer after its timeout: listening for hints waits on
    # without it.
    cloud = await make_cloud(redis={"timeout": 0.5}, events={"read_wait": 0.2})
    tokens, home, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.open()
        await asyncio.sleep(1)  # the silence itself, longer than the client's timeout
        removed = await cloud.http.delete(
            f"/app/v1/homes/{home['id']}/devices/{device.id}", headers=bearer(tokens)
        )
        assert removed.status_code == 204
        assert await link.hear() == {"type": "bye", "reason": "revoked"}


async def test_a_replica_checks_its_devices_in_one_statement_however_many(cloud) -> None:
    engine = create_engine(cloud.database, cloud.settings.database, "ai-cloud tests")
    try:
        async with PostgresStore(engine).transaction() as store:
            # More than a statement takes parameters, which IN would spend one for each.
            assert await store.registrations([uuid.uuid7() for _ in range(40_000)]) == {}
    finally:
        await engine.dispose()


async def test_a_device_removed_before_its_hello_is_told(cloud) -> None:
    tokens, home, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        removed = await cloud.http.delete(
            f"/app/v1/homes/{home['id']}/devices/{device.id}", headers=bearer(tokens)
        )
        assert removed.status_code == 204
        assert await link.hear() == {"type": "bye", "reason": "revoked"}
        assert await link.ended() == 1000


@pytest.mark.parametrize(
    "change",
    [
        "DELETE FROM devices.devices WHERE id = :id",
        # Removed, and registered again.
        "UPDATE devices.devices SET create_time = create_time + interval '1 second' WHERE id = :id",
    ],
    ids=["removed", "registered again"],
)
async def test_a_device_removed_as_it_says_hello_is_told(cloud, change: str) -> None:
    _, _, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        # After its connection opened, with the hint lost.
        await cloud.sql(change, id=device.id)
        await link.say(hello())
        assert await link.hear() == {"type": "bye", "reason": "revoked"}
        assert await link.ended() == 1000


async def test_a_replica_that_goes_away_closes_a_connection_without_a_session(cloud) -> None:
    _, _, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await cloud.app.state.going_away()
        # It sends nothing before the device's hello: no reconnect, for no session.
        assert await link.ended() == 1012


async def test_what_is_not_a_hint_is_ignored(cloud) -> None:
    tokens, home, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.open()
        await cloud.redis("PUBLISH", "devices:hints", "not a device")
        removed = await cloud.http.delete(
            f"/app/v1/homes/{home['id']}/devices/{device.id}", headers=bearer(tokens)
        )
        assert removed.status_code == 204
        assert await link.hear() == {"type": "bye", "reason": "revoked"}


async def test_a_server_stops_even_if_its_devices_cannot_be_asked_to_connect_again(
    cloud, caplog: pytest.LogCaptureFixture
) -> None:
    _, _, device = await registered(cloud)

    async def failing() -> None:
        raise RuntimeError("for the test")

    cloud.app.state.going_away = failing
    serving = contextlib.AsyncExitStack()
    url = await serving.enter_async_context(served(cloud))
    try:
        async with linked(url, await token(cloud, device)) as link:
            await link.open()
            await serving.aclose()
            # uvicorn closes the connection itself.
            assert await link.ended() == 1012
    finally:
        await serving.aclose()
    assert "could not be asked to connect again" in caplog.text


async def reading_nothing(cloud: Cloud, server: ApiServer, device: Device) -> ClientConnection:
    """A session of ``device``, at ``server``, that reads nothing and has little room for what
    it does not read, once what the cloud sends it waits: past what its connection buffers.
    The cloud must answer at once what it sends (MANY)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
    sock.setblocking(False)
    await asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", port(server)))
    reader = await connect(
        url(server),
        sock=sock,
        additional_headers={"Authorization": f"Bearer {await token(cloud, device)}"},
        ping_interval=None,
        max_queue=1,
    )
    [connection] = server.server_state.connections
    transport = connection.transport  # type: ignore[attr-defined]
    transport.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
    await reader.send(json.dumps(hello()))
    await until(lambda: cloud.redis("EXISTS", PRESENCE_PREFIX + str(device.id)))
    # Errors, until the cloud's writes wait.
    seq = 0
    async with asyncio.timeout(30):
        while transport.get_write_buffer_size() < 2**16:
            for _ in range(100):
                seq += 1
                await reader.send(json.dumps({"type": "device.changed", "seq": seq}))
            await asyncio.sleep(0)
    return reader


async def test_a_server_stops_in_time_though_a_device_reads_nothing(make_cloud) -> None:
    cloud = await make_cloud(
        api={"shutdown_timeout": 1}, devices={"link": {**MANY, "close_timeout": 1}}
    )
    tokens, home, device = await registered(cloud)
    config = server_config(cloud.app, cloud.settings, host="127.0.0.1", port=0, lifespan="off")
    server = ApiServer(config, cloud.app.state.going_away)
    serving = asyncio.create_task(server.serve())
    try:

        async def started() -> bool:
            return server.started

        await until(started)
        reader = await reading_nothing(cloud, server, device)
        server.should_exit = True
        # Its sessions' close_timeout, then its own shutdown_timeout.
        async with asyncio.timeout(4):
            await serving
        reader.transport.abort()
    finally:
        server.force_exit = server.should_exit = True
        serving.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serving
    assert not await cloud.redis("EXISTS", PRESENCE_PREFIX + str(device.id))
    assert await _disconnected(cloud, tokens, home, device)  # and registered still


# What one device can exhaust (docs/decisions/0012-what-one-device-can-exhaust.md).


async def test_a_device_that_reads_nothing_is_cut_once_its_session_ends(make_cloud) -> None:
    cloud = await make_cloud(
        api={"ws_close_timeout": 0.5}, devices={"link": {**MANY, "close_timeout": 0.5}}
    )
    _, _, device = await registered(cloud)
    async with running(cloud) as server:
        reader = await reading_nothing(cloud, server, device)
        # A frame that the cloud cannot answer ends the session, whose bye cannot leave.
        await reader.send(b"binary")

        async def cut() -> bool:
            return not server.server_state.connections

        await until(cut)
        reader.transport.abort()


async def test_a_close_waits_for_the_device_s_own_no_longer_than_ws_close_timeout(
    make_cloud,
) -> None:
    cloud = await make_cloud(api={"ws_close_timeout": 0.5})
    _, _, device = await registered(cloud)
    async with running(cloud) as server, linked(url(server), await token(cloud, device)) as link:
        await link.open()
        # A device that hears nothing more, so never answers the cloud's close.
        link.ws.transport.pause_reading()
        await link.say(b"binary")

        async def closed() -> bool:
            return not server.server_state.connections

        # Within until's 5 s, where uvicorn waits 10 s.
        await until(closed)
        link.ws.transport.abort()


async def test_a_device_that_connects_too_often_is_told_when_to_try_again(make_cloud) -> None:
    cloud = await make_cloud(devices={"connection_rate": {"limit": 2, "period": 60}})
    _, _, device = await registered(cloud)
    _, _, other = await registered(cloud)
    async with served(cloud) as at:
        access_token = await token(cloud, device)
        for _ in range(2):
            async with linked(at, access_token) as link:
                await link.open()
        # Counted for the device, whatever its token.
        refusal = await refused(at, await token(cloud, device))
        assert refusal.status == 429
        assert refusal.headers["content-type"] == "application/problem+json"
        assert refusal.body["status"] == 429
        # Two a minute: the next in 30 s, in whole seconds, none of them early.
        retry_after = refusal.headers["retry-after"]
        assert 25 <= int(retry_after) <= 30
        assert refusal.body["detail"] == f"over the rate: retry after {retry_after} s"
        # Each device counts on its own.
        async with linked(at, await token(cloud, other)) as link:
            await link.open()


async def test_a_replica_that_takes_no_more_connections_says_when_to_try_again(
    make_cloud,
) -> None:
    cloud = await make_cloud(devices={"link": {"connect_rate": 0.01, "connect_burst": 2}})
    _, _, device = await registered(cloud)
    async with served(cloud) as at:
        access_token = await token(cloud, device)
        for _ in range(2):
            async with linked(at, access_token) as link:
                await link.open()
        refusal = await refused(at, access_token)
        assert refusal.status == 503
        assert refusal.headers["content-type"] == "application/problem+json"
        # One a hundred seconds: the next in 100 s.
        assert 95 <= int(refusal.headers["retry-after"]) <= 100


def forged(cloud: Cloud, device: Device) -> str:
    """A token as the cloud's, for ``device``, but for its signature."""
    header, claims, _ = signed(cloud, device).split(".")
    signature = base64.urlsafe_b64encode(bytes(64)).rstrip(b"=").decode()
    return f"{header}.{claims}.{signature}"


async def test_attempts_without_a_valid_token_lock_no_device_out(make_cloud) -> None:
    cloud = await make_cloud(
        devices={
            "connection_rate": {"limit": 1, "period": 60},
            "link": {"connect_rate": 0.01, "connect_burst": 2},
        }
    )
    _, _, eager = await registered(cloud)
    _, _, device = await registered(cloud)
    async with served(cloud) as at:
        eager_token = await token(cloud, eager)
        async with linked(at, eager_token) as link:
            await link.open()
        # Past its own rate, however often it tries.
        for _ in range(10):
            assert (await refused(at, eager_token)).status == 429
        for access_token in (None, "not-a-token", forged(cloud, device)) * 10:
            assert (await refused(at, access_token)).status == 401
        # None of them spent what the replica takes: it takes the second of its two.
        async with linked(at, await token(cloud, device)) as link:
            await link.open()


async def test_a_device_whose_connections_cannot_be_counted_is_told_to_try_again(
    cloud, caplog: pytest.LogCaptureFixture
) -> None:
    _, _, device = await registered(cloud)
    # Redis fails as the device's connection is counted: its key holds what no count reads.
    await cloud.redis("HSET", CONNECTION_RATE_PREFIX + str(device.id), "due", "0")
    async with served(cloud) as at:
        refusal = await refused(at, await token(cloud, device))
    assert refusal.status == 503
    assert refusal.headers["content-type"] == "application/problem+json"
    assert refusal.body["status"] == 503
    # After its backoff: Redis says nothing of when it will count again.
    assert "retry-after" not in refusal.headers
    assert "Exception in ASGI application" not in caplog.text


async def test_a_connection_that_fails_to_open_is_answered_as_the_api_answers(
    cloud, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, device = await registered(cloud)
    access_token = await token(cloud, device)

    async def failing(*args: object) -> None:
        raise RuntimeError("the registry failed")

    monkeypatch.setattr(PostgresTransaction, "device", failing)
    async with served(cloud) as at:
        refusal = await refused(at, access_token)
    assert refusal.status == 500
    assert refusal.headers["content-type"] == "application/problem+json"
    assert refusal.body["status"] == 500
    assert "a device's connection could not be opened" in caplog.text
    assert "Exception in ASGI application" not in caplog.text


@pytest.mark.parametrize(
    "kind", ["pings", "pongs", "a fragment of 1,023 bytes", "a flood of continuations"]
)
async def test_frames_that_break_the_link_s_rules_close_the_connection(
    make_cloud, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Pings and unsolicited Pongs past twice the ping interval of them, and a fragment under
    1 KiB that is not its message's last (Messages, Frames, since 3.4)."""
    caplog.set_level(logging.INFO)
    # The allowance comes back no faster than the test runs.
    monkeypatch.setattr(websocket, "PINGS_A_SECOND", 1e-3)
    cloud = await make_cloud(devices={"link": {"ping_interval": 2}})
    _, _, device = await registered(cloud)
    async with served(cloud) as at, linked(at, await token(cloud, device)) as link:
        await link.open()
        match kind:
            case "pings" | "pongs":
                send = link.ws.ping if kind == "pings" else link.ws.pong
                for _ in range(4):
                    await send()
                await link.say({"type": "ping", "received": 0})
                assert (await link.hear())["type"] == "pong"
                await send()
            case "a fragment of 1,023 bytes":
                await link.ws.send(["{" + " " * (KIB - 2), "}"])
            case "a flood of continuations":
                await link.ws.send(["{" + " " * (KIB - 1), *[""] * 1000, "}"])
        assert await link.ended() == 1008
        await until(lambda: _ended_as(caplog, ["flooded"]))


async def test_text_that_is_no_utf_8_closes_the_connection(
    cloud, caplog: pytest.LogCaptureFixture
) -> None:
    """With 1007, as WebSocket fails a connection over it (RFC 6455, 8.1), and as no error of
    the cloud's: uvicorn would log one, with a traceback. The session ends with it (Messages,
    Validation, since 3.5)."""
    caplog.set_level(logging.INFO)
    _, _, device = await registered(cloud)
    async with served(cloud) as at, linked(at, await token(cloud, device)) as link:
        await link.open()
        # Below websockets' own check.
        link.ws.protocol.send_text(b"\xff")
        link.ws.send_data()
        assert await link.ended() == 1007
        await until(lambda: _ended_as(caplog, ["invalid_text"]))
    assert [each.getMessage() for each in caplog.records if each.levelno >= logging.ERROR] == []


# A key that masks nothing, for frames written whole below the client.
MASK = bytes(4)


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param(b"\x81\x00", id="unmasked"),  # RFC 6455, 5.1
        pytest.param(b"\xc1\x80" + MASK, id="a reserved bit"),  # 5.2
        pytest.param(b"\x83\x80" + MASK, id="an opcode undefined"),  # 5.2
        pytest.param(b"\x80\x80" + MASK, id="a continuation of nothing"),  # 5.4
        pytest.param(b"\x09\x80" + MASK, id="a ping fragmented"),  # 5.5
        pytest.param(b"\x89\xfe\x00\x7e" + MASK + bytes(126), id="a ping of 126 bytes"),  # 5.5
    ],
)
async def test_a_frame_past_websockets_own_rules_closes_the_connection(
    cloud, caplog: pytest.LogCaptureFixture, frame: bytes
) -> None:
    """With 1002, as WebSocket fails a connection over it (RFC 6455, 7.4.1), and as no error of
    the cloud's. The session ends with it (Messages, Validation, since 3.6)."""
    caplog.set_level(logging.INFO)
    _, _, device = await registered(cloud)
    async with served(cloud) as at, linked(at, await token(cloud, device)) as link:
        await link.open()
        # Below the client's own checks.
        link.ws.transport.write(frame)
        assert await link.ended() == 1002
        await until(lambda: _ended_as(caplog, ["invalid_frame"]))
    assert [each.getMessage() for each in caplog.records if each.levelno >= logging.ERROR] == []


@pytest.mark.parametrize(
    ("code", "ended"),
    [
        (1002, "invalid_frame"),
        (1007, "invalid_text"),
        (1008, "flooded"),
        (1009, "too_big"),
        (1001, "closed"),
    ],
)
async def test_the_devices_own_close_ends_the_session_as_the_clouds_would(
    cloud, caplog: pytest.LogCaptureFixture, code: int, ended: str
) -> None:
    """A device fails the connection with the same codes, over what the cloud sent, and both
    sides end the session (Messages: Validation, Sizes and Frames, since 3.6); any other close
    leaves it to resume (Resuming a session)."""
    caplog.set_level(logging.INFO)
    _, _, device = await registered(cloud)
    async with served(cloud) as at:
        async with linked(at, await token(cloud, device)) as link:
            await link.open()
            await link.ws.close(code)
        await until(lambda: _ended_as(caplog, [ended]))


# A request to upgrade to what uvicorn does not serve, HTTP/2 over plain TCP.
UNSUPPORTED_UPGRADE = (
    f"GET {PATH} HTTP/1.1\r\nHost: cloud.test\r\nConnection: Upgrade\r\nUpgrade: h2c\r\n\r\n"
).encode()
# A port past 65,535, which httptools' URL parser refuses (tried 2026-11-22).
INVALID_URL = b"GET http://cloud.test:65536/ HTTP/1.1\r\nHost: cloud.test\r\n\r\n"


async def answered(server: ApiServer, request: bytes) -> bytes:
    """The status line that ``server`` answers ``request`` with, sent as it is."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port(server))
    try:
        writer.write(request)
        async with asyncio.timeout(WAIT):
            return await reader.readuntil(b"\r\n")
    finally:
        writer.close()


@pytest.mark.parametrize("access_log", [False, True])
async def test_handshakes_are_logged_as_requests_are(
    make_cloud, caplog: pytest.LogCaptureFixture, access_log: bool
) -> None:
    caplog.set_level(logging.INFO)
    cloud = await make_cloud(api={"access_log": access_log})
    _, _, device = await registered(cloud)
    access_token = await token(cloud, device)
    async with running(cloud) as server:
        at = url(server)
        caplog.clear()
        for refused_token in (None, "not-a-token", forged(cloud, device)):
            await refused(at, refused_token)
        # Nor does a request that uvicorn cannot parse, or upgrade, a URL whose port httptools
        # refuses as uvicorn's callback parses it among them.
        for request in (b"NOT HTTP\r\n\r\n", INVALID_URL, UNSUPPORTED_UPGRADE):
            assert (await answered(server, request)).startswith(b"HTTP/1.1 ")
        # A refusal writes nothing of its own: a flood of them would write lines for each.
        refusals = [each.getMessage() for each in caplog.records]
        if access_log:
            assert sum('"WebSocket /device/v3/link" 401' in line for line in refusals) == 3
            assert "Invalid HTTP request received." in refusals
            assert "Unsupported upgrade request." in refusals
        else:
            assert refusals == []
        async with linked(at, access_token) as link:
            await link.open()
    handshakes = [
        each.getMessage() for each in caplog.records if '"WebSocket ' in each.getMessage()
    ]
    assert len(handshakes) == (4 if access_log else 0), handshakes
    warnings = [each for each in caplog.records if each.levelno >= logging.WARNING]
    invalid = "Invalid HTTP request received."
    unserved = [invalid, invalid, "Unsupported upgrade request."]
    assert [each.getMessage() for each in warnings] == (unserved if access_log else [])
    # A peer's error, with no traceback of the server's.
    assert not any(each.exc_info for each in warnings)


class Failed(Exception):
    """An error of the server's own code."""


@pytest.mark.parametrize("access_log", [False, True])
async def test_a_request_that_the_server_fails_to_parse_is_logged_with_its_error(
    make_cloud, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch, access_log: bool
) -> None:
    """uvicorn logs it as a request that cannot be served, as httptools gives the error of
    one of uvicorn's callbacks as its own (httptools 0.9.0, parser.pyx)."""

    def failing(protocol: object, url: bytes) -> None:
        raise Failed

    caplog.set_level(logging.INFO)
    monkeypatch.setattr(HttpToolsProtocol, "on_url", failing)
    cloud = await make_cloud(api={"access_log": access_log})
    async with running(cloud) as server:
        caplog.clear()
        assert (await answered(server, b"GET / HTTP/1.1\r\nHost: cloud.test\r\n\r\n")).startswith(
            b"HTTP/1.1 400 "
        )
    [logged] = [
        each for each in caplog.records if each.getMessage() == "Invalid HTTP request received."
    ]
    assert logged.exc_info is not None
    assert isinstance(logged.exc_info[1], httptools.HttpParserCallbackError)
    assert isinstance(logged.exc_info[1].__context__, Failed)
