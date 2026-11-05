# SPDX-License-Identifier: MIT
"""The relay: from each module's outbox to its stream, in the worker
(docs/decisions/0009-domain-events.md, decisions 3 and 4).

It polls, as microservices.io's polling publisher does, without LISTEN/NOTIFY. Each round,
for each outbox:

1. **Numbering.** The events written since the last round get the next places in the
   stream, in the order they were written. One relay at a time numbers an outbox, under a
   transaction-level advisory lock, so that every place given follows the ones before.
   First, it checks that the stream holds no place the outbox has not given yet, as after
   PostgreSQL is restored from a backup: the outbox would give those places again, and
   the stream would refuse the events that took them. Such a stream is written again.
2. **Writing.** What the outbox holds past the stream's last place goes to the stream, at
   its places. A crash between the two steps, or a Redis that lost what it held, leaves the
   stream short, and the next round fills it in. Two relays that write at once each write
   what the other has not: the stream refuses a place twice.

Every ``sweep_interval``, what is past the retention goes, from the outbox and the stream
alike, under the same lock as the numbering. An event the stream does not hold yet never
goes.
"""

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from ai_cloud.shared.events.outbox import Outbox
from ai_cloud.shared.events.queue import EventQueue
from ai_cloud.shared.events.settings import EventsSettings

log = logging.getLogger(__name__)


class Relay:
    def __init__(
        self,
        engine: AsyncEngine,
        queue: EventQueue,
        outboxes: Sequence[Outbox],
        settings: EventsSettings,
    ) -> None:
        self._engine = engine
        self._queue = queue
        self._outboxes = outboxes
        self._settings = settings

    async def relay(self) -> int:
        """One round over every outbox; how many events it numbered or wrote."""
        moved = 0
        for outbox in self._outboxes:
            moved += await self._number(outbox)
            moved += await self._write(outbox)
        return moved

    async def sweep(self) -> None:
        """Let go of what is past the retention: the events that happened longer ago and
        are in their stream, and what the stream holds before the first event its outbox
        keeps."""
        before = datetime.now(UTC) - timedelta(seconds=self._settings.retention)
        for outbox in self._outboxes:
            last = await self._queue.last(outbox.stream)
            async with self._engine.begin() as connection:
                if not await outbox.take_turn(connection):
                    continue  # another relay numbers or sweeps this outbox now
                forgotten = await outbox.forget(connection, relayed=last, before=before)
                first = await outbox.first_position(connection)
            await self._queue.trim(outbox.stream, before=last + 1 if first is None else first)
            if forgotten:
                log.info(
                    "events past their retention have gone",
                    extra={"stream": outbox.stream, "count": forgotten},
                )

    async def run(self, stop: asyncio.Event) -> None:
        """Relay until ``stop`` is set: at once while there is more, and every
        ``poll_interval`` while there is none."""
        loop = asyncio.get_running_loop()
        next_sweep = loop.time()
        while not stop.is_set():
            try:
                if loop.time() >= next_sweep:
                    await self.sweep()
                    next_sweep = loop.time() + self._settings.sweep_interval
                if await self.relay():
                    continue
                wait = self._settings.poll_interval
            except Exception:
                log.warning("relaying events failed: the relay tries again", exc_info=True)
                wait = self._settings.reconnect_delay
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), wait)

    async def _number(self, outbox: Outbox) -> int:
        async with self._engine.begin() as connection:
            if not await outbox.take_turn(connection):
                return 0  # another relay numbers or sweeps this outbox now
            # While the turn is held, no new place is given, and a place is given before it
            # is written: the stream is past the outbox only if the outbox lost what it gave.
            if await self._queue.last(outbox.stream) > await outbox.last_position(connection):
                log.warning(
                    "a stream holds places its outbox has not given: it is written again",
                    extra={"stream": outbox.stream},
                )
                await self._queue.drop(outbox.stream)
            seqs = await outbox.unnumbered(connection, self._settings.batch_size)
            await outbox.number(connection, seqs)
        return len(seqs)

    async def _write(self, outbox: Outbox) -> int:
        last = await self._queue.last(outbox.stream)
        async with self._engine.connect() as connection:
            events = await outbox.after(connection, last, self._settings.batch_size)
        await self._queue.append(outbox.stream, events)
        return len(events)
