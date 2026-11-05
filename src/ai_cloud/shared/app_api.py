# SPDX-License-Identifier: MIT
"""What the modules that serve the app API share: its paths, how its requests carry their
access tokens, bodies, identifiers, pages and Idempotency-Keys, and how it answers
(platform/protocols/app/README.md, Requests and Errors).

Each module serves its own routes, and turns its own errors into problems; the conventions
below are the API's, and are written once.
"""

import base64
import binascii
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Request
from pydantic import Field

from ai_cloud.shared.config import Settings
from ai_cloud.shared.problems import Problem
from ai_cloud.shared.protocols import Schema

# Fixed by the app API (platform/protocols/app/README.md, and schema.json).
PROTOCOL = "app"
API = "/app/v1"
PROBLEMS = f"{API}/problems/"
PAGE_SIZE_DEFAULT = 20  # Sessions
PAGE_SIZE_MAX = 50
# Requests: a UUID that the app makes, as a quoted string.
_IDEMPOTENCY_KEY_HEADER = "idempotency-key"
_IDEMPOTENCY_KEY = re.compile(
    r'"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"'
)

_JSON = "application/json"
# What answers carrying tokens or personal data say to caches (RFC 6749, 5.1).
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
# What a request without a valid access token gets (RFC 6750, 3; the app API, Requests).
CHALLENGE = {"WWW-Authenticate": 'Bearer error="invalid_token"'}


class AppApiSettings(Settings):
    # Seconds a retry with an Idempotency-Key gets the first answer (idempotency_retention).
    idempotency_retention: Annotated[int, Field(ge=1)]


@dataclass(frozen=True)
class IdempotencyKey:
    """What makes a request's retry recognisable: the key the app gave it, and the SHA-256
    of what the request asks."""

    key: UUID
    fingerprint: bytes


@dataclass(frozen=True)
class Page[T]:
    """One page of a list."""

    items: tuple[T, ...]
    # Empty on the last page.
    next_page_token: str


# How a module's errors become problems: for each class of error, the code, status and title
# of a problem the app acts on (the app API, Errors), or the status of an about:blank one.
type TypedProblems = Mapping[type[Exception], tuple[str, int, str]]
type StatusProblems = Mapping[type[Exception], int]


def problem_of(error: Exception, typed: TypedProblems, statuses: StatusProblems) -> Problem:
    """The problem the API answers for ``error``: of a type of its own if ``typed`` names its
    class, otherwise about:blank, of the status ``statuses`` gives it. An error that neither
    names is raised again, for the server to answer 500 and log it."""
    for kind, (code, status, title) in typed.items():
        if isinstance(error, kind):
            return Problem(status, str(error), type=PROBLEMS + code, title=title)
    for kind, status in statuses.items():
        if isinstance(error, kind):
            # A request whose access token failed signs in again, or refreshes (RFC 6750, 3).
            return Problem(status, str(error), headers=CHALLENGE if status == 401 else None)
    raise error


def over_the_rate(retry_after: float, detail: str) -> Problem:
    """What a request over a limit gets: 429, and when to try again (the app API, Errors)."""
    return Problem(429, detail, headers={"Retry-After": str(math.ceil(retry_after))})


def bearer_token(request: Request) -> str:
    """The access token the request carries (RFC 6750, 2.1); a problem without one."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise Problem(401, "a bearer access token is needed", headers=CHALLENGE)
    return token


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


async def idempotency_key(request: Request) -> IdempotencyKey:
    """The request's Idempotency-Key, with what the request asks: its method, its path and
    its body. A problem if there is not one key, a UUID as a quoted string."""
    values = request.headers.getlist(_IDEMPOTENCY_KEY_HEADER)
    found = _IDEMPOTENCY_KEY.fullmatch(values[0]) if len(values) == 1 else None
    if found is None:
        raise Problem(400, "Idempotency-Key must be one UUID, as a quoted string")
    asked = b"\n".join((request.method.encode(), request.url.path.encode(), await request.body()))
    return IdempotencyKey(UUID(found[1]), hashlib.sha256(asked).digest())


def page_request(request: Request) -> tuple[int, UUID | None]:
    """The page size that a request to a list asks for, and the identifier of the item its
    page starts after: ``None`` for the first page."""
    text = request.query_params.get("page_size")
    if text is None:
        page_size = PAGE_SIZE_DEFAULT
    elif text.isascii() and text.isdigit() and 1 <= int(text) <= PAGE_SIZE_MAX:
        page_size = int(text)
    else:
        raise Problem(400, f"page_size must be from 1 to {PAGE_SIZE_MAX}")
    return page_size, page_after(request.query_params.get("page_token"))


def page_after(page_token: str | None) -> UUID | None:
    """The identifier of the item the page that ``page_token`` names starts after; a
    problem for a token that no list gave."""
    if not page_token:
        return None
    try:
        return UUID(bytes=base64.urlsafe_b64decode(page_token + "=" * (-len(page_token) % 4)))
    except binascii.Error, ValueError:
        raise Problem(400, "the page token is not one this list gave") from None


def page_of[T](found: list[T], page_size: int, identifier: Callable[[T], UUID]) -> Page[T]:
    """The page that ``found``, read with one item more than ``page_size``, makes."""
    items = tuple(found[:page_size])
    more = len(found) > page_size
    return Page(items, _page_token(identifier(items[-1])) if more else "")


def instant(moment: datetime) -> str:
    """RFC 3339, in UTC, to the second."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _page_token(last: UUID) -> str:
    return base64.urlsafe_b64encode(last.bytes).rstrip(b"=").decode()
