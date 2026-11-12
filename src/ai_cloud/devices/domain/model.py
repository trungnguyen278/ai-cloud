# SPDX-License-Identifier: MIT
"""The devices' types and rules, without I/O (ai-platform ADR-0012 and ADR-0013, and its app
API 1.5).

A device belongs to a home, not to the person who added it. What binds it there is the
cloud's record of its identifier, which it made itself, and of its key, which its
registration carried. The owner or an admin of the home asks for a challenge, which the
device signs with that key at set-up, and the app relays: the challenge carries the home,
which the device never learns, and works once. The app relays the device's own name with it,
which the device keeps in the home until the owner or an admin renames it.
"""

import hashlib
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from ai_cloud.shared.etags import weak_etag

# Random bytes in a challenge (the app API's $defs/challenge).
CHALLENGE_BYTES = 32
# A device's identifier, as the protocols write a UUID: in either case (RFC 9562, 4).
_IDENTIFIER = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class Access(Enum):
    """What a person in a home may do with its devices (the app API's What each role may
    do): everyone sees them; the owner and admins add, rename and remove them."""

    SEE = "see"
    MANAGE = "manage"


@dataclass(frozen=True)
class DeviceKey:
    """A device's public key, as its registration carried it."""

    # Its JWK thumbprint (RFC 7638), which the device's assertions name as their kid.
    thumbprint: str
    # The JWK: kty, crv, x and y (RFC 7518, 6.2.1).
    jwk: dict[str, str]


@dataclass(frozen=True)
class Description:
    """What a device says it is, in its hello on the cloud link (The session, hello)."""

    # Its kind, such as robot.
    kind: str
    # Its model, as its maker names it.
    model: str
    # The version of its software.
    software: str


@dataclass(frozen=True)
class Device:
    """A device in a home."""

    # The identifier the device made itself (ai-platform ADR-0004).
    id: UUID
    home_id: UUID
    # When it was registered into the home. A device removed and registered again is a new
    # registration, with a time of its own.
    create_time: datetime
    # What its latest hello said it is; None until its first.
    description: Description | None
    # Its name in the home, which people see: None for one that an app of the app API 1.4
    # registered, until someone names it.
    name: str | None
    # Its version, which its name alone changes: its connections and its hello do not, so
    # that a rename does not race the device.
    version: int

    @property
    def etag(self) -> str:
        return weak_etag(self.version)


@dataclass(frozen=True)
class Shown:
    """A device as the app sees it: the registry's record, and whether its control channel is
    open now."""

    device: Device
    connected: bool


@dataclass(frozen=True)
class HeldKey:
    """A key the cloud holds, and the device that holds it."""

    device_id: UUID
    # The JWK: kty, crv, x and y.
    jwk: dict[str, str]


@dataclass(frozen=True)
class DeviceToken:
    """An access token for a device, and the seconds it is valid."""

    access_token: str
    expires_in: int


@dataclass(frozen=True)
class TokenHolder:
    """Who a device's access token speaks for: the device, and when the token was issued, in
    seconds since the epoch."""

    device_id: UUID
    issued: int


@dataclass(frozen=True)
class Opening:
    """A connection the registry lets open: the device, and the registration it is under."""

    device_id: UUID
    registered: datetime


@dataclass(frozen=True)
class Registration:
    """What a device's registration says, once verified: who the device is, and its key."""

    device_id: UUID
    key: DeviceKey


@dataclass(frozen=True)
class Challenge:
    """A challenge to register a device into a home, issued to one account. The cloud keeps
    its hash, never the challenge itself."""

    home_id: UUID
    account_id: UUID
    create_time: datetime
    expire_time: datetime


@dataclass(frozen=True)
class IssuedChallenge:
    """What the app gets to register a device: the challenge, the address that devices use,
    and the seconds the challenge is valid."""

    challenge: str
    server: str
    expires_in: int


@dataclass(frozen=True)
class Registered:
    """A registration the cloud accepted: the device in the home, and whether it is new
    there, or was there already with this identifier and this key."""

    device: Shown
    new: bool


@dataclass(frozen=True)
class Answer:
    """What a request was answered, as a retry with its Idempotency-Key gets it again: its
    status, and its body."""

    status: int
    body: dict[str, Any]


@dataclass(frozen=True)
class FirstAnswer:
    """The first answer to a request with an Idempotency-Key, and the SHA-256 of what that
    request asked."""

    fingerprint: bytes
    answer: Answer


class Binding(Enum):
    """Where a registration's device stands, against what the cloud holds."""

    # Neither its identifier nor its key is known: it is a new device.
    NEW = "new"
    # It is in the home already, with this identifier and this key: one that lost its state.
    SAME = "same"
    # It is in another home, or its key or its identifier is another device's.
    TAKEN = "taken"


def binding(home_id: UUID, holder: Device | None, key_holder: UUID | None) -> Binding:
    """Where a registration into ``home_id`` stands. ``holder`` is the device the cloud holds
    with the registration's identifier, and ``key_holder`` the identifier of the device that
    holds its key, if the cloud holds either (ai-platform ADR-0012, decision 5)."""
    if holder is None and key_holder is None:
        return Binding.NEW
    if holder is not None and holder.home_id == home_id and key_holder == holder.id:
        return Binding.SAME
    return Binding.TAKEN


def renamed(device: Device, name: str) -> Device:
    """The device with that name, at its next version."""
    return replace(device, name=name, version=device.version + 1)


def named_again(device: Device, name: str | None) -> Device | None:
    """The device that registers again into its home, with the name its registration brought,
    if it had none; ``None`` if it keeps what it has (the app API, Adding a device)."""
    if device.name is not None or name is None:
        return None
    return renamed(device, name)


def identifier(value: object) -> UUID | None:
    """The device identifier that a token's claim holds, compared as a UUID, whatever the
    case of its letters; ``None`` for anything else."""
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        return None
    return UUID(value)


def new_challenge() -> str:
    """A challenge: random bytes, base64url without padding."""
    return secrets.token_urlsafe(CHALLENGE_BYTES)


def challenge_hash(challenge: str) -> bytes:
    """What the cloud keeps of a challenge: its SHA-256."""
    return hashlib.sha256(challenge.encode()).digest()
