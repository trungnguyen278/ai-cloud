# SPDX-License-Identifier: MIT
"""The devices' endpoints: the app API's devices (platform/protocols/app/README.md,
Devices), and the cloud link's token endpoint and control channel
(platform/protocols/cloud/README.md, Connecting).

Each body is read and checked here (SEC-14); the services do the rest. A registration's
answer is kept as the routes make it, so that a retry gets the same, a refusal included. The
token endpoint answers errors as OAuth does (RFC 6749, 5.2); the control channel's handshake
as Problem Details, whose status the device acts on.
"""

import logging
import math
from collections.abc import Awaitable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, WebSocket
from fastapi.responses import JSONResponse

from ai_cloud.devices.adapters.websocket import WebSocketChannel
from ai_cloud.devices.domain import errors, link
from ai_cloud.devices.domain.assertion import ASSERTION_TYPE, GRANT_TYPE, TOKEN_PATH
from ai_cloud.devices.domain.model import Answer, Registered, Shown
from ai_cloud.devices.service import DevicesService, DeviceTokens, Links, Outcome
from ai_cloud.shared.app_api import (
    API,
    NO_STORE,
    PROTOCOL,
    StatusProblems,
    TypedProblems,
    bearer_token,
    idempotency_key,
    instant,
    item_id,
    json_body,
    over_the_rate,
    page_request,
    problem_of,
)
from ai_cloud.shared.oauth import CHALLENGE, OAuthError, bearer, form
from ai_cloud.shared.problems import MEDIA_TYPE, Problem
from ai_cloud.shared.protocols import protocol_schema

log = logging.getLogger(__name__)

# The problems the app acts on (the app API, Errors), with their titles.
_TYPED: TypedProblems = {
    errors.RoleRequired: ("role_required", 403, "Role required"),
    errors.RegistrationInvalid: ("registration_invalid", 400, "Registration invalid"),
    errors.DeviceTaken: ("device_taken", 409, "Device taken"),
    errors.DeviceLimitReached: ("device_limit_reached", 409, "Device limit reached"),
}
# Any other problem is about:blank.
_STATUSES: StatusProblems = {
    errors.Unauthenticated: 401,
    errors.NotFound: 404,
    errors.IdempotencyKeyBusy: 409,
    errors.IdempotencyKeyReused: 422,
}


def router(
    service: DevicesService, tokens: DeviceTokens, links: Links, *, max_json_depth: int
) -> APIRouter:
    """The devices' routes. A body nests at most ``max_json_depth`` deep."""
    registration_schema = protocol_schema(PROTOCOL, "device_registration")

    async def account(request: Request) -> UUID:
        return await call(service.authenticate(bearer_token(request)))

    Account = Annotated[UUID, Depends(account)]
    routes = APIRouter()

    @routes.post(API + "/homes/{home_id}/device-challenges")
    async def issue_challenge(home_id: str, account: Account) -> Response:
        issued = await call(service.issue_challenge(account, item_id(home_id)))
        body = {
            "challenge": issued.challenge,
            "server": issued.server,
            "expires_in": issued.expires_in,
        }
        return JSONResponse(body, 201, headers=NO_STORE)

    @routes.post(API + "/homes/{home_id}/devices")
    async def register(home_id: str, request: Request, account: Account) -> Response:
        body = await json_body(request, registration_schema, max_json_depth)
        key = await idempotency_key(request)
        given = await call(
            service.register(account, item_id(home_id), body["registration"], key, _answer)
        )
        return _response(given)

    @routes.get(API + "/homes/{home_id}/devices")
    async def list_devices(home_id: str, request: Request, account: Account) -> Response:
        page_size, after = page_request(request)
        page = await call(service.devices(account, item_id(home_id), page_size, after))
        body = {
            "devices": [_device(device) for device in page.items],
            "next_page_token": page.next_page_token,
        }
        return JSONResponse(body, headers=NO_STORE)

    @routes.get(API + "/homes/{home_id}/devices/{device_id}")
    async def read_device(home_id: str, device_id: str, account: Account) -> Response:
        device = await call(service.device(account, item_id(home_id), item_id(device_id)))
        return JSONResponse(_device(device), headers=NO_STORE)

    @routes.delete(API + "/homes/{home_id}/devices/{device_id}")
    async def remove_device(home_id: str, device_id: str, account: Account) -> Response:
        await call(service.remove(account, item_id(home_id), item_id(device_id)))
        return Response(status_code=204)

    # The cloud link's token endpoint, which devices reach without the app API's version.

    @routes.post(TOKEN_PATH)
    async def device_token(request: Request) -> Response:
        try:
            fields = await form(request)
            grant = fields.get("grant_type")
            if grant is None:
                raise OAuthError("invalid_request", "grant_type is missing")
            if grant != GRANT_TYPE:
                raise OAuthError("unsupported_grant_type", f"this endpoint grants {GRANT_TYPE}")
            if fields.get("client_assertion_type") != ASSERTION_TYPE:
                raise OAuthError(
                    "invalid_request", f"client_assertion_type must be {ASSERTION_TYPE}"
                )
            signed = fields.get("client_assertion")
            if signed is None:
                raise OAuthError("invalid_request", "client_assertion is missing")
            # One way to authenticate a client at a time (RFC 6749, 2.3 and 5.2).
            if "client_secret" in fields or "authorization" in request.headers:
                raise OAuthError("invalid_request", "an assertion, and no other credentials")
            address = request.client.host if request.client is not None else ""
            issued = await tokens.issue(address, signed, fields.get("client_id"))
        except OAuthError as error:
            return error.response()
        except errors.DevicesError as error:
            return _refused(error).response()
        body = {
            "access_token": issued.access_token,
            "token_type": "Bearer",
            "expires_in": issued.expires_in,
        }
        return JSONResponse(body, headers=NO_STORE)

    # The cloud link's control channel, whose path carries its major version.

    @routes.websocket(link.PATH)
    async def control_channel(websocket: WebSocket) -> None:
        token = bearer(websocket.headers)
        try:
            if token is None:
                raise errors.Unauthenticated("a bearer access token is needed")
            opening = await links.open(token)
        except errors.Unauthenticated as error:
            # The device gets a new token, and connects again after its backoff.
            log.info("a device's connection was refused", extra={"reason": str(error)})
            refusal = Problem(401, str(error), headers=CHALLENGE)
            await websocket.send_denial_response(refusal.response())
            return
        except errors.Unavailable as error:
            # The device connects again after its backoff, to another replica.
            await websocket.send_denial_response(Problem(503, str(error)).response())
            return
        await websocket.accept()
        await links.serve(WebSocketChannel(websocket), opening)

    return routes


async def call[T](work: Awaitable[T]) -> T:
    """``work``'s result; its errors as the API answers them."""
    try:
        return await work
    except errors.DevicesError as error:
        raise _problem(error) from None


def _problem(error: errors.DevicesError) -> Problem:
    if isinstance(error, errors.RateLimited):
        return over_the_rate(error.retry_after, str(error))
    return problem_of(error, _TYPED, _STATUSES)


def _refused(error: errors.DevicesError) -> OAuthError:
    """How the token endpoint answers a device it refuses: ``invalid_client`` only for a key it
    does not hold, after which the device offers set-up. It tries again after its backoff
    after any other refusal (the cloud link, Tokens from the server)."""
    match error:
        case errors.RateLimited():
            # Not logged: a flood would fill the logs.
            retry = {"Retry-After": str(math.ceil(error.retry_after))}
            return OAuthError("temporarily_unavailable", str(error), 429, headers=retry)
        case errors.UnknownDevice():
            refused = OAuthError("invalid_client", str(error), 401)
        case errors.AssertionInvalid():
            refused = OAuthError("invalid_request", str(error))
        case _:
            raise error
    log.info("a device's token request was refused", extra={"reason": str(error)})
    return refused


def _answer(outcome: Outcome) -> Answer:
    """The answer to a registration whose challenge was used: the device, new or not, or the
    problem it met."""
    if isinstance(outcome, Registered):
        return Answer(201 if outcome.new else 200, _device(outcome.device))
    problem = _problem(outcome)
    return Answer(problem.status, problem.body())


def _response(answer: Answer) -> Response:
    if answer.status >= 400:
        return JSONResponse(answer.body, answer.status, media_type=MEDIA_TYPE)
    return JSONResponse(answer.body, answer.status, headers=NO_STORE)


def _device(shown: Shown) -> dict[str, Any]:
    device = shown.device
    body: dict[str, Any] = {"id": str(device.id)}
    if device.description is not None:
        # Absent until its first hello.
        body["kind"] = device.description.kind
        body["model"] = device.description.model
        body["software"] = device.description.software
    body["connected"] = shown.connected
    body["create_time"] = instant(device.create_time)
    return body
