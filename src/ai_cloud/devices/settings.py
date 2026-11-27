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
    # And the characters of their text, a second, in bursts of up to text_burst: what reading
    # them costs grows with them. A message longer than the burst waits for a full one. Both
    # count for the device on the replica, however many connections it holds there.
    text_rate: Annotated[float, Field(gt=0)]
    text_burst: Annotated[int, Field(ge=1)]
    # Connections a replica takes a second from all devices together, in bursts of up to
    # connect_burst, counted once a token verifies and its device's own rate lets it through;
    # past them it answers 503, with when to try again.
    connect_rate: Annotated[float, Field(gt=0)]
    connect_burst: Annotated[int, Field(ge=1)]
    # Seconds between two checks of a replica's connections against the registry and the
    # presence, and between two tries to hear hints after Redis failed: the longest that a
    # lost hint delays a bye.
    check_interval: Annotated[float, Field(gt=0)]
    # Seconds a connection's presence lasts unless the replica that holds it keeps it: longer
    # than check_interval.
    presence_ttl: Annotated[float, Field(gt=0)]
    # Seconds a session's end takes at most, from when it begins: the message it is handling,
    # if it pauses, giving its record up, its last messages and its close, and leaving its
    # presence, together. One that must stop the message it is handling is not resumed.
    close_timeout: Annotated[float, Field(gt=0)]
    # Seconds a session can be resumed after its connection was lost (resume_window), counted
    # from when the cloud saw the loss. One that its connection holds lasts presence_ttl
    # unless its replica keeps it.
    resume_window: Annotated[float, Field(gt=0)]
    # How many sent messages a session keeps until its device says it received them, to send
    # again on a resumed one (replay); past them, the oldest go, and a device that had not
    # received them gets a new session.
    replay: Annotated[int, Field(ge=1)]
    # Seconds a hello that resumes a session waits for the connection that holds it to hand it
    # over, past which the device gets a new session: shorter than hello_timeout, which the
    # device waits for the cloud's hello.
    takeover_timeout: Annotated[float, Field(gt=0)]

    @model_validator(mode="after")
    def _presence_outlasts_a_check(self) -> Self:
        if self.presence_ttl <= self.check_interval:
            # Otherwise presence would lapse between two checks of a connection still open.
            raise ValueError("presence_ttl must be longer than check_interval")
        return self

    @model_validator(mode="after")
    def _takeover_fits_in_a_hello(self) -> Self:
        if self.takeover_timeout >= self.hello_timeout:
            # Otherwise the device would give up on the cloud's hello first.
            raise ValueError("takeover_timeout must be shorter than hello_timeout")
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
    # How often one device may open its control channel (SEC-15).
    connection_rate: RateSettings
    # Their control channels.
    link: LinkSettings
