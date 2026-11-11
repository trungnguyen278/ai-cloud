# SPDX-License-Identifier: MIT
"""OAuth 2.0 as the cloud speaks it: the form its token endpoints take, how they answer an
error (RFC 6749, 3.2, 5.1 and 5.2), and the bearer tokens that requests carry (RFC 6750).
The app's sign-in and API, and the devices' tokens and control channels, all speak it."""

from collections.abc import Mapping
from urllib.parse import parse_qsl

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

FORM = "application/x-www-form-urlencoded"
# What answers carrying tokens or personal data say to caches (RFC 6749, 5.1).
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
# What a resource answers along with a 401 for its access token (RFC 6750, 3).
CHALLENGE = {"WWW-Authenticate": 'Bearer error="invalid_token"'}


class OAuthError(Exception):
    """An error as a token endpoint answers it: its code, and a description for logs."""

    def __init__(
        self,
        error: str,
        description: str,
        status: int = 400,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status
        self.headers = dict(headers or {})

    def response(self) -> JSONResponse:
        body = {"error": self.error, "error_description": self.description}
        return JSONResponse(body, self.status, headers={**NO_STORE, **self.headers})


def bearer(headers: Headers) -> str | None:
    """The access token that a request's Authorization header carries (RFC 6750, 2.1), if it
    carries one."""
    scheme, _, token = headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def media_type(request: Request) -> str:
    return request.headers.get("content-type", "").partition(";")[0].strip().lower()


async def form(request: Request) -> dict[str, str]:
    """The endpoint's form fields. A field without a value counts as absent, and none may come
    twice (RFC 6749, 3.1 and 3.2)."""
    if media_type(request) != FORM:
        raise OAuthError("invalid_request", f"the body must be {FORM}")
    body = await request.body()
    try:
        pairs = parse_qsl(body.decode(), keep_blank_values=True, strict_parsing=True)
    except ValueError:  # UnicodeDecodeError included
        raise OAuthError("invalid_request", "the body is not a form") from None
    fields: dict[str, str] = {}
    seen: set[str] = set()
    for name, value in pairs:
        if name in seen:
            raise OAuthError("invalid_request", f"{name} is repeated")
        seen.add(name)
        if value:
            fields[name] = value
    return fields
