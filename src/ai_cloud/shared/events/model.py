# SPDX-License-Identifier: MIT
"""What a domain event is: CloudEvents 1.0's attributes, and the names of the types, the
streams and the consumers (docs/decisions/0009-domain-events.md, decision 2).

An event's type is ``<module>.<thing>.<what happened>.v<major>``, such as
``accounts.home.deleted.v1``: its module, which writes it, then its name in the past tense,
then the major version of its data. A change that consumers can ignore, such as a new
optional field, keeps the type; any other makes a new one, written beside the old one until
every consumer reads it (the CloudEvents primer, "Versioning of CloudEvents").
"""

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol, Self
from uuid import UUID

# CloudEvents 1.0.2: "Compliant event producers MUST use a value of 1.0".
SPEC_VERSION = "1.0"
# The media type of every event's data: a JSON object.
DATA_CONTENT_TYPE = "application/json"

# A name in a type or a consumer's name: lower-case words, joined by underscores (ai-platform
# ADR-0004, Names).
_WORD = r"[a-z][a-z0-9_]*"
TYPE_PATTERN = re.compile(rf"(?P<module>{_WORD})(?:\.{_WORD}){{2,}}\.v[1-9][0-9]*")
# A consumer: its module, then what it does, such as devices.home_deletions.
CONSUMER_PATTERN = re.compile(rf"(?P<module>{_WORD})\.{_WORD}")

# What an event's fields are, in a stream.
_FIELDS = ("specversion", "id", "source", "type", "subject", "time", "datacontenttype", "data")


class MalformedEvent(ValueError):
    """What a stream holds is not an event: a field missing, or malformed."""


class DomainEvent(Protocol):
    """What a module writes when something happened: its type, the identifier of what it
    happened to, and its data, as a JSON object that holds no personal data."""

    TYPE: ClassVar[str]

    @property
    def subject(self) -> str: ...

    def data(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Event:
    """A domain event as it travels: CloudEvents 1.0's attributes, and its data."""

    # A UUIDv7, unique to the event: a consumer that meets it twice has met one event.
    id: UUID
    # The module that wrote it, as a URI reference: /accounts.
    source: str
    type: str
    # The identifier of what it happened to, such as the home deleted.
    subject: str
    # When it happened.
    time: datetime
    data: dict[str, Any]

    @property
    def module(self) -> str:
        """The module that wrote it, which names its stream."""
        return module_of(self.type)

    def fields(self) -> dict[str, str]:
        """The event as text fields: CloudEvents' attributes, and the data in JSON, as in
        its binary content mode, where a protocol's headers carry the attributes. The JSON
        is ASCII, its other characters escaped: text that UTF-8 cannot carry, such as a lone
        surrogate, which the outbox's JSON column takes as an escape, travels as one, and
        never holds up the events behind it."""
        return {
            "specversion": SPEC_VERSION,
            "id": str(self.id),
            "source": self.source,
            "type": self.type,
            "subject": self.subject,
            "time": rfc3339(self.time),
            "datacontenttype": DATA_CONTENT_TYPE,
            "data": json.dumps(self.data, separators=(",", ":")),
        }

    @classmethod
    def from_fields(cls, fields: Mapping[str, str]) -> Self:
        """The event that ``fields`` make; ``MalformedEvent`` if they make none."""
        for name in _FIELDS:
            if name not in fields:
                raise MalformedEvent(f"no {name}")
        if fields["specversion"] != SPEC_VERSION or fields["datacontenttype"] != DATA_CONTENT_TYPE:
            raise MalformedEvent("not a CloudEvent 1.0 with JSON data")
        try:
            event = cls(
                id=UUID(fields["id"]),
                source=fields["source"],
                type=fields["type"],
                subject=fields["subject"],
                time=datetime.fromisoformat(fields["time"]),
                data=json.loads(fields["data"]),
            )
        except ValueError:
            raise MalformedEvent("a field is malformed") from None
        if (
            event.time.tzinfo is None
            or not isinstance(event.data, dict)
            or TYPE_PATTERN.fullmatch(event.type) is None
        ):
            raise MalformedEvent("a field is malformed")
        return event


type Handler = Callable[[Event], Awaitable[None]]


@dataclass(frozen=True)
class Consumer:
    """What a module does with the events of some types: a handler for each.

    An event arrives at least once, and maybe again: after a crash, or a retry. A handler is
    therefore idempotent: deleting what is gone does nothing, and a handler whose effect
    would repeat records the event's id in its module's table, in its own transaction
    (microservices.io's idempotent consumer). Nor does it count on an order: two workers, or
    a retry, can handle two events about one thing in either order. It raises to have the
    event tried again later.
    """

    # <module>.<what it does>, such as devices.home_deletions: its consumer group's name.
    name: str
    handlers: Mapping[str, Handler]

    def __post_init__(self) -> None:
        consumer_module(self.name)
        if not self.handlers:
            raise ValueError("a consumer handles some type of event")
        for type_ in self.handlers:
            module_of(type_)

    @property
    def streams(self) -> list[str]:
        """The streams of the modules whose events it handles."""
        return sorted({stream_of(module_of(type_)) for type_ in self.handlers})

    @property
    def dead_letters(self) -> str:
        """Where it puts aside the events it cannot handle: its module's dead letters."""
        return dead_letters_of(consumer_module(self.name))


def module_of(type_: str) -> str:
    """The module of an event's type; ``ValueError`` if it is not a type's name."""
    match = TYPE_PATTERN.fullmatch(type_)
    if match is None:
        raise ValueError(f"{type_!r} is not <module>.<thing>.<what happened>.v<major>")
    return match["module"]


def consumer_module(name: str) -> str:
    """The module of a consumer's name; ``ValueError`` if it is not one."""
    match = CONSUMER_PATTERN.fullmatch(name)
    if match is None:
        raise ValueError(f"{name!r} is not <module>.<what it does>")
    return match["module"]


def stream_of(module: str) -> str:
    """The Redis key of a module's stream: like every key, it starts with its module."""
    return f"{module}:events"


def dead_letters_of(module: str) -> str:
    """The Redis key of the dead letters that a module's consumers put aside."""
    return f"{module}:dead-events"


def rfc3339(time: datetime) -> str:
    """An instant as ai-platform's conventions write it: RFC 3339, in UTC, with Z."""
    return time.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
