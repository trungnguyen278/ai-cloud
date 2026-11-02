# SPDX-License-Identifier: MIT
"""The accounts' store, in PostgreSQL (docs/decisions/0008-data-and-tokens.md)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ai_cloud.accounts.db.tables import accounts, identities, refresh_tokens, sessions
from ai_cloud.accounts.domain.errors import IdentityConflict
from ai_cloud.accounts.domain.model import Account, Identity, Session
from ai_cloud.accounts.ports import IdentityOwner

_UNIQUE_VIOLATION = "23505"  # PostgreSQL's SQLSTATE for unique_violation
_IDENTITY_KEY = "uq_identities_provider_subject"


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

    async def identity(self, provider: str, subject: str) -> IdentityOwner | None:
        row = (
            await self._db.execute(
                select(identities.c.id, identities.c.account_id, identities.c.email).where(
                    identities.c.provider == provider, identities.c.subject == subject
                )
            )
        ).one_or_none()
        return None if row is None else IdentityOwner(row.id, row.account_id, row.email)

    async def add_account(self, account: Account) -> None:
        await self._db.execute(
            insert(accounts).values(
                id=account.id,
                display_name=account.display_name,
                email=account.email,
                languages=list(account.languages),
                version=account.version,
                create_time=account.create_time,
            )
        )
        for identity in account.identities:
            await self.add_identity(account.id, identity)

    async def add_identity(self, account_id: UUID, identity: Identity) -> None:
        try:
            # A savepoint, so that a conflict leaves the transaction usable.
            async with self._db.begin_nested():
                await self._db.execute(
                    insert(identities).values(
                        id=identity.id,
                        account_id=account_id,
                        provider=identity.provider,
                        subject=identity.subject,
                        email=identity.email,
                        create_time=identity.create_time,
                    )
                )
        except IntegrityError as error:
            if _violates(error, _IDENTITY_KEY):
                raise IdentityConflict("the identity was stored meanwhile") from None
            raise

    async def set_identity_email(self, identity_id: UUID, email: str) -> None:
        await self._db.execute(
            update(identities).where(identities.c.id == identity_id).values(email=email)
        )

    async def account(self, account_id: UUID, *, lock: bool) -> Account | None:
        query = select(accounts).where(accounts.c.id == account_id)
        row = (await self._db.execute(query.with_for_update() if lock else query)).one_or_none()
        if row is None:
            return None
        found = await self._db.execute(
            select(identities)
            .where(identities.c.account_id == account_id)
            .order_by(identities.c.create_time, identities.c.id)
        )
        return Account(
            id=row.id,
            display_name=row.display_name,
            email=row.email,
            languages=tuple(row.languages),
            identities=tuple(
                Identity(i.id, i.provider, i.subject, i.email, i.create_time) for i in found
            ),
            create_time=row.create_time,
            version=row.version,
        )

    async def save_account(self, account: Account) -> None:
        await self._db.execute(
            update(accounts)
            .where(accounts.c.id == account.id)
            .values(
                display_name=account.display_name,
                languages=list(account.languages),
                version=account.version,
            )
        )

    async def delete_account(self, account_id: UUID) -> None:
        await self._db.execute(delete(accounts).where(accounts.c.id == account_id))

    async def delete_identity(self, identity_id: UUID) -> None:
        await self._db.execute(delete(identities).where(identities.c.id == identity_id))

    async def add_session(self, session: Session, refresh_hash: bytes) -> None:
        await self._db.execute(
            insert(sessions).values(
                id=session.id,
                account_id=session.account_id,
                client_id=session.client_id,
                label=session.label,
                create_time=session.create_time,
                last_use_time=session.last_use_time,
            )
        )
        await self._add_refresh_token(session.id, refresh_hash, session.create_time)

    async def lock_account(self, account_id: UUID) -> bool:
        found = await self._db.execute(
            select(accounts.c.id).where(accounts.c.id == account_id).with_for_update()
        )
        return found.first() is not None

    async def use_refresh_token(self, refresh_hash: bytes, now: datetime) -> bool:
        # One statement: of two transactions using the same token, the second waits for
        # the first, then finds it used.
        used = await self._db.execute(
            update(refresh_tokens)
            .where(refresh_tokens.c.hash == refresh_hash, refresh_tokens.c.use_time.is_(None))
            .values(use_time=now)
            .returning(refresh_tokens.c.hash)
        )
        return used.first() is not None

    async def refresh_token_session(self, refresh_hash: bytes) -> UUID | None:
        return (
            await self._db.execute(
                select(refresh_tokens.c.session_id).where(refresh_tokens.c.hash == refresh_hash)
            )
        ).scalar_one_or_none()

    async def session(self, session_id: UUID, *, lock: bool) -> Session | None:
        query = select(sessions).where(sessions.c.id == session_id)
        row = (await self._db.execute(query.with_for_update() if lock else query)).one_or_none()
        return None if row is None else _session(row)

    async def continue_session(
        self, session_id: UUID, refresh_hash: bytes, now: datetime, forget_before: datetime
    ) -> None:
        await self._add_refresh_token(session_id, refresh_hash, now)
        await self._db.execute(
            update(sessions).where(sessions.c.id == session_id).values(last_use_time=now)
        )
        await self._db.execute(
            delete(refresh_tokens).where(
                refresh_tokens.c.session_id == session_id,
                refresh_tokens.c.use_time < forget_before,
            )
        )

    async def end_session(self, session_id: UUID, *, account_id: UUID | None) -> bool:
        query = delete(sessions).where(sessions.c.id == session_id)
        if account_id is not None:
            query = query.where(sessions.c.account_id == account_id)
        return (await self._db.execute(query.returning(sessions.c.id))).first() is not None

    async def end_idle_sessions(self, account_id: UUID, *, unused_since: datetime) -> None:
        await self._db.execute(
            delete(sessions).where(
                sessions.c.account_id == account_id, sessions.c.last_use_time < unused_since
            )
        )

    async def sessions(
        self, account_id: UUID, *, used_since: datetime, after: UUID | None, limit: int
    ) -> list[Session]:
        query = (
            select(sessions)
            .where(sessions.c.account_id == account_id, sessions.c.last_use_time >= used_since)
            .order_by(sessions.c.id.desc())  # UUIDv7: newest first
            .limit(limit)
        )
        if after is not None:
            query = query.where(sessions.c.id < after)
        return [_session(row) for row in await self._db.execute(query)]

    async def _add_refresh_token(
        self, session_id: UUID, refresh_hash: bytes, now: datetime
    ) -> None:
        await self._db.execute(
            insert(refresh_tokens).values(hash=refresh_hash, session_id=session_id, create_time=now)
        )


def _session(row: Row[Any]) -> Session:
    return Session(
        id=row.id,
        account_id=row.account_id,
        client_id=row.client_id,
        label=row.label,
        create_time=row.create_time,
        last_use_time=row.last_use_time,
    )


def _violates(error: IntegrityError, constraint: str) -> bool:
    """Whether ``error`` is a unique violation of ``constraint``."""
    original = error.orig
    if getattr(original, "sqlstate", None) != _UNIQUE_VIOLATION:
        return False
    # SQLAlchemy's asyncpg adapter raises its own error from asyncpg's, which names it.
    return getattr(getattr(original, "__cause__", None), "constraint_name", None) == constraint
