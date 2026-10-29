# SPDX-License-Identifier: MIT
from pathlib import Path

import pytest

from ai_cloud.shared.config import (
    CONFIG_DIR_ENV,
    ConfigError,
    Settings,
    available_environments,
    deep_merge,
    default_config_dir,
    load_config,
)


def write_config(directory: Path, base: str, environments: dict[str, str], local: str = "") -> Path:
    (directory / "environments").mkdir(parents=True)
    (directory / "base.toml").write_text(base, encoding="utf-8")
    for name, text in environments.items():
        (directory / "environments" / f"{name}.toml").write_text(text, encoding="utf-8")
    if local:
        (directory / "local.toml").write_text(local, encoding="utf-8")
    return directory


class Inner(Settings):
    port: int
    hosts: list[str]


class Outer(Settings):
    inner: Inner
    name: str


def test_base_then_environment_then_local(tmp_path: Path) -> None:
    config_dir = write_config(
        tmp_path,
        base='name = "base"\n[inner]\nport = 1\nhosts = ["a", "b"]\n',
        environments={"one": '[inner]\nport = 2\nhosts = ["c"]\n', "two": ""},
        local="[inner]\nport = 3\n",
    )
    assert load_config("one", config_dir) == {"inner": {"port": 3, "hosts": ["c"]}, "name": "base"}
    assert load_config("two", config_dir) == {
        "inner": {"port": 3, "hosts": ["a", "b"]},
        "name": "base",
    }


def test_the_base_is_required(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"base\.toml not found"):
        load_config("one", tmp_path)


def test_an_unknown_environment_names_the_available_ones(tmp_path: Path) -> None:
    config_dir = write_config(tmp_path, base="", environments={"one": "", "two": ""})
    assert available_environments(config_dir) == ["one", "two"]
    with pytest.raises(ConfigError, match=r"unknown environment 'three' \(available: one, two\)"):
        load_config("three", config_dir)
    # Only an environment's name: never a path to another file.
    with pytest.raises(ConfigError, match="unknown environment"):
        load_config("../base", config_dir)


def test_a_malformed_file_is_named(tmp_path: Path) -> None:
    config_dir = write_config(tmp_path, base="", environments={"one": "port = "})
    with pytest.raises(ConfigError, match=r"one\.toml"):
        load_config("one", config_dir)


def test_the_directory_comes_from_the_environment_or_the_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert default_config_dir() == Path(__file__).resolve().parents[2] / "config"
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path))
    assert default_config_dir() == tmp_path


def test_deep_merge_merges_tables_and_replaces_the_rest() -> None:
    base = {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    override = {"a": {"c": [3]}, "d": {"e": 2}}
    assert deep_merge(base, override) == {"a": {"b": 1, "c": [3]}, "d": {"e": 2}}
    assert base == {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    assert override == {"a": {"c": [3]}, "d": {"e": 2}}


def test_settings_are_typed_and_frozen() -> None:
    settings = Outer.from_config({"inner": {"port": 1, "hosts": ["a"]}, "name": "x"})
    assert settings.inner == Inner(port=1, hosts=["a"])
    with pytest.raises(ValueError, match="frozen"):
        settings.name = "y"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        ({"inner": {"port": 1, "hosts": []}}, "name: Field required"),
        ({"inner": {"port": 1}, "name": "x"}, "inner.hosts: Field required"),
        (
            {"inner": {"port": 1, "hosts": [], "prot": 2}, "name": "x"},
            "inner.prot: Extra inputs are not permitted",
        ),
        ({"inner": {"port": "1", "hosts": []}, "name": "x"}, "inner.port: Input should be"),
        ({"inner": "port", "name": "x"}, "inner: Input should be"),
    ],
)
def test_every_problem_is_named(data: dict, problem: str) -> None:
    with pytest.raises(ConfigError) as caught:
        Outer.from_config(data)
    assert problem in str(caught.value)


def test_problems_never_show_values() -> None:
    with pytest.raises(ConfigError) as caught:
        Outer.from_config({"inner": {"port": "s3cr3t", "hosts": []}, "name": "x"})
    assert "s3cr3t" not in str(caught.value)
