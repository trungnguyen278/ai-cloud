# SPDX-License-Identifier: MIT
"""What the accounts need from outside: their store, the nonces, and the providers that
vouch for people."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ai_cloud.accounts.domain.model import Account, Identity, Session, SignIn


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

    async def continue_session(
        self, session_id: UUID, refresh_hash: bytes, now: datetime, forget_before: datetime
    ) -> None:
        """Give the session its next refresh token, note its use, and forget the tokens
        it used before ``forget_before``."""
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
