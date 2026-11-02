# SPDX-License-Identifier: MIT
"""Limits on what a request may bring (SEC-15), kept by the process itself behind those
of the edge, so that no request makes it hold more than it should."""

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ai_cloud.shared.problems import Problem


class BodyLimit:
    """Answers 413 to a request whose body is larger than ``limit`` bytes: at once when
    it says so, and as soon as it has sent that much when it does not."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        detail = f"a request's body has {self.limit} bytes at most"
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.limit:
            await Problem(413, detail).response()(scope, receive, send)
            return
        received = 0

        async def limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    # Raised where the route reads the body: answered as any problem.
                    raise HTTPException(413, detail)
            return message

        await self.app(scope, limited, send)
