# SPDX-License-Identifier: MIT
import asyncio

import pytest
import redis.asyncio

from ai_cloud.shared.rates import RateSettings, RedisRateLimiter, address_subject


@pytest.mark.anyio
async def test_a_subject_acts_so_many_times_then_waits(redis_url: str) -> None:
    client = redis.asyncio.Redis.from_url(redis_url)
    try:
        limiter = RedisRateLimiter(client, "test:", RateSettings(limit=4, period=60))
        assert [await limiter.wait("a") for _ in range(4)] == [0, 0, 0, 0]
        waited = await limiter.wait("a")
        # The next is due when the first of the burst has aged a quarter of the period.
        assert 14.9 < waited <= 15
        # A refused action counts nothing, and another subject has its own share.
        assert 14.9 < await limiter.wait("a") <= 15
        assert await limiter.wait("b") == 0
        # Redis forgets a subject once its next action is due anyway.
        remaining = await client.pttl("test:a")
        assert 0 < remaining <= 60_000
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_the_steady_rate_comes_back(redis_url: str) -> None:
    client = redis.asyncio.Redis.from_url(redis_url)
    try:
        limiter = RedisRateLimiter(client, "test:", RateSettings(limit=2, period=0.2))
        assert [await limiter.wait("a") for _ in range(2)] == [0, 0]
        waited = await limiter.wait("a")
        assert 0 < waited <= 0.1
        # Waiting as long as it says is enough; asyncio may wake a timer tick early.
        await asyncio.sleep(waited + 0.05)
        assert await limiter.wait("a") == 0
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("address", "subject"),
    [
        ("192.0.2.10", "192.0.2.10"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff::1", "2001:db8:1:2::/64"),
        ("::ffff:192.0.2.10", "192.0.2.10"),
        ("testclient", "testclient"),
    ],
)
def test_what_an_address_counts_as(address: str, subject: str) -> None:
    assert address_subject(address, 64) == subject
