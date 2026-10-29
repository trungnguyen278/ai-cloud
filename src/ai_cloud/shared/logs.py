# SPDX-License-Identifier: MIT
"""Logs: structured, and without content (docs/architecture.md, Observability; PRIV-5).

Every process logs through the standard library's ``logging``, as uvicorn and LiveKit
Agents do. A record's fields come from ``extra``::

    log.info("device connected", extra={"device": device_id})

What a user says, hears or is shown never goes in a log, nor does a secret. In
production each record is a JSON object on one line, for the machine that collects
them; in development, a line of text for a person.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from ai_cloud.shared.config import Settings

TEXT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# What every record has (logging.LogRecord's attributes), which is not a field of its own.
_RECORD = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
# uvicorn's copy of each of its messages, with colours.
_NOT_FIELDS = frozenset({"color_message"})


class LoggingSettings(Settings):
    # The least severe records kept.
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    # json: an object per line, for a machine; text: for a person.
    format: Literal["json", "text"]


def configure_logging(settings: LoggingSettings) -> None:
    """Send this process's records to the standard error, as ``settings`` say."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if settings.format == "json" else TextFormatter())
    logging.basicConfig(level=settings.level, handlers=[handler], force=True)


class JsonFormatter(logging.Formatter):
    """A record as a JSON object: its time (RFC 3339, UTC), level, logger and message,
    then its fields, and its exception if it has one."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in fields(record).items():
            entry.setdefault(key, value)  # a field never hides what every record has
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, default=str)


class TextFormatter(logging.Formatter):
    """A record as a line of text, its fields after the message as ``key=value``."""

    def __init__(self) -> None:
        super().__init__(TEXT_FORMAT)

    def formatMessage(self, record: logging.LogRecord) -> str:
        text = super().formatMessage(record)
        extra = " ".join(f"{key}={value!r}" for key, value in fields(record).items())
        return f"{text} {extra}" if extra else text


def fields(record: logging.LogRecord) -> dict[str, Any]:
    """The fields ``extra`` gave a record."""
    return {
        key: value
        for key, value in vars(record).items()
        if key not in _RECORD and key not in _NOT_FIELDS
    }
