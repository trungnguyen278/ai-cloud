# SPDX-License-Identifier: MIT
"""The app API's homes, the people in them and their invitations
(platform/protocols/app/README.md, Homes)."""

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ai_cloud.accounts.domain.model import (
    HomeChanges,
    HomeView,
    Invitation,
    Member,
    MemberChanges,
    NewHome,
    Role,
)
from ai_cloud.accounts.routes.http import call
from ai_cloud.accounts.service import HomesService
from ai_cloud.shared.app_api import (
    API,
    NO_STORE,
    PROTOCOL,
    idempotency_key,
    instant,
    item_id,
    json_body,
    page_request,
)
from ai_cloud.shared.protocols import protocol_schema

_ACCOUNT = "account"  # $defs/member's kind, for a person with an account
_PROFILE = "profile"  # and for one without


def home_routes(service: HomesService, Authenticated: Any) -> APIRouter:
    """The routes of homes. ``Authenticated`` annotates who a request speaks for."""
    schemas = {
        name: protocol_schema(PROTOCOL, name)
        for name in (
            "home_create",
            "home_update",
            "home_transfer",
            "id_token_request",
            "profile_create",
            "member_update",
            "invitation_code_request",
        )
    }
    routes = APIRouter()

    # Homes.

    @routes.get(f"{API}/homes")
    async def list_homes(request: Request, principal: Authenticated) -> Response:
        page_size, after = page_request(request)
        page = await call(service.homes(principal, page_size, after))
        body = {
            "homes": [_home(view) for view in page.items],
            "next_page_token": page.next_page_token,
        }
        return JSONResponse(body, headers=NO_STORE)

    @routes.post(f"{API}/homes")
    async def create_home(request: Request, principal: Authenticated) -> Response:
        body = await json_body(request, schemas["home_create"])
        new = NewHome(body["name"], body["time_zone"], tuple(body["languages"]))
        key = await idempotency_key(request)
        return _tagged(await call(service.create_home(principal, new, key, _home)), 201)

    @routes.get(API + "/homes/{home_id}")
    async def read_home(home_id: str, principal: Authenticated) -> Response:
        return _tagged(_home(await call(service.home(principal, item_id(home_id)))))

    @routes.patch(API + "/homes/{home_id}")
    async def change_home(home_id: str, request: Request, principal: Authenticated) -> Response:
        body = await json_body(request, schemas["home_update"])
        languages = body.get("languages")
        changes = HomeChanges(
            body.get("name"),
            body.get("time_zone"),
            None if languages is None else tuple(languages),
        )
        if_match = request.headers.get("if-match")
        view = await call(service.update_home(principal, item_id(home_id), changes, if_match))
        return _tagged(_home(view))

    @routes.post(API + "/homes/{home_id}:transfer")
    async def transfer_home(home_id: str, request: Request, principal: Authenticated) -> Response:
        body = await json_body(request, schemas["home_transfer"])
        view = await call(
            service.transfer_home(
                principal, item_id(home_id), item_id(body["to_member_id"]), body["id_token"]
            )
        )
        return _tagged(_home(view))

    @routes.post(API + "/homes/{home_id}:delete")
    async def delete_home(home_id: str, request: Request, principal: Authenticated) -> Response:
        body = await json_body(request, schemas["id_token_request"])
        await call(service.delete_home(principal, item_id(home_id), body["id_token"]))
        return Response(status_code=204)

    # The people in a home.

    @routes.get(API + "/homes/{home_id}/members")
    async def list_members(home_id: str, request: Request, principal: Authenticated) -> Response:
        page_size, after = page_request(request)
        page = await call(service.members(principal, item_id(home_id), page_size, after))
        body = {
            "members": [
                _member(member, current=member.account_id == principal.account_id)
                for member in page.items
            ],
            "next_page_token": page.next_page_token,
        }
        return JSONResponse(body, headers=NO_STORE)

    @routes.post(API + "/homes/{home_id}/members")
    async def add_profile(home_id: str, request: Request, principal: Authenticated) -> Response:
        body = await json_body(request, schemas["profile_create"])
        key = await idempotency_key(request)
        answer = await call(
            service.add_profile(
                principal,
                item_id(home_id),
                body["display_name"],
                key,
                lambda profile: _member(profile, False),
            )
        )
        return _tagged(answer, 201)

    @routes.patch(API + "/homes/{home_id}/members/{member_id}")
    async def change_member(
        home_id: str, member_id: str, request: Request, principal: Authenticated
    ) -> Response:
        body = await json_body(request, schemas["member_update"])
        role = body.get("role")
        changes = MemberChanges(body.get("display_name"), None if role is None else Role(role))
        if_match = request.headers.get("if-match")
        member = await call(
            service.update_member(
                principal, item_id(home_id), item_id(member_id), changes, if_match
            )
        )
        return _tagged(_member(member, current=member.account_id == principal.account_id))

    @routes.delete(API + "/homes/{home_id}/members/{member_id}")
    async def remove_member(home_id: str, member_id: str, principal: Authenticated) -> Response:
        await call(service.remove_member(principal, item_id(home_id), item_id(member_id)))
        return Response(status_code=204)

    # Invitations.

    @routes.post(API + "/homes/{home_id}/invitations")
    async def create_invitation(home_id: str, principal: Authenticated) -> Response:
        made = await call(service.create_invitation(principal, item_id(home_id)))
        return JSONResponse(_invitation(made.invitation, made.code), 201, headers=NO_STORE)

    @routes.get(API + "/homes/{home_id}/invitations")
    async def list_invitations(
        home_id: str, request: Request, principal: Authenticated
    ) -> Response:
        page_size, after = page_request(request)
        page = await call(service.invitations(principal, item_id(home_id), page_size, after))
        body = {
            "invitations": [_invitation(invitation) for invitation in page.items],
            "next_page_token": page.next_page_token,
        }
        return JSONResponse(body, headers=NO_STORE)

    @routes.delete(API + "/homes/{home_id}/invitations/{invitation_id}")
    async def revoke_invitation(
        home_id: str, invitation_id: str, principal: Authenticated
    ) -> Response:
        await call(service.revoke_invitation(principal, item_id(home_id), item_id(invitation_id)))
        return Response(status_code=204)

    @routes.post(f"{API}/invitations:lookup")
    async def look_up_invitation(request: Request, principal: Authenticated) -> Response:
        body = await json_body(request, schemas["invitation_code_request"])
        preview = await call(service.look_up_invitation(principal, body["code"]))
        answer = {
            "home_name": preview.home_name,
            "invited_by": preview.invited_by,
            "expire_time": instant(preview.expire_time),
        }
        return JSONResponse(answer, headers=NO_STORE)

    @routes.post(f"{API}/invitations:accept")
    async def accept_invitation(request: Request, principal: Authenticated) -> Response:
        body = await json_body(request, schemas["invitation_code_request"])
        view, joined = await call(service.accept_invitation(principal, body["code"]))
        return _tagged(_home(view), 201 if joined else 200)

    return routes


def _tagged(body: dict[str, Any], status: int = 200) -> Response:
    return JSONResponse(body, status, headers={**NO_STORE, "ETag": body["etag"]})


def _home(view: HomeView) -> dict[str, Any]:
    home, member = view.home, view.member
    assert member.role is not None  # the person who asks has an account
    return {
        "id": str(home.id),
        "name": home.name,
        "time_zone": home.time_zone,
        "languages": list(home.languages),
        "member_id": str(member.id),
        "role": member.role.value,
        "create_time": instant(home.create_time),
        "etag": home.etag,
    }


def _member(member: Member, current: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(member.id),
        "kind": _PROFILE if member.account_id is None else _ACCOUNT,
        "display_name": member.display_name,
        "current": current,
        "create_time": instant(member.create_time),
        "etag": member.etag,
    }
    if member.role is not None:
        body["role"] = member.role.value
    return body


def _invitation(invitation: Invitation, code: str | None = None) -> dict[str, Any]:
    body = {
        "id": str(invitation.id),
        "invited_by": invitation.invited_by_name,
        "create_time": instant(invitation.create_time),
        "expire_time": instant(invitation.expire_time),
    }
    if code is not None:
        body["code"] = code
    return body
