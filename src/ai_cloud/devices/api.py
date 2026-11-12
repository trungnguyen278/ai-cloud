# SPDX-License-Identifier: MIT
"""What the entry points and the other modules may use of the devices.

The api process serves ``Devices.router``: the app API's devices, and the devices' token
endpoint and control channel. It runs ``Devices.run`` beside, which checks the control
channels it holds, and calls ``Devices.going_away`` before it stops. The worker runs
``consumers``, by which a deleted home's devices are revoked; ``ai-cloud migrate`` runs
``MIGRATIONS``.
"""

import asyncio

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_cloud.accounts.api import AccountDeleted, Accounts, HomeDeleted, MemberRemoved
from ai_cloud.devices.adapters.accounts import AccountsPeople
from ai_cloud.devices.adapters.postgres import PostgresStore
from ai_cloud.devices.adapters.redis import RedisAssertions, RedisHints, RedisPresence
from ai_cloud.devices.db.tables import MIGRATIONS
from ai_cloud.devices.domain.link import MAX_MESSAGE
from ai_cloud.devices.routes import router
from ai_cloud.devices.service import DevicesService, DeviceTokens, Forgetting, Links
from ai_cloud.devices.settings import DevicesSettings
from ai_cloud.shared.app_api import AppApiSettings
from ai_cloud.shared.events import Consumer
from ai_cloud.shared.jose import SigningKeys, TokenSettings
from ai_cloud.shared.rates import RedisRateLimiter

__all__ = ["MAX_MESSAGE", "MIGRATIONS", "Devices", "DevicesSettings", "consumers"]

CHALLENGE_RATE_PREFIX = "devices:challenge-rate:"
TOKEN_RATE_PREFIX = "devices:token-rate:"


class Devices:
    """The devices module, put together for the api process."""

    def __init__(
        self,
        settings: DevicesSettings,
        app_api: AppApiSettings,
        tokens: TokenSettings,
        *,
        keys: SigningKeys,
        accounts: Accounts,
        engine: AsyncEngine,
        redis: Redis,
    ) -> None:
        store = PostgresStore(engine)
        presence = RedisPresence(redis, settings.link.presence_ttl)
        hints = RedisHints(redis)
        service = DevicesService(
            settings=settings,
            app_api=app_api,
            store=store,
            people=AccountsPeople(accounts),
            presence=presence,
            hints=hints,
            challenge_rate=RedisRateLimiter(redis, CHALLENGE_RATE_PREFIX, settings.challenge_rate),
        )
        device_tokens = DeviceTokens(
            settings=settings,
            tokens=tokens,
            keys=keys,
            store=store,
            assertions=RedisAssertions(redis),
            token_rate=RedisRateLimiter(redis, TOKEN_RATE_PREFIX, settings.token_rate),
        )
        self._links = Links(
            settings=settings.link,
            leeway=tokens.leeway,
            tokens=device_tokens,
            store=store,
            presence=presence,
            hints=hints,
        )
        self.router = router(
            service, device_tokens, self._links, max_json_depth=app_api.max_json_depth
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Check the control channels that this process holds, until ``stop`` is set."""
        await self._links.run(stop)

    async def going_away(self) -> None:
        """Before the process stops: ask each device connected to it to connect again."""
        await self._links.going_away()


def consumers(engine: AsyncEngine, redis: Redis) -> list[Consumer]:
    """What the devices do with the other modules' events, in the worker: a deleted home's
    devices are revoked, and what a person asked for goes with their place in a home, or with
    their account."""
    forgetting = Forgetting(PostgresStore(engine), RedisHints(redis))
    return [
        Consumer("devices.home_deletions", {HomeDeleted.TYPE: forgetting.home_deleted}),
        Consumer("devices.member_removals", {MemberRemoved.TYPE: forgetting.member_removed}),
        Consumer("devices.account_deletions", {AccountDeleted.TYPE: forgetting.account_deleted}),
    ]
