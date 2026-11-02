# SPDX-License-Identifier: MIT
"""JOSE: the cloud's own signing keys, and the keys that providers publish
(docs/decisions/0008-data-and-tokens.md, decision 4).

PyJWT signs and verifies every token; this module finds the key it needs. Errors say
why a token failed, never what it holds, and never show a key.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any, Self

import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm
from pydantic import Field

from ai_cloud.shared.config import ConfigError, Settings

SIGNING_KEYS_ENV = "AI_CLOUD_SIGNING_KEYS"

# What signs the cloud's own tokens (docs/decisions/0008-data-and-tokens.md, decision 4).
ALGORITHM = "ES256"
_CURVE = "P-256"

log = logging.getLogger(__name__)


class TokenSettings(Settings):
    # The issuer the cloud names in its own tokens (their iss). Only the cloud reads it.
    issuer: Annotated[str, Field(min_length=1)]
    # Seconds by which clocks may disagree when a token's times are checked: the cloud's
    # own, and those of the providers whose ID tokens it reads.
    leeway: Annotated[float, Field(ge=0)]


class TokenError(Exception):
    """A token that does not verify. The message says why, in a few words."""


class SigningKeys:
    """The cloud's private keys, newest first. The first signs; all of them verify, so
    that a key retired from signing still verifies what it signed until that expires.

    They come from a JWK set (RFC 7517) in the environment, made with ``ai-cloud
    signing-key``. To rotate: add a new key at the end, then move it to the front, then
    remove the old one once the tokens it signed have expired. Each step is a restart.
    """

    def __init__(self, keys: Sequence[tuple[str, ec.EllipticCurvePrivateKey]]) -> None:
        if not keys:
            raise ValueError("at least one key is needed")
        self._signer = keys[0]
        self._verifiers = {kid: key.public_key() for kid, key in keys}

    @classmethod
    def from_jwk_set(cls, text: str) -> Self:
        """The keys of the JWK set ``text``; ``ConfigError`` names what is wrong with it,
        never a key."""
        try:
            data = json.loads(text)
        except ValueError:
            raise ConfigError(f"{SIGNING_KEYS_ENV} is not JSON") from None
        entries = data.get("keys") if isinstance(data, dict) else None
        if not isinstance(entries, list) or not entries:
            raise ConfigError(f'{SIGNING_KEYS_ENV} must be a JWK set: {{"keys": [...]}}')
        keys: list[tuple[str, ec.EllipticCurvePrivateKey]] = []
        for index, entry in enumerate(entries):
            where = f"{SIGNING_KEYS_ENV}, key {index}"
            if not isinstance(entry, dict):
                raise ConfigError(f"{where}: not a JWK")
            kid = entry.get("kid")
            if not isinstance(kid, str) or not kid:
                raise ConfigError(f"{where}: no kid")
            if kid in (known for known, _ in keys):
                raise ConfigError(f"{where}: kid repeated")
            if entry.get("kty") != "EC" or entry.get("crv") != _CURVE or "d" not in entry:
                raise ConfigError(f"{where}: not a private {_CURVE} key ({ALGORITHM})")
            try:
                key = ECAlgorithm.from_jwk(entry)
            except jwt.PyJWTError, ValueError, TypeError:
                # Their messages may hold the key.
                raise ConfigError(f"{where}: not a valid key") from None
            if not isinstance(key, ec.EllipticCurvePrivateKey):
                raise ConfigError(f"{where}: not a private key")
            keys.append((kid, key))
        return cls(keys)

    def sign(self, claims: Mapping[str, Any], *, typ: str) -> str:
        """A JWT of ``claims``, signed by the newest key, with header ``typ``."""
        kid, key = self._signer
        return jwt.encode(dict(claims), key, algorithm=ALGORITHM, headers={"kid": kid, "typ": typ})

    def verify(
        self,
        token: str,
        *,
        typ: str,
        issuer: str,
        audience: str,
        leeway: float,
        require: Sequence[str],
    ) -> dict[str, Any]:
        """The claims of ``token``, if one of these keys signed it, its header's ``typ``
        is ``typ``, it was issued by ``issuer`` for ``audience``, it has not expired,
        and it has every claim in ``require``. ``TokenError`` otherwise."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError:
            raise TokenError("malformed") from None
        key = self._verifiers.get(kid) if isinstance(kid, str) else None
        if key is None:
            raise TokenError("unknown key")
        try:
            decoded = jwt.decode_complete(
                token,
                key,
                algorithms=[ALGORITHM],
                issuer=issuer,
                audience=audience,
                leeway=leeway,
                options={"require": list(require), "strict_aud": True},
            )
        except jwt.PyJWTError as error:
            raise TokenError(failure(error)) from None
        # Explicit typing (RFC 8725, 3.11): one kind of token never passes for another.
        if decoded["header"].get("typ") != typ:
            raise TokenError("wrong type")
        return decoded["payload"]


def new_signing_key() -> dict[str, str]:
    """A new private key, as a JWK for ``SigningKeys``."""
    key = ECAlgorithm.to_jwk(ec.generate_private_key(ec.SECP256R1()), as_dict=True)
    return {"kid": str(uuid.uuid7()), "alg": ALGORITHM, "use": "sig", **key}


def failure(error: jwt.PyJWTError) -> str:
    """Why PyJWT refused a token, in a few words of our own: its messages are not
    promised to leave out what the token holds."""
    match error:
        case jwt.ExpiredSignatureError():
            return "expired"
        case jwt.ImmatureSignatureError():
            return "not valid yet"
        case jwt.InvalidAudienceError():
            return "wrong audience"
        case jwt.InvalidIssuerError():
            return "wrong issuer"
        case jwt.MissingRequiredClaimError():
            return f"no {error.claim} claim"
        case jwt.InvalidSignatureError():
            return "bad signature"
        case jwt.InvalidAlgorithmError():
            return "wrong algorithm"
        case jwt.DecodeError():
            return "malformed"
        case _:
            return "invalid"


class JwksSettings(Settings):
    # Seconds to wait for a provider's keys.
    timeout: Annotated[float, Field(gt=0)]
    # Seconds at least between two fetches of a provider's keys: neither a token naming a
    # key that does not exist, nor a provider that says not to keep its keys, makes the
    # cloud fetch them for every token.
    refetch_interval: Annotated[float, Field(gt=0)]
    # Seconds the keys are kept when the provider's answer does not say how long.
    default_max_age: Annotated[float, Field(gt=0)]


class KeysUnavailable(Exception):
    """A provider's keys could not be fetched, and none are kept."""


_MAX_AGE = re.compile(r"(?:^|,)\s*max-age\s*=\s*\"?(\d+)\"?\s*(?:,|$)", re.IGNORECASE)


class PublishedKeys:
    """The keys a provider publishes at ``url``, as a JWK set: kept as long as its
    caching headers allow (RFC 9111), and fetched again for a key ID they lack. If the
    provider cannot be reached, the keys already kept go on serving."""

    def __init__(
        self,
        http: httpx2.AsyncClient,
        url: str,
        settings: JwksSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._url = url
        self._settings = settings
        self._clock = clock
        self._keys: dict[str, jwt.PyJWK] | None = None
        self._fresh_until = 0.0
        self._fetched_at = -float("inf")
        self._lock = asyncio.Lock()

    async def key(self, kid: str) -> jwt.PyJWK | None:
        """The key ``kid``, or ``None`` if the provider has none of that ID.
        ``KeysUnavailable`` if its keys cannot be fetched and none are kept."""
        if self._keys is None or self._clock() >= self._fresh_until:
            await self._refresh(stale=True)
        if self._keys is None or kid not in self._keys:
            await self._refresh(stale=False)
        return None if self._keys is None else self._keys.get(kid)

    async def _refresh(self, *, stale: bool) -> None:
        """Fetch the keys, if they are stale (``stale``) or a key is missing, unless
        they were fetched less than ``refetch_interval`` ago."""
        async with self._lock:
            now = self._clock()
            fresh = self._keys is not None and now < self._fresh_until
            if (stale and fresh) or now - self._fetched_at < self._settings.refetch_interval:
                if self._keys is None:
                    raise KeysUnavailable(self._url)
                return  # another request fetched them meanwhile, or did so a moment ago
            self._fetched_at = now
            try:
                keys, max_age = await self._fetch()
            except (httpx2.HTTPError, ValueError, jwt.PyJWTError) as error:
                log.warning(
                    "cannot fetch a provider's keys",
                    extra={"url": self._url, "error": type(error).__name__},
                )
                if self._keys is None:
                    raise KeysUnavailable(self._url) from None
                return
            self._keys = keys
            self._fresh_until = now + max(max_age, self._settings.refetch_interval)

    async def _fetch(self) -> tuple[dict[str, jwt.PyJWK], float]:
        response = await self._http.get(
            self._url,
            headers={"Accept": "application/json"},
            timeout=self._settings.timeout,
            follow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("not a JWK set")
        keys = {
            key.key_id: key
            for key in jwt.PyJWKSet.from_dict(data).keys
            if key.key_id is not None and key.public_key_use in (None, "sig")
        }
        return keys, self._max_age(response.headers)

    def _max_age(self, headers: httpx2.Headers) -> float:
        """How long the answer may be kept: its ``max-age``, less its ``Age``."""
        found = _MAX_AGE.search(headers.get("Cache-Control", ""))
        if found is None:
            return self._settings.default_max_age
        try:
            age = int(headers.get("Age", "0"))
        except ValueError:
            age = 0
        return max(0, int(found.group(1)) - age)
