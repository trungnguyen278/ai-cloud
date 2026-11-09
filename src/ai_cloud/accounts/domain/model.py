# SPDX-License-Identifier: MIT
"""The accounts' types and rules, without I/O (ai-platform ADR-0008 and ADR-0009, the app
API 1.1).

An account is a person. The ways they sign in are its identities: a provider, and the
provider's identifier for them (its ``sub``). A session is one app on one phone, signed
in, and holds one refresh token at a time; the ones before it are remembered until they
could no longer be valid, so that one presented again ends the session (RFC 9700).

A home is where devices and people belong. A member is one person in one home, with a name
that is that home's: a person with an account, who has a role, or a profile, who never
signs in. A person joins with an invitation's code, of which the cloud keeps the hash.
"""

import hashlib
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

# Fixed by the app API (platform/protocols/app/schema.json).
DISPLAY_NAME_MAX = 100  # $defs/display_name, maxLength
MAX_IDENTITIES = 16  # $defs/account, identities' maxItems

# Random bytes in a nonce (the app API's $defs/nonce), in a refresh token
# (docs/decisions/0008-data-and-tokens.md, decision 4) and in an invitation's code (the app
# API's $defs/invitation_code).
NONCE_BYTES = 32
REFRESH_TOKEN_BYTES = 32
INVITATION_CODE_BYTES = 32


class Role(StrEnum):
    """What a person with an account may do in a home (ai-platform ADR-0009, decision 2)."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


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
        return _etag(self.version)


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
class Home:
    id: UUID
    name: str
    # An IANA time zone.
    time_zone: str
    # What its devices speak until their own settings say otherwise, most preferred first
    # (ai-platform ADR-0002).
    languages: tuple[str, ...]
    create_time: datetime
    # Raised by a change to the fields above, and by handing the home over: it makes the
    # ETag.
    version: int

    @property
    def etag(self) -> str:
        return _etag(self.version)


@dataclass(frozen=True)
class NewHome:
    """What a person gives a home they create."""

    name: str
    time_zone: str
    languages: tuple[str, ...]


@dataclass(frozen=True)
class HomeChanges:
    """What a person changes in a home; ``None`` leaves a field as it is."""

    name: str | None
    time_zone: str | None
    languages: tuple[str, ...] | None


@dataclass(frozen=True)
class Member:
    """One person in one home."""

    id: UUID
    home_id: UUID
    # None for a profile, who never signs in.
    account_id: UUID | None
    # This home's name for the person. It starts as their account's, and does not follow it.
    display_name: str
    # None for a profile.
    role: Role | None
    create_time: datetime
    # Raised by every change to the person: it makes the ETag.
    version: int

    @property
    def etag(self) -> str:
        return _etag(self.version)


@dataclass(frozen=True)
class MemberChanges:
    """What is changed about a person in a home; ``None`` leaves a field as it is."""

    display_name: str | None
    role: Role | None


@dataclass(frozen=True)
class HomeView:
    """A home, as one of its people sees it."""

    home: Home
    # The person who asks.
    member: Member


@dataclass(frozen=True)
class Membership:
    """An account's person in a home, as the other modules see them."""

    member_id: UUID
    role: Role
    # Whether they may change the home and what is in it (may_manage).
    manages: bool


@dataclass(frozen=True)
class Invitation:
    id: UUID
    home_id: UUID
    # The person who made it.
    invited_by: UUID
    # That person's name in the home.
    invited_by_name: str
    create_time: datetime
    expire_time: datetime
    # The person it brought in; None while it is unused.
    used_by: UUID | None


@dataclass(frozen=True)
class NewInvitation:
    """An invitation just made, and its code, which is never seen again."""

    invitation: Invitation
    code: str


@dataclass(frozen=True)
class InvitationPreview:
    """What a person sees of an invitation before they agree."""

    home_name: str
    invited_by: str
    expire_time: datetime


@dataclass(frozen=True)
class FirstAnswer:
    """What the first request with an Idempotency-Key was answered."""

    fingerprint: bytes
    body: dict[str, Any]


def new_nonce() -> str:
    """A nonce: random bytes, base64url without padding."""
    return secrets.token_urlsafe(NONCE_BYTES)


def new_refresh_token() -> str:
    """A refresh token: random bytes, base64url without padding."""
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def new_invitation_code() -> str:
    """An invitation's code: random bytes, base64url without padding."""
    return secrets.token_urlsafe(INVITATION_CODE_BYTES)


def token_hash(token: str) -> bytes:
    """What the cloud keeps of a refresh token or an invitation's code: its SHA-256, never
    the thing itself."""
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


def may_manage(member: Member) -> bool:
    """Whether ``member`` may change the home and what is in it: invite people, add
    profiles, and add and remove devices (the app API's What each role may do)."""
    return member.role in (Role.OWNER, Role.ADMIN)


def may_change(actor: Member, target: Member) -> bool:
    """Whether ``actor`` may rename or remove ``target``, who is someone else: the owner
    anyone, and an admin members and profiles."""
    match actor.role:
        case Role.OWNER:
            return True
        case Role.ADMIN:
            return target.role in (Role.MEMBER, None)
        case _:
            return False


def invitation_usable(invitation: Invitation, asker: Member | None, now: datetime) -> bool:
    """Whether a person may go on with an invitation they hold the code of. ``asker`` is
    that person in its home, if they are in it. A code works once, but the person it
    brought in may try it again, so that a retry after a lost answer ends in the same
    place."""
    if now >= invitation.expire_time:
        return False
    return invitation.used_by is None or (asker is not None and invitation.used_by == asker.id)


def etag_matches(if_match: str, etag: str) -> bool:
    """Whether ``If-Match`` names ``etag``: as written, the app API's ETags being compared
    as the cloud gives them; ``*`` matches any."""
    return any(candidate.strip() in ("*", etag) for candidate in if_match.split(","))


def _etag(version: int) -> str:
    return f'W/"{version}"'
