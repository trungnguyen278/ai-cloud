# SPDX-License-Identifier: MIT
"""Signing in: the nonce and the exchange (the app API, Signing in)."""

import asyncio
import time
from types import SimpleNamespace

import jwt
import pytest
from app_api import ADDRESS, CLIENT, EXCHANGE, ID_TOKEN, bearer, oauth_error, problem

from ai_cloud.accounts.adapters import development

pytestmark = pytest.mark.anyio


async def test_a_first_sign_in_makes_the_account(cloud) -> None:
    tokens = await cloud.sign_in("Linh@Example.com")
    assert tokens["token_type"] == "Bearer"
    assert tokens["issued_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert tokens["expires_in"] == cloud.settings.accounts.access_lifetime
    account = await cloud.account(tokens)
    assert account["display_name"] == "Linh"
    assert account["email"] == "Linh@Example.com"
    assert account["languages"] == []
    assert account["etag"] == 'W/"1"'
    [identity] = account["identities"]
    assert identity["provider"] == "development"
    assert identity["email"] == "Linh@Example.com"


async def test_the_same_identity_signs_in_to_the_same_account(cloud) -> None:
    first = await cloud.account(await cloud.sign_in("linh@example.com"))
    second = await cloud.account(await cloud.sign_in("LINH@example.com", label="iPhone 16"))
    assert second["id"] == first["id"]
    [before], [after] = first["identities"], second["identities"]
    assert after["id"] == before["id"]
    # The identity's email follows what its provider says now; the account's stays.
    assert after["email"] == "LINH@example.com"
    assert second["email"] == "linh@example.com"
    other = await cloud.account(await cloud.sign_in("an@example.com"))
    assert other["id"] != first["id"]


async def test_a_nonce_is_good_for_one_sign_in(cloud) -> None:
    nonce = await cloud.nonce()
    assert (await cloud.exchange(await cloud.id_token(nonce=nonce))).status_code == 200
    again = await cloud.exchange(await cloud.id_token(nonce=nonce))
    assert "nonce" in oauth_error(again, "invalid_grant")


async def test_a_nonce_the_cloud_did_not_issue_is_refused(cloud) -> None:
    response = await cloud.exchange(await cloud.id_token(nonce="A" * 43))
    assert "nonce" in oauth_error(response, "invalid_grant")


async def test_a_nonce_expires_with_its_lifetime(cloud) -> None:
    nonce = await cloud.nonce()
    remaining = await cloud.redis("PTTL", f"accounts:nonce:{nonce}")
    assert 0 < remaining <= cloud.settings.accounts.nonce_lifetime * 1000


async def test_an_address_gets_so_many_nonces(make_cloud) -> None:
    cloud = await make_cloud(accounts={"nonce_rate": {"limit": 3, "period": 60}})
    for _ in range(3):
        await cloud.nonce()
    refused = await cloud.http.post("/app/v1/nonces")
    problem(refused, 429)
    assert int(refused.headers["retry-after"]) == 20  # the next is due in 60 / 3 seconds
    [key] = await cloud.redis("KEYS", "accounts:nonce-rate:*")
    assert key.decode() == f"accounts:nonce-rate:{ADDRESS}"


async def test_a_token_from_an_unknown_issuer_is_refused(cloud) -> None:
    nonce = await cloud.nonce()
    claims = {"iss": "https://elsewhere.example", "aud": CLIENT, "sub": "x", "nonce": nonce}
    now = int(time.time())
    forged = jwt.encode({**claims, "iat": now, "exp": now + 60}, "secret" * 8, "HS256")
    assert "issuer" in oauth_error(await cloud.exchange(forged), "invalid_grant")
    # The nonce was not used up by a token that failed.
    assert (await cloud.exchange(await cloud.id_token(nonce=nonce))).status_code == 200


async def test_a_forged_token_of_a_known_issuer_is_refused(cloud) -> None:
    token = await cloud.id_token()
    claims = jwt.decode(token, options={"verify_signature": False})
    kid = jwt.get_unverified_header(token)["kid"]
    # Signed with no key, with a secret, or with an unknown key: never what the
    # provider's key would sign.
    for algorithm, key, header in (
        ("none", None, {"kid": kid}),
        ("HS256", "secret" * 8, {"kid": kid}),
        ("HS256", "secret" * 8, {"kid": "made-up"}),
    ):
        forged = jwt.encode(claims, key, algorithm, headers=header)
        oauth_error(await cloud.exchange(forged), "invalid_grant")
    # A genuine token whose signature was changed.
    head, body, signature = token.split(".")
    other = "AAAA" if signature[:4] != "AAAA" else "BBBB"
    tampered = f"{head}.{body}.{other}{signature[4:]}"
    assert "signature" in oauth_error(await cloud.exchange(tampered), "invalid_grant")
    # None of them used the nonce up.
    assert (await cloud.exchange(token)).status_code == 200


@pytest.mark.parametrize(
    "token", ["not a token", "a.b.c", "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOjF9.c2ln"]
)
async def test_a_malformed_token_is_refused(cloud, token: str) -> None:
    oauth_error(await cloud.exchange(token), "invalid_grant")


async def test_an_expired_token_is_refused(cloud, monkeypatch: pytest.MonkeyPatch) -> None:
    lifetime = cloud.settings.accounts.development.id_token_lifetime
    then = time.time() - lifetime - cloud.settings.tokens.leeway - 5
    monkeypatch.setattr(development, "time", SimpleNamespace(time=lambda: then))
    expired = await cloud.id_token()
    monkeypatch.undo()
    assert "expired" in oauth_error(await cloud.exchange(expired), "invalid_grant")


async def test_an_unknown_client_is_refused(cloud) -> None:
    response = await cloud.exchange(await cloud.id_token(), client_id="someone-else")
    oauth_error(response, "invalid_client", 401)


@pytest.mark.parametrize(
    ("form", "error", "says"),
    [
        ({"client_id": CLIENT}, "invalid_request", "grant_type"),
        ({"grant_type": "password", "client_id": CLIENT}, "unsupported_grant_type", "grant"),
        (
            {"grant_type": EXCHANGE, "subject_token": "x", "client_id": CLIENT},
            "invalid_request",
            "subject_token_type",
        ),
        (
            {
                "grant_type": EXCHANGE,
                "subject_token": "x",
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                "client_id": CLIENT,
            },
            "invalid_request",
            "subject_token_type",
        ),
        (
            {
                "grant_type": EXCHANGE,
                "subject_token": "x" * 8193,
                "subject_token_type": ID_TOKEN,
                "client_id": CLIENT,
            },
            "invalid_request",
            "maxLength",
        ),
    ],
)
async def test_a_malformed_request_says_what_is_wrong(cloud, form, error, says) -> None:
    description = oauth_error(await cloud.http.post("/app/token", data=form), error)
    assert says in description
    # What was sent is not repeated back: it might be a token.
    assert "x" * 50 not in description


async def test_a_field_may_not_come_twice(cloud) -> None:
    body = f"grant_type={EXCHANGE}&subject_token=a&subject_token=b&client_id={CLIENT}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    response = await cloud.http.post("/app/token", content=body, headers=headers)
    assert "repeated" in oauth_error(response, "invalid_request")


async def test_an_empty_field_counts_as_absent(cloud) -> None:
    response = await cloud.exchange(await cloud.id_token(), label="")
    assert response.status_code == 200
    sessions = await cloud.http.get(
        "/app/v1/account/sessions",
        headers={"Authorization": f"Bearer {response.json()['access_token']}"},
    )
    assert "label" not in sessions.json()["sessions"][0]


async def test_the_token_endpoint_takes_a_form(cloud) -> None:
    response = await cloud.http.post("/app/token", json={"grant_type": EXCHANGE})
    assert "application/x-www-form-urlencoded" in oauth_error(response, "invalid_request")


async def test_a_session_label_with_control_characters_is_refused(cloud) -> None:
    response = await cloud.exchange(await cloud.id_token(), label="Pixel\u00008")
    assert "session_label" in oauth_error(response, "invalid_request")


async def test_the_development_provider_is_absent_elsewhere(make_cloud) -> None:
    cloud = await make_cloud(accounts={"development": {"enabled": False}})
    response = await cloud.http.post(
        "/app/v1/development/id-tokens", json={"nonce": "A" * 43, "display_name": "Test"}
    )
    problem(response, 404)


async def test_the_development_provider_checks_its_requests(cloud) -> None:
    url = "/app/v1/development/id-tokens"
    problem(await cloud.http.post(url, json={"nonce": "short", "display_name": "T"}), 400)
    body = {"nonce": "A" * 43, "display_name": "T", "email": "t\u0000@example.com"}
    problem(await cloud.http.post(url, json=body), 400)


async def test_two_first_sign_ins_of_one_identity_make_one_account(cloud) -> None:
    id_tokens = [await cloud.id_token("new@example.com") for _ in range(4)]
    answers = await asyncio.gather(*(cloud.exchange(id_token) for id_token in id_tokens))
    assert [answer.status_code for answer in answers] == [200] * 4
    accounts = {(await cloud.account(answer.json()))["id"] for answer in answers}
    assert len(accounts) == 1
    [[identities]] = await cloud.sql("SELECT count(*) FROM accounts.identities")
    assert identities == 1


async def test_a_sign_in_after_the_account_is_deleted_makes_a_new_one(cloud) -> None:
    tokens = await cloud.sign_in("linh@example.com")
    before = (await cloud.account(tokens))["id"]
    deleted = await cloud.http.post(
        "/app/v1/account:delete",
        json={"id_token": await cloud.id_token("linh@example.com")},
        headers=bearer(tokens),
    )
    assert deleted.status_code == 204
    again = await cloud.sign_in("linh@example.com")
    assert (await cloud.account(again))["id"] != before


async def test_a_body_is_so_large_at_most(make_cloud) -> None:
    cloud = await make_cloud(api={"max_body_size": 1024})
    form = {"grant_type": EXCHANGE, "subject_token": "x" * 2000, "client_id": CLIENT}
    problem(await cloud.http.post("/app/token", data=form), 413)

    async def chunks():
        for _ in range(4):
            yield b"x" * 512

    # Without a length, it is cut off as it comes.
    streamed = await cloud.http.post(
        "/app/token",
        content=chunks(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    problem(streamed, 413)
    # Below the limit, the request goes on.
    assert (await cloud.exchange(await cloud.id_token())).status_code == 200
