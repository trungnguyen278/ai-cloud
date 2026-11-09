# SPDX-License-Identifier: MIT
"""The devices' table of the configuration, ``[devices]`` (the app API's Settings)."""

from typing import Annotated

from pydantic import Field

from ai_cloud.shared.config import Settings
from ai_cloud.shared.rates import RateSettings

# Bounds the app API sets on what the cloud answers (platform/protocols/app/schema.json).
_SERVER_PATTERN = r"^https?://"  # $defs/device_challenge, server
_SERVER_MAX = 2048
_CHALLENGE_RESPONSE_MAX = 3600  # $defs/device_challenge, expires_in


class DevicesSettings(Settings):
    # The address that devices use to reach the cloud, which the app passes on at set-up:
    # their registrations name it as their audience (the app API's server).
    server: Annotated[str, Field(pattern=_SERVER_PATTERN, max_length=_SERVER_MAX)]
    # Seconds a challenge to register a device is valid (challenge_lifetime).
    challenge_lifetime: Annotated[int, Field(ge=1, le=_CHALLENGE_RESPONSE_MAX)]
    # How many challenges an account gets (challenge_rate).
    challenge_rate: RateSettings
    # How many devices a home may have (devices_per_home).
    devices_per_home: Annotated[int, Field(ge=1)]
