# SPDX-License-Identifier: MIT
import pytest

from ai_cloud.__main__ import ENVIRONMENT_ENV
from ai_cloud.shared.config import CONFIG_DIR_ENV


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own variables never change what the tests see."""
    for name in (CONFIG_DIR_ENV, ENVIRONMENT_ENV):
        monkeypatch.delenv(name, raising=False)
