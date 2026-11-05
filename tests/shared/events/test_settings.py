# SPDX-License-Identifier: MIT
"""The events' settings: how long a retry waits, and what must hold between the delays."""

from typing import Any

import pytest
from events_module import EVENTS

from ai_cloud.entry.settings import CloudSettings
from ai_cloud.shared.config import ConfigError, deep_merge, load_config
from ai_cloud.shared.events import EventsSettings


def events(**changes: Any) -> EventsSettings:
    return EventsSettings.from_config({**EVENTS, **changes})


def test_each_retry_waits_twice_as_long_up_to_the_most() -> None:
    settings = events(retry_delay=1, retry_delay_max=4)
    assert [settings.backoff(deliveries) for deliveries in range(1, 6)] == [1, 2, 4, 4, 4]
    # Redis counts deliveries in 64 bits, and marks one that failed for good with the most.
    assert settings.backoff(2**63 - 1) == 4


def test_the_retries_take_their_waits_and_every_handler_s_time() -> None:
    settings = events(handler_timeout=0.5, retry_delay=1, retry_delay_max=4, max_deliveries=4)
    assert settings.retry_span() == 1 + 2 + 4 + 4 * 0.5


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"handler_timeout": 1, "retry_delay": 1}, "handler_timeout must be less than retry_delay"),
        ({"retry_delay": 2, "retry_delay_max": 1}, "retry_delay_max must be at least retry_delay"),
        ({"retention": 7.8}, "retention must be longer than an event's retries take"),
        ({"batch_size": 0}, "batch_size: Input should be greater than or equal to 1"),
        ({"poll_interval": 0}, "poll_interval: Input should be greater than 0"),
    ],
)
def test_the_delays_are_checked_together(changes: dict[str, Any], problem: str) -> None:
    with pytest.raises(ConfigError, match=problem):
        events(**changes)


def test_a_read_ends_before_redis_s_client_gives_up_on_it() -> None:
    config = load_config("development")
    with pytest.raises(ConfigError, match=r"events\.read_wait must be less than redis\.timeout"):
        CloudSettings.from_config(
            deep_merge(config, {"events": {"read_wait": config["redis"]["timeout"]}})
        )


def test_the_configuration_s_retries_end_well_within_the_retention() -> None:
    settings = CloudSettings.from_config(load_config("production")).events
    assert settings.retry_span() < settings.retention / 2
