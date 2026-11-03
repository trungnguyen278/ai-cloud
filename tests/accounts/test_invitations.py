# SPDX-License-Identifier: MIT
"""Invitations: making them, seeing and revoking them, and joining with their codes (the app
API, Invitations)."""

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from app_api import bearer, conforms, problem

pytestmark = pytest.mark.anyio


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text)


async def look_up(cloud, tokens, code: str):
    return await cloud.http.post(
        "/app/v1/invitations:lookup", json={"code": code}, headers=bearer(tokens)
    )


async def expire(cloud) -> None:
    await cloud.sql("UPDATE accounts.invitations SET expire_time = now()")


async def test_making_an_invitation(cloud) -> None:
    family = await cloud.family()
    invitation = await cloud.invite(family.admin, family.home)
    assert invitation["invited_by"] == "An"
    lifetime = timedelta(seconds=cloud.settings.accounts.invitation_lifetime)
    assert instant(invitation["expire_time"]) - instant(invitation["create_time"]) == lifetime
    assert len(invitation["code"]) == 43


async def test_only_its_hash_is_kept(cloud) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    code = (await cloud.invite(tokens, home))["code"]
    [row] = await cloud.sql("SELECT * FROM accounts.invitations")
    assert code not in repr(tuple(row))
    assert code.encode() not in bytes(row.code_hash)
    assert len(row.code_hash) == 32


async def test_a_code_stays_out_of_the_logs(cloud, caplog: pytest.LogCaptureFixture) -> None:
    family = await cloud.family()
    caplog.clear()
    code = (await cloud.invite(family.owner, family.home))["code"]
    await look_up(cloud, family.outsider, code)
    await cloud.accept(family.outsider, code)
    await cloud.accept(family.member, code)
    await expire(cloud)
    await cloud.accept(family.outsider, code)
    assert caplog.records
    for record in caplog.records:
        assert code not in repr(record.__dict__)


async def test_seeing_the_open_ones_without_their_codes(cloud) -> None:
    family = await cloud.family()
    first = await cloud.invite(family.owner, family.home)
    second = await cloud.invite(family.admin, family.home)
    used = await cloud.invite(family.owner, family.home)
    assert (await cloud.accept(family.outsider, used["code"])).status_code == 201
    listed, token = [], ""
    for _ in range(2):
        response = await cloud.http.get(
            f"{family.path}/invitations",
            params={"page_size": 1, "page_token": token},
            headers=bearer(family.admin),
        )
        page = conforms(response, "invitation_list")
        assert response.headers["cache-control"] == "no-store"
        listed += page["invitations"]
        token = page["next_page_token"]
    assert token == ""
    assert [each["id"] for each in listed] == [first["id"], second["id"]]
    assert [each["invited_by"] for each in listed] == ["Linh", "An"]
    assert all("code" not in each for each in listed)


async def test_an_expired_invitation_is_not_open(cloud) -> None:
    family = await cloud.family()
    invitation = await cloud.invite(family.owner, family.home)
    await expire(cloud)
    response = await cloud.http.get(f"{family.path}/invitations", headers=bearer(family.owner))
    assert response.json()["invitations"] == []
    revoked = await cloud.http.delete(
        f"{family.path}/invitations/{invitation['id']}", headers=bearer(family.owner)
    )
    problem(revoked, 404)


async def test_invitations_are_for_the_owner_and_admins(cloud) -> None:
    family = await cloud.family()
    invitation = await cloud.invite(family.owner, family.home)
    path = f"{family.path}/invitations"
    headers = bearer(family.member)
    for answer in (
        await cloud.http.post(path, headers=headers),
        await cloud.http.get(path, headers=headers),
        await cloud.http.delete(f"{path}/{invitation['id']}", headers=headers),
    ):
        problem(answer, 403, "role_required")


async def test_revoking_one(cloud) -> None:
    family = await cloud.family()
    invitation = await cloud.invite(family.owner, family.home)
    path = f"{family.path}/invitations/{invitation['id']}"
    # An admin may revoke the owner's.
    assert (await cloud.http.delete(path, headers=bearer(family.admin))).status_code == 204
    problem(await look_up(cloud, family.outsider, invitation["code"]), 404, "invitation_invalid")
    problem(await cloud.accept(family.outsider, invitation["code"]), 404, "invitation_invalid")
    problem(await cloud.http.delete(path, headers=bearer(family.admin)), 404)


async def test_a_used_invitation_cannot_be_revoked(cloud) -> None:
    family = await cloud.family()
    invitation = await cloud.invite(family.owner, family.home)
    assert (await cloud.accept(family.outsider, invitation["code"])).status_code == 201
    response = await cloud.http.delete(
        f"{family.path}/invitations/{invitation['id']}", headers=bearer(family.owner)
    )
    problem(response, 404)


async def test_an_invitation_of_another_home_is_not_this_home_s(cloud) -> None:
    family = await cloud.family()
    theirs = await cloud.create_home(family.outsider)
    invitation = await cloud.invite(family.outsider, theirs)
    response = await cloud.http.delete(
        f"{family.path}/invitations/{invitation['id']}", headers=bearer(family.owner)
    )
    problem(response, 404)
    assert (await cloud.accept(family.member, invitation["code"])).status_code == 201


async def test_a_home_has_so_many_open_invitations(make_cloud) -> None:
    cloud = await make_cloud(accounts={"invitations_per_home": 2, "nonce_rate": {"limit": 100}})
    family = await cloud.family()
    await cloud.sql("DELETE FROM accounts.invitations")
    first = await cloud.invite(family.owner, family.home)
    await cloud.invite(family.owner, family.home)
    full = await cloud.http.post(f"{family.path}/invitations", headers=bearer(family.owner))
    problem(full, 409, "invitation_limit_reached")
    # A used one makes room.
    assert (await cloud.accept(family.outsider, first["code"])).status_code == 201
    await cloud.invite(family.owner, family.home)
    problem(
        await cloud.http.post(f"{family.path}/invitations", headers=bearer(family.owner)),
        409,
        "invitation_limit_reached",
    )
    # And so do expired ones, which then go for good.
    await expire(cloud)
    await cloud.invite(family.owner, family.home)
    [[kept]] = await cloud.sql("SELECT count(*) FROM accounts.invitations")
    assert kept == 1


async def test_looking_before_agreeing(cloud) -> None:
    family = await cloud.family()
    invitation = await cloud.invite(family.admin, family.home)
    response = await look_up(cloud, family.outsider, invitation["code"])
    assert response.headers["cache-control"] == "no-store"
    assert conforms(response, "invitation_preview") == {
        "home_name": family.home["name"],
        "invited_by": "An",
        "expire_time": invitation["expire_time"],
    }
    # Looking uses nothing up.
    assert (await cloud.accept(family.outsider, invitation["code"])).status_code == 201


async def test_the_name_shown_is_the_home_s_for_that_person(cloud) -> None:
    family = await cloud.family()
    await cloud.http.patch(
        f"{family.path}/members/{family.ids['admin']}",
        json={"display_name": "Chị An"},
        headers=bearer(family.owner),
    )
    invitation = await cloud.invite(family.admin, family.home)
    assert invitation["invited_by"] == "Chị An"
    preview = await look_up(cloud, family.outsider, invitation["code"])
    assert preview.json()["invited_by"] == "Chị An"


async def test_joining(cloud) -> None:
    family = await cloud.family()
    invitation = await cloud.invite(family.owner, family.home)
    response = await cloud.accept(family.outsider, invitation["code"])
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    home = conforms(response, "home")
    assert home["id"] == family.home["id"]
    assert home["role"] == "member"
    assert home["etag"] == response.headers["etag"]
    assert await cloud.home(family.outsider, family.home) == home
    people = await cloud.members(family.outsider, family.home)
    [person] = [each for each in people if each["current"]]
    assert person["id"] == home["member_id"]
    assert (person["kind"], person["display_name"], person["role"]) == ("account", "Bảo", "member")


async def test_a_retry_after_a_lost_answer_ends_in_the_same_place(cloud) -> None:
    family = await cloud.family()
    code = (await cloud.invite(family.owner, family.home))["code"]
    first = await cloud.accept(family.outsider, code)
    assert first.status_code == 201
    again = await cloud.accept(family.outsider, code)
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert (await look_up(cloud, family.outsider, code)).status_code == 200
    assert len(await cloud.members(family.owner, family.home)) == 5


async def test_a_person_already_in_the_home_leaves_the_code_unused(cloud) -> None:
    family = await cloud.family()
    code = (await cloud.invite(family.owner, family.home))["code"]
    for tokens in (family.member, family.owner):
        response = await cloud.accept(tokens, code)
        assert response.status_code == 200, response.text
        assert response.json()["member_id"] != ""
    # Nobody has changed role, and the code still brings in someone new.
    assert (await cloud.home(family.owner, family.home))["role"] == "owner"
    assert (await cloud.accept(family.outsider, code)).status_code == 201


async def test_a_code_works_once(cloud) -> None:
    family = await cloud.family()
    third = await cloud.sign_in("third@example.com", "Ba")
    code = (await cloud.invite(family.owner, family.home))["code"]
    assert (await cloud.accept(family.outsider, code)).status_code == 201
    problem(await cloud.accept(third, code), 404, "invitation_invalid")
    problem(await look_up(cloud, third, code), 404, "invitation_invalid")
    # Not even for someone who is in the home, other than the person it brought in.
    problem(await cloud.accept(family.member, code), 404, "invitation_invalid")


async def test_two_people_with_one_code(cloud) -> None:
    family = await cloud.family()
    third = await cloud.sign_in("third@example.com", "Ba")
    code = (await cloud.invite(family.owner, family.home))["code"]
    answers = await asyncio.gather(cloud.accept(family.outsider, code), cloud.accept(third, code))
    assert sorted(answer.status_code for answer in answers) == [201, 404]
    assert len(await cloud.members(family.owner, family.home)) == 5


async def test_one_person_twice_at_once(cloud) -> None:
    family = await cloud.family()
    code = (await cloud.invite(family.owner, family.home))["code"]
    answers = await asyncio.gather(
        cloud.accept(family.outsider, code), cloud.accept(family.outsider, code)
    )
    assert sorted(answer.status_code for answer in answers) == [200, 201]
    assert len(await cloud.members(family.owner, family.home)) == 5


async def test_a_code_does_not_bring_back_someone_who_left(cloud) -> None:
    family = await cloud.family()
    code = (await cloud.invite(family.owner, family.home))["code"]
    joined = await cloud.accept(family.outsider, code)
    left = await cloud.http.delete(
        f"{family.path}/members/{joined.json()['member_id']}", headers=bearer(family.outsider)
    )
    assert left.status_code == 204
    problem(await cloud.accept(family.outsider, code), 404, "invitation_invalid")
    problem(await look_up(cloud, family.outsider, code), 404, "invitation_invalid")


async def test_a_code_does_not_bring_back_someone_removed(cloud) -> None:
    family = await cloud.family()
    code = (await cloud.invite(family.owner, family.home))["code"]
    joined = await cloud.accept(family.outsider, code)
    removed = await cloud.http.delete(
        f"{family.path}/members/{joined.json()['member_id']}", headers=bearer(family.owner)
    )
    assert removed.status_code == 204
    problem(await cloud.accept(family.outsider, code), 404, "invitation_invalid")


@pytest.mark.parametrize("person", ["outsider", "member"])
async def test_an_expired_code_fails_for_everyone(cloud, person: str) -> None:
    family = await cloud.family()
    code = (await cloud.invite(family.owner, family.home))["code"]
    await expire(cloud)
    tokens = getattr(family, person)
    problem(await look_up(cloud, tokens, code), 404, "invitation_invalid")
    problem(await cloud.accept(tokens, code), 404, "invitation_invalid")


async def test_every_failure_looks_the_same(cloud) -> None:
    family = await cloud.family()
    unknown = "A" * 43
    revoked = await cloud.invite(family.owner, family.home)
    await cloud.http.delete(
        f"{family.path}/invitations/{revoked['id']}", headers=bearer(family.owner)
    )
    used = await cloud.invite(family.owner, family.home)
    assert (await cloud.accept(family.member, used["code"])).status_code == 200
    third = await cloud.sign_in("third@example.com", "Ba")
    assert (await cloud.accept(third, used["code"])).status_code == 201
    expired = await cloud.invite(family.owner, family.home)
    await cloud.sql(
        "UPDATE accounts.invitations SET expire_time = now() WHERE id = :id",
        id=uuid.UUID(expired["id"]),
    )
    bodies = [
        problem(await cloud.accept(family.outsider, code), 404, "invitation_invalid")
        for code in (unknown, revoked["code"], used["code"], expired["code"])
    ]
    assert all(body == bodies[0] for body in bodies)


@pytest.mark.parametrize(
    "code", ["", "short", "!" * 43, "A" * 44, "A" * 43 + "\u0000", "A" * 43 + "\n"]
)
async def test_a_code_is_checked(cloud, code: str) -> None:
    tokens = await cloud.sign_in()
    problem(await look_up(cloud, tokens, code), 400)
    problem(await cloud.accept(tokens, code), 400)


async def test_joining_needs_room_in_the_account(make_cloud) -> None:
    cloud = await make_cloud(accounts={"homes_per_account": 1})
    family = await cloud.family()
    await cloud.create_home(family.outsider)
    code = (await cloud.invite(family.owner, family.home))["code"]
    problem(await cloud.accept(family.outsider, code), 409, "home_limit_reached")
    # The code stays good for someone with room.
    third = await cloud.sign_in("third@example.com", "Ba")
    assert (await cloud.accept(third, code)).status_code == 201


async def test_joining_needs_room_in_the_home(make_cloud) -> None:
    cloud = await make_cloud(accounts={"members_per_home": 4})
    family = await cloud.family()
    code = (await cloud.invite(family.owner, family.home))["code"]
    problem(await cloud.accept(family.outsider, code), 409, "member_limit_reached")
    # Someone already in it needs no room.
    assert (await cloud.accept(family.member, code)).status_code == 200


async def test_an_account_tries_so_many_codes(make_cloud) -> None:
    cloud = await make_cloud(accounts={"invitation_rate": {"limit": 2, "period": 60}})
    tokens = await cloud.sign_in()
    other = await cloud.sign_in("an@example.com")
    code = "A" * 43
    problem(await look_up(cloud, tokens, code), 404, "invitation_invalid")
    problem(await cloud.accept(tokens, code), 404, "invitation_invalid")
    over = await cloud.accept(tokens, code)
    problem(over, 429)
    assert int(over.headers["retry-after"]) >= 1
    problem(await look_up(cloud, tokens, code), 429)
    # Each account has its own count.
    problem(await cloud.accept(other, code), 404, "invitation_invalid")


async def test_an_invitation_needs_an_account(cloud) -> None:
    for path in ("/app/v1/invitations:lookup", "/app/v1/invitations:accept"):
        problem(await cloud.http.post(path, json={"code": "A" * 43}), 401)
