# SPDX-License-Identifier: MIT
"""The accounts' part of the app API, over HTTP (platform/protocols/app/README.md).

Each body is read and checked against the protocol's schema here (SEC-14); the services do
the rest. The OAuth endpoints answer errors as OAuth does (RFC 6749, 5.2), and the API with
Problem Details (RFC 9457).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ai_cloud.accounts.domain.model import Principal
from ai_cloud.accounts.routes.accounts import account_routes
from ai_cloud.accounts.routes.homes import home_routes
from ai_cloud.accounts.routes.http import call
from ai_cloud.accounts.service import AccountsService, HomesService
from ai_cloud.shared.app_api import bearer_token


def router(
    accounts: AccountsService, homes: HomesService, *, development: bool, max_json_depth: int
) -> APIRouter:
    """The accounts' routes; the development provider's too, if ``development``. A body
    nests at most ``max_json_depth`` deep."""

    async def principal(request: Request) -> Principal:
        return await call(accounts.authenticate(bearer_token(request)))

    authenticated = Annotated[Principal, Depends(principal)]
    routes = APIRouter()
    routes.include_router(
        account_routes(
            accounts, authenticated, development=development, max_json_depth=max_json_depth
        )
    )
    routes.include_router(home_routes(homes, authenticated, max_json_depth=max_json_depth))
    return routes
