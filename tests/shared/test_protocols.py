# SPDX-License-Identifier: MIT
from pathlib import Path

import pytest

from ai_cloud.shared import protocols
from ai_cloud.shared.protocols import PLATFORM_DIR_ENV, platform_dir, protocol_schema

TOKEN = "eyJ" + "s3cr3t" * 2000


def test_the_platform_is_this_checkout_s() -> None:
    assert (platform_dir() / "app" / "schema.json").is_file()


def test_a_body_that_matches_has_no_problem() -> None:
    schema = protocol_schema("app", "revoke_request")
    assert schema.problem({"token": "t", "client_id": "ai-app"}) is None


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ({"client_id": "ai-app"}, "'token' is a required property"),
        ({"token": TOKEN, "client_id": "ai-app"}, "token: breaks maxLength 8192"),
        ({"token": 1, "client_id": "ai-app"}, "token: must be of type string"),
        ({"token": "t", "client_id": "ai-app", "token_type_hint": TOKEN[:20]}, "enum"),
        ([TOKEN], "the body: must be of type object"),
    ],
)
def test_a_problem_names_the_rule_never_the_value(body: object, says: str) -> None:
    problem = protocol_schema("app", "revoke_request").problem(body)
    assert problem is not None and says in problem
    assert "s3cr3t" not in problem


def test_an_unknown_definition_or_protocol(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no nothing"):
        protocol_schema("app", "nothing")
    monkeypatch.setenv(PLATFORM_DIR_ENV, str(tmp_path))
    protocols._read.cache_clear()
    try:
        assert platform_dir() == tmp_path
        with pytest.raises(ValueError, match="cannot read the app protocol's schema"):
            protocol_schema("app", "revoke_request")
    finally:
        protocols._read.cache_clear()
