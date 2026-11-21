# SPDX-License-Identifier: MIT
import asyncio
import socket
from types import SimpleNamespace

import pytest
import redis.asyncio

from ai_cloud.shared import rates
from ai_cloud.shared.rates import (
    RateSettings,
    RatesUnavailable,
    RedisRateLimiter,
    TokenBucket,
    address_subject,
)


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


@pytest.mark.anyio
async def test_a_rate_that_cannot_be_counted_says_so() -> None:
    # A port that nothing listens on, as Redis's while it is down.
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]
    client = redis.asyncio.Redis(port=port, socket_connect_timeout=1)
    try:
        limiter = RedisRateLimiter(client, "test:", RateSettings(limit=4, period=60))
        with pytest.raises(RatesUnavailable) as unavailable:
            await limiter.wait("a")
        assert isinstance(unavailable.value.__cause__, redis.RedisError)
    finally:
        await client.aclose()


def test_a_bucket_takes_a_burst_then_its_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(rates, "time", SimpleNamespace(monotonic=lambda: now[0]))
    bucket = TokenBucket(10, 3)
    assert [bucket.take_now() for _ in range(3)] == [0, 0, 0]
    assert bucket.take_now() == pytest.approx(0.1)
    now[0] = 0.05
    assert bucket.take_now() == pytest.approx(0.05)
    now[0] = 0.1
    assert bucket.take_now() == 0
    # Never more than the burst, however long it waited.
    now[0] = 100
    assert [bucket.take_now() for _ in range(4)] == [0, 0, 0, pytest.approx(0.1)]


def test_a_cost_past_the_burst_waits_for_a_full_bucket_and_delays_what_follows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(rates, "time", SimpleNamespace(monotonic=lambda: now[0]))
    bucket = TokenBucket(100, 50)
    assert bucket.take_now(30) == 0
    # Not until the bucket is full again; then the whole cost is taken.
    assert bucket.take_now(80) == pytest.approx(0.3)
    now[0] = 0.3
    assert bucket.take_now(80) == 0
    # What it lacked, 30, and then the next cost.
    assert bucket.take_now(1) == pytest.approx(0.31)


def test_a_bucket_is_full_once_what_it_lent_is_made_up(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(rates, "time", SimpleNamespace(monotonic=lambda: now[0]))
    bucket = TokenBucket(10, 3)
    assert bucket.full()
    assert bucket.take_now(2) == 0
    assert not bucket.full()
    now[0] = 0.19
    assert not bucket.full()
    now[0] = 0.2
    assert bucket.full()
