# SPDX-License-Identifier: MIT
"""The providers' ID tokens, verified as OpenID Connect says, without a database."""

import json
import time
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from forge import hs256, public_pem
from jwt.algorithms import RSAAlgorithm

from ai_cloud.accounts.adapters import google
from ai_cloud.accounts.adapters.oidc import IdTokenProviders, OidcProvider, StaticKeys
from ai_cloud.accounts.domain.errors import InvalidGrant, ProviderUnavailable
from ai_cloud.shared.jose import JwksSettings

pytestmark = pytest.mark.anyio

CLIENT_ID = "000000000000-example.apps.googleusercontent.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "google-key"
JWKS = JwksSettings(timeout=5, refetch_interval=60, default_max_age=3600)


def google_keys(request: httpx2.Request) -> httpx2.Response:
    assert str(request.url) == google.KEYS_URL
    public = json.loads(RSAAlgorithm.to_jwk(KEY.public_key()))
    keys = [{**public, "kid": KID, "alg": "RS256", "use": "sig"}]
    return httpx2.Response(200, json={"keys": keys}, headers={"Cache-Control": "max-age=600"})


def provider(handler: Any = google_keys) -> OidcProvider:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return google.google(http, [CLIENT_ID], JWKS, leeway=30)


def token(key: Any = KEY, algorithm: str = "RS256", kid: str = KID, **changes: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "100000000000000000001",
        "email": "linh@example.com",
        "email_verified": True,
        "name": "Linh",
        "nonce": "n" * 43,
        "iat": now,
        "exp": now + 3600,
        **changes,
    }
    return jwt.encode(
        {k: v for k, v in claims.items() if v is not None}, key, algorithm, {"kid": kid}
    )


async def test_a_google_id_token() -> None:
    sign_in = await provider().verify(token())
    assert (sign_in.provider, sign_in.subject) == ("google", "100000000000000000001")
    assert (sign_in.email, sign_in.name, sign_in.nonce) == ("linh@example.com", "Linh", "n" * 43)
    assert abs(sign_in.issue_time.timestamp() - time.time()) < 5
    # Google's issuer comes in two forms.
    assert (await provider().verify(token(iss="accounts.google.com"))).subject


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        ({"aud": "another-app.apps.googleusercontent.com"}, "wrong audience"),
        ({"iss": "https://accounts.google.com.example"}, "wrong issuer"),
        ({"exp": int(time.time()) - 60}, "expired"),
        ({"nonce": None}, "no nonce claim"),
        ({"sub": None}, "no sub claim"),
        ({"sub": 100}, "refused"),
        ({"nonce": 7}, "malformed"),
        ({"iat": "yesterday"}, "refused"),
    ],
)
async def test_what_a_google_id_token_must_be(changes: dict[str, Any], says: str) -> None:
    with pytest.raises(InvalidGrant, match=says):
        await provider().verify(token(**changes))


async def test_an_email_counts_once_google_has_checked_it() -> None:
    for verified in (False, "false", None):
        assert (await provider().verify(token(email_verified=verified))).email is None
    assert (await provider().verify(token(email_verified="true"))).email == "linh@example.com"
    assert (await provider().verify(token(email="x\u0000@example.com"))).email is None


async def test_a_token_google_did_not_sign_is_refused() -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(InvalidGrant, match="bad signature"):
        await provider().verify(token(other))
    with pytest.raises(InvalidGrant, match="unknown key"):
        await provider().verify(token(other, kid="other"))
    # Google's public key, used as a shared secret (RFC 8725, 2.1).
    claims = jwt.decode(token(), options={"verify_signature": False})
    with pytest.raises(InvalidGrant, match="wrong algorithm"):
        await provider().verify(hs256(claims, public_pem(KEY), kid=KID))
    with pytest.raises(InvalidGrant, match="wrong algorithm"):
        await provider().verify(token(None, "none"))


async def test_google_unreachable_is_not_the_token_s_fault() -> None:
    with pytest.raises(ProviderUnavailable):
        await provider(lambda _: httpx2.Response(503)).verify(token())


async def test_a_token_goes_to_the_provider_its_issuer_names() -> None:
    development_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    development = OidcProvider(
        "development",
        issuers=["ai-cloud-development"],
        audiences=["ai-app"],
        keys=StaticKeys(
            [
                jwt.PyJWK(
                    {**json.loads(RSAAlgorithm.to_jwk(development_key.public_key())), "kid": "d"}
                )
            ]
        ),
        algorithms=["RS256"],
        leeway=30,
    )
    providers = IdTokenProviders([provider(), development])
    assert (await providers.verify(token())).provider == "google"
    from_development = token(development_key, kid="d", iss="ai-cloud-development", aud="ai-app")
    assert (await providers.verify(from_development)).provider == "development"
    # A development token cannot pass for Google's, nor Google's for one of development.
    with pytest.raises(InvalidGrant):
        await providers.verify(token(development_key, kid="d"))
    not_text = hs256({"iss": 7, "sub": "x"}, b"secret", kid=KID)
    for bad in ("x", token(iss="https://elsewhere.example"), not_text):
        with pytest.raises(InvalidGrant):
            await providers.verify(bad)
    with pytest.raises(ValueError, match="two providers"):
        IdTokenProviders([provider(), provider()])
