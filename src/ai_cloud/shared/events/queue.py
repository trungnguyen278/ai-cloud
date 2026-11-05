# SPDX-License-Identifier: MIT
"""What the relay and the consumers need of the queue that carries events: Redis Streams
(``streams.py``), and NATS or Kafka behind the same port should the cloud grow that far
(docs/architecture.md, Growth).

Each module's events are a stream, in the order of their places, and each consumer is a
group, every one of which is given every event. Within a group, an event goes to one of its
consumers, and stays pending until it is acknowledged, or taken over by another consumer
once it has waited long enough.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ai_cloud.shared.events.model import Event


@dataclass(frozen=True)
class Delivery:
    """An event as a consumer group is given it."""

    stream: str
    # Its entry in the stream, by which it is acknowledged.
    entry: str
    # What the entry holds.
    fields: dict[str, str]
    # How many times the group has been given it, this time included.
    deliveries: int

    def event(self) -> Event:
        """The event the entry holds; ``MalformedEvent`` if it holds none."""
        return Event.from_fields(self.fields)


@dataclass(frozen=True)
class Pending:
    """An event a consumer group was given and has not acknowledged."""

    entry: str
    # Seconds since it was last given to a consumer.
    idle: float
    # How many times the group has been given it.
    deliveries: int


class GroupLost(Exception):
    """The stream, or its consumer group, is gone: Redis lost it, or the relay wrote the
    stream again. Joining again reads it from its start."""


class EventQueue(Protocol):
    # The relay's side.

    async def last(self, stream: str) -> int:
        """The last place written to the stream, or 0 if it has none."""
        ...

    async def append(self, stream: str, events: Sequence[tuple[int, Event]]) -> None:
        """Write the events at their places, in the order given; one whose place the stream
        has reached already is left as it is."""
        ...

    async def drop(self, stream: str) -> None:
        """Delete the stream, with its consumer groups."""
        ...

    async def trim(self, stream: str, *, before: int) -> None:
        """Let go of the stream's events whose places come before ``before``."""
        ...

    # The consumers' side.

    async def join(self, stream: str, group: str) -> None:
        """Make the consumer group, if it is not there, to read the stream from its
        start."""
        ...

    async def read(
        self, group: str, consumer: str, streams: Sequence[str], wait: float
    ) -> Delivery | None:
        """The next event of ``streams`` that no consumer of the group has been given,
        waiting up to ``wait`` seconds for one, or none at all for 0. ``GroupLost``."""
        ...

    async def pending(
        self, stream: str, group: str, *, idle: float, after: str | None, limit: int
    ) -> list[Pending]:
        """Up to ``limit`` of the group's pending events that have waited ``idle`` seconds
        at least, in the order of the stream, from the one after ``after``. ``GroupLost``."""
        ...

    async def claim(
        self, stream: str, group: str, consumer: str, pending: Pending, *, idle: float
    ) -> Delivery | None:
        """Take a pending event over, if it has still waited ``idle`` seconds at least: of
        consumers that try at once, one gets it. ``None`` if another did, or the stream let
        it go. ``GroupLost``."""
        ...

    async def ack(self, stream: str, group: str, entry: str) -> None:
        """The group is done with the event."""
        ...

    async def bury(self, delivery: Delivery, group: str, dead_letters: str, reason: str) -> None:
        """Put the event aside in ``dead_letters``, with ``group`` and ``reason``, and
        acknowledge it, both at once."""
        ...

    async def trim_dead_letters(self, dead_letters: str, *, before: datetime) -> None:
        """Let go of the dead letters put aside before ``before``."""
        ...

    async def leave(self, stream: str, group: str, consumer: str) -> None:
        """Forget the consumer, if it holds no pending event, which would be lost with
        it."""
        ...
