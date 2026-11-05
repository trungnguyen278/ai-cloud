# SPDX-License-Identifier: MIT
"""Running consumers in the worker (docs/decisions/0009-domain-events.md, decisions 5 and 6).

A consumer (``model.Consumer``) is a consumer group, which reads the streams of the types
it handles from the start of what they hold. Every worker runs each consumer; an event
goes to one of them, and is acknowledged once its handler returns. So an event arrives at
least once, and maybe again: after a crash, or a retry.

A handler that raises, or takes longer than ``handler_timeout``, leaves its event pending.
It is tried again after ``retry_delay``, then after twice as long each time, up to
``retry_delay_max``. After ``max_deliveries``, or at once for an entry that is not an event,
it goes to its module's dead letters, with an error in the log for the operators.
"""

import asyncio
import logging
import os
import secrets
import socket
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from ai_cloud.shared.events.model import Consumer, Event, MalformedEvent
from ai_cloud.shared.events.queue import Delivery, EventQueue, GroupLost
from ai_cloud.shared.events.settings import EventsSettings

# Random bytes that tell apart two processes on one host, in a consumer's name.
_INSTANCE_BYTES = 4

log = logging.getLogger(__name__)


def instance_name() -> str:
    """This process's name among a consumer group's consumers: its host, its process, and
    random bytes, since two containers may share a process number."""
    return f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(_INSTANCE_BYTES)}"


class Subscription:
    """A consumer, run in this process: one of its group's consumers."""

    def __init__(
        self,
        consumer: Consumer,
        queue: EventQueue,
        settings: EventsSettings,
        *,
        name: str | None = None,
    ) -> None:
        self.consumer = consumer
        self.name = instance_name() if name is None else name
        self._queue = queue
        self._settings = settings

    async def join(self) -> None:
        """Make the consumer's group on each of its streams, if it is not there."""
        for stream in self.consumer.streams:
            await self._queue.join(stream, self.consumer.name)

    async def step(self, wait: float) -> bool:
        """Try again the events whose retries are due, then handle the next new one,
        waiting up to ``wait`` seconds for it if there was nothing to try again; whether
        there was anything. ``GroupLost`` if a stream or the group is gone."""
        retried = await self._retry()
        delivery = await self._queue.read(
            self.consumer.name, self.name, self.consumer.streams, 0 if retried else wait
        )
        if delivery is not None:
            await self._handle(delivery)
        return retried or delivery is not None

    async def sweep(self) -> None:
        """Let go of the dead letters past the retention."""
        before = datetime.now(UTC) - timedelta(seconds=self._settings.retention)
        await self._queue.trim_dead_letters(self.consumer.dead_letters, before=before)

    async def leave(self) -> None:
        """Leave the group, as the process stops, unless it holds an event that is still
        to be tried again: another consumer takes that over in time."""
        for stream in self.consumer.streams:
            await self._queue.leave(stream, self.consumer.name, self.name)

    async def run(self, stop: asyncio.Event) -> None:
        """Consume until ``stop`` is set, then leave the group."""
        loop = asyncio.get_running_loop()
        next_sweep = loop.time()
        joined = False
        while not stop.is_set():
            try:
                if not joined:
                    await self.join()
                    joined = True
                if loop.time() >= next_sweep:
                    await self.sweep()
                    next_sweep = loop.time() + self._settings.sweep_interval
                await self.step(self._settings.read_wait)
            except GroupLost:
                joined = False
                log.info(
                    "a consumer's group is gone: it joins again",
                    extra={"consumer": self.consumer.name},
                )
            except Exception:
                log.warning(
                    "a consumer failed: it tries again",
                    extra={"consumer": self.consumer.name},
                    exc_info=True,
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self._settings.reconnect_delay)
        try:
            await self.leave()
        except Exception:
            log.warning(
                "a consumer could not leave its group",
                extra={"consumer": self.consumer.name},
                exc_info=True,
            )

    async def _retry(self) -> bool:
        tried = False
        for stream in self.consumer.streams:
            after: str | None = None
            while True:
                page = await self._queue.pending(
                    stream,
                    self.consumer.name,
                    idle=self._settings.retry_delay,
                    after=after,
                    limit=self._settings.batch_size,
                )
                for pending in page:
                    spent = pending.deliveries >= self._settings.max_deliveries
                    # An event whose last delivery was in vain only waits to be sure that
                    # its handler no longer runs: a consumer stopped while handling it.
                    wait = (
                        self._settings.retry_delay
                        if spent
                        else self._settings.backoff(pending.deliveries)
                    )
                    if pending.idle < wait:
                        continue
                    delivery = await self._queue.claim(
                        stream, self.consumer.name, self.name, pending, idle=wait
                    )
                    if delivery is None:
                        continue
                    tried = True
                    if spent:
                        # Taken over only to be put aside: that is no delivery.
                        delivery = replace(delivery, deliveries=pending.deliveries)
                        await self._bury(delivery, "its last delivery never ended")
                    else:
                        await self._handle(delivery)
                if len(page) < self._settings.batch_size:
                    break
                after = page[-1].entry
        return tried

    async def _handle(self, delivery: Delivery) -> None:
        try:
            event = delivery.event()
        except MalformedEvent as error:
            await self._bury(delivery, f"not an event: {error}")
            return
        handler = self.consumer.handlers.get(event.type)
        if handler is not None:
            try:
                async with asyncio.timeout(self._settings.handler_timeout):
                    await handler(event)
            except Exception:
                spent = delivery.deliveries >= self._settings.max_deliveries
                log.warning(
                    "an event's handler failed: it is put aside"
                    if spent
                    else "an event's handler failed: it is tried again later",
                    extra=self._about(delivery, event),
                    exc_info=True,
                )
                if spent:
                    await self._bury(delivery, "its handler failed on every delivery")
                return
        await self._queue.ack(delivery.stream, self.consumer.name, delivery.entry)

    async def _bury(self, delivery: Delivery, reason: str) -> None:
        await self._queue.bury(delivery, self.consumer.name, self.consumer.dead_letters, reason)
        try:
            about = self._about(delivery, delivery.event())
        except MalformedEvent:
            about = self._about(delivery, None)
        log.error("an event is put aside as a dead letter", extra={**about, "reason": reason})

    def _about(self, delivery: Delivery, event: Event | None) -> dict[str, object]:
        """What the logs say of an event: never its data."""
        about: dict[str, object] = {
            "consumer": self.consumer.name,
            "stream": delivery.stream,
            "entry": delivery.entry,
            "deliveries": delivery.deliveries,
        }
        if event is not None:
            about.update(event=str(event.id), type=event.type)
        return about
