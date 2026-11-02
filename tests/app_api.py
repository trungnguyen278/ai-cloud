# SPDX-License-Identifier: MIT
"""An app that speaks the app API (platform/protocols/app/README.md) to an api served in
the tests' own process, and checks every answer against ai-platform's schema."""

from typing import Any

import httpx2
import redis.asyncio
from sqlalchemy import text

from ai_cloud.entry.settings import CloudSettings
from ai_cloud.shared.database import create_engine
from ai_cloud.shared.jose import SigningKeys
from ai_cloud.shared.problems import MEDIA_TYPE
from ai_cloud.shared.protocols import protocol_schema

CLIENT = "ai-app"
ADDRESS = "192.0.2.10"  # the app's, as the api sees it (TEST-NET-1, RFC 5737)
EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ID_TOKEN = "urn:ietf:params:oauth:token-type:id_token"


def conforms(response: httpx2.Response, definition: str) -> dict[str, Any]:
    """The response's body, once it matches ``definition`` of the app API's schema, and
    what the schema tells apart of the cloud's bodies."""
    body = response.json()
    for name in (definition, "from_cloud"):
        assert protocol_schema("app", name).problem(body) is None, (name, body)
    return body


def problem(response: httpx2.Response, status: int, code: str | None = None) -> dict[str, Any]:
    """The problem a response carries, of ``status``, and of type ``code`` or about:blank."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == MEDIA_TYPE
    body = conforms(response, "problem")
    assert body["type"] == (f"/app/v1/problems/{code}" if code else "about:blank")
    assert body["status"] == status
    return body


def oauth_error(response: httpx2.Response, error: str, status: int = 400) -> str:
    """The OAuth error a response carries; its description."""
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    body = conforms(response, "oauth_error")
    assert body["error"] == error
    return body["error_description"]


def bearer(tokens: dict[str, Any] | str) -> dict[str, str]:
    token = tokens if isinstance(tokens, str) else tokens["access_token"]
    return {"Authorization": f"Bearer {token}"}


class Cloud:
    """The api, as an app reaches it."""

    def __init__(
        self,
        http: httpx2.AsyncClient,
        *,
        keys: SigningKeys,
        settings: CloudSettings,
        database: str,
        redis_url: str,
    ) -> None:
        self.http = http
        self.keys = keys
        self.settings = settings
        self.database = database
        self.redis_url = redis_url

    async def nonce(self) -> str:
        response = await self.http.post("/app/v1/nonces")
        assert response.status_code == 201, response.text
        assert response.headers["cache-control"] == "no-store"
        return conforms(response, "nonce_response")["nonce"]

    async def id_token(
        self, email: str | None = "linh@example.com", name: str = "Linh", nonce: str = ""
    ) -> str:
        """An ID token of the development provider, for a new nonce of the cloud's."""
        body: dict[str, Any] = {"nonce": nonce or await self.nonce(), "display_name": name}
        if email is not None:
            body["email"] = email
        response = await self.http.post("/app/v1/development/id-tokens", json=body)
        assert response.status_code == 201, response.text
        return conforms(response, "development_id_token_response")["id_token"]

    async def exchange(
        self, id_token: str, client_id: str = CLIENT, label: str | None = "Pixel 8"
    ) -> httpx2.Response:
        form = {
            "grant_type": EXCHANGE,
            "subject_token": id_token,
            "subject_token_type": ID_TOKEN,
            "client_id": client_id,
        }
        if label is not None:
            form["session_label"] = label
        return await self.http.post("/app/token", data=form)

    async def sign_in(
        self, email: str | None = "linh@example.com", name: str = "Linh", **options: Any
    ) -> dict[str, Any]:
        """Tokens for the test person ``email`` names, through the whole exchange."""
        response = await self.exchange(await self.id_token(email, name), **options)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        return conforms(response, "token_response")

    async def refresh(self, refresh_token: str, client_id: str = CLIENT) -> httpx2.Response:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        return await self.http.post("/app/token", data=form)

    async def revoke(self, token: str, client_id: str = CLIENT, **hint: str) -> httpx2.Response:
        return await self.http.post(
            "/app/revoke", data={"token": token, "client_id": client_id, **hint}
        )

    async def account(self, tokens: dict[str, Any]) -> dict[str, Any]:
        response = await self.http.get("/app/v1/account", headers=bearer(tokens))
        assert response.status_code == 200, response.text
        return conforms(response, "account")

    async def sql(self, statement: str, **values: Any) -> list[Any]:
        """Run ``statement`` on the database, for what no request does, such as aging a
        session."""
        engine = create_engine(self.database, self.settings.database, "ai-cloud tests")
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement), values)
                return list(result) if result.returns_rows else []
        finally:
            await engine.dispose()

    async def redis(self, *command: Any) -> Any:
        """Run a Redis command, to see what the cloud keeps there."""
        client = redis.asyncio.Redis.from_url(self.redis_url)
        try:
            return await client.execute_command(*command)
        finally:
            await client.aclose()
