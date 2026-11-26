# SPDX-License-Identifier: MIT
"""The sessions' records in memory, as RedisSessions keeps them in Redis, but for their
lapsing: a test's own store of the devices' sessions, which tests/devices/test_records.py
holds to the adapter's rules."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ai_cloud.devices.domain.link import Claim, Handing, Keeping, Record


@dataclass
class Held:
    """A device's session: what its holder wrote last, the connection that holds it, if one
    does, and the newer one that claimed it, if one did."""

    record: Record
    owner: UUID | None
    taker: UUID | None = None


class Records:
    """Each device's session, which never lapses here."""

    def __init__(self) -> None:
        self.held: dict[UUID, Held] = {}

    async def start(self, record: Record, connection: UUID) -> None:
        self.held[record.device_id] = Held(record, connection)

    async def claim(
        self,
        device_id: UUID,
        connection: UUID,
        session: UUID,
        registered: datetime,
        terms: str,
        minor: int,
    ) -> Record | Claim:
        held = self.held.get(device_id)
        if held is None or held.record.session != session:
            return Claim.GONE
        if held.record.registered != registered:
            return Claim.REGISTRATION
        if held.record.terms != terms:
            return Claim.TERMS
        if held.record.minor > minor:
            return Claim.VERSION
        if held.owner not in (None, connection):
            held.taker = connection
            return Claim.WAITING
        held.owner, held.taker = connection, None
        return held.record

    async def take(
        self,
        device_id: UUID,
        connection: UUID,
        session: UUID,
        registered: datetime,
        terms: str,
        *,
        last: bool,
    ) -> Record | Claim:
        held = self.held.get(device_id)
        if held is None or held.record.session != session:
            return Claim.GONE
        if held.owner == connection:
            return held.record
        if held.taker == connection:
            if last:
                held.taker = None
            return Claim.WAITING
        return Claim.SUPERSEDED

    async def keep(self, held: Sequence[tuple[UUID, UUID, UUID]]) -> dict[UUID, Keeping]:
        found = {}
        for device_id, connection, session in held:
            each = self.held.get(device_id)
            if each is None:
                found[connection] = Keeping.MISSING
            elif each.record.session != session:
                found[connection] = Keeping.REPLACED
            elif each.owner != connection or each.taker is not None:
                found[connection] = Keeping.TAKEN
            else:
                found[connection] = Keeping.HELD
        return found

    async def hand(self, record: Record, connection: UUID) -> Handing:
        held = self.held.get(record.device_id)
        if held is None or held.record.session != record.session or held.owner != connection:
            return Handing.LOST
        held.record = record
        if held.taker is not None:
            held.owner, held.taker = held.taker, None
            return Handing.HANDED
        held.owner = None
        return Handing.PARKED

    async def end(self, device_id: UUID, connection: UUID, session: UUID) -> None:
        held = self.held.get(device_id)
        if held is not None and held.record.session == session and held.owner == connection:
            del self.held[device_id]

    async def forget(self, device_ids: Collection[UUID]) -> None:
        for device_id in device_ids:
            self.held.pop(device_id, None)
