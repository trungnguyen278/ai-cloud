# SPDX-License-Identifier: MIT
"""The accounts' store, in PostgreSQL (docs/decisions/0008-data-and-tokens.md)."""

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, delete, exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as upsert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ai_cloud.accounts.db.tables import (
    OUTBOX,
    accounts,
    homes,
    idempotency_keys,
    identities,
    invitations,
    members,
    refresh_tokens,
    sessions,
)
from ai_cloud.accounts.domain.errors import IdentityConflict
from ai_cloud.accounts.domain.events import AccountsEvent
from ai_cloud.accounts.domain.model import (
    Account,
    FirstAnswer,
    Home,
    HomeView,
    IdempotencyKey,
    Identity,
    Invitation,
    Member,
    Role,
    Session,
)
from ai_cloud.accounts.ports import IdentityOwner

_UNIQUE_VIOLATION = "23505"  # PostgreSQL's SQLSTATE for unique_violation
_IDENTITY_KEY = "uq_identities_provider_subject"

# A person's columns beside their home's, whose names they share.
_MEMBER_PREFIX = "member_"
_MEMBER_BESIDE_HOME = [column.label(_MEMBER_PREFIX + column.name) for column in members.c]
# An invitation, with the name of the person who made it.
_INVITATION = select(*invitations.c, members.c.display_name.label("invited_by_name")).join_from(
    invitations, members, members.c.id == invitations.c.invited_by
)


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

    async def emit(self, event: AccountsEvent) -> None:
        await OUTBOX.write(self._db, event)

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

    async def may_refresh_again(
        self, session_id: UUID, refresh_hash: bytes, since: datetime
    ) -> bool:
        presented = (
            await self._db.execute(
                select(refresh_tokens.c.use_time).where(
                    refresh_tokens.c.hash == refresh_hash,
                    refresh_tokens.c.session_id == session_id,
                )
            )
        ).scalar_one_or_none()
        if presented is None or presented < since:
            return False
        replaced = (
            await self._db.execute(
                select(refresh_tokens.c.replaces).where(
                    refresh_tokens.c.session_id == session_id,
                    refresh_tokens.c.use_time.is_(None),
                )
            )
        ).scalar_one_or_none()
        return replaced == refresh_hash

    async def supersede_current_refresh_token(self, session_id: UUID, now: datetime) -> None:
        await self._db.execute(
            update(refresh_tokens)
            .where(refresh_tokens.c.session_id == session_id, refresh_tokens.c.use_time.is_(None))
            .values(use_time=now)
        )

    async def continue_session(
        self,
        session_id: UUID,
        refresh_hash: bytes,
        now: datetime,
        forget_before: datetime,
        replaces: bytes,
    ) -> None:
        await self._add_refresh_token(session_id, refresh_hash, now, replaces=replaces)
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
        self, session_id: UUID, refresh_hash: bytes, now: datetime, replaces: bytes | None = None
    ) -> None:
        await self._db.execute(
            insert(refresh_tokens).values(
                hash=refresh_hash, session_id=session_id, create_time=now, replaces=replaces
            )
        )

    # Homes.

    async def add_home(self, home: Home) -> None:
        await self._db.execute(
            insert(homes).values(
                id=home.id,
                name=home.name,
                time_zone=home.time_zone,
                languages=list(home.languages),
                version=home.version,
                create_time=home.create_time,
            )
        )

    async def home(self, home_id: UUID, *, lock: bool) -> Home | None:
        query = select(homes).where(homes.c.id == home_id)
        row = (await self._db.execute(query.with_for_update() if lock else query)).one_or_none()
        return None if row is None else _home(row)

    async def save_home(self, home: Home) -> None:
        await self._db.execute(
            update(homes)
            .where(homes.c.id == home.id)
            .values(
                name=home.name,
                time_zone=home.time_zone,
                languages=list(home.languages),
                version=home.version,
            )
        )

    async def delete_home(self, home_id: UUID) -> None:
        await self._db.execute(delete(homes).where(homes.c.id == home_id))

    async def homes_of(self, account_id: UUID, *, after: UUID | None, limit: int) -> list[HomeView]:
        query = (
            select(homes, *_MEMBER_BESIDE_HOME)
            .join_from(homes, members, members.c.home_id == homes.c.id)
            .where(members.c.account_id == account_id)
            .order_by(homes.c.id)  # UUIDv7: oldest first
            .limit(limit)
        )
        if after is not None:
            query = query.where(homes.c.id > after)
        return [
            HomeView(_home(row), _member(row, _MEMBER_PREFIX))
            for row in await self._db.execute(query)
        ]

    async def home_ids_of(self, account_id: UUID) -> list[UUID]:
        found = await self._db.execute(
            select(members.c.home_id)
            .where(members.c.account_id == account_id)
            .order_by(members.c.home_id)
        )
        return list(found.scalars())

    async def count_homes(self, account_id: UUID) -> int:
        return await self._count(members, members.c.account_id == account_id)

    # The people in a home.

    async def add_member(self, member: Member) -> None:
        await self._db.execute(
            insert(members).values(
                id=member.id,
                home_id=member.home_id,
                account_id=member.account_id,
                display_name=member.display_name,
                role=member.role,
                version=member.version,
                create_time=member.create_time,
            )
        )

    async def member(self, home_id: UUID, member_id: UUID) -> Member | None:
        row = (
            await self._db.execute(
                select(members).where(members.c.home_id == home_id, members.c.id == member_id)
            )
        ).one_or_none()
        return None if row is None else _member(row)

    async def member_of_account(self, home_id: UUID, account_id: UUID) -> Member | None:
        row = (
            await self._db.execute(
                select(members).where(
                    members.c.home_id == home_id, members.c.account_id == account_id
                )
            )
        ).one_or_none()
        return None if row is None else _member(row)

    async def members(self, home_id: UUID, *, after: UUID | None, limit: int) -> list[Member]:
        query = (
            select(members)
            .where(members.c.home_id == home_id)
            .order_by(members.c.id)  # UUIDv7: oldest first
            .limit(limit)
        )
        if after is not None:
            query = query.where(members.c.id > after)
        return [_member(row) for row in await self._db.execute(query)]

    async def count_members(self, home_id: UUID) -> int:
        return await self._count(members, members.c.home_id == home_id)

    async def has_other_accounts(self, home_id: UUID, member_id: UUID) -> bool:
        found = await self._db.execute(
            select(
                exists().where(
                    members.c.home_id == home_id,
                    members.c.id != member_id,
                    members.c.account_id.is_not(None),
                )
            )
        )
        return found.scalar_one()

    async def save_member(self, member: Member) -> None:
        await self._db.execute(
            update(members)
            .where(members.c.id == member.id)
            .values(display_name=member.display_name, role=member.role, version=member.version)
        )

    async def delete_member(self, member_id: UUID) -> None:
        await self._db.execute(delete(members).where(members.c.id == member_id))

    # Invitations.

    async def add_invitation(self, invitation: Invitation, code_hash: bytes) -> None:
        await self._db.execute(
            insert(invitations).values(
                id=invitation.id,
                code_hash=code_hash,
                home_id=invitation.home_id,
                invited_by=invitation.invited_by,
                create_time=invitation.create_time,
                expire_time=invitation.expire_time,
                used_by=invitation.used_by,
            )
        )

    async def invitation_by_code(self, code_hash: bytes) -> Invitation | None:
        row = (
            await self._db.execute(_INVITATION.where(invitations.c.code_hash == code_hash))
        ).one_or_none()
        return None if row is None else _invitation(row)

    async def open_invitations(
        self, home_id: UUID, now: datetime, *, after: UUID | None, limit: int
    ) -> list[Invitation]:
        query = (
            _INVITATION.where(
                invitations.c.home_id == home_id,
                invitations.c.used_by.is_(None),
                invitations.c.expire_time > now,
            )
            .order_by(invitations.c.id)  # UUIDv7: oldest first
            .limit(limit)
        )
        if after is not None:
            query = query.where(invitations.c.id > after)
        return [_invitation(row) for row in await self._db.execute(query)]

    async def count_open_invitations(self, home_id: UUID, now: datetime) -> int:
        return await self._count(
            invitations,
            invitations.c.home_id == home_id,
            invitations.c.used_by.is_(None),
            invitations.c.expire_time > now,
        )

    async def use_invitation(self, invitation_id: UUID, member_id: UUID) -> None:
        await self._db.execute(
            update(invitations).where(invitations.c.id == invitation_id).values(used_by=member_id)
        )

    async def revoke_invitation(self, home_id: UUID, invitation_id: UUID, now: datetime) -> bool:
        revoked = await self._db.execute(
            delete(invitations)
            .where(
                invitations.c.id == invitation_id,
                invitations.c.home_id == home_id,
                invitations.c.used_by.is_(None),
                invitations.c.expire_time > now,
            )
            .returning(invitations.c.id)
        )
        return revoked.first() is not None

    async def revoke_invitations_by(self, member_id: UUID) -> None:
        await self._db.execute(
            delete(invitations).where(
                invitations.c.invited_by == member_id, invitations.c.used_by.is_(None)
            )
        )

    async def forget_expired_invitations(self, home_id: UUID, now: datetime) -> None:
        await self._db.execute(
            delete(invitations).where(
                invitations.c.home_id == home_id, invitations.c.expire_time <= now
            )
        )

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
                select(idempotency_keys.c.fingerprint, idempotency_keys.c.answer).where(
                    idempotency_keys.c.account_id == account_id,
                    idempotency_keys.c.key == key,
                    idempotency_keys.c.create_time >= since,
                )
            )
        ).one_or_none()
        return None if row is None else FirstAnswer(row.fingerprint, row.answer)

    async def add_first_answer(
        self,
        account_id: UUID,
        key: IdempotencyKey,
        member_id: UUID,
        body: dict[str, Any],
        now: datetime,
    ) -> None:
        answer = {
            "fingerprint": key.fingerprint,
            "member_id": member_id,
            "answer": body,
            "create_time": now,
        }
        await self._db.execute(
            upsert(idempotency_keys)
            .values(account_id=account_id, key=key.key, **answer)
            .on_conflict_do_update(
                index_elements=[idempotency_keys.c.account_id, idempotency_keys.c.key],
                set_=answer,
            )
        )

    async def _count(self, table: Any, *conditions: Any) -> int:
        found = await self._db.execute(select(func.count()).select_from(table).where(*conditions))
        return found.scalar_one()


def idempotency_lock(account_id: UUID, key: UUID) -> int:
    """The advisory lock of an account's Idempotency-Key: a bigint, as PostgreSQL's locks
    are named by. Two keys that share one would only make a request try again."""
    digest = hashlib.sha256(account_id.bytes + key.bytes).digest()
    return int.from_bytes(digest[:8], signed=True)


def _home(row: Row[Any]) -> Home:
    return Home(
        id=row.id,
        name=row.name,
        time_zone=row.time_zone,
        languages=tuple(row.languages),
        create_time=row.create_time,
        version=row.version,
    )


def _member(row: Row[Any], prefix: str = "") -> Member:
    fields = row._mapping
    role = fields[prefix + "role"]
    return Member(
        id=fields[prefix + "id"],
        home_id=fields[prefix + "home_id"],
        account_id=fields[prefix + "account_id"],
        display_name=fields[prefix + "display_name"],
        role=None if role is None else Role(role),
        create_time=fields[prefix + "create_time"],
        version=fields[prefix + "version"],
    )


def _invitation(row: Row[Any]) -> Invitation:
    return Invitation(
        id=row.id,
        home_id=row.home_id,
        invited_by=row.invited_by,
        invited_by_name=row.invited_by_name,
        create_time=row.create_time,
        expire_time=row.expire_time,
        used_by=row.used_by,
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
