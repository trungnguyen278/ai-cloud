# SPDX-License-Identifier: MIT
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ai_cloud.shared import protocols
from ai_cloud.shared.protocols import (
    PLATFORM_DIR_ENV,
    Schema,
    ecma_end,
    platform_dir,
    protocol_schema,
)

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
    protocol_schema.cache_clear()
    try:
        assert platform_dir() == tmp_path
        with pytest.raises(ValueError, match="cannot read the app protocol's schema"):
            protocol_schema("app", "revoke_request")
    finally:
        protocols._read.cache_clear()
        protocol_schema.cache_clear()


@pytest.mark.parametrize(
    ("definition", "good"),
    [
        ("language", "vi"),
        ("time_zone", "UTC"),
        ("invitation_code", "A" * 43),
        ("name", "Linh"),
        ("uuid", "01a1a06c-1400-75e6-a77f-f2149dbe59d5"),
    ],
)
def test_a_pattern_s_end_is_the_very_end(definition: str, good: str) -> None:
    schema = protocol_schema("app", definition)
    assert schema.problem(good) is None
    assert "breaks pattern" in str(schema.problem(good + "\n"))


@pytest.mark.parametrize(
    ("pattern", "python"),
    [
        ("^[a-z]+$", r"^[a-z]+\Z"),
        ("^a", "^a"),
        (r"^cost\$", r"^cost\$"),  # an escaped dollar is a dollar
        (r"^a\\$", r"^a\\\Z"),  # an escaped backslash, then the end
    ],
)
def test_a_pattern_ends_where_ecma_262_ends_it(pattern: str, python: str) -> None:
    assert ecma_end(pattern) == python


def test_an_escaped_dollar_is_a_dollar() -> None:
    schema = Schema("test", {"type": "string", "pattern": r"^cost\$"})
    assert schema.problem("cost$") is None
    assert schema.problem("cost") is not None


def test_a_pattern_is_only_for_text() -> None:
    assert protocol_schema("app", "name").problem(1) == "the body: must be of type string"


def _patterns(schema: Any) -> Iterator[str]:
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "pattern" and isinstance(value, str):
                yield value
            else:
                yield from _patterns(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _patterns(value)


# Classes that Python's regular expressions read otherwise than ECMA-262's, on text. Measured
# against Node 24 on 2026-11-03: Python's \d and \w take in every script's digits and letters,
# and its \s takes U+001C to U+001F and U+0085 where ECMA-262's takes U+FEFF.
_SHORTHANDS = re.compile(r"\\[dDwWsSbB]")


def test_every_pattern_reads_alike_in_python_and_ecma_262() -> None:
    """What ``protocols.ecma_end`` counts on, and ai-platform's ADR-0010 promises: each
    pattern's only ``$`` ends it, and none uses a class whose meaning differs between the
    two."""
    schemas = sorted(platform_dir().glob("*/schema.json"))
    assert schemas
    found = 0
    for path in schemas:
        for pattern in _patterns(json.loads(path.read_text(encoding="utf-8"))):
            found += 1
            assert "$" not in pattern[:-1], (path, pattern)
            assert _SHORTHANDS.search(pattern) is None, (path, pattern)
    assert found > 0


@pytest.mark.parametrize("pattern", [r"^\d+$", r"^\w$", r"a\sb", r"\bx"])
def test_a_shorthand_would_be_noticed(pattern: str) -> None:
    assert _SHORTHANDS.search(pattern) is not None
