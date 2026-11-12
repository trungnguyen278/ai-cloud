# SPDX-License-Identifier: MIT
"""The devices' token endpoint: a device proves who it is with an assertion that its key
signs, and gets an access token (platform/protocols/cloud/README.md, Tokens from the server;
RFC 7523)."""

import asyncio
import hashlib
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx2
import pytest
import redis.asyncio
from app_api import Cloud, bearer
from device import Device
from forge import hs256, public_pem

from ai_cloud.accounts.api import HomeDeleted
from ai_cloud.devices.adapters.postgres import PostgresStore
from ai_cloud.devices.adapters.redis import RedisHints
from ai_cloud.devices.service import Forgetting
from ai_cloud.shared.database import create_engine
from ai_cloud.shared.events import Event
from ai_cloud.shared.jose import TokenError
from ai_cloud.shared.protocols import protocol_schema

pytestmark = pytest.mark.anyio


async def registered(cloud: Cloud) -> tuple[dict[str, Any], dict[str, Any], Device]:
    """A home with its owner's tokens, and a device registered into it."""
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    device = Device()
    await cloud.add_device(tokens, home, device)
    return tokens, home, device


def oauth_error(response: httpx2.Response, error: str, status: int = 400) -> str:
    """The OAuth error a response carries (RFC 6749, 5.2), of ``status``; its description."""
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"error", "error_description"}, body
    assert body["error"] == error, body
    assert isinstance(body["error_description"], str)
    return body["error_description"]


def issued(response: httpx2.Response) -> dict[str, Any]:
    """The token a response carries, once it matches the cloud link's schema."""
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    body = response.json()
    assert protocol_schema("cloud", "token_response").problem(body) is None, body
    return body


async def test_a_registered_device_gets_a_token(cloud) -> None:
    tokens, _, device = await registered(cloud)
    body = issued(await cloud.device_token(device.assertion(cloud.token_endpoint)))
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == cloud.settings.devices.access_lifetime
    claims = cloud.keys.verify(
        body["access_token"],
        typ="at+jwt",
        issuer=cloud.settings.tokens.issuer,
        audience="device",
        leeway=0,
        require=["sub", "client_id", "iat", "exp", "jti"],
    )
    assert claims["sub"] == claims["client_id"] == str(device.id)
    assert claims["exp"] - claims["iat"] == body["expires_in"]
    # It is a device's: the app's API takes none, and the app's tokens open nothing of a
    # device's.
    refused = await cloud.http.get("/app/v1/account", headers=bearer(body["access_token"]))
    assert refused.status_code == 401
    with pytest.raises(TokenError, match="wrong audience"):
        cloud.keys.verify(
            tokens["access_token"],
            typ="at+jwt",
            issuer=cloud.settings.tokens.issuer,
            audience="device",
            leeway=0,
            require=[],
        )


async def test_a_key_the_cloud_does_not_hold_gets_invalid_client(cloud) -> None:
    await registered(cloud)
    response = await cloud.device_token(Device().assertion(cloud.token_endpoint))
    assert oauth_error(response, "invalid_client", 401) == "no device holds this key"


async def test_a_removed_device_gets_no_token(cloud) -> None:
    tokens, home, device = await registered(cloud)
    issued(await cloud.device_token(device.assertion(cloud.token_endpoint)))
    removed = await cloud.http.delete(
        f"/app/v1/homes/{home['id']}/devices/{device.id}", headers=bearer(tokens)
    )
    assert removed.status_code == 204
    response = await cloud.device_token(device.assertion(cloud.token_endpoint))
    oauth_error(response, "invalid_client", 401)


async def test_a_deleted_home_s_device_gets_no_token(cloud) -> None:
    _, home, device = await registered(cloud)
    engine = create_engine(cloud.database, cloud.settings.database, "ai-cloud tests")
    client = redis.asyncio.Redis.from_url(cloud.redis_url)
    try:
        data = {"home_id": home["id"]}
        event = Event(uuid.uuid7(), "/accounts", HomeDeleted.TYPE, "s", datetime.now(UTC), data)
        await Forgetting(PostgresStore(engine), RedisHints(client)).home_deleted(event)
    finally:
        await client.aclose()
        await engine.dispose()
    response = await cloud.device_token(device.assertion(cloud.token_endpoint))
    oauth_error(response, "invalid_client", 401)


# What a broken or forged assertion changes, from the time and the cloud it is made for.
type Changes = Callable[[int, Cloud], dict[str, Any]]


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        (lambda now, cloud: {"header": {"typ": "JWS"}}, "not a JWT"),
        (lambda now, cloud: {"without": ("typ",)}, "not a JWT"),
        (lambda now, cloud: {"without": ("kid",)}, "names no key"),
        (lambda now, cloud: {"header": {"kid": "not a thumbprint"}}, "names no key"),
        (lambda now, cloud: {"signer": Device().key}, "bad signature"),
        (
            lambda now, cloud: {"claims": {"aud": "https://cloud.example/device/token"}},
            "wrong audience",
        ),
        (
            lambda now, cloud: {"claims": {"aud": cloud.settings.devices.server}},
            "wrong audience",
        ),
        (lambda now, cloud: {"claims": {"aud": [cloud.token_endpoint]}}, "wrong audience"),
        (lambda now, cloud: {"claims": {"exp": now - 600}}, "expired"),
        (lambda now, cloud: {"claims": {"iat": now + 600, "exp": now + 660}}, "not valid yet"),
        (lambda now, cloud: {"claims": {"nbf": now + 600}}, "not valid yet"),
        (lambda now, cloud: {"claims": {"iat": now, "exp": now + 301}}, "it lives longer"),
        (lambda now, cloud: {"claims": {"iat": str(now)}}, "its times are not numbers"),
        (lambda now, cloud: {"claims": {"iat": True}}, "its times are not numbers"),
        (lambda now, cloud: {"without": ("iss",)}, "no iss claim"),
        (lambda now, cloud: {"without": ("sub",)}, "no sub claim"),
        (lambda now, cloud: {"without": ("aud",)}, "no aud claim"),
        (lambda now, cloud: {"without": ("iat",)}, "no iat claim"),
        (lambda now, cloud: {"without": ("exp",)}, "no exp claim"),
        (lambda now, cloud: {"without": ("jti",)}, "no jti claim"),
        (lambda now, cloud: {"claims": {"jti": ""}}, "its jti is empty"),
        (lambda now, cloud: {"claims": {"jti": 7}}, "invalid"),
        (
            lambda now, cloud: {"claims": {"iss": str(uuid.uuid7())}},
            "it does not speak for its key's device",
        ),
        (
            lambda now, cloud: {"claims": {"sub": "robot", "iss": "robot"}},
            "it does not speak for its key's device",
        ),
    ],
    ids=[
        "typ JWS",
        "no typ",
        "no kid",
        "kid not a thumbprint",
        "another key",
        "another cloud",
        "the server",
        "a list",
        "expired",
        "iat ahead",
        "nbf ahead",
        "too long",
        "iat text",
        "iat true",
        "no iss",
        "no sub",
        "no aud",
        "no iat",
        "no exp",
        "no jti",
        "jti empty",
        "jti number",
        "another device",
        "not an identifier",
    ],
)
async def test_what_an_assertion_must_be(cloud, changes: Changes, why: str) -> None:
    _, _, device = await registered(cloud)
    assertion = device.assertion(cloud.token_endpoint, **changes(int(time.time()), cloud))
    response = await cloud.device_token(assertion)
    assert oauth_error(response, "invalid_request").startswith(why)


async def test_another_device_s_identity_is_not_its_own(cloud) -> None:
    tokens, home, device = await registered(cloud)
    other = Device()
    await cloud.add_device(tokens, home, other)
    claims = {"iss": str(other.id), "sub": str(other.id)}
    response = await cloud.device_token(device.assertion(cloud.token_endpoint, claims=claims))
    oauth_error(response, "invalid_request")
    # Its key's name, with its own signature: the signature fails.
    header = {"kid": other.thumbprint}
    response = await cloud.device_token(device.assertion(cloud.token_endpoint, header=header))
    assert oauth_error(response, "invalid_request") == "bad signature"


async def test_no_other_algorithm_passes(cloud) -> None:
    _, _, device = await registered(cloud)
    now = int(time.time())
    claims = {
        "iss": str(device.id),
        "sub": str(device.id),
        "aud": cloud.token_endpoint,
        "iat": now,
        "exp": now + 60,
        "jti": "a",
    }
    # The algorithm confusion of RFC 8725, 2.1: the public key as an HMAC secret.
    confused = hs256(claims, public_pem(device.key), kid=device.thumbprint)
    response = await cloud.device_token(confused)
    assert oauth_error(response, "invalid_request") == "wrong algorithm"


async def test_what_the_clocks_may_disagree_by_is_allowed(cloud) -> None:
    _, _, device = await registered(cloud)
    leeway = int(cloud.settings.tokens.leeway)
    now = int(time.time())
    # A device whose clock is a little ahead: its iat is in the cloud's future.
    ahead = {"iat": now + leeway - 5, "exp": now + leeway + 55}
    issued(await cloud.device_token(device.assertion(cloud.token_endpoint, claims=ahead)))
    # A header's typ is a media type, whatever its case.
    lower = {"typ": "jwt"}
    issued(await cloud.device_token(device.assertion(cloud.token_endpoint, header=lower)))
    # An assertion may live 5 minutes, and no longer.
    longest = {"iat": now, "exp": now + 300}
    issued(await cloud.device_token(device.assertion(cloud.token_endpoint, claims=longest)))


async def test_the_audience_is_the_endpoint_as_a_device_makes_it(make_cloud) -> None:
    # The device drops a trailing slash from its server, then adds the path.
    cloud = await make_cloud(devices={"server": "https://devices.example/"})
    _, _, device = await registered(cloud)
    endpoint = "https://devices.example/device/token"
    assert cloud.token_endpoint == endpoint
    issued(await cloud.device_token(device.assertion(endpoint)))


async def test_an_assertion_works_once(cloud) -> None:
    _, _, device = await registered(cloud)
    assertion = device.assertion(cloud.token_endpoint)
    issued(await cloud.device_token(assertion))
    again = await cloud.device_token(assertion)
    assert oauth_error(again, "invalid_request") == "this assertion was used before"
    # A new one works.
    issued(await cloud.device_token(device.assertion(cloud.token_endpoint)))


async def test_a_used_assertion_is_kept_as_long_as_it_could_be_valid(cloud) -> None:
    _, _, device = await registered(cloud)
    jti = "an assertion's own"
    expires = int(time.time()) + 60
    claims = {"jti": jti, "exp": expires}
    issued(await cloud.device_token(device.assertion(cloud.token_endpoint, claims=claims)))
    digest = hashlib.sha256(jti.encode()).hexdigest()
    kept = await cloud.redis("PTTL", f"devices:assertion:{device.id}:{digest}")
    lasts = (expires + cloud.settings.tokens.leeway - time.time()) * 1000
    assert lasts - 5000 < kept <= lasts + 1000


async def test_each_device_s_assertions_are_its_own(cloud) -> None:
    tokens, home, device = await registered(cloud)
    other = Device()
    await cloud.add_device(tokens, home, other)
    for each in (device, other):
        claims = {"jti": "the same"}
        issued(await cloud.device_token(each.assertion(cloud.token_endpoint, claims=claims)))


async def test_two_requests_with_one_assertion_get_one_token(cloud) -> None:
    _, _, device = await registered(cloud)
    assertion = device.assertion(cloud.token_endpoint)
    answers = await asyncio.gather(*(cloud.device_token(assertion) for _ in range(5)))
    assert sorted(answer.status_code for answer in answers) == [200, 400, 400, 400, 400]


async def test_a_client_id_names_the_device(cloud) -> None:
    _, _, device = await registered(cloud)
    same = str(device.id).upper()
    issued(await cloud.device_token(device.assertion(cloud.token_endpoint), client_id=same))
    response = await cloud.device_token(
        device.assertion(cloud.token_endpoint), client_id=str(uuid.uuid7())
    )
    assert oauth_error(response, "invalid_request") == "client_id names another device"


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"grant_type": ""}, "invalid_request"),
        ({"grant_type": "password"}, "unsupported_grant_type"),
        ({"client_assertion_type": ""}, "invalid_request"),
        (
            {"client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:saml2-bearer"},
            "invalid_request",
        ),
    ],
)
async def test_the_request_is_oauth_s(cloud, fields: dict[str, str], error: str) -> None:
    _, _, device = await registered(cloud)
    response = await cloud.device_token(device.assertion(cloud.token_endpoint), **fields)
    oauth_error(response, error)


async def test_the_request_is_a_form_with_one_assertion(cloud) -> None:
    _, _, device = await registered(cloud)
    oauth_error(await cloud.device_token(None), "invalid_request")
    assertion = device.assertion(cloud.token_endpoint)
    as_json = await cloud.http.post(
        "/device/token",
        json={
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
        },
    )
    oauth_error(as_json, "invalid_request")
    twice = await cloud.http.post(
        "/device/token",
        content=(
            "grant_type=client_credentials"
            "&client_assertion_type=urn%3Aietf%3Aparams%3Aoauth%3Aclient-assertion-type%3Ajwt-bearer"
            f"&client_assertion={assertion}&client_assertion={assertion}"
        ),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    oauth_error(twice, "invalid_request")
    # One JWT only (RFC 7523, 2.2).
    two = await cloud.device_token(f"{assertion}.{assertion}")
    oauth_error(two, "invalid_request")
    # One way to authenticate at a time (RFC 6749, 2.3).
    secret = await cloud.device_token(assertion, client_secret="hunter2")
    assert oauth_error(secret, "invalid_request") == "an assertion, and no other credentials"
    basic = await cloud.http.post(
        "/device/token",
        data={
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
        },
        headers={"Authorization": "Basic ZGV2aWNlOmh1bnRlcjI="},
    )
    oauth_error(basic, "invalid_request")
    # None of those used the assertion up.
    issued(await cloud.device_token(assertion))


async def test_tokens_are_limited_for_each_address(make_cloud, caplog) -> None:
    cloud = await make_cloud(devices={"token_rate": {"limit": 2, "period": 60}})
    _, _, device = await registered(cloud)
    for _ in range(2):
        issued(await cloud.device_token(device.assertion(cloud.token_endpoint)))
    over = await cloud.device_token(device.assertion(cloud.token_endpoint))
    oauth_error(over, "temporarily_unavailable", 429)
    assert 0 < int(over.headers["retry-after"]) <= 60
    # A flood would fill the logs: what goes over the rate is not logged.
    refused = "a device's token request was refused"
    assert not [record for record in caplog.records if record.getMessage() == refused]


async def test_no_assertion_or_token_reaches_the_logs(cloud, caplog) -> None:
    _, _, device = await registered(cloud)
    assertion = device.assertion(cloud.token_endpoint)
    token = issued(await cloud.device_token(assertion))["access_token"]
    used = await cloud.device_token(assertion)
    oauth_error(used, "invalid_request")
    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    for secret in (assertion, assertion.split(".")[1], token, token.split(".")[2]):
        assert secret not in logged
    [refused] = [
        r for r in caplog.records if r.getMessage() == "a device's token request was refused"
    ]
    assert refused.reason == "this assertion was used before"
