# SPDX-License-Identifier: MIT
"""The development environment's provider (the app API's Development): it signs test
people in without Google or Apple.

It makes ID tokens as a provider does, signed by a key it makes when the process starts,
and the cloud verifies them as any provider's. Sign-in then runs the same path end to
end. It must never run in production: anyone who reaches it can sign in as anybody.
"""

import json
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from ai_cloud.accounts.adapters.oidc import OidcProvider, StaticKeys
from ai_cloud.accounts.settings import DevelopmentSettings

NAME = "development"
# As Google's and Apple's published keys are, measured on 2026-11-01: RS256, with RSA keys
# of 2048 bits and the public exponent 65537.
ALGORITHM = "RS256"
_KEY_SIZE = 2048
_PUBLIC_EXPONENT = 65537


class DevelopmentProvider:
    def __init__(self, settings: DevelopmentSettings, leeway: float) -> None:
        self._settings = settings
        self._key = rsa.generate_private_key(public_exponent=_PUBLIC_EXPONENT, key_size=_KEY_SIZE)
        self._kid = str(uuid.uuid7())
        public = json.loads(RSAAlgorithm.to_jwk(self._key.public_key()))
        self.verifier = OidcProvider(
            NAME,
            issuers=[settings.issuer],
            audiences=[settings.client_id],
            keys=StaticKeys([jwt.PyJWK({**public, "kid": self._kid, "alg": ALGORITHM})]),
            algorithms=[ALGORITHM],
            leeway=leeway,
        )

    def issue(self, nonce: str, display_name: str, email: str | None) -> str:
        now = int(time.time())
        claims: dict[str, object] = {
            "iss": self._settings.issuer,
            "aud": self._settings.client_id,
            # The same test person each time: their email decides, or their name without one.
            "sub": email.lower() if email else display_name,
            "name": display_name,
            "nonce": nonce,
            "iat": now,
            "exp": now + self._settings.id_token_lifetime,
        }
        if email:
            claims |= {"email": email, "email_verified": True}
        return jwt.encode(claims, self._key, algorithm=ALGORITHM, headers={"kid": self._kid})
