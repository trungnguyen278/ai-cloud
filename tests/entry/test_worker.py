# SPDX-License-Identifier: MIT
"""The worker process: it relays the modules' events and runs their consumers, until it is
told to stop."""

import asyncio
import signal
import uuid
from typing import Any

import pytest
from events_module import EVENTS, until

from ai_cloud.accounts.api import OUTBOX, HomeDeleted
from ai_cloud.entry import worker
from ai_cloud.entry.worker import STOP_SIGNALS, WorkerSecrets, serve
from ai_cloud.shared.config import ConfigError
from ai_cloud.shared.database import create_engine
from ai_cloud.shared.events import Consumer, Event

# Connections open as they are first used: the tests that name these reach neither.
DATABASE = "postgresql://cloud:hunter2@127.0.0.1:1/cloud"
REDIS = "redis://:hunter2@127.0.0.1:1/0"


def test_the_secrets_are_read_and_checked() -> None:
    secrets = WorkerSecrets.read({"AI_CLOUD_DATABASE_URL": DATABASE, "AI_CLOUD_REDIS_URL": REDIS})
    assert (secrets.database_url, secrets.redis_url) == (DATABASE, REDIS)
    for environ, problem in (
        ({"AI_CLOUD_REDIS_URL": REDIS}, "AI_CLOUD_DATABASE_URL"),
        ({"AI_CLOUD_DATABASE_URL": DATABASE}, "AI_CLOUD_REDIS_URL"),
        (
            {
                "AI_CLOUD_DATABASE_URL": "mysql://cloud:hunter2@db/cloud",
                "AI_CLOUD_REDIS_URL": REDIS,
            },
            "postgresql://",
        ),
        (
            {"AI_CLOUD_DATABASE_URL": DATABASE, "AI_CLOUD_REDIS_URL": "http://:hunter2@cache"},
            "AI_CLOUD_REDIS_URL must be",
        ),
    ):
        with pytest.raises(ConfigError, match=problem) as caught:
            WorkerSecrets.read(environ)
        assert "hunter2" not in str(caught.value)


@pytest.mark.anyio
async def test_the_worker_relays_and_consumes_until_it_is_stopped(
    database: str, redis_url: str, make_settings: Any
) -> None:
    settings = make_settings(events=EVENTS)
    seen: list[Event] = []

    async def handle(event: Event) -> None:
        seen.append(event)

    stop = asyncio.Event()
    running = asyncio.create_task(
        serve(
            settings,
            WorkerSecrets(database, redis_url),
            stop,
            consumers=[Consumer("tests.home_deletions", {HomeDeleted.TYPE: handle})],
        )
    )
    home_id = uuid.uuid7()
    engine = create_engine(database, settings.database, "ai-cloud tests")
    try:
        async with engine.begin() as connection:
            await OUTBOX.write(connection, HomeDeleted(home_id))
    finally:
        await engine.dispose()

    async def handled() -> bool:
        return len(seen) == 1

    await until(handled)
    stop.set()
    await asyncio.wait_for(running, 5)
    assert HomeDeleted.from_data(seen[0].data) == HomeDeleted(home_id)


@pytest.mark.parametrize("signum", STOP_SIGNALS, ids=lambda each: signal.Signals(each).name)
def test_a_signal_stops_the_worker(
    monkeypatch: pytest.MonkeyPatch,
    make_settings: Any,
    caplog: pytest.LogCaptureFixture,
    signum: int,
) -> None:
    before = signal.getsignal(signum)

    async def told_to_stop(_settings: Any, _secrets: Any, stop: asyncio.Event) -> None:
        signal.raise_signal(signum)
        await asyncio.wait_for(stop.wait(), 5)

    monkeypatch.setattr(worker, "serve", told_to_stop)
    worker.run(make_settings(), WorkerSecrets(DATABASE, REDIS))
    # What handled the signal before handles it again.
    assert signal.getsignal(signum) == before
    [stopping] = [r for r in caplog.records if r.getMessage() == "the worker process stops"]
    assert stopping.signal == signal.Signals(signum).name
    assert caplog.records[-1].getMessage() == "the worker process has stopped"
