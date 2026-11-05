# SPDX-License-Identifier: MIT
"""The devices' table of the configuration: what the cloud answers stays within what the app
API's schema allows."""

from typing import Any

import pytest

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
