# SPDX-License-Identifier: MIT
"""A device of the tests' own, with a key it made, as a robot's TPM makes one: it signs its
registration as set-up says (platform/protocols/setup/README.md, The registration), and the
tests make it sign what a broken or a forged one would."""

import json
import secrets
import uuid
from typing import Any
from uuid import UUID

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from ai_cloud.shared.jose import b64url, thumbprint

REGISTRATION_TYPE = "device-registration+jwt"
# Bytes in each half of an ES256 signature (RFC 7518, 3.4).
_HALF = 32


def b64url_json(value: Any) -> str:
    return b64url(json.dumps(value, separators=(",", ":")).encode())


def p256_jwk(key: ec.EllipticCurvePublicKey) -> dict[str, str]:
    numbers = key.public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url(numbers.x.to_bytes(_HALF)),
        "y": b64url(numbers.y.to_bytes(_HALF)),
    }


class Device:
    """A device: its identifier, and its key."""

    def __init__(
        self, device_id: UUID | None = None, key: ec.EllipticCurvePrivateKey | None = None
    ) -> None:
        self.id = device_id or uuid.uuid7()
        self.key = key or ec.generate_private_key(ec.SECP256R1())

    @property
    def jwk(self) -> dict[str, str]:
        return p256_jwk(self.key.public_key())

    @property
    def thumbprint(self) -> str:
        return thumbprint(self.jwk)

    def registration(
        self,
        challenge: str,
        server: str,
        *,
        header: dict[str, Any] | None = None,
        claims: dict[str, Any] | None = None,
        without: tuple[str, ...] = (),
        signer: ec.EllipticCurvePrivateKey | None = None,
    ) -> str:
        """Its registration with ``server`` for ``challenge``: a JWT signed with ES256 by its
        key, or by ``signer``, carrying its public key. ``header`` and ``claims`` change what
        it says, and ``without`` leaves parts of either out."""
        head = {"alg": "ES256", "typ": REGISTRATION_TYPE, "jwk": self.jwk, **(header or {})}
        body = {
            "iss": str(self.id),
            "sub": str(self.id),
            "aud": server,
            "nonce": challenge,
            "jti": secrets.token_urlsafe(16),
            **(claims or {}),
        }
        for name in without:
            head.pop(name, None)
            body.pop(name, None)
        signed = f"{b64url_json(head)}.{b64url_json(body)}"
        der = (signer or self.key).sign(signed.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return f"{signed}.{b64url(r.to_bytes(_HALF) + s.to_bytes(_HALF))}"
