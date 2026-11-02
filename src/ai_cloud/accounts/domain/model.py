# SPDX-License-Identifier: MIT
"""The accounts' types and rules, without I/O (ai-platform ADR-0008, the app API 1.0).

An account is a person. The ways they sign in are its identities: a provider, and the
provider's identifier for them (its ``sub``). A session is one app on one phone, signed
in, and holds one refresh token at a time; the ones before it are remembered until they
could no longer be valid, so that one presented again ends the session (RFC 9700).
"""

import hashlib
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

# Fixed by the app API (platform/protocols/app/schema.json).
DISPLAY_NAME_MAX = 100  # $defs/display_name, maxLength
MAX_IDENTITIES = 16  # $defs/account, identities' maxItems

# Random bytes in a nonce (the app API's $defs/nonce) and in a refresh token
# (docs/decisions/0008-data-and-tokens.md, decision 4).
NONCE_BYTES = 32
REFRESH_TOKEN_BYTES = 32


@dataclass(frozen=True)
class Identity:
    """A way to sign in to an account."""

    id: UUID
    provider: str
    subject: str
    email: str | None
    create_time: datetime


@dataclass(frozen=True)
class Account:
    id: UUID
    display_name: str
    email: str | None
    # Most preferred first (ai-platform ADR-0002).
    languages: tuple[str, ...]
    identities: tuple[Identity, ...]
    create_time: datetime
    # Raised by every change, the identities' included: it makes the ETag.
    version: int

    @property
    def etag(self) -> str:
        return f'W/"{self.version}"'


@dataclass(frozen=True)
class Session:
    id: UUID
    account_id: UUID
    client_id: str
    # What the person sees in their list of sessions, such as the phone's model.
    label: str | None
    create_time: datetime
    last_use_time: datetime


@dataclass(frozen=True)
class SignIn:
    """What a provider's ID token says, once verified."""

    provider: str
    subject: str
    email: str | None
    name: str | None
    issue_time: datetime
    nonce: str


@dataclass(frozen=True)
class Principal:
    """Who a request's access token speaks for: an account, in one of its sessions."""

    account_id: UUID
    session_id: UUID


@dataclass(frozen=True)
class Tokens:
    """What signing in or refreshing gives the app."""

    access_token: str
    # Seconds until the access token expires.
    expires_in: int
    refresh_token: str


@dataclass(frozen=True)
class AccountChanges:
    """What a person changes in their account; ``None`` leaves a field as it is."""

    display_name: str | None
    languages: tuple[str, ...] | None


@dataclass(frozen=True)
class SessionPage:
    sessions: tuple[Session, ...]
    # Empty on the last page.
    next_page_token: str


def new_nonce() -> str:
    """A nonce: random bytes, base64url without padding."""
    return secrets.token_urlsafe(NONCE_BYTES)


def new_refresh_token() -> str:
    """A refresh token: random bytes, base64url without padding."""
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def token_hash(token: str) -> bytes:
    """What the cloud keeps of a refresh token: its SHA-256, never the token."""
    return hashlib.sha256(token.encode()).digest()


def has_control_characters(text: str) -> bool:
    """Whether ``text`` holds control characters, which have no place in a name and
    which PostgreSQL refuses in part (NUL)."""
    return any(unicodedata.category(character) == "Cc" for character in text)


def display_name_for(sign_in: SignIn) -> str | None:
    """The name an account starts with: the one the provider gave, or else its email
    before the @. ``None`` if the provider gave neither."""
    name = "".join(c for c in sign_in.name or "" if unicodedata.category(c) != "Cc").strip()
    if not name and sign_in.email:
        name = sign_in.email.partition("@")[0].strip()
    return name[:DISPLAY_NAME_MAX] or None
