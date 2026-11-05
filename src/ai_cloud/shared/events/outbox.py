# SPDX-License-Identifier: MIT
"""A module's outbox: the table it writes its events to, in the transaction of the change
they record, so that an event exists if and only if its change committed
(docs/decisions/0009-domain-events.md, decision 1; microservices.io's transactional outbox).

Each module declares its own, in its own schema, beside its tables, and its migrations
create it: a module that moves to a database of its own takes its outbox along. The
worker's relay reads it (``relay.py``).

An event has two numbers. ``seq`` is the order in which events were written, which is the
order of their changes for any one thing, since a change to a thing holds it until its
transaction ends. ``position`` is its place in the module's stream, which the relay gives it
once its transaction has committed, so that the stream's order never has a gap to fill.
"""

import hashlib
import uuid
from collections.abc import Sequence as Seq
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Identity,
    Index,
    MetaData,
    Sequence,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    bindparam,
    column,
    delete,
    func,
    insert,
    select,
    table,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_cloud.shared.events.model import DomainEvent, Event, module_of, stream_of

TABLE = "outbox"
POSITIONS = "outbox_position"


class Outbox:
    """A module's outbox, declared on the metadata of its tables, whose schema is the
    module's name."""

    def __init__(self, tables: MetaData) -> None:
        if tables.schema is None:
            raise ValueError("an outbox is in its module's schema")
        self.module = tables.schema
        self.stream = stream_of(self.module)
        # CloudEvents' source: the module, as a URI reference.
        self.source = f"/{self.module}"
        self.table = Table(
            TABLE,
            tables,
            # The order in which events were written.
            Column("seq", BigInteger, Identity(always=True), primary_key=True),
            Column("id", Uuid, nullable=False),
            Column("type", Text, nullable=False),
            Column("subject", Text, nullable=False),
            # When it happened.
            Column("create_time", DateTime(timezone=True), nullable=False),
            Column("data", JSON, nullable=False),
            # Its place in the stream, once the relay has given it one.
            Column("position", BigInteger),
            UniqueConstraint("position"),
            # What the relay numbers next.
            Index("ix_outbox_unnumbered", "seq", postgresql_where=text("position IS NULL")),
        )
        # The places in the stream, which only grow.
        self.positions = Sequence(POSITIONS, schema=self.module, metadata=tables)
        self._sequence = table(
            POSITIONS, column("last_value"), column("is_called"), schema=self.module
        )
        # The transaction-level advisory lock under which one relay at a time numbers or
        # sweeps the outbox: a bigint, as PostgreSQL's locks are named by.
        digest = hashlib.sha256(f"{self.module}.{TABLE} relay".encode()).digest()
        self._turn = int.from_bytes(digest[:8], signed=True)

    async def write(self, connection: AsyncConnection, event: DomainEvent) -> None:
        """Write ``event`` in the transaction that ``connection`` is in, to stand or fall
        with the change it records."""
        if not connection.in_transaction():
            raise RuntimeError("an event is written in the transaction of its change")
        if module_of(event.TYPE) != self.module:
            raise ValueError(f"{event.TYPE} is not one of {self.module}'s events")
        await connection.execute(
            insert(self.table).values(
                id=uuid.uuid7(),
                type=event.TYPE,
                subject=event.subject,
                create_time=datetime.now(UTC),
                data=event.data(),
            )
        )

    # What the relay does with it, each in a transaction of its own.

    async def take_turn(self, connection: AsyncConnection) -> bool:
        """Hold this outbox until the transaction ends, to number or sweep it, without
        waiting; whether no other relay holds it."""
        held = await connection.execute(select(func.pg_try_advisory_xact_lock(self._turn)))
        return bool(held.scalar_one())

    async def unnumbered(self, connection: AsyncConnection, limit: int) -> list[int]:
        """The ``seq`` of the oldest events without a place in the stream."""
        found = await connection.execute(
            select(self.table.c.seq)
            .where(self.table.c.position.is_(None))
            .order_by(self.table.c.seq)
            .limit(limit)
        )
        return list(found.scalars())

    async def number(self, connection: AsyncConnection, seqs: Seq[int]) -> None:
        """Give the events that ``seqs`` name, in that order, the next places in the
        stream. The numbering is held, so that every place given now follows every one
        given before."""
        if not seqs:
            return
        taken = await connection.execute(
            select(self.positions.next_value()).select_from(func.generate_series(1, len(seqs)))
        )
        places = sorted(taken.scalars())
        await connection.execute(
            update(self.table)
            .where(self.table.c.seq == bindparam("b_seq"))
            .values(position=bindparam("b_position")),
            [{"b_seq": seq, "b_position": place} for seq, place in zip(seqs, places, strict=True)],
        )

    async def last_position(self, connection: AsyncConnection) -> int:
        """The last place in the stream given to an event, or 0 for none. A sequence is
        outside transactions: this is the last one given, whatever has committed."""
        row = (await connection.execute(select(self._sequence))).one()
        return int(row.last_value) if row.is_called else 0

    async def after(
        self, connection: AsyncConnection, position: int, limit: int
    ) -> list[tuple[int, Event]]:
        """The events whose places come after ``position``, in their order."""
        found = await connection.execute(
            select(self.table)
            .where(self.table.c.position > position)
            .order_by(self.table.c.position)
            .limit(limit)
        )
        return [
            (
                row.position,
                Event(
                    id=row.id,
                    source=self.source,
                    type=row.type,
                    subject=row.subject,
                    time=row.create_time,
                    data=row.data,
                ),
            )
            for row in found
        ]

    async def forget(self, connection: AsyncConnection, *, relayed: int, before: datetime) -> int:
        """Delete the events that happened before ``before`` and are in the stream, at
        ``relayed`` or before; how many."""
        deleted = await connection.execute(
            delete(self.table).where(
                self.table.c.position <= relayed, self.table.c.create_time < before
            )
        )
        return deleted.rowcount

    async def first_position(self, connection: AsyncConnection) -> int | None:
        """The first place in the stream of an event the outbox keeps."""
        found = await connection.execute(select(func.min(self.table.c.position)))
        return found.scalar_one()
