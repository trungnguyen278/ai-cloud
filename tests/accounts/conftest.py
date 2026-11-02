# SPDX-License-Identifier: MIT
"""The api, served in the tests' own process with the development provider."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx2
import pytest
from app_api import ADDRESS, Cloud

from ai_cloud.entry.api import ApiSecrets, create_app
from ai_cloud.shared.jose import SigningKeys

type MakeCloud = Callable[..., Awaitable[Cloud]]


@pytest.fixture
async def make_cloud(
    database: str, redis_url: str, make_settings: Any, jwk_set: Callable[[int], str]
) -> AsyncIterator[MakeCloud]:
    """Serve an api with these changes to the development settings, and give an app that
    reaches it."""
    opened: list[tuple[AbstractAsyncContextManager[None], httpx2.AsyncClient]] = []

    async def make(*, keys: SigningKeys | None = None, **changes: Any) -> Cloud:
        keys = keys or SigningKeys.from_jwk_set(jwk_set(1))
        settings = make_settings(**changes)
        app = create_app(settings, ApiSecrets(database, redis_url, keys))
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        transport = httpx2.ASGITransport(app=app, client=(ADDRESS, 50000))
        http = httpx2.AsyncClient(transport=transport, base_url="http://cloud.test")
        opened.append((lifespan, http))
        return Cloud(http, keys=keys, settings=settings, database=database, redis_url=redis_url)

    yield make
    for lifespan, http in reversed(opened):
        await http.aclose()
        await lifespan.__aexit__(None, None, None)


@pytest.fixture
async def cloud(make_cloud: MakeCloud) -> Cloud:
    return await make_cloud()
