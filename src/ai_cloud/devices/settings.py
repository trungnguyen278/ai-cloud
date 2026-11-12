# SPDX-License-Identifier: MIT
"""The devices' table of the configuration, ``[devices]`` (the app API's Settings)."""

from typing import Annotated, Self

from pydantic import Field, model_validator

from ai_cloud.shared.config import Settings
from ai_cloud.shared.rates import AddressRateSettings, RateSettings

# Bounds the app API sets on what the cloud answers (platform/protocols/app/schema.json).
_SERVER_PATTERN = r"^https?://"  # $defs/device_challenge, server
_SERVER_MAX = 2048
_CHALLENGE_RESPONSE_MAX = 3600  # $defs/device_challenge, expires_in
# And the cloud link (platform/protocols/cloud/schema.json).
_TOKEN_RESPONSE_MAX = 86400  # $defs/token_response, expires_in
_PING_INTERVAL_MAX = 3600  # $defs/cloud_hello, ping_interval


class LinkSettings(Settings):
    """The devices' control channels (the cloud link's Settings, and
    docs/decisions/0011-control-channels.md)."""

    # Seconds the cloud waits for a device's hello once its connection is open
    # (hello_timeout).
    hello_timeout: Annotated[float, Field(gt=0)]
    # Seconds between a device's pings, which the cloud's hello gives (ping_interval). The
    # cloud closes a connection from which nothing has come for two of them, or to which
    # nothing could be sent for as long.
    ping_interval: Annotated[float, Field(gt=0, le=_PING_INTERVAL_MAX)]
    # What the cloud takes from a device (SEC-15): messages a second, in bursts of up to
    # burst; up to backlog more wait their turn, and past them the cloud drops what comes.
    rate: Annotated[float, Field(gt=0)]
    burst: Annotated[int, Field(ge=1)]
    backlog: Annotated[int, Field(ge=1)]
    # How deep a device's message may nest its arrays and objects: deeper, it is unreadable.
    max_json_depth: Annotated[int, Field(ge=1)]
    # Seconds between two checks of a replica's connections against the registry and the
    # presence, and between two tries to hear hints after Redis failed: the longest that a
    # lost hint delays a bye.
    check_interval: Annotated[float, Field(gt=0)]
    # Seconds a connection's presence lasts unless the replica that holds it keeps it: longer
    # than check_interval.
    presence_ttl: Annotated[float, Field(gt=0)]
    # Seconds the cloud waits, when it goes away, for its last messages to leave.
    close_timeout: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def _presence_outlasts_a_check(self) -> Self:
        if self.presence_ttl <= self.check_interval:
            # Otherwise presence would lapse between two checks of a connection still open.
            raise ValueError("presence_ttl must be longer than check_interval")
        return self


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
    # Seconds a device's access token is valid (the cloud link's Tokens from the server).
    access_lifetime: Annotated[int, Field(ge=1, le=_TOKEN_RESPONSE_MAX)]
    # How many tokens one address may ask for (SEC-15).
    token_rate: AddressRateSettings
    # Their control channels.
    link: LinkSettings
