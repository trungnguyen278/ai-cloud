# SPDX-License-Identifier: MIT
"""The people in a home: profiles, names, roles, and removing them (the app API, The people
in a home)."""

import asyncio
import uuid

import pytest
from app_api import bearer, conforms, once, problem
from sqlalchemy import text

from ai_cloud.shared.database import create_engine

pytestmark = pytest.mark.anyio


async def test_a_profile_has_a_name_and_no_role(cloud) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    response = await cloud.http.post(
        f"/app/v1/homes/{home['id']}/members", json={"display_name": "Bà"}, headers=once(tokens)
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    profile = conforms(response, "member")
    assert profile["kind"] == "profile"
    assert profile["display_name"] == "Bà"
    assert profile["current"] is False
    assert "role" not in profile
    assert profile["etag"] == response.headers["etag"] == 'W/"1"'
    assert (await cloud.members(tokens, home))[1] == profile


@pytest.mark.parametrize(
    "body", [{}, {"display_name": ""}, {"display_name": "x" * 101}, {"display_name": "Bà\n"}]
)
async def test_a_profile_is_checked(cloud, body: dict[str, str]) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    response = await cloud.http.post(
        f"/app/v1/homes/{home['id']}/members", json=body, headers=once(tokens)
    )
    problem(response, 400)
    assert len(await cloud.members(tokens, home)) == 1


async def test_adding_a_profile_is_for_the_owner_and_admins(cloud) -> None:
    family = await cloud.family()
    assert (await cloud.add_profile(family.admin, family.home, "Ông"))["kind"] == "profile"
    response = await cloud.http.post(
        f"{family.path}/members", json={"display_name": "x"}, headers=once(family.member)
    )
    problem(response, 403, "role_required")


async def test_adding_a_profile_needs_a_key_and_a_retry_adds_none(cloud) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    path = f"/app/v1/homes/{home['id']}/members"
    body = {"display_name": "Bà"}
    problem(await cloud.http.post(path, json=body, headers=bearer(tokens)), 400)
    headers = once(tokens)
    first = await cloud.http.post(path, json=body, headers=headers)
    assert first.status_code == 201
    renamed = await cloud.http.patch(
        f"{path}/{first.json()['id']}", json={"display_name": "Ngoại"}, headers=bearer(tokens)
    )
    assert renamed.status_code == 200
    retry = await cloud.http.post(path, json=body, headers=headers)
    assert (retry.status_code, retry.content) == (201, first.content)
    assert [each["display_name"] for each in await cloud.members(tokens, home)] == [
        "Linh",
        "Ngoại",
    ]
    problem(await cloud.http.post(path, json={"display_name": "Ông"}, headers=headers), 422)


async def test_a_retry_from_someone_no_longer_in_the_home_answers_as_any_request(cloud) -> None:
    family = await cloud.family()
    headers = once(family.admin)
    path = f"{family.path}/members"
    made = await cloud.http.post(path, json={"display_name": "Ông"}, headers=headers)
    assert made.status_code == 201
    removed = await cloud.http.delete(f"{path}/{family.ids['admin']}", headers=bearer(family.owner))
    assert removed.status_code == 204
    problem(await cloud.http.post(path, json={"display_name": "Ông"}, headers=headers), 404)


async def test_a_removed_profile_takes_the_first_answer_along(cloud) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    profile = await cloud.add_profile(tokens, home)
    [[before]] = await cloud.sql("SELECT count(*) FROM accounts.idempotency_keys")
    response = await cloud.http.delete(
        f"/app/v1/homes/{home['id']}/members/{profile['id']}", headers=bearer(tokens)
    )
    assert response.status_code == 204
    [[after]] = await cloud.sql("SELECT count(*) FROM accounts.idempotency_keys")
    assert (before, after) == (2, 1)


async def test_an_old_first_answer_does_not_deadlock_with_removing_its_person(cloud) -> None:
    family = await cloud.family()
    path = f"{family.path}/members"
    made = await cloud.http.post(path, json={"display_name": "Ông"}, headers=once(family.admin))
    profile = uuid.UUID(made.json()["id"])
    await cloud.sql(
        "UPDATE accounts.idempotency_keys SET create_time = now() - interval '2 days' "
        "WHERE member_id = :id",
        id=profile,
    )
    engine = create_engine(cloud.database, cloud.settings.database, "ai-cloud tests")
    try:
        async with engine.begin() as remover:
            # Removing a person holds the home first.
            await remover.execute(
                text("SELECT 1 FROM accounts.homes WHERE id = :id FOR UPDATE"),
                {"id": uuid.UUID(family.home["id"])},
            )
            adding = asyncio.create_task(
                cloud.http.post(path, json={"display_name": "Mẹ"}, headers=once(family.admin))
            )
            async with asyncio.timeout(20):
                while not await _waits_for_a_home(engine):
                    await asyncio.sleep(0.05)
            # The cascade reaches the old answer, which the waiting request must not hold.
            await remover.execute(
                text("DELETE FROM accounts.members WHERE id = :id"), {"id": profile}
            )
        assert (await adding).status_code == 201
    finally:
        await engine.dispose()


async def _waits_for_a_home(engine) -> bool:
    async with engine.connect() as watcher:
        found = await watcher.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND query LIKE '%FOR UPDATE%' AND query LIKE '%accounts.homes%'"
            )
        )
        return found.scalar_one() >= 1


async def test_a_home_has_so_many_people(make_cloud) -> None:
    cloud = await make_cloud(accounts={"members_per_home": 2})
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    await cloud.add_profile(tokens, home)
    response = await cloud.http.post(
        f"/app/v1/homes/{home['id']}/members", json={"display_name": "x"}, headers=once(tokens)
    )
    problem(response, 409, "member_limit_reached")


async def test_listing_people_in_pages(cloud) -> None:
    family = await cloud.family()
    listed, token = [], ""
    for _ in range(4):
        response = await cloud.http.get(
            f"{family.path}/members",
            params={"page_size": 1, "page_token": token},
            headers=bearer(family.member),
        )
        page = conforms(response, "member_list")
        listed += page["members"]
        token = page["next_page_token"]
    assert token == ""
    assert [each["display_name"] for each in listed] == ["Linh", "An", "Minh", "Bà"]
    assert [each["current"] for each in listed] == [False, False, True, False]
    assert [each["kind"] for each in listed] == ["account", "account", "account", "profile"]
    # Nothing else about a person is shown.
    for person in listed:
        assert set(person) <= {
            "id",
            "kind",
            "display_name",
            "role",
            "current",
            "create_time",
            "etag",
        }


async def test_anyone_in_the_home_sees_who_is_in_it(cloud) -> None:
    family = await cloud.family()
    for tokens in (family.owner, family.admin, family.member):
        assert len(await cloud.members(tokens, family.home)) == 4


@pytest.mark.parametrize(
    ("actor", "target", "allowed"),
    [
        ("owner", "owner", True),
        ("owner", "admin", True),
        ("owner", "member", True),
        ("owner", "profile", True),
        ("admin", "owner", False),
        ("admin", "admin", True),
        ("admin", "member", True),
        ("admin", "profile", True),
        ("member", "owner", False),
        ("member", "admin", False),
        ("member", "member", True),
        ("member", "profile", False),
    ],
)
async def test_who_may_rename_whom(cloud, actor: str, target: str, allowed: bool) -> None:
    family = await cloud.family()
    response = await cloud.http.patch(
        f"{family.path}/members/{family.ids[target]}",
        json={"display_name": "Tên mới"},
        headers=bearer(getattr(family, actor)),
    )
    if allowed:
        member = conforms(response, "member")
        assert member["display_name"] == "Tên mới"
        assert member["current"] is (actor == target)
        assert member["etag"] == response.headers["etag"]
    else:
        problem(response, 403, "role_required")


async def test_a_person_s_name_is_the_home_s(cloud) -> None:
    family = await cloud.family()
    second = await cloud.create_home(family.member, name="Nhà Minh")
    await cloud.join(family.owner, second, family.member)
    response = await cloud.http.patch(
        f"{family.path}/members/{family.ids['owner']}",
        json={"display_name": "Mẹ"},
        headers=bearer(family.owner),
    )
    assert response.status_code == 200
    # Their other home, and their account, stay as they were.
    names = [each["display_name"] for each in await cloud.members(family.member, second)]
    assert names == ["Minh", "Linh"]
    assert (await cloud.account(family.owner))["display_name"] == "Linh"
    # And it does not follow the account afterwards.
    await cloud.http.patch(
        "/app/v1/account", json={"display_name": "Linh Nguyễn"}, headers=bearer(family.member)
    )
    names = [each["display_name"] for each in await cloud.members(family.member, second)]
    assert names == ["Minh", "Linh"]


async def test_if_match_changes_only_the_person_who_was_read(cloud) -> None:
    family = await cloud.family()
    path = f"{family.path}/members/{family.ids['member']}"
    [read] = [
        each
        for each in await cloud.members(family.owner, family.home)
        if each["id"] == family.ids["member"]
    ]
    headers = {**bearer(family.owner), "If-Match": read["etag"]}
    first = await cloud.http.patch(path, json={"display_name": "A"}, headers=headers)
    assert first.status_code == 200
    assert first.json()["etag"] == 'W/"2"'
    problem(await cloud.http.patch(path, json={"display_name": "B"}, headers=headers), 412)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"display_name": ""},
        {"display_name": "x\n"},
        {"role": "owner"},
        {"role": "root"},
        {"role": None},
    ],
)
async def test_a_change_to_a_person_is_checked(cloud, body: dict) -> None:
    family = await cloud.family()
    response = await cloud.http.patch(
        f"{family.path}/members/{family.ids['member']}", json=body, headers=bearer(family.owner)
    )
    problem(response, 400)


async def test_the_owner_changes_roles(cloud) -> None:
    family = await cloud.family()
    path = f"{family.path}/members/{family.ids['member']}"
    raised = await cloud.http.patch(path, json={"role": "admin"}, headers=bearer(family.owner))
    assert conforms(raised, "member")["role"] == "admin"
    assert (await cloud.home(family.member, family.home))["role"] == "admin"
    lowered = await cloud.http.patch(path, json={"role": "member"}, headers=bearer(family.owner))
    assert conforms(lowered, "member")["role"] == "member"
    assert lowered.json()["etag"] == 'W/"3"'


@pytest.mark.parametrize("actor", ["admin", "member"])
@pytest.mark.parametrize("target", ["owner", "admin", "member", "profile"])
async def test_only_the_owner_changes_roles(cloud, actor: str, target: str) -> None:
    family = await cloud.family()
    response = await cloud.http.patch(
        f"{family.path}/members/{family.ids[target]}",
        json={"role": "admin"},
        headers=bearer(getattr(family, actor)),
    )
    problem(response, 403, "role_required")


@pytest.mark.parametrize("target", ["owner", "profile"])
async def test_a_role_goes_on_another_person_with_an_account(cloud, target: str) -> None:
    family = await cloud.family()
    response = await cloud.http.patch(
        f"{family.path}/members/{family.ids[target]}",
        json={"role": "member"},
        headers=bearer(family.owner),
    )
    problem(response, 409)
    assert (await cloud.home(family.owner, family.home))["role"] == "owner"


async def test_a_change_is_whole_or_not_at_all(cloud) -> None:
    family = await cloud.family()
    response = await cloud.http.patch(
        f"{family.path}/members/{family.ids['member']}",
        json={"display_name": "Tên", "role": "admin"},
        headers=bearer(family.admin),
    )
    problem(response, 403, "role_required")
    names = [each["display_name"] for each in await cloud.members(family.owner, family.home)]
    assert "Tên" not in names


async def test_demoting_an_admin_revokes_the_invitations_they_made(cloud) -> None:
    family = await cloud.family()
    theirs = await cloud.invite(family.admin, family.home)
    owners = await cloud.invite(family.owner, family.home)
    response = await cloud.http.patch(
        f"{family.path}/members/{family.ids['admin']}",
        json={"role": "member"},
        headers=bearer(family.owner),
    )
    assert response.status_code == 200
    listed = await cloud.http.get(f"{family.path}/invitations", headers=bearer(family.owner))
    assert [each["id"] for each in listed.json()["invitations"]] == [owners["id"]]
    problem(await cloud.accept(family.outsider, theirs["code"]), 404, "invitation_invalid")


async def test_keeping_an_admin_an_admin_revokes_nothing(cloud) -> None:
    family = await cloud.family()
    theirs = await cloud.invite(family.admin, family.home)
    response = await cloud.http.patch(
        f"{family.path}/members/{family.ids['admin']}",
        json={"role": "admin"},
        headers=bearer(family.owner),
    )
    assert response.status_code == 200
    assert (await cloud.accept(family.outsider, theirs["code"])).status_code == 201


@pytest.mark.parametrize(
    ("actor", "target", "allowed"),
    [
        ("owner", "admin", True),
        ("owner", "member", True),
        ("owner", "profile", True),
        ("admin", "owner", False),
        ("admin", "member", True),
        ("admin", "profile", True),
        ("member", "owner", False),
        ("member", "admin", False),
        ("member", "profile", False),
    ],
)
async def test_who_may_remove_whom(cloud, actor: str, target: str, allowed: bool) -> None:
    family = await cloud.family()
    response = await cloud.http.delete(
        f"{family.path}/members/{family.ids[target]}", headers=bearer(getattr(family, actor))
    )
    if allowed:
        assert response.status_code == 204, response.text
        remaining = {each["id"] for each in await cloud.members(family.owner, family.home)}
        assert family.ids[target] not in remaining
    else:
        problem(response, 403, "role_required")
        assert len(await cloud.members(family.owner, family.home)) == 4


async def test_an_admin_does_not_remove_another_admin(cloud) -> None:
    family = await cloud.family()
    raised = await cloud.http.patch(
        f"{family.path}/members/{family.ids['member']}",
        json={"role": "admin"},
        headers=bearer(family.owner),
    )
    assert raised.status_code == 200
    response = await cloud.http.delete(
        f"{family.path}/members/{family.ids['member']}", headers=bearer(family.admin)
    )
    problem(response, 403, "role_required")


@pytest.mark.parametrize("person", ["admin", "member"])
async def test_leaving(cloud, person: str) -> None:
    family = await cloud.family()
    tokens = getattr(family, person)
    response = await cloud.http.delete(
        f"{family.path}/members/{family.ids[person]}", headers=bearer(tokens)
    )
    assert response.status_code == 204, response.text
    problem(await cloud.http.get(family.path, headers=bearer(tokens)), 404)
    listed = await cloud.http.get("/app/v1/homes", headers=bearer(tokens))
    assert listed.json()["homes"] == []
    # Their account stays.
    assert (await cloud.account(tokens))["etag"] == 'W/"1"'


async def test_the_owner_cannot_leave(cloud) -> None:
    family = await cloud.family()
    response = await cloud.http.delete(
        f"{family.path}/members/{family.ids['owner']}", headers=bearer(family.owner)
    )
    problem(response, 409, "owner_cannot_leave")
    assert len(await cloud.members(family.owner, family.home)) == 4


async def test_removing_a_person_revokes_the_invitations_they_made(cloud) -> None:
    family = await cloud.family()
    theirs = await cloud.invite(family.admin, family.home)
    response = await cloud.http.delete(
        f"{family.path}/members/{family.ids['admin']}", headers=bearer(family.owner)
    )
    assert response.status_code == 204
    problem(await cloud.accept(family.outsider, theirs["code"]), 404, "invitation_invalid")


async def test_an_identifier_the_home_does_not_hold_answers_404(cloud) -> None:
    family = await cloud.family()
    theirs = await cloud.create_home(family.outsider)
    for target in (theirs["member_id"], str(uuid.uuid7()), "not-an-id"):
        path = f"{family.path}/members/{target}"
        renamed = await cloud.http.patch(
            path, json={"display_name": "x"}, headers=bearer(family.owner)
        )
        problem(renamed, 404)
        problem(await cloud.http.delete(path, headers=bearer(family.owner)), 404)
    [person] = await cloud.members(family.outsider, theirs)
    assert person["display_name"] == "Bảo"
