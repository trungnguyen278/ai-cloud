# SPDX-License-Identifier: MIT
"""Problem Details (RFC 9457): how the HTTP API answers an error.

A problem that a client acts on has a ``type`` of its own, which its protocol defines;
any other is ``about:blank``, and its status says it all. ``detail`` is for logs, in
English: what people read comes from the clients' own catalogs.
"""

from collections.abc import Mapping
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

MEDIA_TYPE = "application/problem+json"


class Problem(Exception):
    """An error the API answers as a problem."""

    def __init__(
        self,
        status: int,
        detail: str | None = None,
        *,
        type: str = "about:blank",
        title: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(detail or HTTPStatus(status).phrase)
        self.status = status
        self.detail = detail
        self.type = type
        # A problem of its own type has a title of its own; about:blank's is the
        # status's phrase (RFC 9457, 4.2.1).
        self.title = title or HTTPStatus(status).phrase
        self.headers = dict(headers or {})

    def response(self) -> JSONResponse:
        body: dict[str, object] = {"type": self.type, "title": self.title, "status": self.status}
        if self.detail:
            body["detail"] = self.detail
        return JSONResponse(body, self.status, headers=self.headers, media_type=MEDIA_TYPE)


def install(app: FastAPI) -> None:
    """Make ``app`` answer every error as a problem, its own and FastAPI's alike."""

    async def problem(_: Request, error: Exception) -> Response:
        assert isinstance(error, Problem)
        return error.response()

    async def http_error(_: Request, error: Exception) -> Response:
        assert isinstance(error, HTTPException)
        status = error.status_code
        # Starlette's details are its status phrases, or ours: nothing a client sent.
        detail = error.detail if error.detail != HTTPStatus(status).phrase else None
        return Problem(status, detail, headers=error.headers).response()

    async def invalid(_: Request, _error: Exception) -> Response:
        # The routes read their own bodies; this is a path or query FastAPI checked.
        return Problem(400, "the request's parameters are malformed").response()

    async def failed(_: Request, _error: Exception) -> Response:
        # Starlette raises the error again once this is sent, and the server logs it.
        return Problem(500).response()

    app.add_exception_handler(Problem, problem)
    app.add_exception_handler(HTTPException, http_error)
    app.add_exception_handler(RequestValidationError, invalid)
    app.add_exception_handler(Exception, failed)
