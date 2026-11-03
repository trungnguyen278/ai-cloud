# SPDX-License-Identifier: MIT
"""The app API, version 1.1, over HTTP (platform/protocols/app/README.md).

Each body is read and checked against the protocol's schema here (SEC-14); the services do
the rest. The OAuth endpoints answer errors as OAuth does (RFC 6749, 5.2), and the API with
Problem Details (RFC 9457).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ai_cloud.accounts.domain.model import Principal
from ai_cloud.accounts.routes.accounts import account_routes
from ai_cloud.accounts.routes.homes import home_routes
from ai_cloud.accounts.routes.http import CHALLENGE, call
from ai_cloud.accounts.service import AccountsService, HomesService
from ai_cloud.shared.problems import Problem


def router(accounts: AccountsService, homes: HomesService, *, development: bool) -> APIRouter:
    """The accounts' routes; the development provider's too, if ``development``."""

    async def principal(request: Request) -> Principal:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise Problem(401, "a bearer access token is needed", headers=CHALLENGE)
        return await call(accounts.authenticate(token))

    authenticated = Annotated[Principal, Depends(principal)]
    routes = APIRouter()
    routes.include_router(account_routes(accounts, authenticated, development=development))
    routes.include_router(home_routes(homes, authenticated))
    return routes
