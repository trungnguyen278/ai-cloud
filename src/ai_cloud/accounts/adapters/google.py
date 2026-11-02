# SPDX-License-Identifier: MIT
"""Sign in with Google: its ID tokens, as Google says to verify them.

Source, retrieved 2026-11-01: Google, "Verify the Google ID token on your server side"
(developers.google.com/identity/gsi/web/guides/verify-google-id-token), and its OpenID
Connect discovery document (accounts.google.com/.well-known/openid-configuration).
"""

from collections.abc import Collection

import httpx2

from ai_cloud.accounts.adapters.oidc import OidcProvider
from ai_cloud.shared.jose import JwksSettings, PublishedKeys

NAME = "google"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
KEYS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ALGORITHMS = ("RS256",)


def google(
    http: httpx2.AsyncClient, client_ids: Collection[str], jwks: JwksSettings, leeway: float
) -> OidcProvider:
    """Google, accepting ID tokens issued to one of the project's ``client_ids``."""
    return OidcProvider(
        NAME,
        issuers=ISSUERS,
        audiences=client_ids,
        keys=PublishedKeys(http, KEYS_URL, jwks),
        algorithms=ALGORITHMS,
        leeway=leeway,
    )
