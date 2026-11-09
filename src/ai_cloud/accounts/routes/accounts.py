# SPDX-License-Identifier: MIT
"""The app API's signing in, sessions, and the account (platform/protocols/app/README.md)."""

import logging
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ai_cloud.accounts.domain import errors
from ai_cloud.accounts.domain.model import (
    Account,
    AccountChanges,
    Identity,
    Session,
    Tokens,
    has_control_characters,
)
from ai_cloud.accounts.routes.http import call
from ai_cloud.accounts.service import AccountsService
from ai_cloud.shared.app_api import (
    API,
    NO_STORE,
    PROTOCOL,
    instant,
    item_id,
    json_body,
    media_type,
    page_request,
)
from ai_cloud.shared.problems import Problem
from ai_cloud.shared.protocols import Schema, protocol_schema

# Fixed by the app API (platform/protocols/app/README.md, and schema.json).
TOKEN_PATH = "/app/token"
REVOKE_PATH = "/app/revoke"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"  # RFC 8693, 2.1
REFRESH = "refresh_token"  # RFC 6749, 6
ISSUED_ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"  # RFC 8693, 3

_FORM = "application/x-www-form-urlencoded"

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
        return JSONResponse(body, self.status, headers=NO_STORE)


def account_routes(service: AccountsService, Authenticated: Any, *, development: bool) -> APIRouter:
    """The routes of signing in, sessions and the account; the development provider's too,
    if ``development``. ``Authenticated`` annotates who a request speaks for."""
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

    # Signing in.

    @routes.post(f"{API}/nonces")
    async def issue_nonce(request: Request) -> Response:
        address = request.client.host if request.client is not None else ""
        nonce, lifetime = await call(service.issue_nonce(address))
        return JSONResponse({"nonce": nonce, "expires_in": lifetime}, 201, headers=NO_STORE)

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
        return Response(status_code=200, headers=NO_STORE)

    # The account.

    @routes.get(f"{API}/account")
    async def read_account(principal: Authenticated) -> Response:
        return _account(await call(service.account(principal)))

    @routes.patch(f"{API}/account")
    async def change_account(request: Request, principal: Authenticated) -> Response:
        body = await json_body(request, schemas["account_update"])
        name = body.get("display_name")
        if name is not None and has_control_characters(name):
            raise Problem(400, "display_name: control characters")
        languages = body.get("languages")
        changes = AccountChanges(name, None if languages is None else tuple(languages))
        if_match = request.headers.get("if-match")
        return _account(await call(service.update_account(principal, changes, if_match)))

    @routes.post(f"{API}/account:delete")
    async def delete_account(request: Request, principal: Authenticated) -> Response:
        body = await json_body(request, schemas["id_token_request"])
        await call(service.delete_account(principal, body["id_token"]))
        return Response(status_code=204)

    # Identities.

    @routes.post(f"{API}/account/identities")
    async def link_identity(request: Request, principal: Authenticated) -> Response:
        body = await json_body(request, schemas["id_token_request"])
        identity = await call(service.link_identity(principal, body["id_token"]))
        return JSONResponse(_identity(identity), 201, headers=NO_STORE)

    @routes.delete(API + "/account/identities/{identity_id}")
    async def remove_identity(identity_id: str, principal: Authenticated) -> Response:
        await call(service.remove_identity(principal, item_id(identity_id)))
        return Response(status_code=204)

    # Sessions.

    @routes.get(f"{API}/account/sessions")
    async def list_sessions(request: Request, principal: Authenticated) -> Response:
        page_size, after = page_request(request)
        page = await call(service.sessions(principal, page_size, after))
        body = {
            "sessions": [_session(s, current=s.id == principal.session_id) for s in page.items],
            "next_page_token": page.next_page_token,
        }
        return JSONResponse(body, headers=NO_STORE)

    @routes.delete(API + "/account/sessions/{session_id}")
    async def end_session(session_id: str, principal: Authenticated) -> Response:
        await call(service.end_session(principal, item_id(session_id)))
        return Response(status_code=204)

    # Development: absent elsewhere, so that it answers 404 (the app API, Development).

    if development:

        @routes.post(f"{API}/development/id-tokens")
        async def development_id_token(request: Request) -> Response:
            body = await json_body(request, schemas["development_id_token_request"])
            name, email = body["display_name"], body.get("email")
            if has_control_characters(name) or (email and has_control_characters(email)):
                raise Problem(400, "display_name or email: control characters")
            id_token = service.development_id_token(body["nonce"], name, email)
            return JSONResponse({"id_token": id_token}, 201, headers=NO_STORE)

    return routes


async def _form(request: Request) -> dict[str, str]:
    """The OAuth endpoint's form fields. A field without a value counts as absent, and
    none may come twice (RFC 6749, 3.1 and 3.2)."""
    if media_type(request) != _FORM:
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


def _tokens(tokens: Tokens, *, exchanged: bool) -> Response:
    body: dict[str, Any] = {
        "access_token": tokens.access_token,
        "token_type": "Bearer",
        "expires_in": tokens.expires_in,
        "refresh_token": tokens.refresh_token,
    }
    if exchanged:
        body["issued_token_type"] = ISSUED_ACCESS_TOKEN
    return JSONResponse(body, headers=NO_STORE)


def _account(account: Account) -> Response:
    body: dict[str, Any] = {
        "id": str(account.id),
        "display_name": account.display_name,
        "languages": list(account.languages),
        "identities": [_identity(identity) for identity in account.identities],
        "create_time": instant(account.create_time),
        "etag": account.etag,
    }
    if account.email is not None:
        body["email"] = account.email
    return JSONResponse(body, headers={**NO_STORE, "ETag": account.etag})


def _identity(identity: Identity) -> dict[str, Any]:
    body = {
        "id": str(identity.id),
        "provider": identity.provider,
        "create_time": instant(identity.create_time),
    }
    if identity.email is not None:
        body["email"] = identity.email
    return body


def _session(session: Session, *, current: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(session.id),
        "create_time": instant(session.create_time),
        "last_use_time": instant(session.last_use_time),
        "current": current,
    }
    if session.label is not None:
        body["label"] = session.label
    return body
