# SPDX-License-Identifier: MIT
"""Redis, for what may be lost: nonces, rate limits (docs/decisions/0008-data-and-tokens.md),
and the queue of domain events (docs/decisions/0009-domain-events.md).

Every key starts with its module's name and a colon, so that the modules never meet in
the one keyspace.
"""

from typing import Annotated

from pydantic import Field
from redis.asyncio import Redis

from ai_cloud.shared.config import ConfigError, Settings

REDIS_URL_ENV = "AI_CLOUD_REDIS_URL"

_SCHEMES = ("redis://", "rediss://", "unix://")


class RedisSettings(Settings):
    # Seconds to open a connection.
    connect_timeout: Annotated[float, Field(gt=0)]
    # Seconds to wait for an answer.
    timeout: Annotated[float, Field(gt=0)]
    # Connections each process may keep open.
    max_connections: Annotated[int, Field(ge=1)]


def redis_url(url: str) -> str:
    """``url``, a Redis URL from the environment. ``ConfigError`` if it is not one, without
    the URL, which may hold a password."""
    if not url.startswith(_SCHEMES):
        raise ConfigError(f"{REDIS_URL_ENV} must be a redis://, rediss:// or unix:// URL")
    return url


def create_redis(url: str, settings: RedisSettings) -> Redis:
    """A client for the Redis at ``url``, which connects when it is first used.
    ``ConfigError`` if ``url`` is not a Redis URL, without the URL, which may hold a
    password."""
    redis_url(url)
    try:
        return Redis.from_url(
            url,
            socket_connect_timeout=settings.connect_timeout,
            socket_timeout=settings.timeout,
            max_connections=settings.max_connections,
        )
    except ValueError:
        raise ConfigError(f"{REDIS_URL_ENV} is not a valid Redis URL") from None
