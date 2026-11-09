# SPDX-License-Identifier: MIT
"""The ``api`` process: requests from devices and apps, served by FastAPI
(docs/decisions/0002-stack.md, decision 4).

FastAPI's own description of the API (``/docs``, ``/openapi.json``) is not served: what
crosses to a device or an app is defined by ai-platform's schemas, not by this code.
"""

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Self

import httpx2
import uvicorn
from fastapi import FastAPI

from ai_cloud.accounts.api import Accounts
from ai_cloud.devices.api import Devices
from ai_cloud.entry.settings import CloudSettings
from ai_cloud.shared import problems
from ai_cloud.shared.database import DATABASE_URL_ENV, create_engine, database_url
from ai_cloud.shared.jose import SIGNING_KEYS_ENV, SigningKeys
from ai_cloud.shared.limits import BodyLimit
from ai_cloud.shared.redis import REDIS_URL_ENV, create_redis
from ai_cloud.shared.secrets import read_secret

APPLICATION_NAME = "ai-cloud api"  # how its connections appear in PostgreSQL

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiSecrets:
    """What the api process needs from the environment or the secret store."""

    database_url: str
    redis_url: str
    signing_keys: SigningKeys

    @classmethod
    def read(cls, environ: Mapping[str, str] | None = None) -> Self:
        """The secrets, checked; ``ConfigError`` names what is missing or malformed."""
        database = read_secret(DATABASE_URL_ENV, environ)
        database_url(database)
        return cls(
            database_url=database,
            redis_url=read_secret(REDIS_URL_ENV, environ),
            signing_keys=SigningKeys.from_jwk_set(read_secret(SIGNING_KEYS_ENV, environ)),
        )


def create_app(settings: CloudSettings, secrets: ApiSecrets) -> FastAPI:
    """The api process's application. Its connections open as they are first used, and
    close when it stops."""
    engine = create_engine(secrets.database_url, settings.database, APPLICATION_NAME)
    redis = create_redis(secrets.redis_url, settings.redis)
    http = httpx2.AsyncClient()
    accounts = Accounts(
        settings.accounts,
        settings.tokens,
        settings.app_api,
        keys=secrets.signing_keys,
        engine=engine,
        redis=redis,
        http=http,
    )
    devices = Devices(
        settings.devices,
        settings.app_api,
        settings.tokens,
        keys=secrets.signing_keys,
        accounts=accounts,
        engine=engine,
        redis=redis,
    )
    if accounts.development:
        log.warning("the development provider is on: whoever reaches the api signs in as anyone")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await http.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    problems.install(app)
    app.add_middleware(BodyLimit, limit=settings.api.max_body_size)
    app.include_router(accounts.router)
    app.include_router(devices.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Whether the process runs, for the containers' checks."""
        return {"status": "ok"}

    return app


def run(settings: CloudSettings, secrets: ApiSecrets) -> None:
    """Serve the api until the process is told to stop (SIGINT or SIGTERM)."""
    uvicorn.run(
        create_app(settings, secrets),
        host=settings.api.host,
        port=settings.api.port,
        access_log=settings.api.access_log,
        forwarded_allow_ips=settings.api.forwarded_allow_ips,
        log_config=None,  # the process's logging is configured already (shared.logs)
    )
