# SPDX-License-Identifier: MIT
"""The control channel's session, as the cloud link 3.0 has it, without I/O
(platform/protocols/cloud/README.md: The control channel, Staying connected, Resuming a
session, Messages, The session, Errors).

A device opens a WebSocket with its access token and says its ``hello`` first; the cloud
answers with its own. Each side numbers what it sends in the session, but for the messages
that open, keep and end it: a number not above the last one handled is a duplicate, and one
that skips a number ends the session. What a device sends is checked against the
protocol's schema (SEC-14), and errors name where a message is wrong, never what it holds.
"""

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum, IntEnum, StrEnum
from typing import Any
from uuid import UUID

from ai_cloud.devices.domain.model import Description
from ai_cloud.shared.protocols import Schema

# Fixed by the cloud link (platform/protocols/cloud/README.md).
PROTOCOL = "cloud"  # its schema, platform/protocols/cloud/schema.json
PATH = "/device/v3/link"  # The cloud's address: the path carries the major version
MAJOR, MINOR = 3, 0  # the version the cloud speaks
MAX_MESSAGE = 1 << 20  # Messages, Sizes: "A message holds up to 1 MiB"
UNNUMBERED = frozenset({"hello", "ping", "pong", "reconnect", "bye"})  # Resuming, Numbers
LARGEST_SEQ = 2**53 - 1  # $defs/seq: what a JSON number holds exactly
SILENT_PINGS = 2  # Staying connected: closed after "nothing has come for two intervals"
MAX_ERROR = 1024  # $defs/error, message
_VERSION = re.compile(r"([0-9]{1,3})\.([0-9]{1,3})")  # $defs/version: major.minor


class Bye(StrEnum):
    """Why the cloud ends a session (The session, bye)."""

    # A newer connection of the device took over.
    REPLACED = "replaced"
    # The device is no longer registered: removed, or gone with its home.
    REVOKED = "revoked"
    # Its hello did not come in time. A first message that is no hello the cloud can read
    # ends the session the same way, as the robot ends one whose cloud's hello it cannot.
    NO_HELLO = "no_hello"
    # Its hello named another major version than the path.
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    # A message skipped a number.
    OUT_OF_SEQUENCE = "out_of_sequence"
    # Anything else, as a side takes a reason it does not know (Messages, Unknown content):
    # a ping the cloud cannot read, or the cloud's own failure.
    ERROR = "error"


class Code(StrEnum):
    """The errors the cloud answers a device's message with (Errors)."""

    INVALID_MESSAGE = "invalid_message"
    UNAVAILABLE = "unavailable"
    RATE_LIMITED = "rate_limited"
    INTERNAL = "internal"


class Close(IntEnum):
    """How the cloud closes a connection: WebSocket's close codes (RFC 6455, 7.4.1, and
    IANA's registry for 1012)."""

    # After a bye, from either side.
    NORMAL = 1000
    # Nothing came for two ping intervals: the device seems gone, and resumes if it is not.
    GOING_AWAY = 1001
    # The cloud failed: the device connects again.
    INTERNAL = 1011
    # After reconnect: the cloud restarts, and the device connects again at once.
    RESTART = 1012


class Number(Enum):
    """Where a numbered message stands against the last one handled from its side."""

    # The next one: it is handled.
    NEXT = "next"
    # Not above the last one: sent again, and handled already (SES-6).
    DUPLICATE = "duplicate"
    # It skipped a number: the session ends.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class DeviceHello:
    """What a device's hello says, and what its device.changed changed since."""

    # The minor version it speaks, of the cloud's major.
    minor: int
    # What it is.
    description: Description
    # Its time zone, its languages in its owner's order, and the safety classes its owner
    # allows.
    time_zone: str
    languages: tuple[str, ...]
    allowed_classes: tuple[str, ...]
    # The conversation it is in, if it is in one.
    conversation: UUID | None

    def changed(self, message: dict[str, Any]) -> DeviceHello:
        """The hello as a valid ``device.changed`` changes it."""
        changes: dict[str, Any] = {}
        if "time_zone" in message:
            changes["time_zone"] = message["time_zone"]
        for name in ("languages", "allowed_classes"):
            if name in message:
                changes[name] = tuple(message[name])
        return replace(self, **changes)


def encode(message: dict[str, Any]) -> str:
    """``message`` as one text frame's JSON."""
    return json.dumps(message, separators=(",", ":"), allow_nan=False)


def seq_of(message: Any) -> int | None:
    """A message's number, if it has one that the schema allows."""
    seq = message.get("seq") if isinstance(message, dict) else None
    if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= LARGEST_SEQ:
        return None
    return seq


def number(received: int, seq: int) -> Number:
    """Where the message ``seq`` stands, after the one numbered ``received``."""
    if seq <= received:
        return Number.DUPLICATE
    return Number.NEXT if seq == received + 1 else Number.SKIPPED


def read_hello(message: Any, schema: Schema) -> DeviceHello | Bye:
    """The device's hello, if ``message`` is one the cloud can take; otherwise why the
    session ends. ``schema`` checks a device's messages."""
    if not isinstance(message, dict) or message.get("type") != "hello":
        return Bye.NO_HELLO
    version = message.get("protocol")
    major_minor = _VERSION.fullmatch(version) if isinstance(version, str) else None
    if major_minor is not None and int(major_minor[1]) != MAJOR:
        # Another major's hello need not match this one's schema.
        return Bye.UNSUPPORTED_PROTOCOL
    if schema.problem(message) is not None:
        return Bye.NO_HELLO
    device = message["device"]
    conversation = message.get("conversation")
    return DeviceHello(
        # The schema allows major.minor only.
        minor=int(message["protocol"].partition(".")[2]),
        description=Description(device["kind"], device["model"], device["software"]),
        time_zone=message["time_zone"],
        languages=tuple(message["languages"]),
        allowed_classes=tuple(message["allowed_classes"]),
        conversation=None if conversation is None else UUID(conversation),
    )


def cloud_hello(
    session: UUID, hello: DeviceHello, now: datetime, ping_interval: float
) -> dict[str, Any]:
    """The cloud's answer to ``hello``, opening the new session ``session``. The cloud holds
    no conversations yet, and subscribes to no event."""
    return {
        "type": "hello",
        # The major, and the lower of the two minors.
        "protocol": f"{MAJOR}.{min(MINOR, hello.minor)}",
        "session": str(session),
        "resumed": False,
        "time": instant(now),
        "ping_interval": ping_interval,
        "conversations": False,
        "events": [],
    }


def instant(moment: datetime) -> str:
    """``moment`` in RFC 3339 and UTC, to the millisecond (Messages, Values)."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def bye(reason: Bye) -> dict[str, Any]:
    return {"type": "bye", "reason": str(reason)}


def error(reply_to: int, code: Code, text: str) -> dict[str, Any]:
    """An error about the device's message ``reply_to``, to number and send."""
    return {"type": "error", "reply_to": reply_to, "code": str(code), "message": text[:MAX_ERROR]}
