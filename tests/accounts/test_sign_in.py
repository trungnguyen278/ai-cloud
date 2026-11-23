# SPDX-License-Identifier: MIT
"""Signing in: the nonce and the exchange (the app API, Signing in)."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from urllib.parse import quote_plus, urlencode

import jwt
import pytest
from app_api import ADDRESS, CLIENT, EXCHANGE, ID_TOKEN, bearer, oauth_error, problem

from ai_cloud.accounts.adapters import development
from ai_cloud.shared.protocols import platform_dir

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
    "token", ["not-a-token", "a.b.c", "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOjF9.c2ln"]
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


# ai-platform's vectors of the OAuth forms, each sent to its endpoint as the app sends it: a
# form, in which a token, the app's identifier and Apple's code take at most three bytes for
# each character, for they are written in base64url and dots and in printable ASCII (the app
# API, since 1.8).
FORMS = {
    "token_exchange_request": "/app/token",
    "refresh_request": "/app/token",
    "revoke_request": "/app/revoke",
}
VECTORS = json.loads(
    (platform_dir().parent / "vectors" / "validating.json").read_text(encoding="utf-8")
)["vectors"]


@pytest.mark.parametrize(
    "vector",
    [
        pytest.param(vector, id=vector["id"])
        for vector in VECTORS
        if vector["protocol"] == "app" and vector["definition"] in FORMS
    ],
)
async def test_a_form_is_checked_as_every_side_checks_it(cloud, vector) -> None:
    form = json.loads(vector["text"])
    body = "&".join(f"{_form_component(name)}={_form_component(form[name])}" for name in form)
    response = await cloud.http.post(
        FORMS[vector["definition"]],
        content=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if vector["result"] == "valid":
        # The form passes to the service, which refuses what is no token or client of the
        # cloud's, but revokes whatever the token was (RFC 7009, 2.2).
        if FORMS[vector["definition"]] == "/app/revoke" and form["client_id"] == CLIENT:
            assert response.status_code == 200, response.text
        else:
            assert response.status_code in {400, 401}, response.text
            assert response.json()["error"] in {"invalid_client", "invalid_grant"}
        return
    where, _, rule = oauth_error(response, "invalid_request").partition(": ")
    assert vector["keyword"] in rule
    # The field is named, never what it holds: it might be a token.
    assert form[where] not in rule


def _form_component(text: str) -> str:
    """``text`` in a form as the app writes it, as the WHATWG URL Standard serializes one:
    every byte but letters, digits and ``*-._`` percent-encoded, and a space as ``+``
    (ai-app's ``formEncode``). Python's ``urlencode`` leaves a ``~`` as it is."""
    return quote_plus(text, safe="*").replace("~", "%7E")


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


# The cloud's address: "A request's body holds up to 64 KiB, 65,536 bytes as sent".
LARGEST_BODY = 65_536


@pytest.mark.parametrize("declared", [True, False], ids=["with its length", "without"])
@pytest.mark.parametrize("size", [LARGEST_BODY, LARGEST_BODY + 1])
async def test_a_body_is_so_large_at_most(cloud, size: int, declared: bool) -> None:
    """A body of 64 KiB is read, and a larger one gets 413: at once when it says how long it
    is, and otherwise as it comes, at every endpoint, OAuth's included (the app API, The
    cloud's address)."""
    tokens = await cloud.sign_in()
    start, end = b'{"languages": ["vi"], "padding": "', b'"}'
    change = start + b"x" * (size - len(start) - len(end)) + end
    form = urlencode({"grant_type": EXCHANGE, "client_id": CLIENT, "subject_token": ""})
    token = (form + "x" * (size - len(form))).encode()
    assert len(change) == len(token) == size

    async def chunks(body: bytes) -> AsyncIterator[bytes]:
        for at in range(0, len(body), 4096):
            yield body[at : at + 4096]

    changed = await cloud.http.patch(
        "/app/v1/account",
        content=change if declared else chunks(change),
        headers={**bearer(tokens), "Content-Type": "application/json"},
    )
    exchanged = await cloud.http.post(
        "/app/token",
        content=token if declared else chunks(token),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if size > LARGEST_BODY:
        problem(changed, 413)
        problem(exchanged, 413)
        return
    # Read whole: a field that the cloud does not know is no matter, and an ID token so long
    # is no valid one.
    assert changed.status_code == 200, changed.text
    assert changed.json()["languages"] == ["vi"]
    assert exchanged.status_code == 400, exchanged.text
