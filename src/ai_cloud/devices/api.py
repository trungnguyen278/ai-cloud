# SPDX-License-Identifier: MIT
"""What the entry points and the other modules may use of the devices.

The api process serves ``Devices.router``: the app API's devices, and the devices' token
endpoint. The worker runs ``consumers``, by which a deleted home's devices are revoked;
``ai-cloud migrate`` runs ``MIGRATIONS``.
"""

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_cloud.accounts.api import AccountDeleted, Accounts, HomeDeleted, MemberRemoved
from ai_cloud.devices.adapters.accounts import AccountsPeople
from ai_cloud.devices.adapters.postgres import PostgresStore
from ai_cloud.devices.adapters.redis import RedisAssertions
from ai_cloud.devices.db.tables import MIGRATIONS
from ai_cloud.devices.routes import router
from ai_cloud.devices.service import DevicesService, DeviceTokens, Forgetting
from ai_cloud.devices.settings import DevicesSettings
from ai_cloud.shared.app_api import AppApiSettings
from ai_cloud.shared.events import Consumer
from ai_cloud.shared.jose import SigningKeys, TokenSettings
from ai_cloud.shared.rates import RedisRateLimiter

__all__ = ["MIGRATIONS", "Devices", "DevicesSettings", "consumers"]

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
        service = DevicesService(
            settings=settings,
            app_api=app_api,
            store=store,
            people=AccountsPeople(accounts),
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
        self.router = router(service, device_tokens)


def consumers(engine: AsyncEngine) -> list[Consumer]:
    """What the devices do with the other modules' events, in the worker: a deleted home's
    devices are revoked, and what a person asked for goes with their place in a home, or with
    their account."""
    forgetting = Forgetting(PostgresStore(engine))
    return [
        Consumer("devices.home_deletions", {HomeDeleted.TYPE: forgetting.home_deleted}),
        Consumer("devices.member_removals", {MemberRemoved.TYPE: forgetting.member_removed}),
        Consumer("devices.account_deletions", {AccountDeleted.TYPE: forgetting.account_deleted}),
    ]
