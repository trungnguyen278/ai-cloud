# SPDX-License-Identifier: MIT
"""The ``api`` process: requests from devices and apps, served by FastAPI
(docs/decisions/0002-stack.md, decision 4), and the devices' control channels.

FastAPI's own description of the API (``/docs``, ``/openapi.json``) is not served: what
crosses to a device or an app is defined by ai-platform's schemas, not by this code.
"""

import asyncio
import functools
import logging
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Self

import httptools
import httpx2
import uvicorn
from fastapi import FastAPI

from ai_cloud.accounts.api import Accounts
from ai_cloud.devices.api import MAX_MESSAGE, Devices
from ai_cloud.entry.settings import CloudSettings
from ai_cloud.entry.websocket import WebSocketProtocol
from ai_cloud.shared import problems
from ai_cloud.shared.app_api import MAX_BODY
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
    # The app API's, which the cloud link's token endpoint, which names none, keeps too.
    app.add_middleware(BodyLimit, limit=MAX_BODY)
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


class ApiConfig(uvicorn.Config):
    """uvicorn's settings, whose ``access_log`` also holds the other lines written for each
    request, which anyone can make. uvicorn 0.54 writes them to ``uvicorn.error`` whatever
    ``access_log`` says, where it mutes the access log of HTTP (its config.py): a WebSocket
    handshake's, ``"WebSocket /device/v3/link" 401`` say, at INFO, and websockets' beside it,
    ``connection rejected (401 Unauthorized)``; and a warning for a request it cannot parse
    or upgrade. A flood would write lines for each
    (docs/decisions/0012-what-one-device-can-exhaust.md)."""

    def configure_logging(self) -> None:
        super().configure_logging()
        _REQUEST_LINES.dropped = not self.access_log
        logging.getLogger("uvicorn.error").addFilter(_REQUEST_LINES)


class _RequestLines(logging.Filter):
    """Drops the lines written for each request, unless the access log holds them: a
    WebSocket handshake's accepted or refused, and a request's that cannot be served. nginx
    logs the latter, "client sent invalid request", at its info level, below its default (its
    ngx_http_request.c and ngx_log.c, read 2026-11-22), and in its access log.

    A request that uvicorn's own code failed to parse is no peer's error, though uvicorn 0.54
    logs it as one: httptools gives an exception raised in one of uvicorn's callbacks as the
    parser's error, ``HttpParserCallbackError``, with the exception as its context (its
    parser.pyx, 0.9.0). That line is kept whatever the access log, with the exception, as
    Python's logging lets a filter change a record (Filter.filter, read 2026-11-22); but not
    where the exception is httptools' own over the request, which uvicorn's callback meets as
    it parses the URL, a port past 65,535 among them (tried 2026-11-22): that one is a peer's."""

    # How each of uvicorn's WebSocket protocols begins its line.
    HANDSHAKE = '%s - "WebSocket %s" '
    # uvicorn's, as its HTTP protocols answer 400 (uvicorn 0.54, httptools_impl.py and
    # h11_impl.py).
    INVALID = "Invalid HTTP request received."
    LINES = frozenset(
        {
            # websockets', as its ServerProtocol sends its response (websockets 17.3, server.py).
            "connection open",
            "connection rejected (%d %s)",
            "HTTP response sent (%d %s)",
            INVALID,
            # uvicorn's, as its HTTP protocols serve no upgrade.
            "Unsupported upgrade request.",
        }
    )

    def __init__(self) -> None:
        super().__init__()
        # Whether the lines written for each request are dropped.
        self.dropped = True

    def filter(self, record: logging.LogRecord) -> bool:
        line = record.msg
        if not isinstance(line, str):
            return True
        if line == self.INVALID:
            # Logged as uvicorn handles the parser's error.
            failed = sys.exc_info()[1]
            if isinstance(failed, httptools.HttpParserCallbackError) and not isinstance(
                failed.__context__, httptools.HttpParserError
            ):
                record.exc_info = (type(failed), failed, failed.__traceback__)
                return True
        return not (self.dropped and (line.startswith(self.HANDSHAKE) or line in self.LINES))


_REQUEST_LINES = _RequestLines()


def server_config(app: FastAPI, settings: CloudSettings, **changes: Any) -> uvicorn.Config:
    """How uvicorn serves ``app``, with ``changes`` to uvicorn's own settings."""
    return ApiConfig(
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
            # Compression would refuse some messages of 1 MiB, which the cloud link's every
            # side receives (Sizes, since 3.3): websockets 17.3 compares a compressed frame's
            # length with what the message may still hold once decompressed (frames.py,
            # protocol.py), so a last fragment of one byte after 1 MiB fails. It also costs
            # memory on every connection. RFC 7692 lets a server decline it, and the robot
            # offers none; Node's ws declines it by default (its README, read 2026-11-21).
            "ws_per_message_deflate": False,
            # The cloud link pings with messages of its own (Staying connected), which a proxy
            # in between cannot answer for the device, as it may WebSocket's.
            "ws_ping_interval": None,
            # uvicorn's own protocol, but for the frames below the application that a
            # connection may send, whose Pings the link's ping_interval sets (Frames,
            # since 3.4), and that a connection closed ends within ws_close_timeout: a
            # device that reads nothing would hold its own open.
            "ws": functools.partial(
                WebSocketProtocol,
                close_timeout=settings.api.ws_close_timeout,
                link_ping_interval=settings.devices.link.ping_interval,
            ),
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
