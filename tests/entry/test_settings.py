# SPDX-License-Identifier: MIT
import pytest

from ai_cloud.entry.settings import CloudSettings
from ai_cloud.shared.config import available_environments, default_config_dir, load_config

ENVIRONMENTS = available_environments(default_config_dir())


def test_the_environments_are_there() -> None:
    assert ENVIRONMENTS == ["development", "production"]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_every_environment_is_complete(environment: str) -> None:
    CloudSettings.from_config(load_config(environment))
