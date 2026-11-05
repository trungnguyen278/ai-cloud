# SPDX-License-Identifier: MIT
"""The devices' store, in PostgreSQL (docs/decisions/0008-data-and-tokens.md)."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Row, Table, delete, func, insert, select, tuple_
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ai_cloud.devices.db.tables import challenges, devices, idempotency_keys, keys
from ai_cloud.devices.domain.model import (
    Answer,
    Challenge,
    Device,
    DeviceKey,
    FirstAnswer,
)
from ai_cloud.shared.app_api import IdempotencyKey

_UNIQUE_VIOLATION = "23505"  # PostgreSQL's SQLSTATE for unique_violation


class PostgresStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[PostgresTransaction]:
        async with self._engine.begin() as connection:
            yield PostgresTransaction(connection)


class PostgresTransaction:
    def __init__(self, connection: AsyncConnection) -> None:
        self._db = connection

    async def hold_home(self, home_id: UUID) -> None:
        # An advisory lock, for the home is the accounts' row, and it may have no device yet.
        await self._db.execute(select(func.pg_advisory_xact_lock(home_lock(home_id))))

    # Challenges.

    async def add_challenge(self, challenge_hash: bytes, challenge: Challenge) -> None:
        await self._db.execute(
            insert(challenges).values(
                hash=challenge_hash,
                home_id=challenge.home_id,
                account_id=challenge.account_id,
                create_time=challenge.create_time,
                expire_time=challenge.expire_time,
            )
        )

    async def use_challenge(
        self, challenge_hash: bytes, home_id: UUID, account_id: UUID, now: datetime
    ) -> bool:
        used = await self._db.execute(
            delete(challenges).where(
                challenges.c.hash == challenge_hash,
                challenges.c.home_id == home_id,
                challenges.c.account_id == account_id,
                challenges.c.expire_time > now,
            )
        )
        return used.rowcount == 1

    async def forget_challenge(self, challenge_hash: bytes) -> None:
        await self._db.execute(delete(challenges).where(challenges.c.hash == challenge_hash))

    # Devices.

    async def device(self, device_id: UUID) -> Device | None:
        row = (
            await self._db.execute(select(devices).where(devices.c.id == device_id))
        ).one_or_none()
        return None if row is None else _device(row)

    async def key_holder(self, thumbprint: str) -> UUID | None:
        found = await self._db.execute(
            select(keys.c.device_id).where(keys.c.thumbprint == thumbprint)
        )
        return found.scalar_one_or_none()

    async def devices(self, home_id: UUID, *, after: UUID | None, limit: int) -> list[Device]:
        query = select(devices).where(devices.c.home_id == home_id)
        if after is not None:
            query = query.where(devices.c.id > after)
        found = await self._db.execute(query.order_by(devices.c.id).limit(limit))
        return [_device(row) for row in found]

    async def count_devices(self, home_id: UUID) -> int:
        found = await self._db.execute(
            select(func.count()).select_from(devices).where(devices.c.home_id == home_id)
        )
        return found.scalar_one()

    async def add_device(self, device: Device, key: DeviceKey) -> bool:
        try:
            # A savepoint, so that a conflict leaves the transaction usable.
            async with self._db.begin_nested():
                await self._db.execute(
                    insert(devices).values(
                        id=device.id, home_id=device.home_id, create_time=device.create_time
                    )
                )
                await self._db.execute(
                    insert(keys).values(
                        thumbprint=key.thumbprint,
                        device_id=device.id,
                        jwk=key.jwk,
                        create_time=device.create_time,
                    )
                )
        except IntegrityError as error:
            if _unique_violation(error):
                return False
            raise
        return True

    async def remove_device(self, home_id: UUID, device_id: UUID) -> bool:
        # The first answers that show it before the device, in the order of every forgetting.
        # An answer shows a device of its own home only.
        await self._forget(
            idempotency_keys,
            idempotency_keys.c.device_id == device_id,
            idempotency_keys.c.home_id == home_id,
        )
        removed = await self._db.execute(
            delete(devices).where(devices.c.id == device_id, devices.c.home_id == home_id)
        )
        return removed.rowcount == 1

    # What goes with a home, a person's place in it, or an account. The challenges first: a
    # registration that used one holds it until it ends, so this waits for it, and what is read
    # next includes what it added. One that comes later finds its challenge gone, and adds
    # nothing. Then the first answers, then the devices, which their answers would otherwise
    # hold through their foreign key.

    async def forget_home(self, home_id: UUID) -> int:
        await self._forget(challenges, challenges.c.home_id == home_id)
        await self._forget(idempotency_keys, idempotency_keys.c.home_id == home_id)
        return await self._forget(devices, devices.c.home_id == home_id)

    async def forget_member(self, home_id: UUID, account_id: UUID) -> None:
        await self._forget(
            challenges, challenges.c.home_id == home_id, challenges.c.account_id == account_id
        )
        await self._forget(
            idempotency_keys,
            idempotency_keys.c.home_id == home_id,
            idempotency_keys.c.account_id == account_id,
        )

    async def forget_account(self, account_id: UUID) -> None:
        await self._forget(challenges, challenges.c.account_id == account_id)
        await self._forget(idempotency_keys, idempotency_keys.c.account_id == account_id)

    async def _forget(self, table: Table, *where: ColumnElement[bool]) -> int:
        """Delete the rows of ``table`` that match ``where``, holding them first in the order
        of their primary key; how many. Two transactions that forget some of the same rows,
        as an owner's account and its home do at once, then take them in one order, and never
        wait for each other in a circle. Deleting through an index would take them in that
        index's order."""
        key = list(table.primary_key.columns)
        held = await self._db.execute(select(*key).where(*where).order_by(*key).with_for_update())
        rows = [tuple(row) for row in held]
        if not rows:
            return 0
        forgotten = await self._db.execute(delete(table).where(tuple_(*key).in_(rows)))
        return forgotten.rowcount

    # The first answers to requests with an Idempotency-Key.

    async def hold_idempotency_key(self, account_id: UUID, key: UUID) -> bool:
        # An advisory lock, for there may be no row yet, and it goes with the transaction,
        # or with its connection if the process dies.
        held = await self._db.execute(
            select(func.pg_try_advisory_xact_lock(idempotency_lock(account_id, key)))
        )
        return held.scalar_one()

    async def first_answer(
        self, account_id: UUID, key: UUID, *, since: datetime
    ) -> FirstAnswer | None:
        row = (
            await self._db.execute(
                select(
                    idempotency_keys.c.fingerprint,
                    idempotency_keys.c.status,
                    idempotency_keys.c.answer,
                ).where(
                    idempotency_keys.c.account_id == account_id,
                    idempotency_keys.c.key == key,
                    idempotency_keys.c.create_time >= since,
                )
            )
        ).one_or_none()
        return None if row is None else FirstAnswer(row.fingerprint, Answer(row.status, row.answer))

    async def add_first_answer(
        self,
        account_id: UUID,
        key: IdempotencyKey,
        home_id: UUID,
        device_id: UUID | None,
        answer: Answer,
        now: datetime,
    ) -> None:
        kept: dict[str, Any] = {
            "fingerprint": key.fingerprint,
            "home_id": home_id,
            "device_id": device_id,
            "status": answer.status,
            "answer": answer.body,
            "create_time": now,
        }
        await self._db.execute(
            upsert(idempotency_keys)
            .values(account_id=account_id, key=key.key, **kept)
            .on_conflict_do_update(
                index_elements=[idempotency_keys.c.account_id, idempotency_keys.c.key],
                set_=kept,
            )
        )


def home_lock(home_id: UUID) -> int:
    """The advisory lock under which a home's devices change."""
    return _lock(b"devices.home", home_id.bytes)


def idempotency_lock(account_id: UUID, key: UUID) -> int:
    """The advisory lock of an account's Idempotency-Key, for the devices' requests."""
    return _lock(b"devices.idempotency_key", account_id.bytes + key.bytes)


def _lock(name: bytes, value: bytes) -> int:
    """An advisory lock's name: a bigint, as PostgreSQL's locks are named by. Two that share
    one would only wait for each other."""
    digest = hashlib.sha256(name + b"\0" + value).digest()
    return int.from_bytes(digest[:8], signed=True)


def _device(row: Row[Any]) -> Device:
    return Device(id=row.id, home_id=row.home_id, create_time=row.create_time)


def _unique_violation(error: IntegrityError) -> bool:
    return getattr(error.orig, "sqlstate", None) == _UNIQUE_VIOLATION
