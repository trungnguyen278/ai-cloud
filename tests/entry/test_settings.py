# SPDX-License-Identifier: MIT
import shutil
from pathlib import Path

import pytest

from ai_cloud.entry.settings import CloudSettings
from ai_cloud.shared.config import (
    ConfigError,
    available_environments,
    deep_merge,
    default_config_dir,
    load_config,
)

ENVIRONMENTS = available_environments(default_config_dir())
# What a deployment gives in its own local.toml: values for one deployment are never committed
# (AGENTS.md). Production's devices need the address they reach it at.
DEPLOYMENTS = {"production": {"devices": {"server": "https://cloud.example"}}}


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """This checkout's configuration, without this machine's local.toml."""
    copied = Path(shutil.copytree(default_config_dir(), tmp_path / "config"))
    (copied / "local.toml").unlink(missing_ok=True)
    return copied


def test_the_environments_are_there() -> None:
    assert ENVIRONMENTS == ["development", "production"]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_every_environment_is_complete(environment: str, config_dir: Path) -> None:
    config = deep_merge(load_config(environment, config_dir), DEPLOYMENTS.get(environment, {}))
    CloudSettings.from_config(config)


def test_production_needs_the_address_its_devices_use(config_dir: Path) -> None:
    # Otherwise it would give devices an address of a developer's machine.
    with pytest.raises(ConfigError, match=r"devices\.server"):
        CloudSettings.from_config(load_config("production", config_dir))
