# SPDX-License-Identifier: MIT
"""What a domain event is: its attributes, as CloudEvents 1.0 names them, and the names of
its type, its stream and its consumers."""

import json
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_cloud.shared.events import Consumer, Event
from ai_cloud.shared.events.model import MalformedEvent, consumer_module, module_of, rfc3339


def an_event(**changes: object) -> Event:
    fields: dict = {
        "id": uuid.uuid7(),
        "source": "/accounts",
        "type": "accounts.home.deleted.v1",
        "subject": "0199",
        "time": datetime(2026, 11, 5, 9, 30, 0, 123456, tzinfo=UTC),
        "data": {"home_id": "0199", "name": "Nhà Linh"},
    }
    return Event(**{**fields, **changes})


@pytest.mark.parametrize(
    ("type_", "module"),
    [
        ("accounts.home.deleted.v1", "accounts"),
        ("accounts.member.removed.v12", "accounts"),
        ("devices.device.software_updated.v2", "devices"),
    ],
)
def test_a_type_names_its_module_its_thing_what_happened_and_its_version(
    type_: str, module: str
) -> None:
    assert module_of(type_) == module


@pytest.mark.parametrize(
    "type_",
    [
        "accounts.home.deleted",
        "accounts.deleted.v1",
        "accounts.home.deleted.v0",
        "accounts.home.deleted.V1",
        "Accounts.home.deleted.v1",
        "accounts.home-deleted.x.v1",
        "com.example.accounts.home.deleted",
        "accounts.home.deleted.v1\n",
        "",
    ],
)
def test_anything_else_is_no_type(type_: str) -> None:
    with pytest.raises(ValueError, match="is not <module>"):
        module_of(type_)


def test_a_consumer_is_named_by_its_module_and_what_it_does() -> None:
    assert consumer_module("devices.home_deletions") == "devices"
    for name in ("devices", "devices.home.deletions", "Devices.x", "devices.home-deletions"):
        with pytest.raises(ValueError, match="is not <module>"):
            consumer_module(name)


async def nothing(_event: Event) -> None:
    pass


def test_a_consumer_reads_the_streams_of_the_types_it_handles() -> None:
    consumer = Consumer(
        "devices.cleaning",
        {
            "accounts.home.deleted.v1": nothing,
            "accounts.member.removed.v1": nothing,
            "billing.plan.ended.v1": nothing,
        },
    )
    assert consumer.streams == ["accounts:events", "billing:events"]
    assert consumer.dead_letters == "devices:dead-events"


@pytest.mark.parametrize(
    ("name", "handlers"),
    [
        ("devices", {"accounts.home.deleted.v1": nothing}),
        ("devices.cleaning", {}),
        ("devices.cleaning", {"accounts.home.deleted": nothing}),
    ],
)
def test_a_consumer_is_checked(name: str, handlers: dict) -> None:
    with pytest.raises(ValueError, match=r"consumer|is not"):
        Consumer(name, handlers)


def test_an_event_travels_as_cloudevents_attributes() -> None:
    event = an_event()
    fields = event.fields()
    data = fields.pop("data")
    assert fields == {
        "specversion": "1.0",
        "id": str(event.id),
        "source": "/accounts",
        "type": "accounts.home.deleted.v1",
        "subject": "0199",
        "time": "2026-11-05T09:30:00.123456Z",
        "datacontenttype": "application/json",
    }
    # In ASCII, the name's accent escaped.
    assert data.isascii()
    assert json.loads(data) == event.data
    assert Event.from_fields(event.fields()) == event
    assert event.module == "accounts"


def test_a_time_is_written_in_utc_with_z() -> None:
    hanoi = timezone(timedelta(hours=7))
    assert rfc3339(datetime(2026, 11, 5, 16, 30, tzinfo=hanoi)) == "2026-11-05T09:30:00.000000Z"


@pytest.mark.parametrize(
    "changes",
    [
        {"specversion": "0.3"},
        {"datacontenttype": "text/plain"},
        {"id": "not a uuid"},
        {"time": "yesterday"},
        {"time": "2026-11-05T09:30:00"},
        {"data": "{not json"},
        {"data": "[1, 2]"},
        {"type": "home.deleted"},
    ],
)
def test_fields_that_make_no_event_are_refused(changes: dict[str, str]) -> None:
    fields = {**an_event().fields(), **changes}
    with pytest.raises(MalformedEvent):
        Event.from_fields(fields)


@pytest.mark.parametrize("missing", ["id", "type", "data", "specversion"])
def test_a_missing_field_is_named(missing: str) -> None:
    fields = an_event().fields()
    del fields[missing]
    with pytest.raises(MalformedEvent, match=f"no {missing}"):
        Event.from_fields(fields)
