# SPDX-License-Identifier: MIT
"""The devices' table of the configuration: what the cloud answers stays within what the app
API's schema allows."""

from typing import Any

import pytest

from ai_cloud.devices.settings import LinkSettings
from ai_cloud.shared.config import ConfigError


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"server": "cloud.example"}, "devices.server"),
        ({"server": "ftp://cloud.example"}, "devices.server"),
        ({"server": "https://" + "a" * 2041}, "devices.server"),
        ({"challenge_lifetime": 0}, "devices.challenge_lifetime"),
        ({"challenge_lifetime": 3601}, "devices.challenge_lifetime"),
        ({"devices_per_home": 0}, "devices.devices_per_home"),
        ({"link": {"resume_window": 0}}, "devices.link.resume_window"),
        ({"link": {"replay": 0}}, "devices.link.replay"),
        ({"link": {"takeover_timeout": 0}}, "devices.link.takeover_timeout"),
        ({"link": {"takeover_timeout": 5}}, "takeover_timeout must be shorter than hello_timeout"),
        ({"link": {"presence_ttl": 20}}, "presence_ttl must be longer than check_interval"),
    ],
)
def test_the_devices_settings_are_checked(
    make_settings: Any, changes: dict[str, Any], problem: str
) -> None:
    with pytest.raises(ConfigError, match=problem):
        make_settings(devices=changes)


def test_the_longest_address_will_do(make_settings: Any) -> None:
    server = "https://" + "a" * 2040
    assert make_settings(devices={"server": server}).devices.server == server


def test_how_long_a_session_is_kept_is_set_for_each_deployment() -> None:
    """No default: each deployment's configuration says how long, and how much, a session
    keeps while its device is away, and how long a takeover may take."""
    required = {name for name, field in LinkSettings.model_fields.items() if field.is_required()}
    assert {"resume_window", "replay", "takeover_timeout"} <= required
