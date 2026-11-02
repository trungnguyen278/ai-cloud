# SPDX-License-Identifier: MIT
"""ID tokens of OpenID Connect providers (OpenID Connect Core 1.0, 3.1.3.7), verified by
the cloud itself (ai-platform ADR-0008, decision 2).

Each provider's tokens are told apart by their issuer, then checked against that
provider's keys, algorithms and the project's client IDs. The nonce is the caller's to
check, against those the cloud issued.
"""

import logging
from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import jwt

from ai_cloud.accounts.domain.errors import InvalidGrant, ProviderUnavailable
from ai_cloud.accounts.domain.model import SignIn, has_control_characters
from ai_cloud.shared.jose import KeysUnavailable, failure

log = logging.getLogger(__name__)

# What an ID token must hold (OpenID Connect Core 1.0, 2), and the nonce the cloud asks
# for (the app API, The exchange).
_REQUIRED = ("iss", "sub", "aud", "exp", "iat", "nonce")


class Keys(Protocol):
    async def key(self, kid: str) -> jwt.PyJWK | None:
        """The key ``kid``, or ``None``; ``KeysUnavailable`` if none can be had."""
        ...


class StaticKeys:
    """Keys known in advance, such as the development provider's own."""

    def __init__(self, keys: Collection[jwt.PyJWK]) -> None:
        self._keys = {key.key_id: key for key in keys}

    async def key(self, kid: str) -> jwt.PyJWK | None:
        return self._keys.get(kid)


class OidcProvider:
    """A provider whose ID tokens ``issuers`` sign with ``keys``, for one of
    ``audiences``."""

    def __init__(
        self,
        name: str,
        *,
        issuers: Collection[str],
        audiences: Collection[str],
        keys: Keys,
        algorithms: Sequence[str],
        leeway: float,
    ) -> None:
        self.name = name
        self.issuers = frozenset(issuers)
        self._audiences = list(audiences)
        self._keys = keys
        self._algorithms = list(algorithms)
        self._leeway = leeway

    async def verify(self, token: str) -> SignIn:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            raise InvalidGrant("the ID token is malformed") from None
        if not isinstance(kid, str):
            raise InvalidGrant(f"the {self.name} ID token names no key")
        try:
            key = await self._keys.key(kid)
        except KeysUnavailable:
            raise ProviderUnavailable(f"{self.name}'s keys cannot be fetched") from None
        if key is None:
            raise InvalidGrant(f"the {self.name} ID token names an unknown key")
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=self._algorithms,
                audience=self._audiences,
                issuer=self.issuers,
                leeway=self._leeway,
                options={"require": list(_REQUIRED)},
            )
        except jwt.PyJWTError as error:
            raise InvalidGrant(f"the {self.name} ID token is refused: {failure(error)}") from None
        return self._sign_in(claims)

    def _sign_in(self, claims: dict[str, Any]) -> SignIn:
        subject, nonce, issued = claims["sub"], claims["nonce"], claims["iat"]
        if not isinstance(subject, str) or not subject or not isinstance(nonce, str):
            raise InvalidGrant(f"the {self.name} ID token's sub or nonce is malformed")
        if not isinstance(issued, int | float) or isinstance(issued, bool):
            raise InvalidGrant(f"the {self.name} ID token's iat is malformed")
        email = claims.get("email")
        # Only an email the provider checked is kept (OpenID Connect Core 1.0, 5.1).
        verified = claims.get("email_verified") in (True, "true")
        name = claims.get("name")
        return SignIn(
            provider=self.name,
            subject=subject,
            email=email if _text(email) and verified else None,
            name=name if _text(name) else None,
            issue_time=datetime.fromtimestamp(issued, UTC),
            nonce=nonce,
        )


def _text(value: Any) -> bool:
    """Whether a claim is text the cloud keeps: a string, without control characters."""
    return isinstance(value, str) and bool(value) and not has_control_characters(value)


class IdTokenProviders:
    """Every provider the cloud trusts: a token goes to the one its issuer names."""

    def __init__(self, providers: Sequence[OidcProvider]) -> None:
        self._by_issuer: dict[str, OidcProvider] = {}
        for provider in providers:
            for issuer in provider.issuers:
                if issuer in self._by_issuer:
                    raise ValueError(f"two providers have the issuer {issuer}")
                self._by_issuer[issuer] = provider

    async def verify(self, token: str) -> SignIn:
        try:
            # Read before it is verified only to find its provider, which verifies it.
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            raise InvalidGrant("the ID token is malformed") from None
        issuer = unverified.get("iss")
        provider = self._by_issuer.get(issuer) if isinstance(issuer, str) else None
        if provider is None:
            raise InvalidGrant("the ID token's issuer is not a provider the cloud trusts")
        return await provider.verify(token)
