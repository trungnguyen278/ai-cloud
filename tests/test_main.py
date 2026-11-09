# SPDX-License-Identifier: MIT
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest
import redis
from sqlalchemy import text

from ai_cloud import software_version
from ai_cloud.__main__ import ENVIRONMENT_ENV, main
from ai_cloud.accounts.api import OUTBOX, HomeDeleted
from ai_cloud.entry import api, migrate, worker
from ai_cloud.entry.settings import ApiSettings
from ai_cloud.entry.worker import WorkerSecrets
from ai_cloud.shared.config import default_config_dir
from ai_cloud.shared.database import create_engine
from ai_cloud.shared.jose import SigningKeys

STARTUP = 20.0  # seconds a process may take to answer, on a slow CI machine
# Connections open as they are first used: the api's health check reaches neither.
DATABASE = "postgresql://cloud:hunter2@127.0.0.1:1/cloud"
REDIS = "redis://127.0.0.1:1/0"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A copy of this checkout's configuration, for a test to add a local.toml to."""
    return Path(shutil.copytree(default_config_dir(), tmp_path / "config"))


@pytest.fixture
def secrets(monkeypatch: pytest.MonkeyPatch, jwk_set: Callable[[int], str]) -> dict[str, str]:
    """The api's secrets, in the environment."""
    values = {
        "AI_CLOUD_DATABASE_URL": DATABASE,
        "AI_CLOUD_REDIS_URL": REDIS,
        "AI_CLOUD_SIGNING_KEYS": jwk_set(1),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


@pytest.fixture(autouse=True)
def served(monkeypatch: pytest.MonkeyPatch) -> list[ApiSettings]:
    """What the api would have been served with. Nothing is served in the tests' own
    process, even when a test reaches it by mistake."""
    calls: list[ApiSettings] = []
    monkeypatch.setattr(api, "run", lambda settings, _secrets: calls.append(settings.api))
    return calls


@pytest.fixture(autouse=True)
def migrated(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """What ``migrate`` would have done. Nothing reaches a database."""
    calls: list[tuple[str, ...]] = []

    async def run(url: str, _settings: Any) -> None:
        calls.append(("migrate", url))

    async def revise(url: str, _settings: Any, module: Any, message: str) -> Path:
        calls.append(("revise", url, module.schema, message))
        return Path("written.py")

    monkeypatch.setattr(migrate, "migrate", run)
    monkeypatch.setattr(migrate, "revise", revise)
    return calls


@pytest.fixture(autouse=True)
def worked(monkeypatch: pytest.MonkeyPatch) -> list[WorkerSecrets]:
    """What the worker would have worked with. Nothing works in the tests' own process."""
    calls: list[WorkerSecrets] = []
    monkeypatch.setattr(worker, "run", lambda _settings, secrets: calls.append(secrets))
    return calls


def test_the_environment_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["api"])
    assert caught.value.code == 2
    assert ENVIRONMENT_ENV in capsys.readouterr().err


@pytest.mark.usefixtures("secrets")
def test_the_environment_comes_from_the_command_line_or_a_variable(
    monkeypatch: pytest.MonkeyPatch, config_dir: Path, served: list[ApiSettings]
) -> None:
    # As a deployment's local.toml has it, with the address its devices use.
    local = '[api]\nport = 8123\n[devices]\nserver = "https://cloud.example"\n'
    (config_dir / "local.toml").write_text(local, encoding="utf-8")
    assert main(["api", "--environment", "development", "--config-dir", str(config_dir)]) == 0
    monkeypatch.setenv(ENVIRONMENT_ENV, "production")
    assert main(["api", "--config-dir", str(config_dir)]) == 0
    same = {"port": 8123, "forwarded_allow_ips": ["127.0.0.1", "::1"], "max_body_size": 65536}
    assert served == [
        ApiSettings(host="127.0.0.1", access_log=True, **same),
        ApiSettings(host="0.0.0.0", access_log=False, **same),
    ]


@pytest.mark.parametrize("missing", ["AI_CLOUD_DATABASE_URL", "AI_CLOUD_SIGNING_KEYS"])
def test_the_api_stops_without_its_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    secrets: dict[str, str],
    served: list[ApiSettings],
    missing: str,
) -> None:
    monkeypatch.delenv(missing)
    assert main(["api", "--environment", "development"]) == 2
    error = capsys.readouterr().err
    assert missing in error
    assert "hunter2" not in error
    assert served == []


def test_migrate_needs_the_database_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated: list[tuple[str, ...]],
) -> None:
    assert main(["migrate", "--environment", "development"]) == 2
    assert "AI_CLOUD_DATABASE_URL" in capsys.readouterr().err
    monkeypatch.setenv("AI_CLOUD_DATABASE_URL", DATABASE)
    assert main(["migrate", "--environment", "development"]) == 0
    assert (
        main(
            [
                "migrate",
                "--environment",
                "development",
                "--revision",
                "Add a column",
                "--module",
                "accounts",
            ]
        )
        == 0
    )
    assert migrated == [
        ("migrate", DATABASE),
        ("revise", DATABASE, "accounts", "Add a column"),
    ]
    assert capsys.readouterr().out.strip() == "written.py"


def test_the_worker_needs_the_database_and_redis_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    worked: list[WorkerSecrets],
) -> None:
    for name, value in (("AI_CLOUD_DATABASE_URL", DATABASE), ("AI_CLOUD_REDIS_URL", REDIS)):
        assert main(["worker", "--environment", "development"]) == 2
        assert name in capsys.readouterr().err
        monkeypatch.setenv(name, value)
    assert main(["worker", "--environment", "development"]) == 0
    assert worked == [WorkerSecrets(DATABASE, REDIS)]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--revision", "Add a column"],
        ["--module", "accounts"],
        ["--module", "nothing", "--revision", "x"],
    ],
)
def test_a_revision_names_its_module(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["migrate", "--environment", "development", *arguments])
    assert caught.value.code == 2


def test_a_new_signing_key(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["signing-key"]) == 0
    printed = capsys.readouterr().out
    keys = SigningKeys.from_jwk_set(printed)
    assert keys.sign({"sub": "a"}, typ="at+jwt")
    assert len(json.loads(printed)["keys"]) == 1


@pytest.mark.parametrize(
    ("local", "problem"),
    [
        ("", "unknown environment 'staging'"),
        ("[api]\nport = 0\n", "api.port: Input should be greater than or equal to 1"),
        ("[api]\nport = 65536\n", "api.port: Input should be less than or equal to 65535"),
        ("[api]\nporrt = 1\n", "api.porrt: Extra inputs are not permitted"),
    ],
)
@pytest.mark.usefixtures("secrets")
def test_a_bad_configuration_stops_before_serving(
    capsys: pytest.CaptureFixture[str],
    config_dir: Path,
    served: list[ApiSettings],
    local: str,
    problem: str,
) -> None:
    environment = "development" if local else "staging"
    (config_dir / "local.toml").write_text(local, encoding="utf-8")
    assert main(["api", "--environment", environment, "--config-dir", str(config_dir)]) == 2
    assert problem in capsys.readouterr().err
    assert served == []


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == f"ai-cloud {software_version()}"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_api_process_serves_and_logs(config_dir: Path, secrets: dict[str, str]) -> None:
    port = free_port()
    (config_dir / "local.toml").write_text(
        f'[api]\nport = {port}\n[logging]\nformat = "json"\n', encoding="utf-8"
    )
    command = [sys.executable, "-m", "ai_cloud", "api", "--environment", "development"]
    process = subprocess.Popen(
        [*command, "--config-dir", str(config_dir)],
        env={**os.environ, **secrets},
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        deadline = time.monotonic() + STARTUP
        while True:
            assert process.poll() is None, "the process stopped"
            try:
                response = httpx2.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                break
            except httpx2.TransportError:
                assert time.monotonic() < deadline, "the process never answered"
                time.sleep(0.05)
        assert response.json() == {"status": "ok"}
    finally:
        process.terminate()
        _, errors = process.communicate(timeout=STARTUP)
    # Every record is in the configured form, uvicorn's own included.
    entries = [json.loads(line) for line in errors.splitlines()]
    assert entries[0]["message"] == "starting the api process"
    assert entries[0]["environment"] == "development"
    assert entries[0]["version"] == software_version()
    [access] = [entry for entry in entries if entry["logger"] == "uvicorn.access"]
    assert '"GET /health HTTP/1.1" 200' in access["message"]
    assert not [entry for entry in entries if "color_message" in entry]


async def _write_and_forget(url: str, settings: Any, home_id: uuid.UUID | None) -> None:
    """Write an event to the accounts' outbox, or, for ``None``, empty it."""
    engine = create_engine(url, settings, "ai-cloud tests")
    try:
        async with engine.begin() as connection:
            if home_id is None:
                await connection.execute(text("TRUNCATE accounts.outbox"))
            else:
                await OUTBOX.write(connection, HomeDeleted(home_id))
    finally:
        await engine.dispose()


def test_the_worker_process_relays_until_it_is_told_to_stop(
    config_dir: Path, postgres_url: str, redis_url: str, make_settings: Any
) -> None:
    (config_dir / "local.toml").write_text('[logging]\nformat = "json"\n', encoding="utf-8")
    command = [sys.executable, "-m", "ai_cloud", "worker", "--environment", "development"]
    # Windows sends no SIGTERM, but Ctrl+Break, to a process group of the child's own.
    windows = sys.platform == "win32"
    process = subprocess.Popen(
        [*command, "--config-dir", str(config_dir)],
        env={**os.environ, "AI_CLOUD_DATABASE_URL": postgres_url, "AI_CLOUD_REDIS_URL": redis_url},
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
    )
    database = make_settings().database
    client = redis.Redis.from_url(redis_url)
    try:
        asyncio.run(_write_and_forget(postgres_url, database, uuid.uuid7()))
        deadline = time.monotonic() + STARTUP
        while not client.xlen(OUTBOX.stream):
            assert process.poll() is None, "the process stopped"
            assert time.monotonic() < deadline, "the event was never relayed"
            time.sleep(0.05)
        process.send_signal(signal.CTRL_BREAK_EVENT if windows else signal.SIGTERM)
        _, errors = process.communicate(timeout=STARTUP)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        client.close()
        asyncio.run(_write_and_forget(postgres_url, database, None))
    assert process.returncode == 0, errors
    messages = [json.loads(line)["message"] for line in errors.splitlines()]
    assert messages == [
        "starting the worker process",
        "the worker process stops",
        "the worker process has stopped",
    ]
