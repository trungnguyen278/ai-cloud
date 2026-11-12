# SPDX-License-Identifier: MIT
import json
import re
import time
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
    read_json,
)

TOKEN = "eyJ" + "s3cr3t" * 2000
DEPTH = 64  # as the configuration's max_json_depth, but the tests do not read it


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


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("[" * 20_000 + "]" * 20_000, id="nested too deep"),
        pytest.param("[" * 500_000 + "]" * 500_000, id="nested past Python's reader"),
        pytest.param('{"x": 1e400}', id="a number past a double"),
        pytest.param("-1e400", id="past a double below"),
        pytest.param('{"x": NaN}', id="NaN"),
        pytest.param("[Infinity]", id="Infinity"),
        pytest.param('"\\ud800"', id="a lone surrogate"),
        pytest.param(b"\xff", id="no UTF-8"),
        pytest.param("not JSON", id="no JSON"),
    ],
)
def test_what_is_not_json_is_a_value_error(text: str | bytes) -> None:
    with pytest.raises(ValueError) as raised:
        read_json(text, DEPTH)
    # It names no value: neither the character nor the byte.
    assert not re.search("d800|ff|NaN|Infinity|1e400", str(raised.value), re.IGNORECASE)


def test_json_is_read() -> None:
    assert read_json(b'{"x": [1.5, 1e300, -0, "\\u0041"]}', DEPTH) == {"x": [1.5, 1e300, 0, "A"]}


def _nested(levels: int, *, objects_from: int = 0) -> str:
    """JSON that nests ``levels`` deep, each level an array, or from the level
    ``objects_from`` (counted from 1; 0 for none) an object holding the next."""
    text = "1"
    for level in range(levels, 0, -1):
        text = f'{{"x": {text}}}' if objects_from and level >= objects_from else f"[{text}]"
    return text


@pytest.mark.parametrize("objects_from", [0, 1, 2, 33])
def test_json_is_read_down_to_the_depth_and_no_further(objects_from: int) -> None:
    """``max_depth`` counts levels of arrays and objects, mixed or not; Python's reader is
    not what limits it (it reads 20,000 deep on Linux)."""
    read_json(_nested(DEPTH, objects_from=objects_from), DEPTH)
    with pytest.raises(ValueError):
        read_json(_nested(DEPTH + 1, objects_from=objects_from), DEPTH)


def test_the_depth_is_what_the_caller_gives() -> None:
    assert read_json("[[1]]", 2) == [[1]]
    with pytest.raises(ValueError):
        read_json("[[1]]", 1)
    assert read_json("1", 1) == 1


def test_the_depth_counts_levels_in_every_branch() -> None:
    """The deepest branch comes last, behind branches that reach the limit and no further."""
    within = ",".join([_nested(DEPTH - 1)] * 3)
    read_json(f"[{within}]", DEPTH)
    with pytest.raises(ValueError):
        read_json(f"[{within},{_nested(DEPTH)}]", DEPTH)


def _arrays(schema: Any) -> Iterator[dict[str, Any]]:
    if isinstance(schema, dict):
        if schema.get("uniqueItems"):
            yield schema
        for value in schema.values():
            yield from _arrays(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _arrays(value)


def test_every_array_of_unique_items_is_bounded() -> None:
    """What the cloud's uniqueItems counts on: JSON Schema's own way compares each item with
    each, recursing as deep as they nest."""
    found = 0
    for path in sorted(platform_dir().glob("*/schema.json")):
        for array in _arrays(json.loads(path.read_text(encoding="utf-8"))):
            found += 1
            assert "maxItems" in array, path
            assert "items" in array, path
    assert found > 0


@pytest.mark.parametrize(
    ("items", "says"),
    [
        pytest.param([{"n": n} for n in range(6000)], "breaks maxItems", id="many"),
        pytest.param([json.loads("[" * 1000 + "]" * 1000)] * 2, "of type", id="nested"),
        pytest.param(["vi", "vi"], "uniqueItems", id="the same twice"),
        pytest.param([{"n": 1}, {"n": 1}], "uniqueItems", id="the same object twice"),
        pytest.param(1, "of type array", id="no array"),
    ],
)
def test_unique_items_are_compared_only_where_the_array_may_hold_them(
    items: Any, says: str
) -> None:
    # Objects are items, which cannot be sorted: each is compared with every other.
    schema = Schema(
        "a test's",
        {
            "type": "array",
            "items": {"type": ["string", "object"]},
            "maxItems": 16,
            "uniqueItems": True,
        },
    )
    started = time.monotonic()
    problem = schema.problem(items)
    assert time.monotonic() - started < 1
    assert problem is not None and says in problem
