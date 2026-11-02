# SPDX-License-Identifier: MIT
"""The app API's accounts, version 1.0, over HTTP (platform/protocols/app/README.md).

Each body is read and checked against the protocol's schema here (SEC-14); the service
does the rest. The OAuth endpoints answer errors as OAuth does (RFC 6749, 5.2), and the
API with Problem Details (RFC 9457).
"""

import json
import logging
import math
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import parse_qsl
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from ai_cloud.accounts.domain import errors
from ai_cloud.accounts.domain.model import (
    Account,
    AccountChanges,
    Identity,
    Principal,
    Session,
    Tokens,
    has_control_characters,
)
from ai_cloud.accounts.service import AccountsService
from ai_cloud.shared.problems import Problem
from ai_cloud.shared.protocols import Schema, protocol_schema

# Fixed by the app API (platform/protocols/app/README.md, and schema.json).
PROTOCOL = "app"
API = "/app/v1"
TOKEN_PATH = "/app/token"
REVOKE_PATH = "/app/revoke"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"  # RFC 8693, 2.1
REFRESH = "refresh_token"  # RFC 6749, 6
ISSUED_ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"  # RFC 8693, 3
PROBLEMS = f"{API}/problems/"
PAGE_SIZE_DEFAULT = 20  # Sessions
PAGE_SIZE_MAX = 50

_FORM = "application/x-www-form-urlencoded"
_JSON = "application/json"
# What answers carrying tokens or personal data say to caches (RFC 6749, 5.1).
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
# What a request without a valid access token gets (RFC 6750, 3; the app API, Requests).
_CHALLENGE = {"WWW-Authenticate": 'Bearer error="invalid_token"'}

# The problems the app acts on (the app API, Errors), with their titles.
_TYPED: dict[type[errors.AccountsError], tuple[str, int, str]] = {
    errors.IdentityTaken: ("identity_taken", 409, "Identity taken"),
    errors.IdentityLinked: ("identity_linked", 409, "Identity linked"),
    errors.LastIdentity: ("last_identity", 409, "Last identity"),
    errors.RecentSignInRequired: ("recent_sign_in_required", 403, "Recent sign-in required"),
}
# Any other problem is about:blank.
_STATUSES: dict[type[errors.AccountsError], int] = {
    errors.Unauthenticated: 401,
    errors.IdTokenRefused: 403,
    errors.NotFound: 404,
    errors.InvalidPageToken: 400,
    errors.TooManyIdentities: 409,
    errors.PreconditionFailed: 412,
    errors.ProviderUnavailable: 503,
}

log = logging.getLogger(__name__)


class OAuthError(Exception):
    """An error as the OAuth endpoints answer it."""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status

    def response(self) -> JSONResponse:
        body = {"error": self.error, "error_description": self.description}
        return JSONResponse(body, self.status, headers=_NO_STORE)


def router(service: AccountsService, *, development: bool) -> APIRouter:
    """The accounts' routes; the development provider's too, if ``development``."""
    schemas = {
        name: protocol_schema(PROTOCOL, name)
        for name in (
            "token_exchange_request",
            "refresh_request",
            "revoke_request",
            "account_update",
            "id_token_request",
            "development_id_token_request",
        )
    }
    routes = APIRouter()

    async def principal(request: Request) -> Principal:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise Problem(401, "a bearer access token is needed", headers=_CHALLENGE)
        return await _call(service.authenticate(token))

    Authenticated = Annotated[Principal, Depends(principal)]

    # Signing in.

    @routes.post(f"{API}/nonces")
    async def issue_nonce(request: Request) -> Response:
        address = request.client.host if request.client is not None else ""
        try:
            nonce, lifetime = await service.issue_nonce(address)
        except errors.RateLimited as error:
            retry = str(math.ceil(error.retry_after))
            raise Problem(429, "too many nonces", headers={"Retry-After": retry}) from None
        return JSONResponse({"nonce": nonce, "expires_in": lifetime}, 201, headers=_NO_STORE)

    @routes.post(TOKEN_PATH)
    async def token(request: Request) -> Response:
        try:
            form = await _form(request)
            grant = form.get("grant_type")
            if grant == TOKEN_EXCHANGE:
                _check_form(schemas["token_exchange_request"], form)
                label = form.get("session_label")
                if label is not None and has_control_characters(label):
                    raise OAuthError("invalid_request", "session_label: control characters")
                tokens = await service.exchange(form["subject_token"], form["client_id"], label)
                return _tokens(tokens, exchanged=True)
            if grant == REFRESH:
                _check_form(schemas["refresh_request"], form)
                tokens = await service.refresh(form["refresh_token"], form["client_id"])
                return _tokens(tokens, exchanged=False)
            if grant is None:
                raise OAuthError("invalid_request", "grant_type is missing")
            raise OAuthError("unsupported_grant_type", "this version has no such grant")
        except OAuthError as error:
            return error.response()
        except errors.InvalidClient as error:
            return OAuthError("invalid_client", str(error), 401).response()
        except errors.InvalidGrant as error:
            log.info("a token request was refused", extra={"reason": str(error)})
            return OAuthError("invalid_grant", str(error)).response()
        except errors.ProviderUnavailable as error:
            log.warning("a provider is unavailable", extra={"reason": str(error)})
            return OAuthError("temporarily_unavailable", str(error), 503).response()

    @routes.post(REVOKE_PATH)
    async def revoke(request: Request) -> Response:
        try:
            form = await _form(request)
            _check_form(schemas["revoke_request"], form)
            await service.revoke(form["token"], form.get("token_type_hint"), form["client_id"])
        except OAuthError as error:
            return error.response()
        except errors.InvalidClient as error:
            return OAuthError("invalid_client", str(error), 401).response()
        return Response(status_code=200, headers=_NO_STORE)

    # The account.

    @routes.get(f"{API}/account")
    async def read_account(principal: Authenticated) -> Response:
        return _account(await _call(service.account(principal)))

    @routes.patch(f"{API}/account")
    async def change_account(request: Request, principal: Authenticated) -> Response:
        body = await _json(request, schemas["account_update"])
        name = body.get("display_name")
        if name is not None and has_control_characters(name):
            raise Problem(400, "display_name: control characters")
        languages = body.get("languages")
        changes = AccountChanges(name, None if languages is None else tuple(languages))
        if_match = request.headers.get("if-match")
        return _account(await _call(service.update_account(principal, changes, if_match)))

    @routes.post(f"{API}/account:delete")
    async def delete_account(request: Request, principal: Authenticated) -> Response:
        body = await _json(request, schemas["id_token_request"])
        await _call(service.delete_account(principal, body["id_token"]))
        return Response(status_code=204)

    # Identities.

    @routes.post(f"{API}/account/identities")
    async def link_identity(request: Request, principal: Authenticated) -> Response:
        body = await _json(request, schemas["id_token_request"])
        identity = await _call(service.link_identity(principal, body["id_token"]))
        return JSONResponse(_identity(identity), 201, headers=_NO_STORE)

    @routes.delete(API + "/account/identities/{identity_id}")
    async def remove_identity(identity_id: str, principal: Authenticated) -> Response:
        await _call(service.remove_identity(principal, _id(identity_id)))
        return Response(status_code=204)

    # Sessions.

    @routes.get(f"{API}/account/sessions")
    async def list_sessions(request: Request, principal: Authenticated) -> Response:
        page_size = _page_size(request.query_params.get("page_size"))
        page_token = request.query_params.get("page_token") or None
        page = await _call(service.sessions(principal, page_size, page_token))
        body = {
            "sessions": [_session(s, current=s.id == principal.session_id) for s in page.sessions],
            "next_page_token": page.next_page_token,
        }
        return JSONResponse(body, headers=_NO_STORE)

    @routes.delete(API + "/account/sessions/{session_id}")
    async def end_session(session_id: str, principal: Authenticated) -> Response:
        await _call(service.end_session(principal, _id(session_id)))
        return Response(status_code=204)

    # Development: absent elsewhere, so that it answers 404 (the app API, Development).

    if development:

        @routes.post(f"{API}/development/id-tokens")
        async def development_id_token(request: Request) -> Response:
            body = await _json(request, schemas["development_id_token_request"])
            name, email = body["display_name"], body.get("email")
            if has_control_characters(name) or (email and has_control_characters(email)):
                raise Problem(400, "display_name or email: control characters")
            id_token = service.development_id_token(body["nonce"], name, email)
            return JSONResponse({"id_token": id_token}, 201, headers=_NO_STORE)

    return routes


async def _call[T](work: Awaitable[T]) -> T:
    """``work``'s result; its errors as the API answers them."""
    try:
        return await work
    except errors.AccountsError as error:
        raise _problem(error) from None


def _problem(error: errors.AccountsError) -> Problem:
    for kind, (code, status, title) in _TYPED.items():
        if isinstance(error, kind):
            return Problem(status, str(error), type=PROBLEMS + code, title=title)
    for kind, status in _STATUSES.items():
        if isinstance(error, kind):
            headers = _CHALLENGE if status == 401 else None
            return Problem(status, str(error), headers=headers)
    raise error


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").partition(";")[0].strip().lower()


async def _form(request: Request) -> dict[str, str]:
    """The OAuth endpoint's form fields. A field without a value counts as absent, and
    none may come twice (RFC 6749, 3.1 and 3.2)."""
    if _media_type(request) != _FORM:
        raise OAuthError("invalid_request", f"the body must be {_FORM}")
    body = await request.body()
    try:
        pairs = parse_qsl(body.decode(), keep_blank_values=True, strict_parsing=True)
    except ValueError:  # UnicodeDecodeError included
        raise OAuthError("invalid_request", "the body is not a form") from None
    form: dict[str, str] = {}
    seen: set[str] = set()
    for name, value in pairs:
        if name in seen:
            raise OAuthError("invalid_request", f"{name} is repeated")
        seen.add(name)
        if value:
            form[name] = value
    return form


def _check_form(schema: Schema, form: dict[str, str]) -> None:
    if (problem := schema.problem(form)) is not None:
        raise OAuthError("invalid_request", problem)


async def _json(request: Request, schema: Schema) -> dict[str, Any]:
    """The request's JSON body, checked against ``schema``."""
    if _media_type(request) != _JSON:
        raise Problem(415, f"the body must be {_JSON}")
    try:
        body = json.loads(await request.body())
    except ValueError:  # UnicodeDecodeError included
        raise Problem(400, "the body is not JSON") from None
    if (problem := schema.problem(body)) is not None:
        raise Problem(400, problem)
    return body


def _id(text: str) -> UUID:
    """An identifier in a path: one that is not a UUID names nothing."""
    try:
        return UUID(text)
    except ValueError:
        raise Problem(404, "no such item in the account") from None


def _page_size(text: str | None) -> int:
    if text is None:
        return PAGE_SIZE_DEFAULT
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= PAGE_SIZE_MAX:
        raise Problem(400, f"page_size must be from 1 to {PAGE_SIZE_MAX}")
    return int(text)


def _tokens(tokens: Tokens, *, exchanged: bool) -> Response:
    body: dict[str, Any] = {
        "access_token": tokens.access_token,
        "token_type": "Bearer",
        "expires_in": tokens.expires_in,
        "refresh_token": tokens.refresh_token,
    }
    if exchanged:
        body["issued_token_type"] = ISSUED_ACCESS_TOKEN
    return JSONResponse(body, headers=_NO_STORE)


def _account(account: Account) -> Response:
    body: dict[str, Any] = {
        "id": str(account.id),
        "display_name": account.display_name,
        "languages": list(account.languages),
        "identities": [_identity(identity) for identity in account.identities],
        "create_time": _instant(account.create_time),
        "etag": account.etag,
    }
    if account.email is not None:
        body["email"] = account.email
    return JSONResponse(body, headers={**_NO_STORE, "ETag": account.etag})


def _identity(identity: Identity) -> dict[str, Any]:
    body = {
        "id": str(identity.id),
        "provider": identity.provider,
        "create_time": _instant(identity.create_time),
    }
    if identity.email is not None:
        body["email"] = identity.email
    return body


def _session(session: Session, *, current: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(session.id),
        "create_time": _instant(session.create_time),
        "last_use_time": _instant(session.last_use_time),
        "current": current,
    }
    if session.label is not None:
        body["label"] = session.label
    return body


def _instant(moment: datetime) -> str:
    """RFC 3339, in UTC, to the second."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
