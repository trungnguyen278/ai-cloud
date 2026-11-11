# SPDX-License-Identifier: MIT
"""A home's devices: the challenge to register one, the registration that the app relays,
reading them and removing them (the app API, Devices)."""

import asyncio
import hashlib
import uuid
from typing import Any

import pytest
from app_api import Cloud, bearer, conforms, once, problem
from device import Device
from sqlalchemy import text

from ai_cloud.devices.adapters.postgres import home_lock, idempotency_lock
from ai_cloud.shared.database import create_engine

pytestmark = pytest.mark.anyio

# Enough challenges and nonces for the tests that ask for many.
PLENTY = {"accounts": {"nonce_rate": {"limit": 100}}, "devices": {"challenge_rate": {"limit": 100}}}


async def registered(cloud: Cloud) -> list[Any]:
    """What the registry holds: each device, in its home, with its key."""
    return await cloud.sql(
        "SELECT d.id, d.home_id, k.thumbprint, k.jwk FROM devices.devices d "
        "JOIN devices.keys k ON k.device_id = d.id ORDER BY d.id"
    )


async def first_answers(cloud: Cloud) -> int:
    [[count]] = await cloud.sql("SELECT count(*) FROM devices.idempotency_keys")
    return count


# The challenge.


async def test_a_challenge_for_the_owner_and_an_admin(cloud) -> None:
    family = await cloud.family()
    for person in (family.owner, family.admin):
        issued = await cloud.challenge(person, family.home)
        assert issued["server"] == cloud.settings.devices.server
        assert issued["expires_in"] == cloud.settings.devices.challenge_lifetime
    member = await cloud.http.post(
        f"{family.path}/device-challenges", headers=bearer(family.member)
    )
    problem(member, 403, "role_required")


async def test_a_challenge_is_new_each_time_and_kept_as_its_hash(cloud) -> None:
    family = await cloud.family()
    first = await cloud.challenge(family.owner, family.home)
    second = await cloud.challenge(family.owner, family.home)
    assert first["challenge"] != second["challenge"]
    rows = await cloud.sql(
        "SELECT hash, expire_time - create_time AS lifetime FROM devices.challenges"
    )
    assert {row.hash for row in rows} == {
        hashlib.sha256(each["challenge"].encode()).digest() for each in (first, second)
    }
    assert {row.lifetime.total_seconds() for row in rows} == {first["expires_in"]}


async def test_a_home_the_account_is_not_in_gives_no_challenge(cloud) -> None:
    family = await cloud.family()
    for home_id in (family.home["id"], str(uuid.uuid7()), "not-a-uuid"):
        response = await cloud.http.post(
            f"/app/v1/homes/{home_id}/device-challenges", headers=bearer(family.outsider)
        )
        problem(response, 404)
    nobody = await cloud.http.post(f"{family.path}/device-challenges")
    problem(nobody, 401)
    assert nobody.headers["www-authenticate"] == 'Bearer error="invalid_token"'


async def test_challenges_are_limited_for_each_account(make_cloud) -> None:
    cloud = await make_cloud(devices={"challenge_rate": {"limit": 2, "period": 60}})
    family = await cloud.family()
    for _ in range(2):
        await cloud.challenge(family.owner, family.home)
    over = await cloud.http.post(f"{family.path}/device-challenges", headers=bearer(family.owner))
    problem(over, 429)
    assert 0 < int(over.headers["retry-after"]) <= 60
    # Each account counts on its own.
    await cloud.challenge(family.admin, family.home)


# The registration.


async def test_registering_a_device(cloud) -> None:
    family = await cloud.family()
    device = Device()
    issued = await cloud.challenge(family.owner, family.home)
    response = await cloud.register(
        family.owner, family.home, device.registration(issued["challenge"], issued["server"])
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = conforms(response, "device")
    assert body["id"] == str(device.id)
    assert body["connected"] is False
    assert set(body) == {"id", "connected", "create_time"}
    [row] = await registered(cloud)
    assert (str(row.id), str(row.home_id)) == (str(device.id), family.home["id"])
    assert (row.thumbprint, row.jwk) == (device.thumbprint, device.jwk)
    # The challenge works once: it is gone.
    assert await cloud.sql("SELECT * FROM devices.challenges") == []
    # Everyone in the home sees it; the device belongs to the home.
    for person in (family.owner, family.admin, family.member):
        assert await cloud.devices(person, family.home) == [body]
        read = await cloud.http.get(f"{family.path}/devices/{device.id}", headers=bearer(person))
        assert conforms(read, "device") == body


async def test_a_retry_gets_the_first_answer(cloud) -> None:
    family = await cloud.family()
    issued = await cloud.challenge(family.owner, family.home)
    registration = Device().registration(issued["challenge"], issued["server"])
    key = once(family.owner)["Idempotency-Key"]
    first = await cloud.register(family.owner, family.home, registration, key)
    assert first.status_code == 201
    retry = await cloud.register(family.owner, family.home, registration, key)
    assert (retry.status_code, retry.content) == (201, first.content)
    # The same registration under another key: its challenge is used.
    again = await cloud.register(family.owner, family.home, registration)
    problem(again, 400, "registration_invalid")
    assert len(await cloud.devices(family.owner, family.home)) == 1


async def test_a_refusal_after_the_challenge_is_used_is_kept_too(cloud) -> None:
    family = await cloud.family()
    other = await cloud.create_home(family.owner, name="Nhà khác")
    device = Device()
    await cloud.add_device(family.owner, other, device)
    issued = await cloud.challenge(family.owner, family.home)
    registration = device.registration(issued["challenge"], issued["server"])
    key = once(family.owner)["Idempotency-Key"]
    taken = await cloud.register(family.owner, family.home, registration, key)
    problem(taken, 409, "device_taken")
    # The device leaves the other home: the retry still gets the first answer.
    removed = await cloud.http.delete(
        f"/app/v1/homes/{other['id']}/devices/{device.id}", headers=bearer(family.owner)
    )
    assert removed.status_code == 204
    retry = await cloud.register(family.owner, family.home, registration, key)
    assert (retry.status_code, retry.content) == (409, taken.content)
    assert retry.headers["content-type"] == "application/problem+json"
    # A new challenge, as the app gets after a refusal, registers it.
    await cloud.add_device(family.owner, family.home, device)


async def test_a_registration_that_fails_its_checks_uses_its_challenge(cloud) -> None:
    family = await cloud.family()
    issued = await cloud.challenge(family.owner, family.home)
    device = Device()
    forged = device.registration(issued["challenge"], issued["server"], signer=Device().key)
    key = once(family.owner)["Idempotency-Key"]
    refused = await cloud.register(family.owner, family.home, forged, key)
    assert problem(refused, 400, "registration_invalid")["detail"] == "bad signature"
    # Nothing is tried twice on one challenge: the device's own signature comes too late.
    good = device.registration(issued["challenge"], issued["server"])
    problem(await cloud.register(family.owner, family.home, good), 400, "registration_invalid")
    retry = await cloud.register(family.owner, family.home, forged, key)
    problem(retry, 400, "registration_invalid")
    assert retry.content == refused.content
    assert await cloud.devices(family.owner, family.home) == []


async def test_a_registration_without_a_usable_challenge_keeps_nothing(cloud) -> None:
    family = await cloud.family()
    other = await cloud.create_home(family.owner, name="Nhà khác")
    issued = await cloud.challenge(family.owner, family.home)
    device = Device()
    registration = device.registration(issued["challenge"], issued["server"])
    unknown = device.registration("A" * 43, issued["server"])
    none = device.registration(issued["challenge"], issued["server"], without=("nonce",))
    for tokens, home, signed in (
        # None at all, or a challenge the cloud never issued.
        (family.owner, family.home, none),
        (family.owner, family.home, unknown),
        # Another home's.
        (family.owner, other, registration),
        # Another account's, in the same home.
        (family.admin, family.home, registration),
    ):
        problem(await cloud.register(tokens, home, signed), 400, "registration_invalid")
    assert await first_answers(cloud) == 0
    # Each refused one leaves the challenge to its home and its account.
    response = await cloud.register(family.owner, family.home, registration)
    assert response.status_code == 201, response.text


async def test_a_challenge_expires(cloud) -> None:
    family = await cloud.family()
    issued = await cloud.challenge(family.owner, family.home)
    await cloud.sql("UPDATE devices.challenges SET expire_time = now() - interval '1 second'")
    registration = Device().registration(issued["challenge"], issued["server"])
    expired = await cloud.register(family.owner, family.home, registration)
    problem(expired, 400, "registration_invalid")


async def test_a_member_does_not_add_devices(cloud) -> None:
    family = await cloud.family()
    issued = await cloud.challenge(family.owner, family.home)
    registration = Device().registration(issued["challenge"], issued["server"])
    refused = await cloud.register(family.member, family.home, registration)
    problem(refused, 403, "role_required")
    for tokens in (family.outsider, family.member):
        other = await cloud.http.post(
            f"/app/v1/homes/{uuid.uuid7()}/devices",
            json={"registration": registration},
            headers=once(tokens),
        )
        problem(other, 404)
    outsider = await cloud.register(family.outsider, family.home, registration)
    problem(outsider, 404)
    # The challenge is still the owner's to use.
    assert (await cloud.register(family.owner, family.home, registration)).status_code == 201


async def test_the_audience_is_the_server_the_challenge_gave(make_cloud) -> None:
    cloud = await make_cloud(devices={"server": "https://devices.example/"})
    family = await cloud.family()
    issued = await cloud.challenge(family.owner, family.home)
    assert issued["server"] == "https://devices.example/"
    device = Device()
    # As the cloud's configuration names it, trailing slash and all.
    elsewhere = device.registration(issued["challenge"], "https://devices.example")
    refused = await cloud.register(family.owner, family.home, elsewhere)
    assert problem(refused, 400, "registration_invalid")["detail"] == "wrong audience"
    await cloud.add_device(family.owner, family.home, device)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"registration": "not a jwt"},
        {"registration": 7},
        {"registration": "a.b.c" + "c" * 4096},
    ],
)
async def test_a_registration_is_checked(cloud, body: dict[str, Any]) -> None:
    family = await cloud.family()
    response = await cloud.http.post(
        f"{family.path}/devices", json=body, headers=once(family.owner)
    )
    problem(response, 400)


async def test_a_registration_nested_past_the_configured_depth_is_refused(cloud) -> None:
    family = await cloud.family()
    response = await cloud.http.post(
        f"{family.path}/devices",
        content=b"[" * 1000 + b"]" * 1000,
        headers={**once(family.owner), "Content-Type": "application/json"},
    )
    assert problem(response, 400)["detail"] == "the body is not JSON"


async def test_a_registration_needs_an_idempotency_key(cloud) -> None:
    family = await cloud.family()
    issued = await cloud.challenge(family.owner, family.home)
    registration = Device().registration(issued["challenge"], issued["server"])
    for headers in (bearer(family.owner), {**bearer(family.owner), "Idempotency-Key": "x"}):
        response = await cloud.http.post(
            f"{family.path}/devices", json={"registration": registration}, headers=headers
        )
        problem(response, 400)
    broken = await cloud.http.post(
        f"{family.path}/devices",
        content=b'{"registration": "\\ud800"}',
        headers={**once(family.owner), "Content-Type": "application/json"},
    )
    problem(broken, 400)


async def test_a_key_is_for_one_request(cloud) -> None:
    family = await cloud.family()
    other = await cloud.create_home(family.owner, name="Nhà khác")
    issued = await cloud.challenge(family.owner, family.home)
    registration = Device().registration(issued["challenge"], issued["server"])
    key = once(family.owner)["Idempotency-Key"]
    assert (await cloud.register(family.owner, family.home, registration, key)).status_code == 201
    another = Device().registration(issued["challenge"], issued["server"])
    problem(await cloud.register(family.owner, family.home, another, key), 422)
    problem(await cloud.register(family.owner, other, registration, key), 422)
    # Each account's keys are its own.
    issued = await cloud.challenge(family.admin, family.home)
    mine = Device().registration(issued["challenge"], issued["server"])
    assert (await cloud.register(family.admin, family.home, mine, key)).status_code == 201


async def test_a_retry_while_the_first_request_runs_tries_again(cloud) -> None:
    family = await cloud.family()
    account = await cloud.account(family.owner)
    issued = await cloud.challenge(family.owner, family.home)
    registration = Device().registration(issued["challenge"], issued["server"])
    key = uuid.uuid7()
    engine = create_engine(cloud.database, cloud.settings.database, "ai-cloud tests")
    try:
        async with engine.begin() as first:
            lock = idempotency_lock(uuid.UUID(account["id"]), key)
            await first.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
            running = await cloud.register(family.owner, family.home, registration, f'"{key}"')
            problem(running, 409)
            # Another account's key of the same value is another key.
            issued = await cloud.challenge(family.admin, family.home)
            theirs = Device().registration(issued["challenge"], issued["server"])
            response = await cloud.register(family.admin, family.home, theirs, f'"{key}"')
            assert response.status_code == 201, response.text
    finally:
        await engine.dispose()
    response = await cloud.register(family.owner, family.home, registration, f'"{key}"')
    assert response.status_code == 201


async def test_registrations_in_two_homes_do_not_wait_for_each_other(cloud) -> None:
    family = await cloud.family()
    other = await cloud.create_home(family.owner, name="Nhà khác")
    engine = create_engine(cloud.database, cloud.settings.database, "ai-cloud tests")
    try:
        async with engine.begin() as first:
            # Something holds the family's home.
            lock = home_lock(uuid.UUID(family.home["id"]))
            await first.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": lock})
            added = cloud.add_device(family.owner, other, Device())
            assert (await asyncio.wait_for(added, 5))["connected"] is False
    finally:
        await engine.dispose()


async def test_two_requests_with_one_key_register_once(cloud) -> None:
    family = await cloud.family()
    issued = await cloud.challenge(family.owner, family.home)
    registration = Device().registration(issued["challenge"], issued["server"])
    key = once(family.owner)["Idempotency-Key"]
    answers = await asyncio.gather(
        *(cloud.register(family.owner, family.home, registration, key) for _ in range(5))
    )
    made = [answer for answer in answers if answer.status_code == 201]
    assert made
    assert all(answer.status_code in (201, 409) for answer in answers)
    assert len({answer.content for answer in made}) == 1
    assert len(await cloud.devices(family.owner, family.home)) == 1


async def test_a_first_answer_is_kept_for_the_retention_only(cloud) -> None:
    family = await cloud.family()
    issued = await cloud.challenge(family.owner, family.home)
    registration = Device().registration(issued["challenge"], issued["server"])
    key = once(family.owner)["Idempotency-Key"]
    assert (await cloud.register(family.owner, family.home, registration, key)).status_code == 201
    await cloud.sql(
        "UPDATE devices.idempotency_keys "
        "SET create_time = create_time - make_interval(secs => :seconds)",
        seconds=cloud.settings.app_api.idempotency_retention + 1,
    )
    # Past the retention, the request runs again, and its challenge is used.
    again = await cloud.register(family.owner, family.home, registration, key)
    problem(again, 400, "registration_invalid")
    # The key may then serve another registration, whose answer replaces the old one.
    issued = await cloud.challenge(family.owner, family.home)
    another = Device()
    signed = another.registration(issued["challenge"], issued["server"])
    assert (await cloud.register(family.owner, family.home, signed, key)).status_code == 201
    [kept] = await cloud.sql("SELECT device_id FROM devices.idempotency_keys")
    assert kept.device_id == another.id


# One home per device, and one identifier per key.


async def test_the_same_device_registers_again_in_its_home(cloud) -> None:
    family = await cloud.family()
    device = Device()
    added = await cloud.add_device(family.owner, family.home, device)
    # It lost its state: an admin sets it up again, with its identifier in capitals.
    issued = await cloud.challenge(family.admin, family.home)
    claims = {"iss": str(device.id).upper(), "sub": str(device.id).upper()}
    again = await cloud.register(
        family.admin,
        family.home,
        device.registration(issued["challenge"], issued["server"], claims=claims),
    )
    assert again.status_code == 200, again.text
    assert conforms(again, "device") == added
    assert len(await registered(cloud)) == 1


async def test_a_device_in_another_home_is_taken(cloud) -> None:
    family = await cloud.family()
    theirs = await cloud.create_home(family.outsider, name="Nhà Bảo")
    device = Device()
    await cloud.add_device(family.outsider, theirs, device)
    issued = await cloud.challenge(family.owner, family.home)
    taken = await cloud.register(
        family.owner, family.home, device.registration(issued["challenge"], issued["server"])
    )
    problem(taken, 409, "device_taken")
    assert await cloud.devices(family.owner, family.home) == []
    # Once that home's owner removes it, it is a new device to the cloud, in any home.
    removed = await cloud.http.delete(
        f"/app/v1/homes/{theirs['id']}/devices/{device.id}", headers=bearer(family.outsider)
    )
    assert removed.status_code == 204
    await cloud.add_device(family.owner, family.home, device)


async def test_a_key_or_an_identifier_of_another_device_is_taken(cloud) -> None:
    family = await cloud.family()
    other = await cloud.create_home(family.owner, name="Nhà khác")
    device = Device()
    await cloud.add_device(family.owner, family.home, device)
    for home in (family.home, other):
        for impostor in (Device(device.id), Device(key=device.key)):
            issued = await cloud.challenge(family.owner, home)
            signed = impostor.registration(issued["challenge"], issued["server"])
            problem(await cloud.register(family.owner, home, signed), 409, "device_taken")
    assert len(await registered(cloud)) == 1


async def test_a_home_has_so_many_devices(make_cloud) -> None:
    cloud = await make_cloud(devices={"devices_per_home": 2})
    family = await cloud.family()
    other = await cloud.create_home(family.owner, name="Nhà khác")
    elsewhere = Device()
    # Another home's devices count for that home only.
    await cloud.add_device(family.owner, other, elsewhere)
    devices = [Device(), Device()]
    for device in devices:
        await cloud.add_device(family.owner, family.home, device)
    issued = await cloud.challenge(family.owner, family.home)
    registration = Device().registration(issued["challenge"], issued["server"])
    key = once(family.owner)["Idempotency-Key"]
    full = await cloud.register(family.owner, family.home, registration, key)
    problem(full, 409, "device_limit_reached")
    # That refusal used its challenge too, and a retry gets it again.
    retry = await cloud.register(family.owner, family.home, registration, key)
    assert (retry.status_code, retry.content) == (409, full.content)
    used = await cloud.register(family.owner, family.home, registration)
    problem(used, 400, "registration_invalid")
    # A device that another home holds is taken, full home or not.
    issued = await cloud.challenge(family.owner, family.home)
    taken = await cloud.register(
        family.owner, family.home, elsewhere.registration(issued["challenge"], issued["server"])
    )
    problem(taken, 409, "device_taken")
    # The limit counts new devices only: a full home takes back one that lost its state.
    issued = await cloud.challenge(family.owner, family.home)
    again = await cloud.register(
        family.owner, family.home, devices[0].registration(issued["challenge"], issued["server"])
    )
    assert again.status_code == 200, again.text


async def test_the_limit_holds_for_registrations_that_race(make_cloud) -> None:
    cloud = await make_cloud(devices={"devices_per_home": 1})
    family = await cloud.family()
    signed = []
    for _ in range(4):
        issued = await cloud.challenge(family.owner, family.home)
        signed.append(Device().registration(issued["challenge"], issued["server"]))
    answers = await asyncio.gather(
        *(cloud.register(family.owner, family.home, each) for each in signed)
    )
    assert sorted(answer.status_code for answer in answers) == [201, 409, 409, 409]
    assert len(await cloud.devices(family.owner, family.home)) == 1


async def test_one_device_set_up_in_two_homes_at_once_is_in_one(make_cloud) -> None:
    cloud = await make_cloud(**PLENTY)
    family = await cloud.family()
    homes = [family.home, await cloud.create_home(family.owner, name="Nhà khác")]
    for _ in range(3):
        device = Device()
        signed = []
        for home in homes:
            issued = await cloud.challenge(family.owner, home)
            signed.append(device.registration(issued["challenge"], issued["server"]))
        pairs = zip(homes, signed, strict=True)
        answers = await asyncio.gather(
            *(cloud.register(family.owner, home, each) for home, each in pairs)
        )
        statuses = sorted(answer.status_code for answer in answers)
        assert statuses == [201, 409], [answer.text for answer in answers]
        [row] = await cloud.sql("SELECT count(*) FROM devices.devices WHERE id = :id", id=device.id)
        assert row[0] == 1


# Reading a home's devices.


async def test_listing_a_home_s_devices_in_pages(cloud) -> None:
    family = await cloud.family()
    other = await cloud.create_home(family.owner, name="Nhà khác")
    await cloud.add_device(family.owner, other, Device())
    # Registered in another order than their identifiers': the list keeps theirs.
    ids = sorted(uuid.uuid4() for _ in range(3))
    added = [
        await cloud.add_device(family.owner, family.home, Device(device_id))
        for device_id in reversed(ids)
    ]
    added.reverse()
    assert [device["id"] for device in added] == [str(each) for each in ids]
    seen: list[dict[str, Any]] = []
    token = ""
    while True:
        response = await cloud.http.get(
            f"{family.path}/devices",
            params={"page_size": 2, "page_token": token},
            headers=bearer(family.member),
        )
        page = conforms(response, "device_list")
        seen += page["devices"]
        token = page["next_page_token"]
        if not token:
            break
    assert seen == added
    bad = await cloud.http.get(
        f"{family.path}/devices", params={"page_token": "*"}, headers=bearer(family.owner)
    )
    problem(bad, 400)
    for page_size in ("0", "51", "x"):
        bad = await cloud.http.get(
            f"{family.path}/devices", params={"page_size": page_size}, headers=bearer(family.owner)
        )
        problem(bad, 400)


async def test_only_those_in_the_home_see_its_devices(cloud) -> None:
    family = await cloud.family()
    device = Device()
    await cloud.add_device(family.owner, family.home, device)
    for path in (f"{family.path}/devices", f"{family.path}/devices/{device.id}"):
        problem(await cloud.http.get(path, headers=bearer(family.outsider)), 404)
        problem(await cloud.http.get(path), 401)


async def test_reading_a_device_of_the_home(cloud) -> None:
    family = await cloud.family()
    other = await cloud.create_home(family.owner, name="Nhà khác")
    device, elsewhere = Device(), Device()
    added = await cloud.add_device(family.owner, family.home, device)
    await cloud.add_device(family.owner, other, elsewhere)
    # Identifiers compare as UUIDs, whatever their case.
    read = await cloud.http.get(
        f"{family.path}/devices/{str(device.id).upper()}", headers=bearer(family.member)
    )
    assert conforms(read, "device") == added
    for device_id in (elsewhere.id, uuid.uuid7(), "not-a-uuid"):
        missing = await cloud.http.get(
            f"{family.path}/devices/{device_id}", headers=bearer(family.owner)
        )
        problem(missing, 404)


# Removing a device.


async def test_removing_a_device_revokes_it(cloud) -> None:
    family = await cloud.family()
    device, kept = Device(), Device()
    first = await cloud.add_device(family.owner, family.home, device)
    await cloud.add_device(family.owner, family.home, kept)
    assert await first_answers(cloud) == 2
    removed = await cloud.http.delete(
        f"{family.path}/devices/{device.id}", headers=bearer(family.admin)
    )
    assert removed.status_code == 204
    gone = await cloud.http.get(f"{family.path}/devices/{device.id}", headers=bearer(family.owner))
    problem(gone, 404)
    # The cloud forgets its key, and the first answer that showed it.
    assert [str(row.id) for row in await registered(cloud)] == [str(kept.id)]
    assert await first_answers(cloud) == 1
    # Set up again, it is a new device to the cloud.
    again = await cloud.add_device(family.owner, family.home, device)
    assert again["id"] == first["id"]
    [[count]] = await cloud.sql("SELECT count(*) FROM devices.devices")
    assert count == 2


async def test_only_the_owner_and_admins_remove_devices(cloud) -> None:
    family = await cloud.family()
    device = Device()
    await cloud.add_device(family.owner, family.home, device)
    path = f"{family.path}/devices/{device.id}"
    problem(await cloud.http.delete(path, headers=bearer(family.member)), 403, "role_required")
    problem(await cloud.http.delete(path, headers=bearer(family.outsider)), 404)
    assert len(await cloud.devices(family.owner, family.home)) == 1


async def test_removing_what_the_home_does_not_hold_finds_nothing(cloud) -> None:
    family = await cloud.family()
    other = await cloud.create_home(family.owner, name="Nhà khác")
    elsewhere = Device()
    await cloud.add_device(family.owner, other, elsewhere)
    for device_id in (elsewhere.id, uuid.uuid7(), "not-a-uuid"):
        missing = await cloud.http.delete(
            f"{family.path}/devices/{device_id}", headers=bearer(family.owner)
        )
        problem(missing, 404)
    assert len(await cloud.devices(family.owner, other)) == 1
    # Nor what showed it.
    assert await first_answers(cloud) == 1


async def test_a_device_stays_when_the_person_who_added_it_leaves(cloud) -> None:
    family = await cloud.family()
    device = await cloud.add_device(family.admin, family.home, Device())
    left = await cloud.http.delete(
        f"{family.path}/members/{family.ids['admin']}", headers=bearer(family.admin)
    )
    assert left.status_code == 204
    assert await cloud.devices(family.owner, family.home) == [device]
