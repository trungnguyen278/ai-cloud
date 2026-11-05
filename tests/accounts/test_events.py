# SPDX-License-Identifier: MIT
"""What the accounts tell the other modules: who left a home, which home and which account
went, each in the transaction of its change (docs/decisions/0009-domain-events.md,
decision 8)."""

import uuid
from typing import Any
from uuid import UUID

import pytest
from app_api import Cloud, bearer, problem
from events_module import stream

from ai_cloud.accounts.api import OUTBOX, AccountDeleted, HomeDeleted, MemberRemoved
from ai_cloud.shared.database import create_engine
from ai_cloud.shared.events.relay import Relay
from ai_cloud.shared.events.streams import RedisEventQueue
from ai_cloud.shared.redis import create_redis

pytestmark = pytest.mark.anyio


async def delete_account(cloud: Cloud, tokens: dict[str, Any], email: str) -> Any:
    return await cloud.http.post(
        "/app/v1/account:delete",
        json={"id_token": await cloud.id_token(email)},
        headers=bearer(tokens),
    )


async def test_removing_a_person_tells_who_left_which_home(cloud) -> None:
    family = await cloud.family()
    for person in ("member", "profile"):
        removed = await cloud.http.delete(
            f"{family.path}/members/{family.ids[person]}", headers=bearer(family.owner)
        )
        assert removed.status_code == 204
    member = await cloud.account(family.member)
    assert await cloud.events() == [
        (
            MemberRemoved.TYPE,
            {
                "home_id": family.home["id"],
                "member_id": family.ids["member"],
                "account_id": member["id"],
            },
        ),
        # A profile has no account.
        (
            MemberRemoved.TYPE,
            {"home_id": family.home["id"], "member_id": family.ids["profile"], "account_id": None},
        ),
    ]


async def test_leaving_tells_it_too(cloud) -> None:
    family = await cloud.family()
    left = await cloud.http.delete(
        f"{family.path}/members/{family.ids['admin']}", headers=bearer(family.admin)
    )
    assert left.status_code == 204
    [(kind, data)] = await cloud.events()
    assert (kind, data["member_id"]) == (MemberRemoved.TYPE, family.ids["admin"])


async def test_deleting_a_home_tells_which_home(cloud) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    await cloud.add_profile(tokens, home)
    deleted = await cloud.http.post(
        f"/app/v1/homes/{home['id']}:delete",
        json={"id_token": await cloud.id_token()},
        headers=bearer(tokens),
    )
    assert deleted.status_code == 204, deleted.text
    # No event for each of the people it held: the home's says it.
    assert await cloud.events() == [(HomeDeleted.TYPE, {"home_id": home["id"]})]


async def test_deleting_an_account_tells_of_each_home_then_of_the_account(cloud) -> None:
    family = await cloud.family()
    own = await cloud.create_home(family.member, name="Nhà Minh")
    account = await cloud.account(family.member)
    response = await delete_account(cloud, family.member, "minh@example.com")
    assert response.status_code == 204, response.text
    assert await cloud.events() == [
        (
            MemberRemoved.TYPE,
            {
                "home_id": family.home["id"],
                "member_id": family.ids["member"],
                "account_id": account["id"],
            },
        ),
        (HomeDeleted.TYPE, {"home_id": own["id"]}),
        (AccountDeleted.TYPE, {"account_id": account["id"]}),
    ]


async def test_a_refused_deletion_tells_nothing(cloud) -> None:
    family = await cloud.family()
    own = await cloud.create_home(family.member, name="Nhà Minh")
    await cloud.join(family.outsider, own, family.member)
    response = await delete_account(cloud, family.member, "minh@example.com")
    problem(response, 409, "owns_home_with_members")
    # Leaving the first home had written its event, which went with the rest.
    assert await cloud.events() == []
    assert len(await cloud.members(family.owner, family.home)) == 4


@pytest.mark.parametrize(
    "event",
    [
        MemberRemoved(uuid.uuid7(), uuid.uuid7(), uuid.uuid7()),
        MemberRemoved(uuid.uuid7(), uuid.uuid7(), None),
        HomeDeleted(uuid.uuid7()),
        AccountDeleted(uuid.uuid7()),
    ],
)
def test_a_consumer_reads_an_event_as_it_was_written(event: Any) -> None:
    assert type(event).from_data(event.data()) == event


async def test_the_events_reach_the_accounts_stream(cloud) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    deleted = await cloud.http.post(
        f"/app/v1/homes/{home['id']}:delete",
        json={"id_token": await cloud.id_token()},
        headers=bearer(tokens),
    )
    assert deleted.status_code == 204, deleted.text
    engine = create_engine(cloud.database, cloud.settings.database, "ai-cloud tests")
    redis = create_redis(cloud.redis_url, cloud.settings.redis)
    try:
        relay = Relay(engine, RedisEventQueue(redis), [OUTBOX], cloud.settings.events)
        while await relay.relay():
            pass
        [(_, event)] = await stream(redis, OUTBOX.stream)
    finally:
        await redis.aclose()
        await engine.dispose()
    assert (event.source, event.type, event.subject) == ("/accounts", HomeDeleted.TYPE, home["id"])
    assert HomeDeleted.from_data(event.data) == HomeDeleted(UUID(home["id"]))
