# SPDX-License-Identifier: MIT
"""A device's assertion: the JWT it signs with its key to ask for an access token, OAuth 2.0's
client authentication with a JWT (RFC 7523, 2.2; platform/protocols/cloud/README.md, Tokens
from the server).

The cloud reads which key it names first, to find the device that holds it, then verifies
the rest with that key. Errors say which check failed, never what the assertion holds.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import jwt

from ai_cloud.devices.domain.errors import AssertionInvalid
from ai_cloud.devices.domain.model import identifier
from ai_cloud.shared.jose import ALGORITHM, failure, p256_public_key

# Fixed by the cloud link (platform/protocols/cloud/README.md, Tokens from the server).
TOKEN_PATH = "/device/token"
GRANT_TYPE = "client_credentials"  # RFC 6749, 4.4.2
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"  # RFC 7523, 2.2
LONGEST = 300  # Seconds from an assertion's iat to its exp, at most: "at most 5 minutes later"
_TYPE = "JWT"  # its header's typ; media types compare without case (RFC 7519, 5.1)
_REQUIRED = ("iss", "sub", "aud", "iat", "exp", "jti")
# A key's thumbprint, as its kid: the SHA-256 of its JWK, in base64url (RFC 7638).
_THUMBPRINT = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class Asserted:
    """What an assertion that verified says: the device it speaks for, its identifier, and
    when it expires."""

    device_id: UUID
    jti: str
    expire_time: datetime


def key_of(assertion: str) -> str:
    """The thumbprint of the key that the assertion names, its kid, before anything is
    verified. ``AssertionInvalid`` if it names none, or is not a device's assertion."""
    try:
        header = jwt.get_unverified_header(assertion)
    except jwt.PyJWTError:
        raise AssertionInvalid("malformed") from None
    typ = header.get("typ")
    if not isinstance(typ, str) or typ.upper() != _TYPE:
        raise AssertionInvalid("not a JWT")
    kid = header.get("kid")
    if not isinstance(kid, str) or _THUMBPRINT.fullmatch(kid) is None:
        raise AssertionInvalid("names no key")
    return kid


def verify(
    assertion: str, jwk: Mapping[str, str], device_id: UUID, *, audience: str, leeway: float
) -> Asserted:
    """What the assertion says, once it is checked against ``jwk``, the key of ``device_id``:
    its signature, its audience, its times within ``leeway`` seconds, and that it speaks for
    that device. ``AssertionInvalid`` otherwise. Whether it was used before is the caller's
    to check."""
    key, _ = p256_public_key(jwk)
    try:
        claims = jwt.decode(
            assertion,
            key,
            algorithms=[ALGORITHM],
            audience=audience,
            leeway=leeway,
            options={"require": list(_REQUIRED), "strict_aud": True},
        )
    except jwt.PyJWTError as error:
        raise AssertionInvalid(failure(error)) from None
    issued, expires = _instant(claims["iat"]), _instant(claims["exp"])
    if issued is None or expires is None:
        raise AssertionInvalid("its times are not numbers")
    if expires - issued > LONGEST:
        raise AssertionInvalid(f"it lives longer than {LONGEST} s")
    if not identifier(claims["iss"]) == identifier(claims["sub"]) == device_id:
        raise AssertionInvalid("it does not speak for its key's device")
    jti = claims["jti"]
    if not jti:
        raise AssertionInvalid("its jti is empty")
    return Asserted(device_id, jti, datetime.fromtimestamp(expires, UTC))


def _instant(value: Any) -> float | None:
    """A NumericDate (RFC 7519, 2): a JSON number. PyJWT also takes the text of one, or
    ``true``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
