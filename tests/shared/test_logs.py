# SPDX-License-Identifier: MIT
import json
import logging
import sys
from collections.abc import Iterator
from datetime import datetime

import pytest

from ai_cloud.shared.logs import (
    JsonFormatter,
    LoggingSettings,
    TextFormatter,
    configure_logging,
    fields,
)


def record(exc_info: tuple | None = None, **extra: object) -> logging.LogRecord:
    """An INFO record, ``device d1 connected``, with ``extra``."""
    return logging.getLogger("ai_cloud.test").makeRecord(
        "ai_cloud.test",
        logging.INFO,
        __file__,
        1,
        "device %s connected",
        ("d1",),
        exc_info,
        extra=extra,
    )


def test_json_has_what_every_record_has_then_its_fields() -> None:
    entry = json.loads(JsonFormatter().format(record(device="d1", turns=3)))
    assert entry.pop("time").endswith("+00:00")
    assert entry == {
        "level": "INFO",
        "logger": "ai_cloud.test",
        "message": "device d1 connected",
        "device": "d1",
        "turns": 3,
    }


def test_json_time_is_the_record_s() -> None:
    made = record()
    made.created = 1_800_000_000.25
    entry = json.loads(JsonFormatter().format(made))
    assert datetime.fromisoformat(entry["time"]).timestamp() == 1_800_000_000.25


def test_json_fields_never_hide_what_every_record_has() -> None:
    entry = json.loads(JsonFormatter().format(record(level="quiet", logger="other")))
    assert entry["level"] == "INFO"
    assert entry["logger"] == "ai_cloud.test"


def test_json_writes_any_value_and_the_exception() -> None:
    try:
        raise RuntimeError("broken")
    except RuntimeError:
        made = record(sys.exc_info(), turns={3})
    entry = json.loads(JsonFormatter().format(made))
    assert entry["turns"] == "{3}"
    assert entry["exception"].startswith("Traceback")
    assert "RuntimeError: broken" in entry["exception"]


def test_uvicorn_s_coloured_copy_is_not_a_field() -> None:
    assert fields(record(color_message="\x1b[1mdevice\x1b[0m", device="d1")) == {"device": "d1"}


def test_text_puts_the_fields_after_the_message() -> None:
    assert (
        TextFormatter()
        .format(record(device="d1"))
        .endswith("INFO    ai_cloud.test: device d1 connected device='d1'")
    )
    assert TextFormatter().format(record()).endswith("ai_cloud.test: device d1 connected")


@pytest.fixture
def root_logger() -> Iterator[logging.Logger]:
    """The root logger, put back as it was."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.mark.parametrize(("form", "formatter"), [("json", JsonFormatter), ("text", TextFormatter)])
def test_configure_sets_the_level_and_the_form(
    root_logger: logging.Logger, form: str, formatter: type[logging.Formatter]
) -> None:
    configure_logging(LoggingSettings(level="WARNING", format=form))  # type: ignore[arg-type]
    assert root_logger.level == logging.WARNING
    [handler] = root_logger.handlers
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr
    assert type(handler.formatter) is formatter
