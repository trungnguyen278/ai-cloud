# SPDX-License-Identifier: MIT
"""The accounts' table of the configuration, ``[accounts]`` (the app API's Settings)."""

from typing import Annotated, Self

from pydantic import Field, model_validator

from ai_cloud.shared.config import Settings
from ai_cloud.shared.jose import JwksSettings
from ai_cloud.shared.rates import AddressRateSettings, RateSettings

# Bounds the app API sets on what the cloud answers (platform/protocols/app/schema.json).
_NONCE_RESPONSE_MAX = 3600  # $defs/nonce_response, expires_in
_TOKEN_RESPONSE_MAX = 86400  # $defs/token_response, expires_in

Text = Annotated[str, Field(min_length=1)]


class GoogleSettings(Settings):
    # The project's Google client IDs that an ID token may be issued to (its aud). With
    # none, nobody signs in with Google.
    client_ids: list[Text]


class DevelopmentSettings(Settings):
    # Whether the development provider signs test people in, without Google or Apple
    # (the app API's Development). Anyone who reaches the api can then sign in as anyone
    # they like: never in production.
    enabled: bool
    # The issuer of its ID tokens.
    issuer: Text
    # The client its ID tokens are for: one of the accounts' client_ids.
    client_id: Text
    # Seconds its ID tokens are valid.
    id_token_lifetime: Annotated[int, Field(ge=1)]


class AccountsSettings(Settings):
    # The OAuth client IDs of the apps that may sign in (the app API's client_id).
    client_ids: Annotated[list[Text], Field(min_length=1)]
    # Seconds a nonce stays valid (nonce_lifetime).
    nonce_lifetime: Annotated[int, Field(ge=1, le=_NONCE_RESPONSE_MAX)]
    # Seconds an access token is valid (access_lifetime).
    access_lifetime: Annotated[int, Field(ge=1, le=_TOKEN_RESPONSE_MAX)]
    # Seconds a session lasts unused (refresh_idle).
    refresh_idle: Annotated[int, Field(ge=1)]
    # Seconds after its first use within which a refresh token refreshes again, while its
    # replacement is unused (refresh_reuse): 0 allows no retry.
    refresh_reuse: Annotated[int, Field(ge=0)]
    # Seconds since it was issued within which an ID token may delete its account or a
    # home, or hand a home over (recent_sign_in).
    recent_sign_in: Annotated[int, Field(ge=1)]
    # How many homes an account may be in (homes_per_account).
    homes_per_account: Annotated[int, Field(ge=1)]
    # How many people a home may have, profiles included (members_per_home).
    members_per_home: Annotated[int, Field(ge=1)]
    # Seconds an invitation's code is valid (invitation_lifetime).
    invitation_lifetime: Annotated[int, Field(ge=1)]
    # How many open invitations a home may have (invitations_per_home).
    invitations_per_home: Annotated[int, Field(ge=1)]
    # How many invitation codes an account may try (invitation_rate).
    invitation_rate: RateSettings
    # How many nonces an address gets (nonce_rate).
    nonce_rate: AddressRateSettings
    # Fetching the providers' published keys.
    jwks: JwksSettings
    google: GoogleSettings
    development: DevelopmentSettings

    @model_validator(mode="after")
    def _development_client_is_known(self) -> Self:
        if self.development.client_id not in self.client_ids:
            raise ValueError("development.client_id must be one of client_ids")
        return self
