# SPDX-License-Identifier: MIT
"""The ``api`` process: requests from devices and apps, served by FastAPI
(docs/decisions/0002-stack.md, decision 4).

FastAPI's own description of the API (``/docs``, ``/openapi.json``) is not served: what
crosses to a device or an app is defined by ai-platform's schemas, not by this code.
"""

from typing import Annotated

import uvicorn
from fastapi import FastAPI
from pydantic import Field

from ai_cloud.shared.config import Settings


class ApiSettings(Settings):
    # The address the process listens on.
    host: str
    # Its TCP port.
    port: Annotated[int, Field(ge=1, le=65535)]
    # Whether it logs each request.
    access_log: bool


def create_app() -> FastAPI:
    """The api process's application."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Whether the process runs, for the containers' checks."""
        return {"status": "ok"}

    return app


def run(settings: ApiSettings) -> None:
    """Serve the api until the process is told to stop (SIGINT or SIGTERM)."""
    uvicorn.run(
        create_app(),
        host=settings.host,
        port=settings.port,
        access_log=settings.access_log,
        log_config=None,  # the process's logging is configured already (shared.logs)
    )
