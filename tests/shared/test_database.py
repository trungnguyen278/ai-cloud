# SPDX-License-Identifier: MIT
import pytest

from ai_cloud.shared.config import ConfigError
from ai_cloud.shared.database import database_url


@pytest.mark.parametrize("scheme", ["postgresql", "postgres", "postgresql+asyncpg"])
def test_the_cloud_chooses_the_driver(scheme: str) -> None:
    url = database_url(f"{scheme}://user:hunter2@db:5432/cloud")
    assert url.drivername == "postgresql+asyncpg"
    assert (url.username, url.password, url.host, url.database) == (
        "user",
        "hunter2",
        "db",
        "cloud",
    )


@pytest.mark.parametrize(
    ("url", "problem"),
    [
        ("mysql://user:hunter2@db/cloud", "must be a postgresql:// URL"),
        ("postgresql+psycopg://user:hunter2@db/cloud", "must be a postgresql:// URL"),
        ("user:hunter2 at db", "is not a URL"),
    ],
)
def test_another_url_is_refused_without_being_shown(url: str, problem: str) -> None:
    with pytest.raises(ConfigError, match=problem) as caught:
        database_url(url)
    assert "hunter2" not in str(caught.value)
