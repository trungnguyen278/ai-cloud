# SPDX-License-Identifier: MIT
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ai_cloud.entry.api import ApiSettings, create_app, run


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_fastapi_s_description_is_not_served(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_run_serves_as_the_settings_say(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: calls.append((app, options)))
    run(ApiSettings(host="192.0.2.1", port=8123, access_log=False))
    [(app, options)] = calls
    assert isinstance(app, FastAPI)
    assert options == {
        "host": "192.0.2.1",
        "port": 8123,
        "access_log": False,
        "log_config": None,
    }
