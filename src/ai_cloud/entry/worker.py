# SPDX-License-Identifier: MIT
"""The ``worker`` process: background work (docs/decisions/0001-architecture.md, decision 3).

It relays every module's events from its outbox to its stream, and hands them to each
module's consumers (docs/decisions/0009-domain-events.md). Several workers may run at once:
each event reaches its stream once, and each consumer once at least.

It stops on SIGINT or SIGTERM, and on Ctrl+Break on Windows, which has no SIGTERM to send:
the relay finishes its round, and each consumer the event it handles, within its
``handler_timeout``.
"""

import asyncio
import logging
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import FrameType
from typing import Self

from ai_cloud.accounts.api import OUTBOX as ACCOUNTS
from ai_cloud.entry.settings import CloudSettings
from ai_cloud.shared.database import DATABASE_URL_ENV, create_engine, database_url
from ai_cloud.shared.events import Consumer, Outbox
from ai_cloud.shared.events.consumers import Subscription
from ai_cloud.shared.events.relay import Relay
from ai_cloud.shared.events.streams import RedisEventQueue
from ai_cloud.shared.redis import REDIS_URL_ENV, create_redis, redis_url
from ai_cloud.shared.secrets import read_secret

APPLICATION_NAME = "ai-cloud worker"  # how its connections appear in PostgreSQL

# Every module's outbox.
OUTBOXES: tuple[Outbox, ...] = (ACCOUNTS,)

# What tells the process to stop. Windows sends no SIGTERM, but Ctrl+Break.
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM) + (
    (signal.SIGBREAK,) if hasattr(signal, "SIGBREAK") else ()
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerSecrets:
    """What the worker needs from the environment or the secret store."""

    database_url: str
    redis_url: str

    @classmethod
    def read(cls, environ: Mapping[str, str] | None = None) -> Self:
        """The secrets, checked; ``ConfigError`` names what is missing or malformed."""
        database = read_secret(DATABASE_URL_ENV, environ)
        database_url(database)
        return cls(database_url=database, redis_url=redis_url(read_secret(REDIS_URL_ENV, environ)))


async def serve(
    settings: CloudSettings,
    secrets: WorkerSecrets,
    stop: asyncio.Event,
    *,
    outboxes: Sequence[Outbox] = OUTBOXES,
    consumers: Sequence[Consumer] = (),
) -> None:
    """Relay the outboxes' events, and run the consumers, until ``stop`` is set. No module
    consumes another's events yet: each module's consumers join here as it comes."""
    engine = create_engine(secrets.database_url, settings.database, APPLICATION_NAME)
    redis = create_redis(secrets.redis_url, settings.redis)
    queue = RedisEventQueue(redis)
    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(Relay(engine, queue, outboxes, settings.events).run(stop))
            for consumer in consumers:
                tasks.create_task(Subscription(consumer, queue, settings.events).run(stop))
    finally:
        await redis.aclose()
        await engine.dispose()


def run(settings: CloudSettings, secrets: WorkerSecrets) -> None:
    """Work until the process is told to stop."""
    asyncio.run(_until_stopped(settings, secrets))
    log.info("the worker process has stopped")


async def _until_stopped(settings: CloudSettings, secrets: WorkerSecrets) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def stopping(signum: int, _frame: FrameType | None) -> None:
        if not stop.is_set():
            log.info("the worker process stops", extra={"signal": signal.Signals(signum).name})
        loop.call_soon_threadsafe(stop.set)

    before = {each: signal.signal(each, stopping) for each in STOP_SIGNALS}
    try:
        await serve(settings, secrets, stop)
    finally:
        for each, handler in before.items():
            signal.signal(each, handler)
