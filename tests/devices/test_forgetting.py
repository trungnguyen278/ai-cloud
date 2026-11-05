# SPDX-License-Identifier: MIT
"""What the devices forget when the accounts say that a home or an account is gone: a deleted
home's devices are revoked, and nothing that a registration racing the deletion keeps
outlives it (docs/decisions/0009-domain-events.md). A removal waits for a registration in its
home, too."""

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from app_api import Cloud, bearer
from device import Device
from events_module import EVENTS, until
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_cloud.accounts.api import AccountDeleted, HomeDeleted
from ai_cloud.devices.adapters.postgres import PostgresStore
from ai_cloud.devices.api import consumers
from ai_cloud.devices.domain.errors import RegistrationInvalid
from ai_cloud.devices.domain.model import (
    Access,
    Answer,
    Challenge,
    DeviceKey,
    challenge_hash,
    new_challenge,
)
from ai_cloud.devices.domain.model import Device as Held
from ai_cloud.devices.ports import Transaction
from ai_cloud.devices.service import DevicesService, Forgetting
from ai_cloud.entry.worker import WorkerSecrets, serve
from ai_cloud.shared.app_api import IdempotencyKey
from ai_cloud.shared.database import create_engine
from ai_cloud.shared.events import Event

pytestmark = pytest.mark.anyio

# The names in PostgreSQL of the connections that forget or remove, so that a test sees
# them wait.
FORGETTING = "ai-cloud tests forgetting"
REMOVING = "ai-cloud tests removing"
NOTHING = {"devices": 0, "challenges": 0, "idempotency_keys": 0}


@pytest.fixture
async def engine(cloud: Cloud) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(cloud.database, cloud.settings.database, "ai-cloud tests")
    try:
        yield engine
    finally:
        await engine.dispose()


def event(kind: Any, **data: str) -> Event:
    """An event of the accounts', as the worker hands it to a consumer."""
    return Event(uuid.uuid7(), "/accounts", kind.TYPE, "subject", datetime.now(UTC), data)


async def deliver(cloud: Cloud, engine: AsyncEngine) -> None:
    """Hand every event the accounts wrote to the devices' consumers, as the worker would."""
    handlers = {
        type_: handler
        for consumer in consumers(engine)
        for type_, handler in consumer.handlers.items()
    }
    for row in await cloud.sql("SELECT * FROM accounts.outbox ORDER BY seq"):
        if row.type in handlers:
            await handlers[row.type](
                Event(row.id, "/accounts", row.type, row.subject, row.create_time, row.data)
            )


async def delete_home(cloud: Cloud, tokens: dict[str, Any], home: dict[str, Any]) -> None:
    response = await cloud.http.post(
        f"/app/v1/homes/{home['id']}:delete",
        json={"id_token": await cloud.id_token()},
        headers=bearer(tokens),
    )
    assert response.status_code == 204, response.text


async def kept_for(cloud: Cloud, column: str, value: UUID | str) -> dict[str, int]:
    """What the devices keep for a home, or an account: by ``column``, ``value``."""
    found = {}
    for table in ("devices", "challenges", "idempotency_keys"):
        if table == "devices" and column != "home_id":
            continue
        [[count]] = await cloud.sql(
            f"SELECT count(*) FROM devices.{table} WHERE {column} = :value",
            value=uuid.UUID(str(value)),
        )
        found[table] = count
    return found


async def test_deleting_a_home_revokes_its_devices(cloud, engine, caplog) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    kept = await cloud.create_home(tokens, name="Nhà khác")
    device = Device()
    await cloud.add_device(tokens, home, device)
    await cloud.add_device(tokens, home, Device())
    # A refusal that used its challenge, whose answer shows no device.
    issued = await cloud.challenge(tokens, home)
    forged = Device().registration(issued["challenge"], issued["server"], signer=Device().key)
    assert (await cloud.register(tokens, home, forged)).status_code == 400
    await cloud.challenge(tokens, home)
    await cloud.add_device(tokens, kept, Device())
    assert await kept_for(cloud, "home_id", home["id"]) == {
        "devices": 2,
        "challenges": 1,
        "idempotency_keys": 3,
    }
    await delete_home(cloud, tokens, home)
    await deliver(cloud, engine)
    assert await kept_for(cloud, "home_id", home["id"]) == NOTHING
    [revoked] = [
        r for r in caplog.records if r.getMessage() == "a deleted home's devices are revoked"
    ]
    assert (revoked.home, revoked.devices) == (home["id"], 2)
    [[keys]] = await cloud.sql("SELECT count(*) FROM devices.keys")
    assert keys == 1
    assert len(await cloud.devices(tokens, kept)) == 1
    # The cloud forgot the device's key: set up again, it is a new device, in any home.
    await cloud.add_device(tokens, kept, device)
    # Forgetting again what is gone does nothing, and says nothing.
    caplog.clear()
    await deliver(cloud, engine)
    assert len(await cloud.devices(tokens, kept)) == 2
    assert not [r for r in caplog.records if r.name == "ai_cloud.devices.service"]


async def test_deleting_an_account_forgets_what_it_asked_for(cloud, engine) -> None:
    family = await cloud.family()
    await cloud.add_device(family.admin, family.home, Device())
    await cloud.challenge(family.admin, family.home)
    await cloud.add_device(family.owner, family.home, Device())
    admin = await cloud.account(family.admin)
    response = await cloud.http.post(
        "/app/v1/account:delete",
        json={"id_token": await cloud.id_token("an@example.com", "An")},
        headers=bearer(family.admin),
    )
    assert response.status_code == 204, response.text
    await deliver(cloud, engine)
    assert await kept_for(cloud, "account_id", admin["id"]) == {
        "challenges": 0,
        "idempotency_keys": 0,
    }
    # The devices are the home's, and stay, as do the others' answers.
    assert len(await cloud.devices(family.owner, family.home)) == 2
    [[answers]] = await cloud.sql("SELECT count(*) FROM devices.idempotency_keys")
    assert answers == 1


async def test_the_worker_revokes_a_deleted_home_s_devices(cloud, make_settings) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    await cloud.add_device(tokens, home, Device())
    stop = asyncio.Event()
    secrets = WorkerSecrets(cloud.database, cloud.redis_url)
    running = asyncio.create_task(serve(make_settings(events=EVENTS), secrets, stop))
    try:
        await delete_home(cloud, tokens, home)

        async def revoked() -> bool:
            return (await kept_for(cloud, "home_id", home["id"]))["devices"] == 0

        await until(revoked)
    finally:
        stop.set()
        await asyncio.wait_for(running, 10)


# Races: a registration in flight as its home, or its account, goes.


async def issued(engine: AsyncEngine, home_id: UUID, account_id: UUID) -> str:
    """A challenge for the home, issued to the account."""
    challenge = new_challenge()
    now = datetime.now(UTC)
    async with PostgresStore(engine).transaction() as store:
        await store.add_challenge(
            challenge_hash(challenge),
            Challenge(home_id, account_id, now, now + timedelta(minutes=5)),
        )
    return challenge


async def registering(store: Transaction, challenge: str, home_id: UUID, account_id: UUID) -> None:
    """What a registration does, up to its end: it uses its challenge, adds its device, and
    keeps its answer."""
    now = datetime.now(UTC)
    device = Device()
    await store.hold_home(home_id)
    assert await store.use_challenge(challenge_hash(challenge), home_id, account_id, now)
    assert await store.add_device(
        Held(device.id, home_id, now), DeviceKey(device.thumbprint, device.jwk)
    )
    key = IdempotencyKey(uuid.uuid7(), b"what it asked")
    await store.add_first_answer(account_id, key, home_id, device.id, Answer(201, {}), now)


async def waiting(engine: AsyncEngine, name: str = FORGETTING) -> bool:
    """Whether a connection of that name waits for a lock."""
    async with engine.connect() as connection:
        found = await connection.exec_driver_sql(
            "SELECT count(*) FROM pg_stat_activity "
            f"WHERE application_name = '{name}' AND wait_event_type = 'Lock'"
        )
        return found.scalar_one() > 0


async def forgotten_while_registering(
    cloud: Cloud,
    engine: AsyncEngine,
    home_id: UUID,
    account_id: UUID,
    forget: Callable[[Forgetting], Awaitable[None]],
) -> None:
    """Forget, on a connection of its own, while a registration that used its challenge has
    not ended: the forgetting waits for it."""
    challenge = await issued(engine, home_id, account_id)
    elsewhere = create_engine(cloud.database, cloud.settings.database, FORGETTING)
    try:
        async with PostgresStore(engine).transaction() as store:
            await registering(store, challenge, home_id, account_id)
            forgetting = asyncio.create_task(forget(Forgetting(PostgresStore(elsewhere))))

            async def stalled() -> bool:
                return await waiting(engine)

            await until(stalled)
        await asyncio.wait_for(forgetting, 5)
    finally:
        await elsewhere.dispose()


async def test_a_registration_as_its_home_goes_goes_with_it(cloud, engine) -> None:
    home_id, account_id = uuid.uuid7(), uuid.uuid7()
    deleted = event(HomeDeleted, home_id=str(home_id))
    await forgotten_while_registering(
        cloud, engine, home_id, account_id, lambda forgetting: forgetting.home_deleted(deleted)
    )
    assert await kept_for(cloud, "home_id", home_id) == NOTHING
    [[keys]] = await cloud.sql("SELECT count(*) FROM devices.keys")
    assert keys == 0


async def test_a_registration_as_its_account_goes_keeps_nothing_of_it(cloud, engine) -> None:
    home_id, account_id = uuid.uuid7(), uuid.uuid7()
    deleted = event(AccountDeleted, account_id=str(account_id))
    await forgotten_while_registering(
        cloud, engine, home_id, account_id, lambda forgetting: forgetting.account_deleted(deleted)
    )
    assert await kept_for(cloud, "account_id", account_id) == {
        "challenges": 0,
        "idempotency_keys": 0,
    }
    # The device is its home's.
    assert (await kept_for(cloud, "home_id", home_id))["devices"] == 1


class Managing:
    """What the accounts said before the home or the account went: the account manages the
    home."""

    async def account(self, access_token: str) -> UUID:
        raise AssertionError("not asked")

    async def access(self, account_id: UUID, home_id: UUID) -> Access:
        return Access.MANAGE


class Unlimited:
    async def wait(self, subject: str) -> float:
        return 0.0


@pytest.mark.parametrize("gone", ["home", "account"])
async def test_a_registration_after_its_home_or_account_went_adds_nothing(
    cloud, engine, gone: str
) -> None:
    home_id, account_id = uuid.uuid7(), uuid.uuid7()
    service = DevicesService(
        settings=cloud.settings.devices,
        app_api=cloud.settings.app_api,
        store=PostgresStore(engine),
        people=Managing(),
        challenge_rate=Unlimited(),
    )
    challenge = await service.issue_challenge(account_id, home_id)
    forgetting = Forgetting(PostgresStore(engine))
    if gone == "home":
        await forgetting.home_deleted(event(HomeDeleted, home_id=str(home_id)))
    else:
        await forgetting.account_deleted(event(AccountDeleted, account_id=str(account_id)))
    signed = Device().registration(challenge.challenge, challenge.server)
    with pytest.raises(RegistrationInvalid):
        await service.register(
            account_id,
            home_id,
            signed,
            IdempotencyKey(uuid.uuid7(), b"what it asked"),
            lambda _outcome: Answer(201, {}),
        )
    assert await kept_for(cloud, "home_id", home_id) == NOTHING


async def test_a_removal_waits_for_a_registration_in_its_home(cloud, engine) -> None:
    tokens = await cloud.sign_in()
    home = await cloud.create_home(tokens)
    device = Device()
    await cloud.add_device(tokens, home, device)
    home_id = uuid.UUID(home["id"])
    elsewhere = create_engine(cloud.database, cloud.settings.database, REMOVING)
    service = DevicesService(
        settings=cloud.settings.devices,
        app_api=cloud.settings.app_api,
        store=PostgresStore(elsewhere),
        people=Managing(),
        challenge_rate=Unlimited(),
    )
    try:
        async with PostgresStore(engine).transaction() as store:
            # The device registers again: it holds the home, and finds itself there.
            await store.hold_home(home_id)
            assert await store.device(device.id) is not None
            removing = asyncio.create_task(service.remove(uuid.uuid7(), home_id, device.id))

            async def stalled() -> bool:
                return await waiting(engine, REMOVING)

            await until(stalled)
            # Its answer shows the device, which is still there.
            now = datetime.now(UTC)
            key = IdempotencyKey(uuid.uuid7(), b"what it asked")
            shown = Answer(200, {})
            await store.add_first_answer(uuid.uuid7(), key, home_id, device.id, shown, now)
        await asyncio.wait_for(removing, 5)
    finally:
        await elsewhere.dispose()
    # The removal came after, and took the answer that showed the device along.
    assert await kept_for(cloud, "home_id", home_id) == NOTHING
