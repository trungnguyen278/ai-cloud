# SPDX-License-Identifier: MIT
"""The ``api`` process: requests from devices and apps, served by FastAPI
(docs/decisions/0002-stack.md, decision 4), and the devices' control channels.

FastAPI's own description of the API (``/docs``, ``/openapi.json``) is not served: what
crosses to a device or an app is defined by ai-platform's schemas, not by this code.
"""

import asyncio
import logging
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Self

import httpx2
import uvicorn
from fastapi import FastAPI

from ai_cloud.accounts.api import Accounts
from ai_cloud.devices.api import MAX_MESSAGE, Devices
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
    close when it stops. ``app.state.going_away`` is what to call before the server closes
    the devices' control channels."""
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
        stop = asyncio.Event()
        checking = asyncio.create_task(devices.run(stop), name="devices: control channels")
        try:
            yield
        finally:
            stop.set()
            await checking
            await http.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.going_away = devices.going_away
    problems.install(app)
    app.add_middleware(BodyLimit, limit=settings.api.max_body_size)
    app.include_router(accounts.router)
    app.include_router(devices.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Whether the process runs, for the containers' checks."""
        return {"status": "ok"}

    return app


class ApiServer(uvicorn.Server):
    """uvicorn's server, which asks the devices connected to it to connect again before it
    stops. uvicorn 0.54 closes every WebSocket itself as it shuts down, with code 1012, before
    the application's lifespan ends (``Server.shutdown``): the cloud link wants a
    ``reconnect`` first."""

    def __init__(self, config: uvicorn.Config, going_away: Callable[[], Awaitable[None]]) -> None:
        super().__init__(config)
        self._going_away = going_away

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        try:
            await self._going_away()
        except Exception:
            # The server stops all the same: its devices then see their connections close.
            log.exception("the devices connected here could not be asked to connect again")
        await super().shutdown(sockets)


def server_config(app: FastAPI, settings: CloudSettings, **changes: Any) -> uvicorn.Config:
    """How uvicorn serves ``app``, with ``changes`` to uvicorn's own settings."""
    return uvicorn.Config(
        app,
        **{
            "host": settings.api.host,
            "port": settings.api.port,
            "access_log": settings.api.access_log,
            "forwarded_allow_ips": settings.api.forwarded_allow_ips,
            "log_config": None,  # the process's logging is configured already (shared.logs)
            # A control channel's message has 1 MiB at most: uvicorn closes the connection
            # over a larger one, with 1009 (the cloud link, Messages).
            "ws_max_size": MAX_MESSAGE,
            # The cloud link pings with messages of its own (Staying connected), which a proxy
            # in between cannot answer for the device, as it may WebSocket's.
            "ws_ping_interval": None,
            # uvicorn waits for every connection to close, without a bound by default: a
            # device that reads nothing never lets its own close.
            "timeout_graceful_shutdown": settings.api.shutdown_timeout,
            **changes,
        },
    )


def run(settings: CloudSettings, secrets: ApiSecrets) -> None:
    """Serve the api until the process is told to stop (SIGINT or SIGTERM)."""
    app = create_app(settings, secrets)
    ApiServer(server_config(app, settings), app.state.going_away).run()
