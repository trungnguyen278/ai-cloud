# SPDX-License-Identifier: MIT
"""Rate limits (SEC-15): how often one subject, an address say, may act, kept in Redis for
every process; and token buckets, for what one process takes of one connection.

The limit is the generic cell rate algorithm (GCRA), as the redis-cell module and many
API gateways apply it. A subject may act ``limit`` times in a ``period``, spread out or
in one burst, and never more on average. Redis keeps one value for each subject, the
time its next action is due at the steady rate, which expires once it is past. Redis's
clock decides, so that every process agrees.
"""

import asyncio
import ipaddress
import time
from typing import Annotated, Protocol

from pydantic import Field
from redis.asyncio import Redis
from redis.exceptions import RedisError

from ai_cloud.shared.config import Settings

_MICROSECONDS = 1_000_000

# KEYS[1]: the subject's key. ARGV[1]: the period and ARGV[2]: the interval between two
# actions at the steady rate, both in microseconds. Answers 0 when the action may happen,
# and counts it; otherwise the microseconds to wait, and counts nothing.
_GCRA = """
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000000 + tonumber(time[2])
local period = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local due = tonumber(redis.call('GET', KEYS[1]) or now)
if due < now then due = now end
local next_due = due + interval
local wait = next_due - period - now
if wait > 0 then return math.ceil(wait) end
local keep = math.ceil((next_due - now) / 1000)
redis.call('SET', KEYS[1], string.format('%.0f', next_due), 'PX', keep)
return 0
"""


class RateSettings(Settings):
    # How many times one subject may act in a period.
    limit: Annotated[int, Field(ge=1)]
    # The period, in seconds.
    period: Annotated[float, Field(gt=0)]


class AddressRateSettings(RateSettings):
    # The prefix by which IPv6 addresses count: one host is commonly given a whole /64,
    # and would otherwise have as many addresses as it likes.
    ipv6_prefix: Annotated[int, Field(ge=1, le=128)]


class RatesUnavailable(Exception):
    """What counts the rates cannot answer now, such as Redis while it is down."""


class RateLimiter(Protocol):
    async def wait(self, subject: str) -> float:
        """0 if ``subject`` may act now, which counts; otherwise the seconds it must
        wait, and nothing counts. ``RatesUnavailable`` if nothing can be counted now."""
        ...


class RedisRateLimiter:
    """A rate limit for each subject, in Redis, under keys that start with ``prefix``."""

    def __init__(self, redis: Redis, prefix: str, settings: RateSettings) -> None:
        self._script = redis.register_script(_GCRA)
        self._prefix = prefix
        period = settings.period * _MICROSECONDS
        self._args = [f"{period:.3f}", f"{period / settings.limit:.3f}"]

    async def wait(self, subject: str) -> float:
        try:
            waited = await self._script(keys=[self._prefix + subject], args=self._args)
        except RedisError as error:
            raise RatesUnavailable("the rates cannot be counted now") from error
        return int(waited) / _MICROSECONDS


class TokenBucket:
    """A token bucket, in this process: ``rate`` tokens a second, in bursts of up to
    ``burst``. What costs more than a burst waits for a full bucket, and what it lacks then
    delays what follows."""

    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._at = time.monotonic()

    def take_now(self, cost: float = 1) -> float:
        """0 once ``cost`` is taken, if the bucket holds it, or is full; otherwise the seconds
        until it will, and nothing is taken."""
        self._refill()
        needed = min(cost, self.burst)
        if self._tokens >= needed:
            self._tokens -= cost
            return 0
        return (needed - self._tokens) / self.rate

    async def take(self, cost: float = 1) -> None:
        """Wait for ``cost``, then take it."""
        while (wait := self.take_now(cost)) > 0:
            await asyncio.sleep(wait)

    def full(self) -> bool:
        """Whether the bucket holds a whole burst again: what took from it left no trace."""
        self._refill()
        return self._tokens >= self.burst

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
        self._at = now


def address_subject(address: str, ipv6_prefix: int) -> str:
    """What ``address`` counts as for a rate: an IPv4 address itself, an IPv6 address
    by its network of ``ipv6_prefix`` bits. Anything else, such as a Unix socket's
    name, counts as it is."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network((ip, ipv6_prefix), strict=False))
