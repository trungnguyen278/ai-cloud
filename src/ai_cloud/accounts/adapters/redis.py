# SPDX-License-Identifier: MIT
"""The nonces the cloud issued, in Redis, which forgets them when they expire
(docs/decisions/0008-data-and-tokens.md, decision 3)."""

from redis.asyncio import Redis

PREFIX = "accounts:nonce:"


class RedisNonces:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def issue(self, nonce: str, lifetime: int) -> None:
        await self._redis.set(PREFIX + nonce, b"", ex=lifetime)

    async def consume(self, nonce: str) -> bool:
        # GETDEL reads and forgets in one step: of two requests with the same nonce, one
        # gets it.
        return await self._redis.getdel(PREFIX + nonce) is not None
