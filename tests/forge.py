# SPDX-License-Identifier: MIT
"""Tokens an attacker would make, signed by hand: PyJWT refuses to make some of them."""

import base64
import hashlib
import hmac
import json
from typing import Any

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def public_pem(key: Any) -> bytes:
    """A private key's public half, as PEM: what an attacker can read of it."""
    return key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)


def hs256(claims: dict[str, Any], secret: bytes, **header: str) -> str:
    """A JWT signed with HMAC-SHA256 and ``secret``. With a public key as the secret,
    it is the algorithm confusion of RFC 8725, 2.1."""
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT", **header}).encode())
    body = _b64(json.dumps(claims).encode())
    signature = hmac.new(secret, f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(signature)}"
