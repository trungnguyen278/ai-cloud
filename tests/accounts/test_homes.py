# SPDX-License-Identifier: MIT
"""Homes: creating them, with an Idempotency-Key, reading, changing, handing over and
deleting them (the app API, Homes)."""

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from app_api import Cloud, bearer, conforms, once, problem
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ai_cloud.accounts.adapters import development
from ai_cloud.accounts.adapters.postgres import idempotency_lock
from ai_cloud.shared.database import create_engine
from ai_cloud.shared.protocols import platform_dir

pytestmark = pytest.mark.anyio

HOME = {"name": "Nhà Linh", "time_zone": "Asia/Ho_Chi_Minh", "languages": ["vi", "en"]}


async def test_nobody_gets_a_home_by_signing_in(cloud) -> None:
    tokens = await cloud.sign_in()
    response = await cloud.http.get("/app/v1/homes", headers=bearer(tokens))
    assert conforms(response, "home_list") == {"homes": [], "next_page_token": ""}
    assert response.headers["cache-control"] == "no-store"


async def test_creating_a_home(cloud) -> None:
    tokens = await cloud.sign_in("linh@example.com", "Linh")
    response = await cloud.http.post("/app/v1/homes", json=HOME, headers=once(tokens))
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    home = conforms(response, "home")
    assert {key: home[key] for key in HOME} == HOME
    assert home["role"] == "owner"
    assert home["etag"] == response.headers["etag"] == 'W/"1"'
    assert await cloud.home(tokens, home) == home
    listed = await cloud.http.get("/app/v1/homes", headers=bearer(tokens))
    assert listed.json()["homes"] == [home]
    [owner] = await cloud.members(tokens, home)
    assert owner["id"] == home["member_id"]
    assert (owner["kind"], owner["display_name"], owner["role"]) == ("account", "Linh", "owner")
    assert owner["current"] is True


@pytest.mark.parametrize(
    "body",
    [
        {},
        {**HOME, "name": ""},
        {**HOME, "name": "x" * 101},
        {**HOME, "name": "Nhà\nLinh"},
        {**HOME, "name": "Nhà Linh\n"},
        {**HOME, "name": "Nhà\u0085Linh"},
        {**HOME, "time_zone": "../etc/passwd"},
        {**HOME, "time_zone": "Mars/Olympus_Mons"},
        {**HOME, "time_zone": "localtime"},
        {**HOME, "time_zone": "UTC\n"},
        {**HOME, "languages": ["vi\n"]},
        {**HOME, "languages": []},
        {**HOME, "languages": ["vi", "vi"]},
        {**HOME, "languages": ["not a tag"]},
        {key: value for key, value in HOME.items() if key != "time_zone"},
        {key: value for key, value in HOME.items() if key != "languages"},
    ],
)
async def test_a_new_home_is_checked(cloud, body: dict[str, Any]) -> None:
    tokens = await cloud.sign_in()
    problem(await cloud.http.post("/app/v1/homes", json=body, headers=once(tokens)), 400)
    assert (await cloud.http.get("/app/v1/homes", headers=bearer(tokens))).json()["homes"] == []


@pytest.mark.parametrize("zone", ["Asia/Ho_Chi_Minh", "Asia/Saigon", "UTC", "America/New_York"])
async def test_any_iana_time_zone_will_do(cloud, zone: str) -> None:
    home = await cloud.create_home(await cloud.sign_in(), time_zone=zone)
    assert home["time_zone"] == zone


async def test_an_account_is_in_so_many_homes(make_cloud) -> None:
    cloud = await make_cloud(accounts={"homes_per_account": 2})
    tokens = await cloud.sign_in()
    await cloud.create_home(tokens)
    await cloud.create_home(tokens)
    response = await cloud.http.post("/app/v1/homes", json=HOME, headers=once(tokens))
    problem(response, 409, "home_limit_reached")


async def test_the_limit_holds_for_requests_that_race(make_cloud) -> None:
    cloud = await make_cloud(accounts={"homes_per_account": 1})
    tokens = await cloud.sign_in()
    answers = await asyncio.gather(
        *(cloud.http.post("/app/v1/homes", json=HOME, headers=once(tokens)) for _ in range(4))
    )
    assert sorted(answer.status_code for answer in answers) == [201, 409, 409, 409]


async def test_listing_homes_in_pages(make_cloud) -> None:
    cloud = await make_cloud(accounts={"homes_per_account": 3})
    tokens = await cloud.sign_in()
    homes = [await cloud.create_home(tokens, name=f"Nhà {n}") for n in range(3)]
    listed, token = [], ""
    for _ in homes:
        response = await cloud.http.get(
            "/app/v1/homes", params={"page_size": 1, "page_token": token}, headers=bearer(tokens)
        )
        page = conforms(response, "home_list")
        listed += page["homes"]
        token = page["next_page_token"]
    assert listed == homes
    assert token == ""
    for params, status in (({"page_size": 0}, 400), ({"page_size": 51}, 400)):
        response = await cloud.http.get("/app/v1/homes", params=params, headers=bearer(tokens))
        problem(response, status)
    broken = await cloud.http.get(
        "/app/v1/homes", params={"page_token": "not a token"}, headers=bearer(tokens)
    )
    problem(broken, 400)


async def test_each_account_sees_its_own_homes(cloud) -> None:
    mine = await cloud.sign_in("linh@example.com")
    theirs = await cloud.sign_in("an@example.com")
    home = await cloud.create_home(mine)
    other = await cloud.create_home(theirs)
    response = await cloud.http.get("/app/v1/homes", headers=bearer(mine))
    assert response.json()["homes"] == [home]
    assert other["id"] != home["id"]


# The Idempotency-Key.


@pytest.mark.parametrize(
    "key",
    [
        "",
        "01a1a06c-1400-75e6-a77f-f2149dbe59d5",  # not quoted
        '"not a uuid"',
        '"01a1a06c140075e6a77ff2149dbe59d5"',
        '"01a1a06c-1400-75e6-a77f-f2149dbe59d5";x=1',
    ],
)
async def test_a_key_is_one_uuid_as_a_quoted_string(cloud, key: str) -> None:
    tokens = await cloud.sign_in()
    response = await cloud.http.post(
        "/app/v1/homes", json=HOME, headers=once(tokens) | {"Idempotency-Key": key}
    )
    problem(response, 400)


async def test_no_key_is_refused(cloud) -> None:
    tokens = await cloud.sign_in()
    problem(await cloud.http.post("/app/v1/homes", json=HOME, headers=bearer(tokens)), 400)


async def test_two_keys_are_refused(cloud) -> None:
    tokens = await cloud.sign_in()
    first, second = once(tokens)["Idempotency-Key"], once(tokens)["Idempotency-Key"]
    response = await cloud.http.post(
        "/app/v1/homes",
        json=HOME,
        headers=[
            ("Authorization", bearer(tokens)["Authorization"]),
            ("Idempotency-Key", first),
            ("Idempotency-Key", second),
        ],
    )
    problem(response, 400)


async def test_a_retry_gets_the_first_answer(cloud) -> None:
    tokens = await cloud.sign_in()
    headers = once(tokens)
    first = await cloud.http.post("/app/v1/homes", json=HOME, headers=headers)
    assert first.status_code == 201
    # Whatever has happened since, it is the first answer that comes back.
    home = first.json()
    changed = await cloud.http.patch(
        f"/app/v1/homes/{home['id']}", json={"name": "Nhà"}, headers=bearer(tokens)
    )
    assert changed.status_code == 200
    retry = await cloud.http.post("/app/v1/homes", json=HOME, headers=headers)
    assert retry.status_code == 201
    assert retry.headers["etag"] == 'W/"1"'
    assert retry.content == first.content
    listed = await cloud.http.get("/app/v1/homes", headers=bearer(tokens))
    assert [each["name"] for each in listed.json()["homes"]] == ["Nhà"]


async def test_a_key_is_for_one_request(cloud) -> None:
    tokens = await cloud.sign_in()
    headers = once(tokens)
    home = (await cloud.http.post("/app/v1/homes", json=HOME, headers=headers)).json()
    other_body = await cloud.http.post(
        "/app/v1/homes", json={**HOME, "name": "Nhà khác"}, headers=headers
    )
    problem(other_body, 422)
    other_request = await cloud.http.post(
        f"/app/v1/homes/{home['id']}/members", json={"display_name": "Bà"}, headers=headers
    )
    problem(other_request, 422)


async def test_each_account_s_keys_are_its_own(cloud) -> None:
    mine = await cloud.sign_in("linh@example.com")
    theirs = await cloud.sign_in("an@example.com")
    key = once(mine)["Idempotency-Key"]
    first = await cloud.http.post("/app/v1/homes", json=HOME, headers=once(mine, key))
    second = await cloud.http.post("/app/v1/homes", json=HOME, headers=once(theirs, key))
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


async def test_a_failed_request_keeps_no_answer(make_cloud) -> None:
    cloud = await make_cloud(accounts={"homes_per_account": 1})
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    headers = once(tokens)
    refused = await cloud.http.post("/app/v1/homes", json=HOME, headers=headers)
    problem(refused, 409, "home_limit_reached")
    # Once the person has room, the retry does what it asked.
    id_token = await cloud.id_token()
    deleted = await cloud.http.post(
        f"/app/v1/homes/{home['id']}:delete", json={"id_token": id_token}, headers=bearer(tokens)
    )
    assert deleted.status_code == 204
    assert (await cloud.http.post("/app/v1/homes", json=HOME, headers=headers)).status_code == 201


async def test_a_retry_while_the_first_request_runs_tries_again(cloud) -> None:
    tokens = await cloud.sign_in()
    account = await cloud.account(tokens)
    key = uuid.uuid7()
    headers = once(tokens, f'"{key}"')
    engine = create_engine(cloud.database, cloud.settings.database, "ai-cloud tests")
    try:
        async with engine.begin() as first:
            lock = idempotency_lock(uuid.UUID(account["id"]), key)
            await first.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
            running = await cloud.http.post("/app/v1/homes", json=HOME, headers=headers)
            problem(running, 409)
    finally:
        await engine.dispose()
    assert (await cloud.http.post("/app/v1/homes", json=HOME, headers=headers)).status_code == 201


async def test_two_requests_with_one_key_make_one_home(cloud) -> None:
    tokens = await cloud.sign_in()
    headers = once(tokens)
    answers = await asyncio.gather(
        *(cloud.http.post("/app/v1/homes", json=HOME, headers=headers) for _ in range(5))
    )
    made = [answer for answer in answers if answer.status_code == 201]
    assert made
    assert all(answer.status_code in (201, 409) for answer in answers)
    assert len({answer.content for answer in made}) == 1
    listed = await cloud.http.get("/app/v1/homes", headers=bearer(tokens))
    assert len(listed.json()["homes"]) == 1


async def test_a_first_answer_is_kept_for_the_retention_only(cloud) -> None:
    tokens = await cloud.sign_in()
    headers = once(tokens)
    first = await cloud.http.post("/app/v1/homes", json=HOME, headers=headers)
    await cloud.sql(
        "UPDATE accounts.idempotency_keys "
        "SET create_time = create_time - make_interval(secs => :seconds)",
        seconds=cloud.settings.accounts.idempotency_retention + 1,
    )
    again = await cloud.http.post("/app/v1/homes", json=HOME, headers=headers)
    assert again.status_code == 201
    assert again.json()["id"] != first.json()["id"]
    [[kept]] = await cloud.sql("SELECT count(*) FROM accounts.idempotency_keys")
    assert kept == 1


async def test_a_name_is_text(cloud) -> None:
    tokens = await cloud.sign_in()
    broken = await cloud.http.post(
        "/app/v1/homes",
        content=b'{"name": "x\\ud800", "time_zone": "UTC", "languages": ["vi"]}',
        headers={**once(tokens), "Content-Type": "application/json"},
    )
    problem(broken, 400)


async def test_a_first_answer_goes_with_what_it_made(cloud) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    await cloud.add_profile(tokens, home)
    [[kept]] = await cloud.sql("SELECT count(*) FROM accounts.idempotency_keys")
    assert kept == 2
    response = await cloud.http.post(
        f"/app/v1/homes/{home['id']}:delete",
        json={"id_token": await cloud.id_token()},
        headers=bearer(tokens),
    )
    assert response.status_code == 204
    [[kept]] = await cloud.sql("SELECT count(*) FROM accounts.idempotency_keys")
    assert kept == 0


# Changing a home.


async def test_changing_a_home(cloud) -> None:
    family = await cloud.family()
    for tokens, name in ((family.owner, "Của chủ nhà"), (family.admin, "Của quản trị")):
        response = await cloud.http.patch(family.path, json={"name": name}, headers=bearer(tokens))
        home = conforms(response, "home")
        assert home["name"] == name
        assert home["etag"] == response.headers["etag"]
        assert home["languages"] == HOME["languages"]
        assert home["member_id"] == (await cloud.home(tokens, family.home))["member_id"]
    response = await cloud.http.patch(
        family.path,
        json={"time_zone": "Asia/Bangkok", "languages": ["en"]},
        headers=bearer(family.owner),
    )
    home = conforms(response, "home")
    assert (home["time_zone"], home["languages"]) == ("Asia/Bangkok", ["en"])
    assert home["name"] == "Của quản trị"
    assert home["etag"] == 'W/"4"'


async def test_a_member_does_not_change_a_home(cloud) -> None:
    family = await cloud.family()
    response = await cloud.http.patch(
        family.path, json={"name": "x"}, headers=bearer(family.member)
    )
    problem(response, 403, "role_required")
    assert (await cloud.home(family.owner, family.home))["name"] == HOME["name"]


async def test_if_match_changes_only_the_home_that_was_read(cloud) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    path = f"/app/v1/homes/{home['id']}"
    headers = {**bearer(tokens), "If-Match": home["etag"]}
    assert (await cloud.http.patch(path, json={"name": "A"}, headers=headers)).status_code == 200
    problem(await cloud.http.patch(path, json={"name": "B"}, headers=headers), 412)
    assert (await cloud.home(tokens, home))["name"] == "A"
    anything = {**bearer(tokens), "If-Match": "*"}
    assert (await cloud.http.patch(path, json={"name": "C"}, headers=anything)).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "x\ny"},
        {"name": "x\n"},
        {"time_zone": "Mars/Olympus_Mons"},
        {"languages": []},
    ],
)
async def test_a_change_to_a_home_is_checked(cloud, body: dict[str, Any]) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    response = await cloud.http.patch(
        f"/app/v1/homes/{home['id']}", json=body, headers=bearer(tokens)
    )
    problem(response, 400)
    assert (await cloud.home(tokens, home))["etag"] == 'W/"1"'


async def test_a_home_the_account_is_not_in_answers_as_if_it_did_not_exist(cloud) -> None:
    family = await cloud.family()
    path, ids, stranger = family.path, family.ids, bearer(family.outsider)
    id_token = await cloud.id_token("bao@example.com", "Bảo")
    invitation = await cloud.invite(family.owner, family.home)
    token = {"id_token": id_token}
    for method, url, body in (
        ("GET", path, None),
        ("PATCH", path, {"name": "x"}),
        ("GET", f"{path}/members", None),
        ("POST", f"{path}/members", {"display_name": "x"}),
        ("PATCH", f"{path}/members/{ids['member']}", {"display_name": "x"}),
        ("DELETE", f"{path}/members/{ids['member']}", None),
        ("POST", f"{path}/invitations", None),
        ("GET", f"{path}/invitations", None),
        ("DELETE", f"{path}/invitations/{invitation['id']}", None),
    ):
        response = await cloud.http.request(
            method, url, json=body, headers=once(family.outsider) | stranger
        )
        problem(response, 404)
    # The fresh sign-in first, so that a person outside learns nothing from it.
    problem(
        await cloud.http.post(f"{path}:delete", json=token, headers=stranger),
        404,
    )
    problem(
        await cloud.http.post(
            f"{path}:transfer",
            json={
                "to_member_id": ids["owner"],
                "id_token": await cloud.id_token("bao@example.com"),
            },
            headers=stranger,
        ),
        404,
    )
    # A home that does not exist answers the same.
    missing = await cloud.http.get(f"/app/v1/homes/{uuid.uuid7()}", headers=stranger)
    assert missing.json() == (await cloud.http.get(path, headers=stranger)).json()
    for broken in ("not-an-id", "abc:transfer"):
        problem(await cloud.http.get(f"/app/v1/homes/{broken}", headers=stranger), 404)


# Handing a home over.


async def test_handing_a_home_over(cloud) -> None:
    family = await cloud.family()
    response = await cloud.http.post(
        f"{family.path}:transfer",
        json={"to_member_id": family.ids["member"], "id_token": await cloud.id_token()},
        headers=bearer(family.owner),
    )
    assert response.status_code == 200, response.text
    home = conforms(response, "home")
    assert (home["role"], home["member_id"]) == ("admin", family.ids["owner"])
    assert home["etag"] == 'W/"2"'
    new_owner = await cloud.home(family.member, family.home)
    assert (new_owner["role"], new_owner["etag"]) == ("owner", 'W/"2"')
    roles = {
        each["id"]: each.get("role") for each in await cloud.members(family.owner, family.home)
    }
    assert roles == {
        family.ids["owner"]: "admin",
        family.ids["admin"]: "admin",
        family.ids["member"]: "owner",
        family.ids["profile"]: None,
    }
    # The old owner can leave now; the new one cannot.
    left = await cloud.http.delete(
        f"{family.path}/members/{family.ids['owner']}", headers=bearer(family.owner)
    )
    assert left.status_code == 204
    problem(
        await cloud.http.delete(
            f"{family.path}/members/{family.ids['member']}", headers=bearer(family.member)
        ),
        409,
        "owner_cannot_leave",
    )


async def test_the_new_owner_can_hand_the_home_back(cloud) -> None:
    family = await cloud.family()
    for tokens, email, to in (
        (family.owner, "linh@example.com", "member"),
        (family.member, "minh@example.com", "owner"),
    ):
        response = await cloud.http.post(
            f"{family.path}:transfer",
            json={"to_member_id": family.ids[to], "id_token": await cloud.id_token(email)},
            headers=bearer(tokens),
        )
        assert response.status_code == 200, response.text
    assert (await cloud.home(family.owner, family.home))["role"] == "owner"
    assert (await cloud.home(family.member, family.home))["role"] == "admin"


async def test_only_the_owner_hands_a_home_over(cloud) -> None:
    family = await cloud.family()
    for tokens, email in (
        (family.admin, "an@example.com"),
        (family.member, "minh@example.com"),
    ):
        response = await cloud.http.post(
            f"{family.path}:transfer",
            json={"to_member_id": family.ids["admin"], "id_token": await cloud.id_token(email)},
            headers=bearer(tokens),
        )
        problem(response, 403, "role_required")


async def test_a_home_goes_to_another_person_with_an_account(cloud) -> None:
    family = await cloud.family()
    for to, status in (
        (family.ids["owner"], 409),
        (family.ids["profile"], 409),
        (str(uuid.uuid7()), 404),
    ):
        response = await cloud.http.post(
            f"{family.path}:transfer",
            json={"to_member_id": to, "id_token": await cloud.id_token()},
            headers=bearer(family.owner),
        )
        problem(response, status)
    assert (await cloud.home(family.owner, family.home))["role"] == "owner"


async def test_a_person_of_another_home_is_not_one_of_this_home(cloud) -> None:
    family = await cloud.family()
    theirs = await cloud.create_home(family.outsider)
    response = await cloud.http.post(
        f"{family.path}:transfer",
        json={"to_member_id": theirs["member_id"], "id_token": await cloud.id_token()},
        headers=bearer(family.owner),
    )
    problem(response, 404)


async def test_handing_over_needs_a_fresh_sign_in_with_one_s_own_identity(
    cloud, monkeypatch: pytest.MonkeyPatch
) -> None:
    family = await cloud.family()
    nonce = await cloud.nonce()
    then = time.time() - cloud.settings.accounts.recent_sign_in - 5
    monkeypatch.setattr(development, "time", SimpleNamespace(time=lambda: then))
    old = await cloud.id_token("linh@example.com", nonce=nonce)
    monkeypatch.undo()
    for id_token in (old, await cloud.id_token("an@example.com"), "not.a.token"):
        for action, extra in (("transfer", {"to_member_id": family.ids["member"]}), ("delete", {})):
            response = await cloud.http.post(
                f"{family.path}:{action}",
                json={"id_token": id_token, **extra},
                headers=bearer(family.owner),
            )
            problem(response, 403, "recent_sign_in_required")
    assert (await cloud.home(family.owner, family.home))["role"] == "owner"


async def test_a_home_has_one_owner_whatever_the_code_does(cloud) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    await cloud.add_profile(tokens, home)
    second = await cloud.sign_in("an@example.com", "An")
    member = await cloud.join(second, home, tokens)
    with pytest.raises(IntegrityError, match="ix_members_owner"):
        await cloud.sql(
            "UPDATE accounts.members SET role = 'owner' WHERE id = :id",
            id=uuid.UUID(member["member_id"]),
        )
    # And a person has a role just when they have an account.
    with pytest.raises(IntegrityError, match="ck_members_role_with_account"):
        await cloud.sql(
            "UPDATE accounts.members SET role = NULL WHERE id = :id",
            id=uuid.UUID(member["member_id"]),
        )
    with pytest.raises(IntegrityError, match="ck_members_role_with_account"):
        await cloud.sql(
            "UPDATE accounts.members SET role = 'member' WHERE account_id IS NULL",
        )


# Deleting a home.


async def test_deleting_a_home(cloud) -> None:
    tokens = await cloud.sign_in("linh@example.com", "Linh")
    home = await cloud.create_home(tokens)
    await cloud.add_profile(tokens, home)
    other = await cloud.create_home(tokens, name="Nhà khác")
    invitation = await cloud.invite(tokens, home)
    response = await cloud.http.post(
        f"/app/v1/homes/{home['id']}:delete",
        json={"id_token": await cloud.id_token()},
        headers=bearer(tokens),
    )
    assert response.status_code == 204, response.text
    problem(await cloud.http.get(f"/app/v1/homes/{home['id']}", headers=bearer(tokens)), 404)
    listed = await cloud.http.get("/app/v1/homes", headers=bearer(tokens))
    assert listed.json()["homes"] == [other]
    # The account stays, and what the home held goes.
    assert (await cloud.account(tokens))["display_name"] == "Linh"
    [[homes]] = await cloud.sql("SELECT count(*) FROM accounts.homes")
    [[members]] = await cloud.sql("SELECT count(*) FROM accounts.members")
    [[invitations]] = await cloud.sql("SELECT count(*) FROM accounts.invitations")
    assert (homes, members, invitations) == (1, 1, 0)
    joining = await cloud.http.post(
        "/app/v1/invitations:lookup", json={"code": invitation["code"]}, headers=bearer(tokens)
    )
    problem(joining, 404, "invitation_invalid")


async def test_a_home_is_deleted_only_by_its_owner(cloud) -> None:
    family = await cloud.family()
    for tokens, email in ((family.admin, "an@example.com"), (family.member, "minh@example.com")):
        response = await cloud.http.post(
            f"{family.path}:delete",
            json={"id_token": await cloud.id_token(email)},
            headers=bearer(tokens),
        )
        problem(response, 403, "role_required")
    assert (await cloud.home(family.owner, family.home))["role"] == "owner"


async def test_a_home_is_deleted_only_by_its_last_person_with_an_account(cloud) -> None:
    family = await cloud.family()
    delete = {"id_token": await cloud.id_token()}
    response = await cloud.http.post(
        f"{family.path}:delete", json=delete, headers=bearer(family.owner)
    )
    problem(response, 409, "owns_home_with_members")
    for person in ("admin", "member"):
        removed = await cloud.http.delete(
            f"{family.path}/members/{family.ids[person]}", headers=bearer(family.owner)
        )
        assert removed.status_code == 204
    # With only a profile left, it goes.
    response = await cloud.http.post(
        f"{family.path}:delete",
        json={"id_token": await cloud.id_token()},
        headers=bearer(family.owner),
    )
    assert response.status_code == 204, response.text


# Deleting an account, with its homes.


async def delete_account(cloud: Cloud, tokens: dict[str, Any], email: str) -> Any:
    return await cloud.http.post(
        "/app/v1/account:delete",
        json={"id_token": await cloud.id_token(email)},
        headers=bearer(tokens),
    )


async def test_an_account_with_a_home_of_its_own_takes_it_along(cloud) -> None:
    tokens = await cloud.sign_in("linh@example.com")
    home = await cloud.create_home(tokens)
    await cloud.add_profile(tokens, home)
    await cloud.invite(tokens, home)
    response = await delete_account(cloud, tokens, "linh@example.com")
    assert response.status_code == 204, response.text
    for table in ("accounts", "homes", "members", "invitations", "idempotency_keys"):
        [[count]] = await cloud.sql(f"SELECT count(*) FROM accounts.{table}")
        assert (table, count) == (table, 0)


async def test_the_owner_of_a_home_with_others_cannot_delete_their_account(cloud) -> None:
    family = await cloud.family()
    own = await cloud.create_home(family.owner, name="Nhà riêng")
    response = await delete_account(cloud, family.owner, "linh@example.com")
    problem(response, 409, "owns_home_with_members")
    # Nothing went, not even the home it could have taken along.
    assert (await cloud.account(family.owner))["display_name"] == "Linh"
    assert (await cloud.home(family.owner, own))["name"] == "Nhà riêng"
    assert len(await cloud.members(family.owner, family.home)) == 4


async def test_a_person_with_an_account_leaves_homes_they_do_not_own(cloud) -> None:
    family = await cloud.family()
    invitation = await cloud.invite(family.admin, family.home)
    response = await delete_account(cloud, family.admin, "an@example.com")
    assert response.status_code == 204, response.text
    names = [each["display_name"] for each in await cloud.members(family.owner, family.home)]
    assert names == ["Linh", "Minh", "Bà"]
    # What that person made went with them.
    problem(
        await cloud.http.post(
            "/app/v1/invitations:lookup",
            json={"code": invitation["code"]},
            headers=bearer(family.outsider),
        ),
        404,
        "invitation_invalid",
    )


async def test_handing_the_home_over_clears_the_way_to_delete_the_account(cloud) -> None:
    family = await cloud.family()
    transferred = await cloud.http.post(
        f"{family.path}:transfer",
        json={"to_member_id": family.ids["admin"], "id_token": await cloud.id_token()},
        headers=bearer(family.owner),
    )
    assert transferred.status_code == 200, transferred.text
    response = await delete_account(cloud, family.owner, "linh@example.com")
    assert response.status_code == 204, response.text
    assert (await cloud.home(family.admin, family.home))["role"] == "owner"
    assert len(await cloud.members(family.admin, family.home)) == 3


async def test_deleting_an_account_races_nothing_into_a_deadlock(make_cloud) -> None:
    cloud = await make_cloud(accounts={"nonce_rate": {"limit": 100}})
    family = await cloud.family()
    other = await cloud.create_home(family.member, name="Nhà Minh")
    invitation = await cloud.invite(family.owner, family.home)
    id_token = await cloud.id_token("minh@example.com")
    answers = await asyncio.gather(
        cloud.http.post(
            "/app/v1/account:delete", json={"id_token": id_token}, headers=bearer(family.member)
        ),
        cloud.add_profile(family.owner, family.home, "Ông"),
        cloud.accept(family.outsider, invitation["code"]),
        cloud.http.patch(
            f"{family.path}/members/{family.ids['member']}",
            json={"display_name": "Anh Minh"},
            headers=bearer(family.owner),
        ),
        cloud.http.post(
            f"/app/v1/homes/{other['id']}/members",
            json={"display_name": "Mẹ"},
            headers=once(family.member),
        ),
        cloud.http.patch(
            f"/app/v1/homes/{other['id']}", json={"name": "x"}, headers=bearer(family.member)
        ),
    )
    assert answers[0].status_code == 204, answers[0].text
    assert answers[2].status_code == 201, answers[2].text
    assert answers[3].status_code in (200, 404)
    assert answers[4].status_code in (201, 401, 404)
    assert answers[5].status_code in (200, 401, 404)
    [[homes]] = await cloud.sql("SELECT count(*) FROM accounts.homes")
    assert homes == 1


# The specification's own example, from beginning to end.


async def test_the_example_runs_as_it_says(make_cloud) -> None:
    cloud = await make_cloud(accounts={"nonce_rate": {"limit": 100}})
    example = json.loads(
        (platform_dir() / "app" / "examples" / "homes.json").read_text(encoding="utf-8")
    )
    people = {
        "linh": ("linh@example.com", "Linh"),
        "minh": ("minh@example.com", "Minh"),
        "bao": ("bao@example.com", "Bảo"),
    }
    tokens = {who: await cloud.sign_in(*person) for who, person in people.items()}
    # Who asks from each note on, as the notes tell it.
    speakers = {
        "The second person opens it": "minh",
        "Someone else": "bao",
        "The owner sees": "linh",
        "The new admin": "minh",
        "The owner tries": "linh",
        "So the owner": "linh",
        "The second person, now": "minh",
        "The old owner leaves": "linh",
        "The new owner, the last": "minh",
    }
    who = "linh"
    # What the cloud makes up, for what the example made up.
    known: dict[str, str] = {}

    def ours(value: Any) -> Any:
        if isinstance(value, str):
            for theirs, mine in known.items():
                value = value.replace(theirs, mine)
        elif isinstance(value, dict):
            value = {key: ours(each) for key, each in value.items()}
        return value

    def learn(expected: Any, answered: Any) -> None:
        """Note what the cloud made up, and compare the rest."""
        if isinstance(expected, dict):
            assert isinstance(answered, dict)
            for key, value in expected.items():
                if key == "detail":
                    continue  # for logs, and not part of the protocol
                learn_field = key in ("id", "member_id", "code")
                if key in ("create_time", "expire_time", "nonce"):
                    continue
                if learn_field:
                    assert isinstance(answered[key], str)
                    known[value] = answered[key]
                else:
                    assert key in answered, (key, answered)
                    learn(value, answered[key])
            assert set(answered) <= set(expected) | {"detail"}, answered
        elif isinstance(expected, list):
            assert len(expected) == len(answered), answered
            for each, other in zip(expected, answered, strict=True):
                learn(each, other)
        else:
            assert expected == answered

    messages = iter(example["messages"])
    for message in messages:
        if "note" in message:
            for start, speaker in speakers.items():
                if start in message["note"]:
                    who = speaker
            continue
        method, target = message["request"].split(" ", 1)
        body = ours(message.get("message"))
        if body is not None and "id_token" in body:
            body["id_token"] = await cloud.id_token(people[who][0], people[who][1])
        headers = {**bearer(tokens[who]), **message.get("headers", {})}
        answer = next(messages)
        response = await cloud.http.request(method, ours(target), json=body, headers=headers)
        assert response.status_code == answer["status"], (target, response.text)
        if "message" in answer:
            learn(answer["message"], response.json())
