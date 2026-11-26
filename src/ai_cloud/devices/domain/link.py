# SPDX-License-Identifier: MIT
"""The control channel's session, as the cloud link 3.7 has it, without I/O
(platform/protocols/cloud/README.md: The control channel, Staying connected, Resuming a
session, Messages, The session, Errors).

A device opens a WebSocket with its access token and says its ``hello`` first; the cloud
answers with its own. Each side numbers what it sends in the session, but for the messages
that open, keep and end it: a number not above the last one handled is a duplicate, and one
that skips a number ends the session. What a device sends is checked against the
protocol's schema (SEC-14), and errors name where a message is wrong, never what it holds.
A frame that has no number for an error to answer ends the session (Messages, Validation).

A session outlives its connection: a hello that names it resumes it, under the terms that
its first hello set, with what each side sent after the other's ``received`` sent again
(Resuming a session; docs/decisions/0013-resuming-sessions.md).
"""

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum, IntEnum, StrEnum
from typing import Any
from uuid import UUID

from ai_cloud.devices.domain.model import Description
from ai_cloud.shared.protocols import Schema, Unreadable, read_json

# Fixed by the cloud link (platform/protocols/cloud/README.md).
PROTOCOL = "cloud"  # its schema, platform/protocols/cloud/schema.json
PATH = "/device/v3/link"  # The cloud's address: the path carries the major version
MAJOR, MINOR = 3, 7  # the version the cloud speaks
LEAVE_MINOR = 2  # leave: "in a session of 3.2 or later only"
MAX_MESSAGE = 1 << 20  # Messages, Sizes: "A message holds up to 1 MiB of UTF-8"
UNNUMBERED = frozenset({"hello", "ping", "pong", "reconnect", "bye"})  # Resuming, Numbers
LARGEST_SEQ = 2**53 - 1  # $defs/seq: what a JSON number holds exactly
SILENT_PINGS = 2  # Staying connected: closed after "nothing has come for two intervals"
MAX_ERROR = 1024  # $defs/error, message
MAX_BYE = 256  # $defs/bye, message: $defs/short_text
# What a bye no_hello says, for the device's logs: which of its two causes ended the session
# (hello; the first, as the example first-message.json has it).
NOT_FIRST = "the first message was not a hello"
NOT_IN_TIME = "the hello did not come in time"
_VERSION = re.compile(r"([0-9]{1,3})\.([0-9]{1,3})")  # $defs/version: major.minor


class Bye(StrEnum):
    """Why the cloud ends a session (The session, bye)."""

    # A newer connection of the device took over.
    REPLACED = "replaced"
    # The device is no longer registered: removed, or gone with its home.
    REVOKED = "revoked"
    # Its hello did not come in time, or another message came before it.
    NO_HELLO = "no_hello"
    # Its hello named another major version than the path.
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    # A message skipped a number.
    OUT_OF_SEQUENCE = "out_of_sequence"
    # A frame that the cloud cannot answer: it could not be read, it had no number, or it
    # was a hello, a ping, a pong or a reconnect that failed the schema (Messages,
    # Validation).
    INVALID_MESSAGE = "invalid_message"
    # The cloud's own failure, as a side takes a reason it does not know (Messages, Unknown
    # content).
    ERROR = "error"
    # The device left its home, as its leave asked (leave). Since 3.2.
    LEFT = "left"


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
    # A frame that breaks WebSocket's own rules: "protocol error". The server closes with it
    # (RFC 6455, 5.1 to 5.5 and 7.4.1), and either side may; both then end the session
    # (Messages, Validation, since 3.6).
    PROTOCOL_ERROR = 1002
    # Text that is no UTF-8: "invalid frame payload data". The server closes with it (RFC
    # 6455, 8.1), and either side may; both then end the session (Messages, Validation,
    # since 3.5).
    INVALID_TEXT = 1007
    # Frames below the session broke the link's rules for them: "policy violation". The
    # server closes with it (Messages, Frames, since 3.4).
    POLICY = 1008
    # A message larger than MAX_MESSAGE: "message too big". The server closes with it, and
    # either side may; both then end the session (Messages, Sizes).
    TOO_BIG = 1009
    # The cloud failed: the device connects again.
    INTERNAL = 1011
    # After reconnect: the cloud restarts, and the device connects again at once.
    RESTART = 1012
    # A newer connection resumed the session: "taken over", the link's first of WebSocket's
    # private codes (RFC 6455, 7.4.2), with no bye, which would end the session (Resuming a
    # session, Taken over, since 3.7).
    TAKEN_OVER = 4000


@dataclass(frozen=True)
class Closed:
    """A channel that closed, with the close code it closed with, if it had one."""

    code: int | None = None


@dataclass(frozen=True)
class Message:
    """A message that the cloud can answer (Messages, Validation): a JSON object that every
    side can hold, whose type is text, and which has its number unless its type has none."""

    type: str
    # None for the types that open, keep and end the numbering.
    seq: int | None
    body: dict[str, Any]


@dataclass(frozen=True)
class Unanswerable:
    """A frame that the cloud cannot answer, which ends the session with ``bye``
    ``invalid_message``: why, in a few words that name no value."""

    why: str


class Number(Enum):
    """Where a numbered message stands against the last one handled from its side."""

    # The next one: it is handled.
    NEXT = "next"
    # Not above the last one: sent again, and handled already (SES-6).
    DUPLICATE = "duplicate"
    # It skipped a number: the session ends.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Resume:
    """The session that a hello resumes, and the last number its device received in it."""

    session: UUID
    received: int


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
    # The session it resumes, if it resumes one.
    resume: Resume | None
    # The terms that a session's first hello sets, which a hello that resumes it keeps: its
    # protocol, device, conversations and media, as the SHA-256 of one text that is the same
    # for the same terms (The session, hello: A resuming hello), so that what a device adds
    # to them costs its session's record 64 characters, whatever their size. The cloud
    # reads no events yet.
    terms: str

    @property
    def session_minor(self) -> int:
        """The minor version of a new session: the lower of its own and the cloud's."""
        return min(MINOR, self.minor)

    def changed(self, message: dict[str, Any]) -> DeviceHello:
        """The hello as a valid ``device.changed`` changes it."""
        changes: dict[str, Any] = {}
        if "time_zone" in message:
            changes["time_zone"] = message["time_zone"]
        for name in ("languages", "allowed_classes"):
            if name in message:
                changes[name] = tuple(message[name])
        return replace(self, **changes)


@dataclass(frozen=True)
class Record:
    """What the cloud keeps of a session for its device to resume it (Resuming a session):
    whose it is, under which registration, its terms and its version; then the last number
    handled from the device, the last one sent, and the text of each message sent that the
    device has not said it received, in order, up to ``replay`` of them."""

    session: UUID
    device_id: UUID
    registered: datetime
    terms: str
    minor: int
    received: int = 0
    sent: int = 0
    kept: tuple[str, ...] = ()


class Claim(StrEnum):
    """Why a hello that resumes a session cannot take it now: it waits for the connection that
    holds it to hand it over, or the device gets a new session."""

    # A connection holds it, and is to hand it over.
    WAITING = "waiting"
    # The cloud no longer holds it: ended, lapsed, lost, or not the device's newest.
    GONE = "gone"
    # It is the device's under a registration that was removed since (SEC-6).
    REGISTRATION = "registration"
    # The hello changed the session's terms.
    TERMS = "terms"
    # The cloud no longer speaks the session's version.
    VERSION = "version"
    # A newer connection of the device claimed it since, or took it: it goes on there.
    SUPERSEDED = "superseded"


class Keeping(Enum):
    """What a check finds of the session that a connection holds."""

    # Still the connection's.
    HELD = "held"
    # A newer connection resumes it: the older one is taken over.
    TAKEN = "taken"
    # A newer connection has a new session: the older one's is replaced.
    REPLACED = "replaced"
    # Lost, as Redis may lose it (ADR-0008): the session goes on, and its next loss ends it.
    MISSING = "missing"


class Handing(Enum):
    """What became of a session that its connection gave up, without a bye."""

    # Handed to the newer connection that resumes it.
    HANDED = "handed"
    # Kept for resume_window.
    PARKED = "parked"
    # No longer the connection's to give: replaced, or lost.
    LOST = "lost"


def encode(message: dict[str, Any]) -> str:
    """``message`` as one text frame's JSON."""
    return json.dumps(message, separators=(",", ":"), allow_nan=False)


def read_message(frame: str | bytes) -> Message | Unanswerable:
    """The message that ``frame`` holds, if the cloud can answer it; otherwise why not. JSON
    that every side cannot hold is refused alike, however far its reader got (ai-platform's
    ADR-0014)."""
    if isinstance(frame, bytes):
        # One message per text frame (The control channel).
        return Unanswerable("a binary frame")
    try:
        body = read_json(frame)
    except Unreadable as why:
        return Unanswerable(f"not JSON that every side can hold: {why}")
    if not isinstance(body, dict):
        return Unanswerable("not a JSON object")
    kind = body.get("type")
    if not isinstance(kind, str):
        return Unanswerable("no type that is text")
    if kind in UNNUMBERED:
        return Message(kind, None, body)
    seq = seq_of(body)
    if seq is None:
        return Unanswerable("a numbered type without its number")
    return Message(kind, seq, body)


def seq_of(message: dict[str, Any]) -> int | None:
    """A message's number, if it has one that the schema allows: an integer, which JSON
    Schema also reads in a number such as ``1.0``."""
    seq = message.get("seq")
    if isinstance(seq, float) and seq.is_integer():
        seq = int(seq)
    if isinstance(seq, bool) or not isinstance(seq, int) or not 1 <= seq <= LARGEST_SEQ:
        return None
    return seq


def number(received: int, seq: int) -> Number:
    """Where the message ``seq`` stands, after the one numbered ``received``."""
    if seq <= received:
        return Number.DUPLICATE
    return Number.NEXT if seq == received + 1 else Number.SKIPPED


def read_hello(first: Message, schema: Schema) -> DeviceHello | Bye | Unanswerable:
    """The device's hello, if its first message is one that the cloud can take; otherwise why
    the session ends. ``schema`` checks a device's messages."""
    if first.type != "hello":
        if first.seq is None and (problem := schema.problem(first.body)) is not None:
            # A ping, a pong or a reconnect that fails, which no error can answer, whenever
            # it comes (Messages, Validation).
            return Unanswerable(f"{first.type}: {problem}")
        # The hello did not come first.
        return Bye.NO_HELLO
    message = first.body
    version = message.get("protocol")
    major_minor = _VERSION.fullmatch(version) if isinstance(version, str) else None
    if major_minor is not None and int(major_minor[1]) != MAJOR:
        # Another major's hello need not match this one's schema.
        return Bye.UNSUPPORTED_PROTOCOL
    if (problem := schema.problem(message)) is not None:
        # A hello that fails has no number for an error to answer (The session, hello).
        return Unanswerable(f"hello: {problem}")
    device = message["device"]
    conversation = message.get("conversation")
    resume = message.get("resume")
    return DeviceHello(
        # The schema allows major.minor only.
        minor=int(message["protocol"].partition(".")[2]),
        description=Description(device["kind"], device["model"], device["software"]),
        time_zone=message["time_zone"],
        languages=tuple(message["languages"]),
        allowed_classes=tuple(message["allowed_classes"]),
        conversation=None if conversation is None else UUID(conversation),
        resume=None
        if resume is None
        # $defs/count is an integer, which JSON Schema also reads in a number such as 1.0.
        else Resume(UUID(resume["session"]), int(resume["received"])),
        terms=hashlib.sha256(
            json.dumps(
                {name: message[name] for name in ("protocol", "device", "conversations", "media")},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    )


def cloud_hello(
    session: UUID, minor: int, now: datetime, ping_interval: float, received: int | None = None
) -> dict[str, Any]:
    """The cloud's hello in the session ``session`` of version ``minor``: a new one, or one
    resumed after the device's message ``received``. The cloud holds no conversations yet,
    and subscribes to no event, in any session: a resumed one keeps them as they were."""
    said: dict[str, Any] = {
        "type": "hello",
        "protocol": f"{MAJOR}.{minor}",
        "session": str(session),
        "resumed": received is not None,
    }
    if received is not None:
        said["received"] = received
    return {
        **said,
        "time": instant(now),
        "ping_interval": ping_interval,
        "conversations": False,
        "events": [],
    }


def instant(moment: datetime) -> str:
    """``moment`` in RFC 3339 and UTC, to the millisecond (Messages, Values)."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def bye(reason: Bye, message: str | None = None) -> dict[str, Any]:
    """A bye, with a ``message`` for the device's logs if there is one (The session, bye)."""
    said = {"type": "bye", "reason": str(reason)}
    return said if message is None else {**said, "message": message[:MAX_BYE]}


def error(reply_to: int, code: Code, text: str) -> dict[str, Any]:
    """An error about the device's message ``reply_to``, to number and send."""
    return {"type": "error", "reply_to": reply_to, "code": str(code), "message": text[:MAX_ERROR]}
