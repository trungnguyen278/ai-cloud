# SPDX-License-Identifier: MIT
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_cloud.entry.api import ApiSecrets, ApiServer, create_app, run
from ai_cloud.shared.config import ConfigError
from ai_cloud.shared.jose import SigningKeys

# Connections open as they are first used: none of these tests reaches either.
DATABASE = "postgresql://cloud:hunter2@127.0.0.1:1/cloud"
REDIS = "redis://127.0.0.1:1/0"


@pytest.fixture
def secrets(jwk_set: Callable[[int], str]) -> ApiSecrets:
    return ApiSecrets(DATABASE, REDIS, SigningKeys.from_jwk_set(jwk_set(1)))


@pytest.fixture
def client(make_settings: Any, secrets: ApiSecrets) -> TestClient:
    return TestClient(create_app(make_settings(), secrets))


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_fastapi_s_description_is_not_served(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


def test_an_error_is_a_problem(make_settings: Any, secrets: ApiSecrets) -> None:
    app = create_app(make_settings(), secrets)

    @app.get("/broken")
    async def broken() -> None:
        raise RuntimeError("a bug")

    response = TestClient(app, raise_server_exceptions=False).get("/broken")
    assert response.status_code == 500
    assert response.json() == {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
    }


def test_the_development_provider_is_on_where_it_is_configured(
    make_settings: Any, secrets: ApiSecrets, caplog: pytest.LogCaptureFixture
) -> None:
    create_app(make_settings(accounts={"development": {"enabled": False}}), secrets)
    assert "development provider" not in caplog.text
    create_app(make_settings(), secrets)
    assert "the development provider is on" in caplog.text


def test_run_serves_as_the_settings_say(
    monkeypatch: pytest.MonkeyPatch, make_settings: Any, secrets: ApiSecrets
) -> None:
    served: list[ApiServer] = []
    monkeypatch.setattr(ApiServer, "run", lambda server, sockets=None: served.append(server))
    settings = make_settings(
        api={"host": "192.0.2.1", "forwarded_allow_ips": ["10.0.0.0/8"], "shutdown_timeout": 7}
    )
    run(settings, secrets)
    [server] = served
    config = server.config
    assert isinstance(config.app, FastAPI)
    assert config.host == "192.0.2.1"
    assert config.port == 8000
    assert config.access_log is True
    assert config.forwarded_allow_ips == ["10.0.0.0/8"]
    assert config.log_config is None
    # A control channel's message has 1 MiB at most, and the cloud link pings on its own.
    assert config.ws_max_size == 1 << 20
    assert config.ws_ping_interval is None
    # A connection that never closes holds the stop no longer than the configuration says.
    assert config.timeout_graceful_shutdown == 7


def test_the_secrets_are_read_and_checked(jwk_set: Callable[[int], str]) -> None:
    keys = jwk_set(2)
    secrets = ApiSecrets.read(
        {
            "AI_CLOUD_DATABASE_URL": DATABASE,
            "AI_CLOUD_REDIS_URL": REDIS,
            "AI_CLOUD_SIGNING_KEYS": keys,
        }
    )
    assert (secrets.database_url, secrets.redis_url) == (DATABASE, REDIS)
    for environ, problem in (
        ({"AI_CLOUD_REDIS_URL": REDIS, "AI_CLOUD_SIGNING_KEYS": keys}, "AI_CLOUD_DATABASE_URL"),
        (
            {
                "AI_CLOUD_DATABASE_URL": "mysql://cloud:hunter2@db/cloud",
                "AI_CLOUD_REDIS_URL": REDIS,
                "AI_CLOUD_SIGNING_KEYS": keys,
            },
            "postgresql://",
        ),
        ({"AI_CLOUD_DATABASE_URL": DATABASE, "AI_CLOUD_SIGNING_KEYS": keys}, "AI_CLOUD_REDIS_URL"),
        ({"AI_CLOUD_DATABASE_URL": DATABASE, "AI_CLOUD_REDIS_URL": REDIS}, "SIGNING_KEYS"),
    ):
        with pytest.raises(ConfigError, match=problem) as caught:
            ApiSecrets.read(environ)
        assert "hunter2" not in str(caught.value)


def test_a_redis_url_is_checked(make_settings: Any, secrets: ApiSecrets) -> None:
    with pytest.raises(ConfigError, match="AI_CLOUD_REDIS_URL must be") as caught:
        create_app(
            make_settings(),
            ApiSecrets(DATABASE, "http://:hunter2@cache", secrets.signing_keys),
        )
    assert "hunter2" not in str(caught.value)
