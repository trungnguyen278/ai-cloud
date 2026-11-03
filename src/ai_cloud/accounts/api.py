# SPDX-License-Identifier: MIT
"""What the entry points and the other modules may use of the accounts.

The api process serves ``Accounts.router``; the other modules learn whom a request
speaks for from ``Accounts.authenticate``; ``ai-cloud migrate`` runs ``MIGRATIONS``.
"""

import httpx2
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_cloud.accounts.adapters.development import DevelopmentProvider
from ai_cloud.accounts.adapters.google import google
from ai_cloud.accounts.adapters.oidc import IdTokenProviders, OidcProvider
from ai_cloud.accounts.adapters.postgres import PostgresStore
from ai_cloud.accounts.adapters.redis import RedisNonces
from ai_cloud.accounts.db.tables import MIGRATIONS
from ai_cloud.accounts.domain.errors import Unauthenticated
from ai_cloud.accounts.domain.model import Principal
from ai_cloud.accounts.routes import router
from ai_cloud.accounts.service import AccountsService, HomesService, SignIns
from ai_cloud.accounts.settings import AccountsSettings
from ai_cloud.shared.jose import SigningKeys, TokenSettings
from ai_cloud.shared.rates import RedisRateLimiter

__all__ = ["MIGRATIONS", "Accounts", "AccountsSettings", "Principal", "Unauthenticated"]

NONCE_RATE_PREFIX = "accounts:nonce-rate:"
INVITATION_RATE_PREFIX = "accounts:invitation-rate:"


class Accounts:
    """The accounts module, put together for a process."""

    def __init__(
        self,
        settings: AccountsSettings,
        tokens: TokenSettings,
        *,
        keys: SigningKeys,
        engine: AsyncEngine,
        redis: Redis,
        http: httpx2.AsyncClient,
    ) -> None:
        providers: list[OidcProvider] = []
        if settings.google.client_ids:
            providers.append(google(http, settings.google.client_ids, settings.jwks, tokens.leeway))
        development = (
            DevelopmentProvider(settings.development, tokens.leeway)
            if settings.development.enabled
            else None
        )
        if development is not None:
            providers.append(development.verifier)
        self.development = development is not None
        store = PostgresStore(engine)
        nonces = RedisNonces(redis)
        sign_ins = SignIns(
            nonces=nonces,
            id_tokens=IdTokenProviders(providers),
            recent_sign_in=settings.recent_sign_in,
        )
        self._service = AccountsService(
            settings=settings,
            tokens=tokens,
            keys=keys,
            store=store,
            nonces=nonces,
            nonce_rate=RedisRateLimiter(redis, NONCE_RATE_PREFIX, settings.nonce_rate),
            sign_ins=sign_ins,
            development=development,
        )
        homes = HomesService(
            settings=settings,
            store=store,
            sign_ins=sign_ins,
            invitation_rate=RedisRateLimiter(
                redis, INVITATION_RATE_PREFIX, settings.invitation_rate
            ),
        )
        self.router = router(self._service, homes, development=self.development)

    async def authenticate(self, access_token: str) -> Principal:
        """The account and session ``access_token`` speaks for; ``Unauthenticated`` if
        it does not verify, or its session has ended."""
        return await self._service.authenticate(access_token)
