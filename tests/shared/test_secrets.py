# SPDX-License-Identifier: MIT
from pathlib import Path

import pytest

from ai_cloud.shared.config import ConfigError
from ai_cloud.shared.secrets import read_secret

NAME = "AI_CLOUD_EXAMPLE"
VALUE = "postgresql://user:hunter2@db/cloud"


def test_from_the_environment() -> None:
    assert read_secret(NAME, {NAME: VALUE}) == VALUE


def test_from_a_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_bytes(f"{VALUE}\r\n".encode())
    assert read_secret(NAME, {f"{NAME}_FILE": str(secret)}) == VALUE


@pytest.mark.parametrize(
    ("environ", "problem"),
    [
        ({}, f"{NAME} is not set, nor {NAME}_FILE"),
        ({NAME: ""}, f"{NAME} is not set"),
        ({NAME: VALUE, f"{NAME}_FILE": "x"}, "both set"),
    ],
)
def test_a_missing_or_doubled_secret(environ: dict[str, str], problem: str) -> None:
    with pytest.raises(ConfigError, match=problem) as caught:
        read_secret(NAME, environ)
    assert "hunter2" not in str(caught.value)


def test_an_unreadable_file_is_named_not_shown(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=f"{NAME}_FILE: "):
        read_secret(NAME, {f"{NAME}_FILE": str(tmp_path / "missing")})
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigError, match="not UTF-8"):
        read_secret(NAME, {f"{NAME}_FILE": str(binary)})


def test_the_process_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NAME, VALUE)
    assert read_secret(NAME) == VALUE
