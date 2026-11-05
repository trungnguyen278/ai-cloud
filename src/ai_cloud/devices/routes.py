# SPDX-License-Identifier: MIT
"""The app API's devices, over HTTP (platform/protocols/app/README.md, Devices).

Each body is read and checked against the protocol's schema here (SEC-14); the service does
the rest. A registration's answer is kept as the routes make it, so that a retry gets the
same, a refusal included.
"""

from collections.abc import Awaitable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from ai_cloud.devices.domain import errors
from ai_cloud.devices.domain.model import Answer, Device, Registered
from ai_cloud.devices.service import DevicesService, Outcome
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
from ai_cloud.shared.problems import MEDIA_TYPE, Problem
from ai_cloud.shared.protocols import protocol_schema

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


def router(service: DevicesService) -> APIRouter:
    """The devices' routes."""
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
        body = await json_body(request, registration_schema)
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


def _device(device: Device) -> dict[str, Any]:
    return {
        "id": str(device.id),
        # Whether its control channel is open: none opens before the cloud serves the cloud
        # link.
        "connected": False,
        "create_time": instant(device.create_time),
    }
