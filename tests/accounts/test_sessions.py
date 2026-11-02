# SPDX-License-Identifier: MIT
"""Sessions: refreshing with rotation, signing out, access tokens, and the list of
sessions (the app API, Refreshing, Signing out, Requests and Sessions)."""

import asyncio
import time
import uuid

import pytest
from app_api import CLIENT, bearer, conforms, oauth_error, problem

from ai_cloud.accounts.service import ACCESS_TOKEN_TYPE, APP_AUDIENCE

pytestmark = pytest.mark.anyio

CHALLENGE = 'Bearer error="invalid_token"'


async def test_a_refresh_token_works_once(cloud) -> None:
    first = await cloud.sign_in()
    response = await cloud.refresh(first["refresh_token"])
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    second = conforms(response, "token_response")
    assert "issued_token_type" not in second
    assert second["refresh_token"] != first["refresh_token"]
    assert second["access_token"] != first["access_token"]
    third = conforms(await cloud.refresh(second["refresh_token"]), "token_response")
    # Same account, same session.
    assert (await cloud.account(third))["id"] == (await cloud.account(first))["id"]
    sessions = await cloud.http.get("/app/v1/account/sessions", headers=bearer(third))
    assert len(sessions.json()["sessions"]) == 1


async def test_a_refresh_token_presented_twice_ends_its_session(cloud) -> None:
    first = await cloud.sign_in()
    second = conforms(await cloud.refresh(first["refresh_token"]), "token_response")
    replayed = await cloud.refresh(first["refresh_token"])
    assert "twice" in oauth_error(replayed, "invalid_grant")
    # Neither copy works any more: the session has ended, at once.
    oauth_error(await cloud.refresh(second["refresh_token"]), "invalid_grant")
    response = await cloud.http.get("/app/v1/account", headers=bearer(second))
    problem(response, 401)
    assert response.headers["www-authenticate"] == CHALLENGE


async def test_two_refreshes_with_one_token_end_the_session(cloud) -> None:
    tokens = await cloud.sign_in()
    answers = await asyncio.gather(
        cloud.refresh(tokens["refresh_token"]), cloud.refresh(tokens["refresh_token"])
    )
    assert sorted(answer.status_code for answer in answers) == [200, 400]
    [granted] = [answer.json() for answer in answers if answer.status_code == 200]
    oauth_error(await cloud.refresh(granted["refresh_token"]), "invalid_grant")


async def test_an_unknown_refresh_token_is_refused(cloud) -> None:
    await cloud.sign_in()
    assert "unknown" in oauth_error(await cloud.refresh("x" * 43), "invalid_grant")


async def test_refreshing_needs_a_known_client(cloud) -> None:
    tokens = await cloud.sign_in()
    oauth_error(
        await cloud.refresh(tokens["refresh_token"], client_id="nobody"), "invalid_client", 401
    )
    assert (await cloud.refresh(tokens["refresh_token"])).status_code == 200


async def test_a_refresh_token_works_for_its_own_client_only(make_cloud) -> None:
    cloud = await make_cloud(accounts={"client_ids": [CLIENT, "ai-app-web"]})
    tokens = await cloud.sign_in()
    refused = await cloud.refresh(tokens["refresh_token"], client_id="ai-app-web")
    assert "another client" in oauth_error(refused, "invalid_grant")
    # The token is still its session's.
    assert (await cloud.refresh(tokens["refresh_token"])).status_code == 200


async def test_a_session_left_unused_ends(cloud) -> None:
    tokens = await cloud.sign_in()
    idle = cloud.settings.accounts.refresh_idle
    await cloud.sql(
        "UPDATE accounts.sessions SET last_use_time = last_use_time - make_interval(secs => :s)",
        s=idle + 1,
    )
    assert "unused" in oauth_error(await cloud.refresh(tokens["refresh_token"]), "invalid_grant")
    assert await cloud.sql("SELECT id FROM accounts.sessions") == []


async def test_used_refresh_tokens_are_forgotten_once_they_could_not_be_valid(cloud) -> None:
    tokens = await cloud.sign_in()
    for _ in range(2):
        tokens = conforms(await cloud.refresh(tokens["refresh_token"]), "token_response")
    [[used]] = await cloud.sql(
        "SELECT count(*) FROM accounts.refresh_tokens WHERE use_time IS NOT NULL"
    )
    assert used == 2
    idle = cloud.settings.accounts.refresh_idle
    await cloud.sql(
        "UPDATE accounts.refresh_tokens SET use_time = use_time - make_interval(secs => :s)"
        " WHERE use_time IS NOT NULL",
        s=idle + 1,
    )
    conforms(await cloud.refresh(tokens["refresh_token"]), "token_response")
    [[kept]] = await cloud.sql("SELECT count(*) FROM accounts.refresh_tokens")
    assert kept == 2  # the one just used, and the session's new one


async def test_a_refresh_token_is_kept_as_its_hash(cloud) -> None:
    tokens = await cloud.sign_in()
    [[kept]] = await cloud.sql("SELECT hash FROM accounts.refresh_tokens")
    assert len(kept) == 32
    assert tokens["refresh_token"].encode() not in kept


async def test_signing_out_ends_the_session(cloud) -> None:
    tokens = await cloud.sign_in()
    response = await cloud.revoke(tokens["refresh_token"], token_type_hint="refresh_token")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    oauth_error(await cloud.refresh(tokens["refresh_token"]), "invalid_grant")
    problem(await cloud.http.get("/app/v1/account", headers=bearer(tokens)), 401)


async def test_an_access_token_signs_out_too(cloud) -> None:
    tokens = await cloud.sign_in()
    for hint in ({"token_type_hint": "access_token"}, {}):
        kept = await cloud.sign_in()
        assert (await cloud.revoke(kept["access_token"], **hint)).status_code == 200
        oauth_error(await cloud.refresh(kept["refresh_token"]), "invalid_grant")
    # Another session of the same account goes on.
    assert (await cloud.refresh(tokens["refresh_token"])).status_code == 200


async def test_revoking_answers_200_whatever_the_token(cloud) -> None:
    tokens = await cloud.sign_in()
    for token in ("not a token", "x" * 43, tokens["refresh_token"] + "x"):
        assert (await cloud.revoke(token)).status_code == 200
    assert (await cloud.refresh(tokens["refresh_token"])).status_code == 200


async def test_a_client_signs_out_its_own_sessions_only(make_cloud) -> None:
    cloud = await make_cloud(accounts={"client_ids": [CLIENT, "ai-app-web"]})
    tokens = await cloud.sign_in()
    for token in (tokens["refresh_token"], tokens["access_token"]):
        assert (await cloud.revoke(token, client_id="ai-app-web")).status_code == 200
    assert (await cloud.refresh(tokens["refresh_token"])).status_code == 200


async def test_revoking_needs_a_known_client(cloud) -> None:
    tokens = await cloud.sign_in()
    oauth_error(
        await cloud.revoke(tokens["refresh_token"], client_id="nobody"), "invalid_client", 401
    )
    oauth_error(await cloud.http.post("/app/revoke", data={"client_id": CLIENT}), "invalid_request")


async def test_requests_need_a_bearer_access_token(cloud) -> None:
    tokens = await cloud.sign_in()
    for headers in (
        {},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer"},
        bearer("not.a.token"),
        bearer(tokens["refresh_token"]),
    ):
        response = await cloud.http.get("/app/v1/account", headers=headers)
        problem(response, 401)
        assert response.headers["www-authenticate"] == CHALLENGE


def _claims(cloud, tokens: dict, **changes: object) -> dict[str, object]:
    import jwt

    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    return {**claims, **changes}


async def test_an_access_token_is_checked_whole(cloud) -> None:
    tokens = await cloud.sign_in()
    now = int(time.time())
    leeway = cloud.settings.tokens.leeway
    refused = [
        # Expired.
        (_claims(cloud, tokens, iat=now - 3600, exp=now - leeway - 1), ACCESS_TOKEN_TYPE),
        # For devices, not for the app.
        (_claims(cloud, tokens, aud="device"), ACCESS_TOKEN_TYPE),
        # Another issuer's.
        (_claims(cloud, tokens, iss="elsewhere"), ACCESS_TOKEN_TYPE),
        # Another kind of token.
        (_claims(cloud, tokens), "JWT"),
        # Without its session.
        ({k: v for k, v in _claims(cloud, tokens).items() if k != "sid"}, ACCESS_TOKEN_TYPE),
        # Of another session of the same account, which does not exist.
        (_claims(cloud, tokens, sid=str(uuid.uuid7())), ACCESS_TOKEN_TYPE),
        # Of another account, with this session.
        (_claims(cloud, tokens, sub=str(uuid.uuid7())), ACCESS_TOKEN_TYPE),
    ]
    for claims, typ in refused:
        token = cloud.keys.sign(claims, typ=typ)
        problem(await cloud.http.get("/app/v1/account", headers=bearer(token)), 401)
    # The same claims, signed alike, pass: the refusals were for what changed.
    token = cloud.keys.sign(_claims(cloud, tokens), typ=ACCESS_TOKEN_TYPE)
    assert (await cloud.http.get("/app/v1/account", headers=bearer(token))).status_code == 200
    assert _claims(cloud, tokens)["aud"] == APP_AUDIENCE


async def test_an_access_token_signed_by_another_key_is_refused(make_cloud) -> None:
    first, second = await make_cloud(), await make_cloud()
    tokens = await first.sign_in()
    problem(await second.http.get("/app/v1/account", headers=bearer(tokens)), 401)


async def test_the_list_of_sessions(cloud) -> None:
    older = await cloud.sign_in(label="Pixel 8")
    newer = await cloud.sign_in(label="iPhone 16")
    await cloud.sign_in("someone.else@example.com")
    response = await cloud.http.get("/app/v1/account/sessions", headers=bearer(older))
    body = conforms(response, "session_list")
    assert response.headers["cache-control"] == "no-store"
    assert [s["label"] for s in body["sessions"]] == ["iPhone 16", "Pixel 8"]
    assert [s["current"] for s in body["sessions"]] == [False, True]
    assert body["next_page_token"] == ""
    del newer


async def test_the_sessions_come_in_pages(cloud) -> None:
    tokens = [await cloud.sign_in(label=f"Phone {n}") for n in range(3)]
    labels, page_token = [], None
    for _ in range(3):
        params = {"page_size": "1", **({"page_token": page_token} if page_token else {})}
        response = await cloud.http.get(
            "/app/v1/account/sessions", params=params, headers=bearer(tokens[0])
        )
        body = conforms(response, "session_list")
        labels += [session["label"] for session in body["sessions"]]
        page_token = body["next_page_token"]
        if not page_token:
            break
    assert labels == ["Phone 2", "Phone 1", "Phone 0"]
    assert page_token == ""


@pytest.mark.parametrize("params", [{"page_size": "0"}, {"page_size": "51"}, {"page_size": "x"}])
async def test_a_page_size_is_from_1_to_50(cloud, params: dict[str, str]) -> None:
    tokens = await cloud.sign_in()
    response = await cloud.http.get(
        "/app/v1/account/sessions", params=params, headers=bearer(tokens)
    )
    problem(response, 400)


async def test_a_page_token_is_one_the_list_gave(cloud) -> None:
    tokens = await cloud.sign_in()
    for page_token in ("!", "AAAA"):
        response = await cloud.http.get(
            "/app/v1/account/sessions", params={"page_token": page_token}, headers=bearer(tokens)
        )
        problem(response, 400)


async def test_sessions_left_unused_are_not_listed(cloud) -> None:
    current = await cloud.sign_in(label="Current")
    await cloud.sign_in(label="Forgotten")
    await cloud.sql(
        "UPDATE accounts.sessions SET last_use_time = last_use_time - make_interval(secs => :s)"
        " WHERE label = 'Forgotten'",
        s=cloud.settings.accounts.refresh_idle + 1,
    )
    body = (await cloud.http.get("/app/v1/account/sessions", headers=bearer(current))).json()
    assert [session["label"] for session in body["sessions"]] == ["Current"]
    # The next sign-in forgets them.
    await cloud.sign_in(label="Next")
    [[count]] = await cloud.sql("SELECT count(*) FROM accounts.sessions")
    assert count == 2


async def test_ending_another_session(cloud) -> None:
    here = await cloud.sign_in(label="Here")
    there = await cloud.sign_in(label="There")
    sessions = (await cloud.http.get("/app/v1/account/sessions", headers=bearer(here))).json()
    [other] = [s["id"] for s in sessions["sessions"] if not s["current"]]
    response = await cloud.http.delete(f"/app/v1/account/sessions/{other}", headers=bearer(here))
    assert response.status_code == 204
    oauth_error(await cloud.refresh(there["refresh_token"]), "invalid_grant")
    problem(await cloud.http.get("/app/v1/account", headers=bearer(there)), 401)
    assert (await cloud.http.get("/app/v1/account", headers=bearer(here))).status_code == 200


async def test_a_session_of_another_account_cannot_be_ended(cloud) -> None:
    mine = await cloud.sign_in()
    theirs = await cloud.sign_in("someone.else@example.com")
    sessions = (await cloud.http.get("/app/v1/account/sessions", headers=bearer(theirs))).json()
    [their_session] = [s["id"] for s in sessions["sessions"]]
    for target in (their_session, str(uuid.uuid7()), "not-an-id"):
        response = await cloud.http.delete(
            f"/app/v1/account/sessions/{target}", headers=bearer(mine)
        )
        problem(response, 404)
    assert (await cloud.refresh(theirs["refresh_token"])).status_code == 200
