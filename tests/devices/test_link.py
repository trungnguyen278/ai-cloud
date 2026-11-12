# SPDX-License-Identifier: MIT
"""The devices' control channel (platform/protocols/cloud/README.md: The control channel,
Staying connected, The session, Errors), on a WebSocket that uvicorn serves, against
PostgreSQL and Redis."""

import asyncio
import contextlib
import json
import socket
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import redis.asyncio
from app_api import Cloud, bearer, conforms
from device import Device
from link import DEEP, PATH, hello, linked, refused, served, token
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from ai_cloud.accounts.api import HomeDeleted
from ai_cloud.devices.adapters.postgres import PostgresStore
from ai_cloud.devices.adapters.redis import PRESENCE_PREFIX, RedisPresence
from ai_cloud.devices.api import consumers
from ai_cloud.entry.api import ApiServer, server_config
from ai_cloud.shared.database import create_engine
from ai_cloud.shared.events import Event

pytestmark = pytest.mark.anyio

# Short times, so that what waits for them is quick.
QUICK = {"devices": {"link": {"check_interval": 0.2, "presence_ttl": 2, "close_timeout": 1}}}


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
        for access_token in (None, "", "not a token", tokens["access_token"]):
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
            assert answer["protocol"] == "3.0"  # the lower of the two minors
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
        pytest.param(
            json.dumps(hello(protocol="2.9")), "unsupported_protocol", id="an older major"
        ),
        pytest.param(json.dumps(hello(languages=[])), "no_hello", id="invalid"),
        pytest.param(
            json.dumps(hello(device={"kind": "robot", "model": "m", "software": "\ud800"})),
            "no_hello",
            id="a lone surrogate where the schema takes any text",
        ),
        pytest.param(
            json.dumps(
                hello(events=[{"name": "x", "description": "x", "schema": {"maximum": 0}}])
            ).replace('"maximum": 0', '"maximum": NaN'),
            "no_hello",
            id="NaN where the schema takes anything",
        ),
        pytest.param(json.dumps(hello(protocol="3")), "no_hello", id="no minor"),
        pytest.param(json.dumps({"type": "ping", "received": 0}), "no_hello", id="a ping"),
        pytest.param("[1, 2]", "no_hello", id="no object"),
        pytest.param("not JSON", "no_hello", id="no JSON"),
        pytest.param('{"type": "hello", "x": NaN}', "no_hello", id="NaN"),
        pytest.param('{"type": "hello", "x": "\\ud800"}', "no_hello", id="a lone surrogate"),
        pytest.param(b"\x00binary", "no_hello", id="binary"),
        pytest.param(DEEP, "no_hello", id="nested too deep"),
        pytest.param(
            json.dumps(hello())[:-1] + ', "x": 1e400}', "no_hello", id="a number past a double"
        ),
        # The schema's uniqueItems compares what it holds; JSON Schema's own way takes a time
        # that grows as the square of their number, and recurses as deep as they nest.
        pytest.param(
            json.dumps(hello(languages=[{"n": n} for n in range(6000)])),
            "no_hello",
            id="many items to compare",
        ),
        pytest.param(
            json.dumps(hello(languages=[json.loads("[" * 400 + "]" * 400)] * 2)),
            "no_hello",
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
        assert answer == {"type": "bye", "reason": reason}
        assert await link.ended() == 1000


async def test_a_device_that_says_no_hello_in_time_is_told(make_cloud) -> None:
    cloud = await make_cloud(devices={"link": {"hello_timeout": 0.2}})
    _, _, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        assert await link.hear() == {"type": "bye", "reason": "no_hello"}
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
        # What has no number that the schema allows counts for none.
        await link.say({"type": "device.changed", "seq": True, "languages": ["vi"]})
        await link.say({"type": "device.changed", "seq": 2**53, "languages": ["vi"]})
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
        # What the cloud does not take yet, or has no number, or is no JSON, it ignores.
        for ignored in (
            {"type": "mcp", "seq": 3, "payload": {"jsonrpc": "2.0", "method": "x"}},
            {"type": "something.new", "seq": 4},
            {"type": "error", "seq": 5, "reply_to": 1, "code": "busy", "message": "busy"},
            {"type": "event", "name": "x"},
            {"type": "pong", "received": 0},
            "not JSON",
            b"binary",
            # One message per text frame: a binary frame is no message, whatever it holds.
            json.dumps({"type": "device.changed", "seq": 6, "languages": ["en"]}).encode(),
        ):
            await link.say(ignored)
        await link.say({"type": "ping", "received": 2})
        assert await link.hear() == {"type": "pong", "received": 5}
        # What it cannot read has no number it knows of either; a type that is no text is
        # numbered, and fails the schema.
        for unreadable in (DEEP, '{"type": "device.changed", "seq": 6, "x": 1e400}'):
            await link.say(unreadable)
        await link.say({"type": ["device.changed"], "seq": 6})
        error = await link.hear()
        assert (error["reply_to"], error["code"]) == (6, "invalid_message")
        await link.say({"type": "ping", "received": 3})
        assert await link.hear() == {"type": "pong", "received": 6}


async def test_a_ping_the_cloud_cannot_read_ends_the_session(cloud) -> None:
    _, _, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.open()
        await link.say({"type": "ping"})
        assert await link.hear() == {"type": "bye", "reason": "error"}
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


async def test_a_message_larger_than_the_protocol_allows_closes_the_connection(cloud) -> None:
    _, _, device = await registered(cloud)
    async with served(cloud) as url, linked(url, await token(cloud, device)) as link:
        await link.open()
        text = json.dumps({"type": "event", "seq": 1, "pad": "x" * (1 << 20)})
        await link.say(text)
        assert await link.ended() == 1009


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


async def test_a_server_stops_in_time_though_a_device_reads_nothing(make_cloud) -> None:
    # The cloud answers each ping at once, for the device's window to fill soon.
    many = {"rate": 100_000, "burst": 100_000, "backlog": 100_000}
    cloud = await make_cloud(
        api={"shutdown_timeout": 1}, devices={"link": {**many, "close_timeout": 1}}
    )
    tokens, home, device = await registered(cloud)
    config = server_config(cloud.app, cloud.settings, host="127.0.0.1", port=0, lifespan="off")
    server = ApiServer(config, cloud.app.state.going_away)
    serving = asyncio.create_task(server.serve())
    loop = asyncio.get_running_loop()
    try:

        async def started() -> bool:
            return server.started

        await until(started)
        port = server.servers[0].sockets[0].getsockname()[1]
        # A device that reads nothing, with little room for what it does not read.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        sock.setblocking(False)
        await loop.sock_connect(sock, ("127.0.0.1", port))
        reader = await connect(
            f"ws://127.0.0.1:{port}{PATH}",
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
        # Errors, until the cloud's writes wait: past what its connection buffers.
        seq = 0
        async with asyncio.timeout(30):
            while transport.get_write_buffer_size() < 2**16:
                for _ in range(100):
                    seq += 1
                    await reader.send(json.dumps({"type": "device.changed", "seq": seq}))
                await asyncio.sleep(0)
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
