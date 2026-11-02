# SPDX-License-Identifier: MIT
"""The account: reading it, changing it, its identities, and deleting it (the app API,
The account)."""

import time
from types import SimpleNamespace

import pytest
from app_api import bearer, conforms, problem

from ai_cloud.accounts.adapters import development
from ai_cloud.accounts.domain.model import MAX_IDENTITIES

pytestmark = pytest.mark.anyio


async def test_changing_the_account(cloud) -> None:
    tokens = await cloud.sign_in()
    response = await cloud.http.patch(
        "/app/v1/account",
        json={"display_name": "Linh Nguyễn", "languages": ["vi", "en"]},
        headers=bearer(tokens),
    )
    account = conforms(response, "account")
    assert account["display_name"] == "Linh Nguyễn"
    assert account["languages"] == ["vi", "en"]
    assert account["etag"] == response.headers["etag"] == 'W/"2"'
    assert await cloud.account(tokens) == account
    # One field leaves the other as it is.
    response = await cloud.http.patch(
        "/app/v1/account", json={"languages": []}, headers=bearer(tokens)
    )
    assert conforms(response, "account")["display_name"] == "Linh Nguyễn"
    assert response.json()["languages"] == []


async def test_if_match_changes_only_what_was_read(cloud) -> None:
    tokens = await cloud.sign_in()
    read = await cloud.account(tokens)
    first = await cloud.http.patch(
        "/app/v1/account",
        json={"languages": ["vi"]},
        headers={**bearer(tokens), "If-Match": read["etag"]},
    )
    assert first.status_code == 200
    stale = await cloud.http.patch(
        "/app/v1/account",
        json={"languages": ["en"]},
        headers={**bearer(tokens), "If-Match": read["etag"]},
    )
    problem(stale, 412)
    assert (await cloud.account(tokens))["languages"] == ["vi"]
    anything = await cloud.http.patch(
        "/app/v1/account", json={"languages": ["en"]}, headers={**bearer(tokens), "If-Match": "*"}
    )
    assert anything.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"display_name": ""},
        {"display_name": "x" * 101},
        {"display_name": "Linh\nNguyễn"},
        {"languages": "vi"},
        {"languages": ["vi", "vi"]},
        {"languages": ["not a tag"]},
        {"languages": [f"l{n}" for n in range(11)]},
    ],
)
async def test_a_change_is_checked(cloud, body: dict) -> None:
    tokens = await cloud.sign_in()
    problem(await cloud.http.patch("/app/v1/account", json=body, headers=bearer(tokens)), 400)
    assert (await cloud.account(tokens))["etag"] == 'W/"1"'


async def test_a_change_is_json(cloud) -> None:
    tokens = await cloud.sign_in()
    headers = bearer(tokens)
    form = await cloud.http.patch("/app/v1/account", data={"languages": "vi"}, headers=headers)
    problem(form, 415)
    broken = await cloud.http.patch(
        "/app/v1/account",
        content=b"{not json",
        headers={**headers, "Content-Type": "application/json"},
    )
    problem(broken, 400)


async def test_linking_another_identity(cloud) -> None:
    tokens = await cloud.sign_in("linh@example.com")
    id_token = await cloud.id_token("linh.work@example.com")
    response = await cloud.http.post(
        "/app/v1/account/identities", json={"id_token": id_token}, headers=bearer(tokens)
    )
    assert response.status_code == 201, response.text
    identity = conforms(response, "identity")
    assert identity["email"] == "linh.work@example.com"
    account = await cloud.account(tokens)
    assert [i["id"] for i in account["identities"]][1] == identity["id"]
    assert account["etag"] == 'W/"2"'
    # The new identity signs in to the same account.
    other = await cloud.sign_in("linh.work@example.com")
    assert (await cloud.account(other))["id"] == account["id"]


async def test_an_identity_links_to_one_account(cloud) -> None:
    mine = await cloud.sign_in("linh@example.com")
    await cloud.sign_in("an@example.com")
    for email, code in (
        ("linh@example.com", "identity_linked"),
        ("an@example.com", "identity_taken"),
    ):
        response = await cloud.http.post(
            "/app/v1/account/identities",
            json={"id_token": await cloud.id_token(email)},
            headers=bearer(mine),
        )
        problem(response, 409, code)
    assert len((await cloud.account(mine))["identities"]) == 1


async def test_linking_needs_a_good_id_token(cloud) -> None:
    tokens = await cloud.sign_in()
    response = await cloud.http.post(
        "/app/v1/account/identities", json={"id_token": "not.a.token"}, headers=bearer(tokens)
    )
    problem(response, 403)
    used = await cloud.nonce()
    await cloud.exchange(await cloud.id_token("an@example.com", nonce=used))
    response = await cloud.http.post(
        "/app/v1/account/identities",
        json={"id_token": await cloud.id_token("bao@example.com", nonce=used)},
        headers=bearer(tokens),
    )
    assert "nonce" in problem(response, 403)["detail"]


async def test_an_account_has_so_many_identities(make_cloud) -> None:
    # Each identity takes a nonce: more than an address gets in a minute.
    cloud = await make_cloud(accounts={"nonce_rate": {"limit": 2 * MAX_IDENTITIES}})
    tokens = await cloud.sign_in("p0@example.com")
    for n in range(1, MAX_IDENTITIES):
        response = await cloud.http.post(
            "/app/v1/account/identities",
            json={"id_token": await cloud.id_token(f"p{n}@example.com")},
            headers=bearer(tokens),
        )
        assert response.status_code == 201
    response = await cloud.http.post(
        "/app/v1/account/identities",
        json={"id_token": await cloud.id_token("one.more@example.com")},
        headers=bearer(tokens),
    )
    problem(response, 409)
    assert len((await cloud.account(tokens))["identities"]) == MAX_IDENTITIES


async def test_removing_an_identity(cloud) -> None:
    tokens = await cloud.sign_in("linh@example.com")
    await cloud.http.post(
        "/app/v1/account/identities",
        json={"id_token": await cloud.id_token("linh.work@example.com")},
        headers=bearer(tokens),
    )
    first, second = (await cloud.account(tokens))["identities"]
    response = await cloud.http.delete(
        f"/app/v1/account/identities/{first['id']}", headers=bearer(tokens)
    )
    assert response.status_code == 204
    account = await cloud.account(tokens)
    assert account["identities"] == [second]
    assert account["etag"] == 'W/"3"'
    last = await cloud.http.delete(
        f"/app/v1/account/identities/{second['id']}", headers=bearer(tokens)
    )
    problem(last, 409, "last_identity")
    for target in (first["id"], "not-an-id"):
        missing = await cloud.http.delete(
            f"/app/v1/account/identities/{target}", headers=bearer(tokens)
        )
        problem(missing, 404)


async def test_an_identity_of_another_account_cannot_be_removed(cloud) -> None:
    mine = await cloud.sign_in("linh@example.com")
    theirs = await cloud.sign_in("an@example.com")
    [their_identity] = (await cloud.account(theirs))["identities"]
    response = await cloud.http.delete(
        f"/app/v1/account/identities/{their_identity['id']}", headers=bearer(mine)
    )
    problem(response, 404)


async def test_deleting_the_account(cloud) -> None:
    tokens = await cloud.sign_in("linh@example.com")
    elsewhere = await cloud.sign_in("linh@example.com", label="iPhone 16")
    await cloud.sign_in("an@example.com")
    response = await cloud.http.post(
        "/app/v1/account:delete",
        json={"id_token": await cloud.id_token("linh@example.com")},
        headers=bearer(tokens),
    )
    assert response.status_code == 204, response.text
    # Every session has ended, at once.
    for each in (tokens, elsewhere):
        problem(await cloud.http.get("/app/v1/account", headers=bearer(each)), 401)
    [[accounts]] = await cloud.sql("SELECT count(*) FROM accounts.accounts")
    [[sessions]] = await cloud.sql("SELECT count(*) FROM accounts.sessions")
    [[identities]] = await cloud.sql("SELECT count(*) FROM accounts.identities")
    assert (accounts, sessions, identities) == (1, 1, 1)  # the other person's
    # Signing in again makes a new account.
    again = await cloud.sign_in("linh@example.com")
    assert (await cloud.account(again))["etag"] == 'W/"1"'


async def test_deleting_needs_a_fresh_sign_in_with_one_of_its_identities(
    cloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = await cloud.sign_in("linh@example.com")
    nonce = await cloud.nonce()
    then = time.time() - cloud.settings.accounts.recent_sign_in - 5
    monkeypatch.setattr(development, "time", SimpleNamespace(time=lambda: then))
    old = await cloud.id_token("linh@example.com", nonce=nonce)
    monkeypatch.undo()
    for id_token in (
        old,
        await cloud.id_token("an@example.com"),
        "not.a.token",
    ):
        response = await cloud.http.post(
            "/app/v1/account:delete", json={"id_token": id_token}, headers=bearer(tokens)
        )
        problem(response, 403, "recent_sign_in_required")
    assert (await cloud.account(tokens))["etag"] == 'W/"1"'


async def test_unknown_paths_and_methods_answer_problems(cloud) -> None:
    problem(await cloud.http.get("/app/v1/nothing"), 404)
    problem(await cloud.http.put("/app/v1/account"), 405)
