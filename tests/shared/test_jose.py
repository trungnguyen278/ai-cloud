# SPDX-License-Identifier: MIT
import asyncio
import json
import time
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from forge import hs256, public_pem
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from ai_cloud.shared.config import ConfigError
from ai_cloud.shared.jose import (
    JwksSettings,
    KeysUnavailable,
    PublishedKeys,
    SigningKeys,
    TokenError,
    new_signing_key,
)

TYP = "at+jwt"


def claims(**changes: Any) -> dict[str, Any]:
    now = int(time.time())
    return {"iss": "cloud", "aud": "app", "sub": "a", "iat": now, "exp": now + 60, **changes}


def verify(keys: SigningKeys, token: str, **changes: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "typ": TYP,
        "issuer": "cloud",
        "audience": "app",
        "leeway": 0,
        "require": ["exp", "sub"],
    }
    return keys.verify(token, **{**options, **changes})


def jwk_set(*keys: dict[str, str]) -> str:
    return json.dumps({"keys": list(keys)})


def test_the_newest_key_signs_and_every_key_verifies() -> None:
    old, new = new_signing_key(), new_signing_key()
    before = SigningKeys.from_jwk_set(jwk_set(old))
    signed_before = before.sign(claims(), typ=TYP)
    # The new key added at the end verifies, and the old one still signs.
    during = SigningKeys.from_jwk_set(jwk_set(old, new))
    assert jwt.get_unverified_header(during.sign(claims(), typ=TYP))["kid"] == old["kid"]
    # Moved to the front, the new key signs; the old one verifies what it signed.
    after = SigningKeys.from_jwk_set(jwk_set(new, old))
    signed_after = after.sign(claims(), typ=TYP)
    assert jwt.get_unverified_header(signed_after) == {
        "alg": "ES256",
        "kid": new["kid"],
        "typ": TYP,
    }
    assert verify(after, signed_before)["sub"] == "a"
    assert verify(during, signed_after)["sub"] == "a"
    # Once the old key is gone, what it signed no longer verifies.
    with pytest.raises(TokenError, match="unknown key"):
        verify(SigningKeys.from_jwk_set(jwk_set(new)), signed_before)


@pytest.mark.parametrize(
    ("changes", "token_claims", "reason"),
    [
        ({"typ": "JWT"}, {}, "wrong type"),
        ({"audience": "device"}, {}, "wrong audience"),
        ({"issuer": "elsewhere"}, {}, "wrong issuer"),
        ({}, {"exp": int(time.time()) - 10}, "expired"),
        ({}, {"iat": int(time.time()) + 3600}, "not valid yet"),
        ({"require": ["exp", "sid"]}, {}, "no sid claim"),
        ({}, {"aud": ["app", "device"]}, "wrong audience"),
    ],
)
def test_what_a_token_must_be(changes: dict, token_claims: dict, reason: str) -> None:
    keys = SigningKeys.from_jwk_set(jwk_set(new_signing_key()))
    token = keys.sign(claims(**token_claims), typ=TYP)
    with pytest.raises(TokenError, match=reason):
        verify(keys, token, **changes)


def test_forged_tokens_are_refused() -> None:
    key = new_signing_key()
    keys = SigningKeys.from_jwk_set(jwk_set(key))
    genuine = keys.sign(claims(), typ=TYP)
    head, body, signature = genuine.split(".")
    private = ECAlgorithm.from_jwk(key)
    forged_claims = claims()
    forgeries = {
        "unsigned": jwt.encode(
            forged_claims, None, "none", headers={"kid": key["kid"], "typ": TYP}
        ),
        # The public key, used as a shared secret (RFC 8725, 2.1).
        "confused": hs256(forged_claims, public_pem(private), kid=key["kid"], typ=TYP),
        "tampered": f"{head}.{body}.{'A' * len(signature)}",
        "malformed": "a.b",
    }
    for token in forgeries.values():
        with pytest.raises(TokenError):
            verify(keys, token)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("{", "not JSON"),
        ("[]", "must be a JWK set"),
        ('{"keys": []}', "must be a JWK set"),
        ('{"keys": [1]}', "key 0: not a JWK"),
    ],
)
def test_a_malformed_key_set_is_named(text: str, problem: str) -> None:
    with pytest.raises(ConfigError, match=problem):
        SigningKeys.from_jwk_set(text)


def test_a_bad_key_is_named_never_shown() -> None:
    key = new_signing_key()
    public = {k: v for k, v in key.items() if k != "d"}
    rsa_key = json.loads(RSAAlgorithm.to_jwk(rsa.generate_private_key(65537, 2048)))
    p384 = json.loads(ECAlgorithm.to_jwk(ec.generate_private_key(ec.SECP384R1())))
    broken = {**key, "d": key["d"][::-1]}
    cases = [
        (jwk_set({**key, "kid": ""}), "key 0: no kid"),
        (jwk_set(key, key), "key 1: kid repeated"),
        (jwk_set(public), "not a private P-256 key"),
        (jwk_set({**rsa_key, "kid": "r"}), "not a private P-256 key"),
        (jwk_set({**p384, "kid": "p"}), "not a private P-256 key"),
        (jwk_set(broken), "key 0: not a valid key"),
    ]
    for text, problem in cases:
        with pytest.raises(ConfigError, match=problem) as caught:
            SigningKeys.from_jwk_set(text)
        assert key["d"] not in str(caught.value)
        assert key["d"][::-1] not in str(caught.value)


def test_a_new_key_is_a_private_p256_key() -> None:
    key = new_signing_key()
    assert (key["kty"], key["crv"], key["alg"], key["use"]) == ("EC", "P-256", "ES256", "sig")
    assert isinstance(ECAlgorithm.from_jwk(key), ec.EllipticCurvePrivateKey)
    assert new_signing_key()["kid"] != key["kid"]


# The providers' published keys.

URL = "https://provider.example/keys"
SETTINGS = JwksSettings(timeout=5, refetch_interval=60, default_max_age=3600)


class Provider:
    """A provider's key endpoint, counting its requests."""

    def __init__(self, *kids: str) -> None:
        self.kids = list(kids)
        self.headers: dict[str, str] = {"Cache-Control": "public, max-age=600"}
        self.failing = False
        self.requests = 0

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == URL
        self.requests += 1
        if self.failing:
            return httpx2.Response(503)
        keys = [
            {**json.loads(RSAAlgorithm.to_jwk(_KEY.public_key())), "kid": kid, "alg": "RS256"}
            for kid in self.kids
        ]
        return httpx2.Response(200, json={"keys": keys}, headers=self.headers)


_KEY = rsa.generate_private_key(65537, 2048)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def published(provider: Provider, clock: Clock) -> PublishedKeys:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(provider.handle))
    return PublishedKeys(http, URL, SETTINGS, clock=clock)


@pytest.mark.anyio
async def test_keys_are_kept_as_the_provider_says() -> None:
    provider, clock = Provider("one"), Clock()
    keys = published(provider, clock)
    assert (await keys.key("one")).key_id == "one"
    clock.now += 599
    await keys.key("one")
    assert provider.requests == 1
    clock.now += 1
    await keys.key("one")
    assert provider.requests == 2


@pytest.mark.anyio
async def test_the_age_of_a_cached_answer_counts() -> None:
    provider, clock = Provider("one"), Clock()
    provider.headers = {"Cache-Control": "max-age=600", "Age": "590"}
    keys = published(provider, clock)
    await keys.key("one")
    clock.now += SETTINGS.refetch_interval  # 600 - 590 is less: the floor holds
    await keys.key("one")
    assert provider.requests == 2


@pytest.mark.anyio
async def test_without_caching_headers_the_default_age() -> None:
    provider, clock = Provider("one"), Clock()
    provider.headers = {}
    keys = published(provider, clock)
    await keys.key("one")
    clock.now += SETTINGS.default_max_age - 1
    await keys.key("one")
    assert provider.requests == 1


@pytest.mark.anyio
async def test_an_unknown_key_is_fetched_again_but_not_for_every_token() -> None:
    provider, clock = Provider("one"), Clock()
    keys = published(provider, clock)
    await keys.key("one")
    provider.kids.append("two")
    assert await keys.key("two") is None  # fetched a moment ago: not again
    clock.now += SETTINGS.refetch_interval
    assert (await keys.key("two")).key_id == "two"
    assert await keys.key("three") is None
    assert provider.requests == 2


@pytest.mark.anyio
async def test_kept_keys_serve_while_the_provider_fails() -> None:
    provider, clock = Provider("one"), Clock()
    keys = published(provider, clock)
    await keys.key("one")
    provider.failing = True
    clock.now += 600
    assert (await keys.key("one")).key_id == "one"
    assert (await keys.key("one")).key_id == "one"
    assert provider.requests == 2  # and not again until the interval has passed
    provider.failing = False
    clock.now += SETTINGS.refetch_interval
    await keys.key("one")
    assert provider.requests == 3


@pytest.mark.anyio
async def test_without_keys_kept_a_failing_provider_is_unavailable() -> None:
    provider, clock = Provider("one"), Clock()
    provider.failing = True
    keys = published(provider, clock)
    for _ in range(2):
        with pytest.raises(KeysUnavailable):
            await keys.key("one")
    assert provider.requests == 1


@pytest.mark.anyio
async def test_a_malformed_answer_counts_as_a_failure() -> None:
    clock = Clock()
    for answer in (httpx2.Response(200, json=[]), httpx2.Response(200, content=b"{")):
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(lambda _, a=answer: a))
        with pytest.raises(KeysUnavailable):
            await PublishedKeys(http, URL, SETTINGS, clock=clock).key("one")


@pytest.mark.anyio
async def test_requests_at_once_fetch_once() -> None:
    provider, clock = Provider("one"), Clock()
    keys = published(provider, clock)
    found = await asyncio.gather(*(keys.key("one") for _ in range(5)))
    assert {key.key_id for key in found} == {"one"}
    assert provider.requests == 1
