# SPDX-License-Identifier: MIT
"""What the accounts need from outside: their store, the nonces, and the providers that
vouch for people."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from ai_cloud.accounts.domain.model import (
    Account,
    FirstAnswer,
    Home,
    HomeView,
    IdempotencyKey,
    Identity,
    Invitation,
    Member,
    Session,
    SignIn,
)


@dataclass(frozen=True)
class IdentityOwner:
    """An identity as stored, and the account it signs in to."""

    identity_id: UUID
    account_id: UUID
    email: str | None


class Transaction(Protocol):
    """Reads and writes that stand or fall together. Each raises nothing of its own but
    ``IdentityConflict``."""

    async def identity(self, provider: str, subject: str) -> IdentityOwner | None: ...

    async def add_account(self, account: Account) -> None:
        """Store ``account`` with its identities; ``IdentityConflict`` if one is stored."""
        ...

    async def add_identity(self, account_id: UUID, identity: Identity) -> None:
        """``IdentityConflict`` if the identity is stored already."""
        ...

    async def set_identity_email(self, identity_id: UUID, email: str) -> None: ...

    async def account(self, account_id: UUID, *, lock: bool) -> Account | None:
        """The account, with its identities; ``lock`` holds it until the transaction
        ends, so that changes to it happen one after the other."""
        ...

    async def save_account(self, account: Account) -> None:
        """Write the account's name, languages and version."""
        ...

    async def delete_account(self, account_id: UUID) -> None:
        """Delete the account, with its identities and sessions."""
        ...

    async def delete_identity(self, identity_id: UUID) -> None: ...

    async def add_session(self, session: Session, refresh_hash: bytes) -> None:
        """Store ``session``, whose refresh token is now the one ``refresh_hash`` names."""
        ...

    async def lock_account(self, account_id: UUID) -> bool:
        """Hold the account until the transaction ends; whether it exists."""
        ...

    async def use_refresh_token(self, refresh_hash: bytes, now: datetime) -> bool:
        """Mark the refresh token used, if it was not; whether it was not. Of two
        transactions, one only can use a token."""
        ...

    async def refresh_token_session(self, refresh_hash: bytes) -> UUID | None:
        """The session of a refresh token, used or not."""
        ...

    async def session(self, session_id: UUID, *, lock: bool) -> Session | None: ...

    async def may_refresh_again(
        self, session_id: UUID, refresh_hash: bytes, since: datetime
    ) -> bool:
        """Whether the session's current refresh token replaced this used one, first used
        at ``since`` or after: the answer that gave the current one was lost, and this one
        refreshes again (ai-platform ADR-0011)."""
        ...

    async def supersede_current_refresh_token(self, session_id: UUID, now: datetime) -> None:
        """Mark the session's current refresh token used, as if exchanged at ``now``: it
        works no more, and one presented again ends the session."""
        ...

    async def continue_session(
        self,
        session_id: UUID,
        refresh_hash: bytes,
        now: datetime,
        forget_before: datetime,
        replaces: bytes,
    ) -> None:
        """Give the session its next refresh token, which replaces ``replaces``, note its
        use, and forget the tokens it used before ``forget_before``."""
        ...

    async def end_session(self, session_id: UUID, *, account_id: UUID | None) -> bool:
        """Delete the session, with its refresh tokens, if it is ``account_id``'s (any
        account's for ``None``); whether it was there."""
        ...

    async def end_idle_sessions(self, account_id: UUID, *, unused_since: datetime) -> None: ...

    async def sessions(
        self, account_id: UUID, *, used_since: datetime, after: UUID | None, limit: int
    ) -> list[Session]:
        """The account's sessions used since ``used_since``, newest first, from the one
        after ``after``."""
        ...

    # Homes. A change to a home, or to what is in it, holds the home first. What holds an
    # account too holds it before any home, and homes in the order of their identifiers, so
    # that no two transactions wait for each other.

    async def add_home(self, home: Home) -> None: ...

    async def home(self, home_id: UUID, *, lock: bool) -> Home | None:
        """The home; ``lock`` holds it until the transaction ends, so that changes to it
        and to what is in it happen one after the other."""
        ...

    async def save_home(self, home: Home) -> None:
        """Write the home's name, time zone, languages and version."""
        ...

    async def delete_home(self, home_id: UUID) -> None:
        """Delete the home, with its people and its invitations."""
        ...

    async def homes_of(self, account_id: UUID, *, after: UUID | None, limit: int) -> list[HomeView]:
        """The homes the account is in, oldest first, from the one after ``after``."""
        ...

    async def home_ids_of(self, account_id: UUID) -> list[UUID]:
        """Every home the account is in, in the order of their identifiers."""
        ...

    async def count_homes(self, account_id: UUID) -> int:
        """How many homes the account is in."""
        ...

    # The people in a home.

    async def add_member(self, member: Member) -> None: ...

    async def member(self, home_id: UUID, member_id: UUID) -> Member | None:
        """The person ``member_id`` names in that home; none in any other."""
        ...

    async def member_of_account(self, home_id: UUID, account_id: UUID) -> Member | None:
        """The account's person in that home."""
        ...

    async def members(self, home_id: UUID, *, after: UUID | None, limit: int) -> list[Member]:
        """The home's people, oldest first, from the one after ``after``."""
        ...

    async def count_members(self, home_id: UUID) -> int:
        """How many people the home has, profiles included."""
        ...

    async def has_other_accounts(self, home_id: UUID, member_id: UUID) -> bool:
        """Whether the home has a person with an account other than ``member_id``."""
        ...

    async def save_member(self, member: Member) -> None:
        """Write the person's name, role and version."""
        ...

    async def delete_member(self, member_id: UUID) -> None:
        """Delete the person, with the invitations they made or used."""
        ...

    # Invitations.

    async def add_invitation(self, invitation: Invitation, code_hash: bytes) -> None: ...

    async def invitation_by_code(self, code_hash: bytes) -> Invitation | None:
        """The invitation whose code has that hash, whatever its state."""
        ...

    async def open_invitations(
        self, home_id: UUID, now: datetime, *, after: UUID | None, limit: int
    ) -> list[Invitation]:
        """The home's unused and unexpired invitations, oldest first, from the one after
        ``after``."""
        ...

    async def count_open_invitations(self, home_id: UUID, now: datetime) -> int: ...

    async def use_invitation(self, invitation_id: UUID, member_id: UUID) -> None:
        """Note that the invitation brought ``member_id`` in."""
        ...

    async def revoke_invitation(self, home_id: UUID, invitation_id: UUID, now: datetime) -> bool:
        """Delete the invitation, if it is that home's, unused and unexpired; whether it
        was."""
        ...

    async def revoke_invitations_by(self, member_id: UUID) -> None:
        """Delete the unused invitations that person made."""
        ...

    async def forget_expired_invitations(self, home_id: UUID, now: datetime) -> None: ...

    # The first answers to requests with an Idempotency-Key.

    async def hold_idempotency_key(self, account_id: UUID, key: UUID) -> bool:
        """Hold the account's key until the transaction ends, without waiting; whether no
        other transaction holds it."""
        ...

    async def first_answer(
        self, account_id: UUID, key: UUID, *, since: datetime
    ) -> FirstAnswer | None:
        """What the key's first request was answered, if it came since ``since``."""
        ...

    async def add_first_answer(
        self,
        account_id: UUID,
        key: IdempotencyKey,
        member_id: UUID,
        body: dict[str, Any],
        now: datetime,
    ) -> None:
        """Keep ``body``, the answer to a request that made a home or a person, in place of
        an older answer under the same key. It holds their names, so it goes with
        ``member_id``."""
        ...


class Store(Protocol):
    """The accounts' durable data."""

    def transaction(self) -> AbstractAsyncContextManager[Transaction]:
        """A transaction, committed when the block ends and rolled back if it raises."""
        ...


class Nonces(Protocol):
    """The nonces the cloud issued, until they are used or expire."""

    async def issue(self, nonce: str, lifetime: int) -> None: ...

    async def consume(self, nonce: str) -> bool:
        """Whether ``nonce`` was issued and is unused and unexpired; it is then used."""
        ...


class IdTokens(Protocol):
    """The ID tokens of the providers the cloud trusts."""

    async def verify(self, token: str) -> SignIn:
        """What ``token`` says, once its issuer, signature, audience and expiry are
        checked; its nonce is the caller's to check. ``InvalidGrant`` if it fails,
        ``ProviderUnavailable`` if its provider's keys cannot be had."""
        ...


class DevelopmentIdTokens(Protocol):
    """The development environment's provider, which signs test people in."""

    def issue(self, nonce: str, display_name: str, email: str | None) -> str:
        """An ID token for the test person ``email`` names, or ``display_name`` without
        one."""
        ...
