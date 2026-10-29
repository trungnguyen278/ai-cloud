# SPDX-License-Identifier: MIT
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
import pytest

from ai_cloud import software_version
from ai_cloud.__main__ import ENVIRONMENT_ENV, main
from ai_cloud.entry import api
from ai_cloud.entry.api import ApiSettings
from ai_cloud.shared.config import default_config_dir

STARTUP = 20.0  # seconds a process may take to answer, on a slow CI machine


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A copy of this checkout's configuration, for a test to add a local.toml to."""
    return Path(shutil.copytree(default_config_dir(), tmp_path / "config"))


@pytest.fixture(autouse=True)
def served(monkeypatch: pytest.MonkeyPatch) -> list[ApiSettings]:
    """What the api would have been served with. Nothing is served in the tests' own
    process, even when a test reaches it by mistake."""
    calls: list[ApiSettings] = []
    monkeypatch.setattr(api, "run", calls.append)
    return calls


def test_the_environment_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["api"])
    assert caught.value.code == 2
    assert ENVIRONMENT_ENV in capsys.readouterr().err


def test_the_environment_comes_from_the_command_line_or_a_variable(
    monkeypatch: pytest.MonkeyPatch, config_dir: Path, served: list[ApiSettings]
) -> None:
    (config_dir / "local.toml").write_text("[api]\nport = 8123\n", encoding="utf-8")
    assert main(["api", "--environment", "development", "--config-dir", str(config_dir)]) == 0
    monkeypatch.setenv(ENVIRONMENT_ENV, "production")
    assert main(["api", "--config-dir", str(config_dir)]) == 0
    assert served == [
        ApiSettings(host="127.0.0.1", port=8123, access_log=True),
        ApiSettings(host="0.0.0.0", port=8123, access_log=False),
    ]


@pytest.mark.parametrize(
    ("local", "problem"),
    [
        ("", "unknown environment 'staging'"),
        ("[api]\nport = 0\n", "api.port: Input should be greater than or equal to 1"),
        ("[api]\nport = 65536\n", "api.port: Input should be less than or equal to 65535"),
        ("[api]\nporrt = 1\n", "api.porrt: Extra inputs are not permitted"),
    ],
)
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


def test_the_api_process_serves_and_logs(config_dir: Path) -> None:
    port = free_port()
    (config_dir / "local.toml").write_text(
        f'[api]\nport = {port}\n[logging]\nformat = "json"\n', encoding="utf-8"
    )
    command = [sys.executable, "-m", "ai_cloud", "api", "--environment", "development"]
    process = subprocess.Popen(
        [*command, "--config-dir", str(config_dir)],
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
