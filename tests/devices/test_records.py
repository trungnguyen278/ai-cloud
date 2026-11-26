# SPDX-License-Identifier: MIT
"""The sessions' records, in Redis and in the tests' memory alike: one session a device, taken
by one connection at a time, written only by the connection that holds it (the cloud link,
Resuming a session; docs/decisions/0013-resuming-sessions.md)."""

import dataclasses
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
import redis.asyncio
from records import Records

from ai_cloud.devices.adapters.redis import SESSION_PREFIX, RedisSessions
from ai_cloud.devices.domain.link import Claim, Handing, Keeping, Record

pytestmark = pytest.mark.anyio

REGISTERED = datetime(2026, 11, 11, tzinfo=UTC)
TERMS = '{"protocol":"3.7"}'
LEASE, WINDOW = 60, 120


@pytest.fixture(params=["memory", "redis"])
async def records(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    if request.param == "memory":
        yield Records()
        return
    client = redis.asyncio.Redis.from_url(request.getfixturevalue("redis_url"))
    try:
        yield RedisSessions(client, LEASE, WINDOW)
    finally:
        await client.aclose()


def new(minor: int = 7) -> Record:
    return Record(uuid.uuid7(), uuid.uuid7(), REGISTERED, TERMS, minor)


def written(record: Record) -> Record:
    """``record`` as its holder writes it as it gives it up."""
    return dataclasses.replace(
        record, received=4, sent=3, kept=('{"type":"error","seq":2}', '{"type":"error","seq":3}')
    )


async def claim(records: Any, record: Record, connection: uuid.UUID, **changes: Any) -> Any:
    asked = {
        "session": record.session,
        "registered": record.registered,
        "terms": record.terms,
        "minor": 7,
    } | changes
    return await records.claim(record.device_id, connection, **asked)


async def take(records: Any, record: Record, connection: uuid.UUID, *, last: bool) -> Any:
    return await records.take(
        record.device_id, connection, record.session, record.registered, record.terms, last=last
    )


async def test_a_session_given_up_is_taken_at_once_with_what_was_kept(records: Any) -> None:
    record, older, newer = new(), uuid.uuid7(), uuid.uuid7()
    await records.start(record, older)
    assert await records.keep([(record.device_id, older, record.session)]) == {older: Keeping.HELD}
    assert await records.hand(written(record), older) is Handing.PARKED
    # The registration as another zone writes the same instant.
    elsewhere = REGISTERED.astimezone(timezone(timedelta(hours=7)))
    assert await claim(records, record, newer, registered=elsewhere) == written(record)
    # Once taken, the older connection no longer holds it, nor writes it.
    assert await records.keep([(record.device_id, older, record.session)]) == {older: Keeping.TAKEN}
    assert await records.hand(record, older) is Handing.LOST
    await records.end(record.device_id, older, record.session)
    assert await records.keep([(record.device_id, newer, record.session)]) == {newer: Keeping.HELD}


async def test_a_session_held_is_asked_of_its_connection_then_handed_over(records: Any) -> None:
    record, older, newer = new(), uuid.uuid7(), uuid.uuid7()
    await records.start(record, older)
    assert await claim(records, record, newer) is Claim.WAITING
    assert await take(records, record, newer, last=False) is Claim.WAITING
    assert await records.keep([(record.device_id, older, record.session)]) == {older: Keeping.TAKEN}
    assert await records.hand(written(record), older) is Handing.HANDED
    assert await take(records, record, newer, last=False) == written(record)
    assert await records.keep([(record.device_id, newer, record.session)]) == {newer: Keeping.HELD}


async def test_a_newer_claim_takes_the_place_of_an_older(records: Any) -> None:
    record, holder, first, second = new(), uuid.uuid7(), uuid.uuid7(), uuid.uuid7()
    await records.start(record, holder)
    assert await claim(records, record, first) is Claim.WAITING
    assert await claim(records, record, second) is Claim.WAITING
    assert await take(records, record, first, last=False) is Claim.SUPERSEDED
    assert await records.hand(record, holder) is Handing.HANDED
    assert await take(records, record, second, last=False) == record
    assert await take(records, record, first, last=True) is Claim.SUPERSEDED


async def test_a_claim_not_handed_over_in_time_is_withdrawn(records: Any) -> None:
    record, older, newer = new(), uuid.uuid7(), uuid.uuid7()
    await records.start(record, older)
    assert await claim(records, record, newer) is Claim.WAITING
    assert await take(records, record, newer, last=True) is Claim.WAITING
    # Then the older connection gives it up for its window, and holds it until then.
    assert await records.keep([(record.device_id, older, record.session)]) == {older: Keeping.HELD}
    assert await records.hand(record, older) is Handing.PARKED
    assert await take(records, record, newer, last=False) is Claim.SUPERSEDED


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"session": uuid.uuid7()}, Claim.GONE),
        ({"registered": REGISTERED + timedelta(seconds=1)}, Claim.REGISTRATION),
        ({"terms": '{"protocol":"3.8"}'}, Claim.TERMS),
        ({"minor": 6}, Claim.VERSION),
        # The first that fails says why.
        ({"registered": REGISTERED + timedelta(seconds=1), "terms": "{}"}, Claim.REGISTRATION),
        ({"terms": "{}", "minor": 6}, Claim.TERMS),
    ],
)
async def test_a_claim_takes_only_the_session_it_names_on_its_terms(
    records: Any, changes: dict[str, Any], why: Claim
) -> None:
    record, older, newer = new(), uuid.uuid7(), uuid.uuid7()
    await records.start(record, older)
    assert await claim(records, record, newer, **changes) is why
    # It changed nothing.
    assert await records.keep([(record.device_id, older, record.session)]) == {older: Keeping.HELD}


async def test_another_device_s_claim_finds_nothing(records: Any) -> None:
    record, older, newer = new(), uuid.uuid7(), uuid.uuid7()
    await records.start(record, older)
    other = Record(record.session, uuid.uuid7(), REGISTERED, TERMS, 7)
    assert await claim(records, other, newer) is Claim.GONE
    assert await take(records, other, newer, last=False) is Claim.GONE


async def test_a_new_session_replaces_the_device_s_older(records: Any) -> None:
    record, older, newer = new(), uuid.uuid7(), uuid.uuid7()
    await records.start(record, older)
    replacing = Record(uuid.uuid7(), record.device_id, REGISTERED, TERMS, 7)
    await records.start(replacing, newer)
    assert await records.keep(
        [(record.device_id, older, record.session), (record.device_id, newer, replacing.session)]
    ) == {older: Keeping.REPLACED, newer: Keeping.HELD}
    # The older connection writes nothing over it.
    assert await records.hand(record, older) is Handing.LOST
    await records.end(record.device_id, older, record.session)
    assert await claim(records, record, older) is Claim.GONE
    assert await claim(records, replacing, older) is Claim.WAITING


async def test_a_session_ended_is_forgotten_by_its_holder_only(records: Any) -> None:
    record, older, newer = new(), uuid.uuid7(), uuid.uuid7()
    await records.start(record, older)
    await records.end(record.device_id, newer, record.session)
    assert await records.keep([(record.device_id, older, record.session)]) == {older: Keeping.HELD}
    await records.end(record.device_id, older, record.session)
    assert await records.keep([(record.device_id, older, record.session)]) == {
        older: Keeping.MISSING
    }
    assert await claim(records, record, newer) is Claim.GONE
    assert await records.hand(record, older) is Handing.LOST
    assert await records.keep([]) == {}


async def test_a_removed_device_s_session_is_forgotten_whoever_holds_it(records: Any) -> None:
    held, parked, other = new(), new(), new()
    holders = {record.device_id: uuid.uuid7() for record in (held, parked, other)}
    for record in (held, parked, other):
        await records.start(record, holders[record.device_id])
    await records.hand(written(parked), holders[parked.device_id])
    await records.forget([held.device_id, parked.device_id])
    await records.forget([])
    holder = holders[held.device_id]
    assert await records.keep([(held.device_id, holder, held.session)]) == {holder: Keeping.MISSING}
    assert await claim(records, parked, uuid.uuid7()) is Claim.GONE
    holder = holders[other.device_id]
    assert await records.keep([(other.device_id, holder, other.session)]) == {holder: Keeping.HELD}


async def test_a_session_lasts_a_lease_while_held_and_its_window_once_given_up(
    redis_url: str,
) -> None:
    client = redis.asyncio.Redis.from_url(redis_url)
    try:
        records = RedisSessions(client, 0.5, 30)
        record, older, newer = new(), uuid.uuid7(), uuid.uuid7()
        key = f"{SESSION_PREFIX}{record.device_id}"
        await records.start(record, older)
        assert 0 < await client.pttl(key) <= 500
        await records.hand(record, older)
        assert 500 < await client.pttl(key) <= 30_000
        assert await claim(records, record, newer) == record
        assert 0 < await client.pttl(key) <= 500
        await client.pexpire(key, 100)
        await records.keep([(record.device_id, newer, record.session)])
        assert 100 < await client.pttl(key) <= 500
        # And a claim waiting changes none of it.
        await client.pexpire(key, 100)
        assert await claim(records, record, older) is Claim.WAITING
        assert await client.pttl(key) <= 100
    finally:
        await client.aclose()
