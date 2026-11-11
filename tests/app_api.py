# SPDX-License-Identifier: MIT
"""An app that speaks the app API (platform/protocols/app/README.md) to an api served in
the tests' own process, and checks every answer against ai-platform's schema."""

import uuid
from dataclasses import dataclass
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


def once(tokens: dict[str, Any], key: str | None = None) -> dict[str, str]:
    """The headers of a request with an Idempotency-Key: ``key``, or a new one."""
    return {**bearer(tokens), "Idempotency-Key": key or f'"{uuid.uuid7()}"'}


@dataclass
class Family:
    """A home with one person of each kind, and someone who is not in it."""

    home: dict[str, Any]
    owner: dict[str, Any]
    admin: dict[str, Any]
    member: dict[str, Any]
    outsider: dict[str, Any]
    # The identifier in the home of each of those people, and of a profile.
    ids: dict[str, str]

    @property
    def path(self) -> str:
        return f"/app/v1/homes/{self.home['id']}"


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

    async def create_home(self, tokens: dict[str, Any], **changes: Any) -> dict[str, Any]:
        body = {
            "name": "Nhà Linh",
            "time_zone": "Asia/Ho_Chi_Minh",
            "languages": ["vi", "en"],
            **changes,
        }
        response = await self.http.post("/app/v1/homes", json=body, headers=once(tokens))
        assert response.status_code == 201, response.text
        assert response.headers["cache-control"] == "no-store"
        return conforms(response, "home")

    async def home(self, tokens: dict[str, Any], home: dict[str, Any]) -> dict[str, Any]:
        response = await self.http.get(f"/app/v1/homes/{home['id']}", headers=bearer(tokens))
        assert response.status_code == 200, response.text
        return conforms(response, "home")

    async def members(self, tokens: dict[str, Any], home: dict[str, Any]) -> list[dict[str, Any]]:
        response = await self.http.get(
            f"/app/v1/homes/{home['id']}/members", headers=bearer(tokens)
        )
        assert response.status_code == 200, response.text
        return conforms(response, "member_list")["members"]

    async def add_profile(
        self, tokens: dict[str, Any], home: dict[str, Any], name: str = "Bà"
    ) -> dict[str, Any]:
        response = await self.http.post(
            f"/app/v1/homes/{home['id']}/members",
            json={"display_name": name},
            headers=once(tokens),
        )
        assert response.status_code == 201, response.text
        return conforms(response, "member")

    async def invite(self, tokens: dict[str, Any], home: dict[str, Any]) -> dict[str, Any]:
        response = await self.http.post(
            f"/app/v1/homes/{home['id']}/invitations", headers=bearer(tokens)
        )
        assert response.status_code == 201, response.text
        assert response.headers["cache-control"] == "no-store"
        return conforms(response, "invitation")

    async def accept(self, tokens: dict[str, Any], code: str) -> httpx2.Response:
        return await self.http.post(
            "/app/v1/invitations:accept", json={"code": code}, headers=bearer(tokens)
        )

    async def join(
        self, tokens: dict[str, Any], home: dict[str, Any], inviter: dict[str, Any]
    ) -> dict[str, Any]:
        """Bring the person in with an invitation that ``inviter`` makes: the home, as that
        person sees it."""
        response = await self.accept(tokens, (await self.invite(inviter, home))["code"])
        assert response.status_code == 201, response.text
        return conforms(response, "home")

    async def family(self) -> Family:
        """A home with its owner, Linh; an admin, An; a member, Minh; and a profile, Bà. Bảo,
        who has an account too, is not in it."""
        owner = await self.sign_in("linh@example.com", "Linh")
        admin = await self.sign_in("an@example.com", "An")
        member = await self.sign_in("minh@example.com", "Minh")
        outsider = await self.sign_in("bao@example.com", "Bảo")
        home = await self.create_home(owner)
        ids = {"owner": home["member_id"]}
        ids["admin"] = (await self.join(admin, home, owner))["member_id"]
        ids["member"] = (await self.join(member, home, owner))["member_id"]
        ids["profile"] = (await self.add_profile(owner, home))["id"]
        response = await self.http.patch(
            f"/app/v1/homes/{home['id']}/members/{ids['admin']}",
            json={"role": "admin"},
            headers=bearer(owner),
        )
        assert response.status_code == 200, response.text
        return Family(home, owner, admin, member, outsider, ids)

    async def challenge(self, tokens: dict[str, Any], home: dict[str, Any]) -> dict[str, Any]:
        """A challenge to register a device into the home."""
        response = await self.http.post(
            f"/app/v1/homes/{home['id']}/device-challenges", headers=bearer(tokens)
        )
        assert response.status_code == 201, response.text
        assert response.headers["cache-control"] == "no-store"
        return conforms(response, "device_challenge")

    async def register(
        self, tokens: dict[str, Any], home: dict[str, Any], registration: str, key: str = ""
    ) -> httpx2.Response:
        """Relay a device's registration into the home, with the Idempotency-Key ``key``, or
        a new one."""
        return await self.http.post(
            f"/app/v1/homes/{home['id']}/devices",
            json={"registration": registration},
            headers=once(tokens, key or None),
        )

    async def add_device(
        self, tokens: dict[str, Any], home: dict[str, Any], device: Any
    ) -> dict[str, Any]:
        """Register ``device`` into the home, as set-up does: a challenge, which the device
        signs, then its registration."""
        issued = await self.challenge(tokens, home)
        response = await self.register(
            tokens, home, device.registration(issued["challenge"], issued["server"])
        )
        assert response.status_code == 201, response.text
        return conforms(response, "device")

    async def devices(self, tokens: dict[str, Any], home: dict[str, Any]) -> list[dict[str, Any]]:
        response = await self.http.get(
            f"/app/v1/homes/{home['id']}/devices", headers=bearer(tokens)
        )
        assert response.status_code == 200, response.text
        return conforms(response, "device_list")["devices"]

    @property
    def token_endpoint(self) -> str:
        """The URL of the devices' token endpoint, as a device makes it from its server."""
        return self.settings.devices.server.rstrip("/") + "/device/token"

    async def device_token(self, assertion: str | None, **fields: str) -> httpx2.Response:
        """Ask the token endpoint for a device's access token, with ``assertion`` (RFC 7523)."""
        form = {
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            **fields,
        }
        if assertion is not None:
            form["client_assertion"] = assertion
        return await self.http.post("/device/token", data=form)

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

    async def events(self) -> list[tuple[str, dict[str, Any]]]:
        """The events the accounts have written, in their order: each one's type and data."""
        rows = await self.sql("SELECT type, data FROM accounts.outbox ORDER BY seq")
        return [(row.type, row.data) for row in rows]

    async def redis(self, *command: Any) -> Any:
        """Run a Redis command, to see what the cloud keeps there."""
        client = redis.asyncio.Redis.from_url(self.redis_url)
        try:
            return await client.execute_command(*command)
        finally:
            await client.aclose()
