# SPDX-License-Identifier: MIT
"""What the app API's routes share: what its requests and answers look like, and how errors
become problems (platform/protocols/app/README.md, Requests and Errors)."""

import json
import math
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import Request

from ai_cloud.accounts.domain import errors
from ai_cloud.shared.problems import Problem
from ai_cloud.shared.protocols import Schema

# Fixed by the app API (platform/protocols/app/README.md, and schema.json).
PROTOCOL = "app"
API = "/app/v1"
PROBLEMS = f"{API}/problems/"
PAGE_SIZE_DEFAULT = 20  # Sessions
PAGE_SIZE_MAX = 50

_JSON = "application/json"
# What answers carrying tokens or personal data say to caches (RFC 6749, 5.1).
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
# What a request without a valid access token gets (RFC 6750, 3; the app API, Requests).
CHALLENGE = {"WWW-Authenticate": 'Bearer error="invalid_token"'}

# The problems the app acts on (the app API, Errors), with their titles.
_TYPED: dict[type[errors.AccountsError], tuple[str, int, str]] = {
    errors.OwnsHomeWithMembers: ("owns_home_with_members", 409, "Owns a home with members"),
    errors.IdentityTaken: ("identity_taken", 409, "Identity taken"),
    errors.IdentityLinked: ("identity_linked", 409, "Identity linked"),
    errors.LastIdentity: ("last_identity", 409, "Last identity"),
    errors.RecentSignInRequired: ("recent_sign_in_required", 403, "Recent sign-in required"),
    errors.RoleRequired: ("role_required", 403, "Role required"),
    errors.OwnerCannotLeave: ("owner_cannot_leave", 409, "Owner cannot leave"),
    errors.InvitationInvalid: ("invitation_invalid", 404, "Invitation invalid"),
    errors.HomeLimitReached: ("home_limit_reached", 409, "Home limit reached"),
    errors.MemberLimitReached: ("member_limit_reached", 409, "Member limit reached"),
    errors.InvitationLimitReached: ("invitation_limit_reached", 409, "Invitation limit reached"),
}
# Any other problem is about:blank.
_STATUSES: dict[type[errors.AccountsError], int] = {
    errors.Unauthenticated: 401,
    errors.IdTokenRefused: 403,
    errors.NotFound: 404,
    errors.InvalidPageToken: 400,
    errors.UnknownTimeZone: 400,
    errors.TooManyIdentities: 409,
    errors.NotTransferable: 409,
    errors.RoleNotChangeable: 409,
    errors.IdempotencyKeyBusy: 409,
    errors.PreconditionFailed: 412,
    errors.IdempotencyKeyReused: 422,
    errors.ProviderUnavailable: 503,
}


async def call[T](work: Awaitable[T]) -> T:
    """``work``'s result; its errors as the API answers them."""
    try:
        return await work
    except errors.AccountsError as error:
        raise _problem(error) from None


def _problem(error: errors.AccountsError) -> Problem:
    if isinstance(error, errors.RateLimited):
        retry = str(math.ceil(error.retry_after))
        return Problem(429, str(error), headers={"Retry-After": retry})
    for kind, (code, status, title) in _TYPED.items():
        if isinstance(error, kind):
            return Problem(status, str(error), type=PROBLEMS + code, title=title)
    for kind, status in _STATUSES.items():
        if isinstance(error, kind):
            headers = CHALLENGE if status == 401 else None
            return Problem(status, str(error), headers=headers)
    raise error


def media_type(request: Request) -> str:
    return request.headers.get("content-type", "").partition(";")[0].strip().lower()


async def json_body(request: Request, schema: Schema) -> dict[str, Any]:
    """The request's JSON body, checked against ``schema``."""
    if media_type(request) != _JSON:
        raise Problem(415, f"the body must be {_JSON}")
    try:
        body = json.loads(await request.body())
        # A lone surrogate, which JSON's escapes can spell, is no text: nothing can store it.
        json.dumps(body, ensure_ascii=False).encode()
    except ValueError:  # UnicodeDecodeError and UnicodeEncodeError included
        raise Problem(400, "the body is not JSON") from None
    if (problem := schema.problem(body)) is not None:
        raise Problem(400, problem)
    return body


def item_id(text: str) -> UUID:
    """An identifier in a request: one that is not a UUID names nothing."""
    try:
        return UUID(text)
    except ValueError:
        raise Problem(404, "no such item in the account") from None


def page_request(request: Request) -> tuple[int, str | None]:
    """The page size and token that a request to a list asks for."""
    text = request.query_params.get("page_size")
    if text is None:
        page_size = PAGE_SIZE_DEFAULT
    elif text.isascii() and text.isdigit() and 1 <= int(text) <= PAGE_SIZE_MAX:
        page_size = int(text)
    else:
        raise Problem(400, f"page_size must be from 1 to {PAGE_SIZE_MAX}")
    return page_size, request.query_params.get("page_token") or None


def instant(moment: datetime) -> str:
    """RFC 3339, in UTC, to the second."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
