# SPDX-License-Identifier: MIT
"""A device's registration: the JWT it signs at set-up with its key, which it carries, for
the cloud's challenge (platform/protocols/setup/README.md, The registration).

The cloud reads its challenge first, to use it whatever follows, then verifies the rest.
Errors say which check failed, never what the registration holds.
"""

import re
from uuid import UUID

import jwt

from ai_cloud.devices.domain.errors import RegistrationInvalid
from ai_cloud.devices.domain.model import DeviceKey, Registration
from ai_cloud.shared.jose import ALGORITHM, failure, p256_public_key, thumbprint

# Fixed by set-up (platform/protocols/setup/README.md, The registration, and Limits).
REGISTRATION_TYPE = "device-registration+jwt"
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{16,128}")  # Limits: challenge
# The registration's claims: iss and sub, the device's identifier; aud, the server; nonce,
# the challenge.
_REQUIRED = ("iss", "sub", "aud", "nonce")
# A UUID's text, as the protocols write it, in either case (RFC 9562, 4).
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def challenge_of(registration: str) -> str | None:
    """The challenge a registration names, its nonce, before anything is verified; ``None``
    if it names none that the cloud could have issued."""
    try:
        claims = jwt.decode(registration, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    nonce = claims.get("nonce")
    return nonce if isinstance(nonce, str) and _CHALLENGE.fullmatch(nonce) else None


def verify(registration: str, *, audience: str) -> Registration:
    """Who the device is, and its key, once the registration's type, its key, its signature,
    its audience and its identifiers are checked. ``RegistrationInvalid`` otherwise. Its
    challenge is the caller's to check, as ``challenge_of`` reads it."""
    try:
        header = jwt.get_unverified_header(registration)
    except jwt.PyJWTError:
        raise RegistrationInvalid("malformed") from None
    # Explicit typing (RFC 8725, 3.11): no other JWT passes for a registration.
    if header.get("typ") != REGISTRATION_TYPE:
        raise RegistrationInvalid("not a device's registration")
    try:
        key, jwk = p256_public_key(header.get("jwk"))
    except ValueError as error:
        raise RegistrationInvalid(f"its key: {error}") from None
    try:
        claims = jwt.decode(
            registration,
            key,
            algorithms=[ALGORITHM],
            audience=audience,
            # No times: before it first connects, the device's clock may not be set. The
            # challenge makes the registration fresh.
            options={
                "require": list(_REQUIRED),
                "strict_aud": True,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
            },
        )
    except jwt.PyJWTError as error:
        raise RegistrationInvalid(failure(error)) from None
    issuer, subject = claims["iss"], claims["sub"]
    if not all(isinstance(each, str) and _UUID.fullmatch(each) for each in (issuer, subject)):
        raise RegistrationInvalid("iss and sub are not identifiers")
    if UUID(issuer) != UUID(subject):
        raise RegistrationInvalid("iss and sub differ")
    return Registration(UUID(subject), DeviceKey(thumbprint(jwk), jwk))
