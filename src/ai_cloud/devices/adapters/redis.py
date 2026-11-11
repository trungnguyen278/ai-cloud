# SPDX-License-Identifier: MIT
"""The assertions that devices have used, in Redis, which forgets each once it has expired
(docs/decisions/0008-data-and-tokens.md, decision 3). Losing them reopens, for a few minutes
at most, the replay of an assertion that someone copied in that time."""

import hashlib
import math
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis

PREFIX = "devices:assertion:"


class RedisAssertions:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def first_use(self, device_id: UUID, jti: str, until: datetime) -> bool:
        # Each device's identifiers are its own (RFC 7519, 4.1.7). Their SHA-256 bounds the
        # key whatever the device sent, a lone surrogate included, which JSON can spell.
        digest = hashlib.sha256(jti.encode("utf-8", "surrogatepass")).hexdigest()
        lasts = math.ceil((until - datetime.now(UTC)).total_seconds() * 1000)
        # SET NX: of two requests with one assertion, one only sets it.
        return bool(
            await self._redis.set(f"{PREFIX}{device_id}:{digest}", b"", nx=True, px=max(lasts, 1))
        )
