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
    Unreadable,
    ecma_end,
    platform_dir,
    protocol_schema,
    read_json,
)

TOKEN = "eyJ" + "s3cr3t" * 2000
DEPTH = 64  # the protocols' (ai-platform's ADR-0014, decision 3): the tests do not read the cloud's


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


DEEP = "its arrays and objects nest deeper than the cloud reads"
UNHELD = "it holds U+0000 or a lone surrogate, which no side holds"
NOT_JSON = "it is not JSON"
PAST_RANGE = "a number is past a double's range"
REPEATED = "an object repeats a name"


@pytest.mark.parametrize(
    ("text", "why"),
    [
        pytest.param("[" * 20_000 + "]" * 20_000, DEEP, id="nested too deep"),
        pytest.param("[" * 500_000 + "]" * 500_000, DEEP, id="nested past Python's reader"),
        pytest.param("[" * 65, DEEP, id="nested too deep, and never closed"),
        pytest.param('{"a": 1, "a": 1}', REPEATED, id="a repeated name"),
        pytest.param('[{"a": {"b": 1, "c": 2, "b": 3}}]', REPEATED, id="a repeated name within"),
        # What json would drop, keeping the last value of a name, is refused all the same.
        pytest.param('{"x": "\\u0000", "x": "ok"}', REPEATED, id="U+0000 dropped"),
        pytest.param('{"x": "\\ud800", "x": "ok"}', REPEATED, id="a lone surrogate dropped"),
        pytest.param('{"x": {"\\u0000": 1}, "x": 1}', REPEATED, id="U+0000 in a name dropped"),
        # Depth first, counted on the text: the same answer on every machine.
        pytest.param(
            '{"x": ' + "[" * 100 + "]" * 100 + ', "x": 1}', DEEP, id="nested too deep, dropped"
        ),
        pytest.param(
            '{"x": ' + "[" * 15_000 + "]" * 15_000 + ', "x": 1}',
            DEEP,
            id="nested past Python's reader on Windows, dropped",
        ),
        pytest.param('{"x": 1e400}', PAST_RANGE, id="past a double"),
        pytest.param("-1e400", PAST_RANGE, id="past a double below"),
        pytest.param('{"x": NaN}', "a number is not finite", id="NaN"),
        pytest.param("[Infinity]", "a number is not finite", id="Infinity"),
        pytest.param('"\\ud800"', UNHELD, id="a lone surrogate"),
        pytest.param('["x", "\\udc00y"]', UNHELD, id="a lone low surrogate"),
        pytest.param('"a\\u0000"', UNHELD, id="U+0000"),
        pytest.param('{"a\\u0000": 1}', UNHELD, id="U+0000 in a name"),
        pytest.param('{"a": ["x", {"b": "\\u0000"}]}', UNHELD, id="U+0000 in a value"),
        pytest.param('{"\\ud800": 1}', UNHELD, id="a lone surrogate in a name"),
        pytest.param(b"\xff", "it is not UTF-8", id="no UTF-8"),
        pytest.param('{"x": 1}'.encode("utf-16"), "it is not UTF-8", id="UTF-16"),
        pytest.param(b"\xed\xa0\x80", "it is not UTF-8", id="a surrogate in UTF-8"),
        pytest.param(b'\xef\xbb\xbf{"x": 1}', NOT_JSON, id="a byte order mark"),
        pytest.param('"a\x00"', NOT_JSON, id="a control character unescaped"),
        pytest.param("1" + "0" * 400, PAST_RANGE, id="an integer past a double"),
        pytest.param("[-1" + "0" * 400 + "]", PAST_RANGE, id="an integer past a double below"),
        pytest.param("1" * 5000, PAST_RANGE, id="an integer longer than Python reads"),
        pytest.param("not JSON", NOT_JSON, id="no JSON"),
        # A string that never closes runs to the end of the text, with what it holds.
        pytest.param('["' + "[" * 100, NOT_JSON, id="a string never closed"),
    ],
)
def test_what_is_not_json_every_side_holds_is_unreadable(text: str | bytes, why: str) -> None:
    """Whatever is wrong, the same error, whose message is the cloud's own: it names no
    value, neither a character nor a byte, nor where it is (ai-platform's ADR-0014)."""
    with pytest.raises(Unreadable) as raised:
        read_json(text)
    assert str(raised.value) == why


def test_what_no_side_holds_is_found_down_to_the_deepest_level() -> None:
    within = _nested(DEPTH - 1).replace("1", '"x"')
    read_json(f"[{within}]")
    for unheld in ('"\\u0000"', '"\\udfff"'):
        with pytest.raises(Unreadable):
            read_json(f"[{within.replace('"x"', unheld)}]")
        with pytest.raises(Unreadable):
            read_json(_nested(DEPTH, objects_from=DEPTH).replace('"x"', f"{unheld}"))


def test_what_i_json_forbids_and_the_cloud_holds_is_read() -> None:
    """Noncharacters, and numbers more precise than a double: ADR-0014 lets a receiver take
    them, and the cloud holds them as they come."""
    assert read_json('"\\uffff\\ufdd0"') == "\uffff\ufdd0"
    assert read_json(str(2**53 + 1)) == 2**53 + 1
    assert read_json("1" + "0" * 308) == 10**308
    # A pair of surrogates is the character it encodes.
    assert read_json('"\\ud83d\\ude00"') == "\U0001f600"
    # The same name in different objects is no repeated name.
    assert read_json('[{"a": {"a": 1}}, {"a": 2}]') == [{"a": {"a": 1}}, {"a": 2}]


def test_json_is_read() -> None:
    assert read_json(b'{"x": [1.5, 1e300, -0, "\\u0041"]}') == {"x": [1.5, 1e300, 0, "A"]}
    assert read_json("1") == 1
    # Each closes what it opened: siblings nest no deeper than one of them.
    assert (
        read_json(f"[{', '.join(['{}'] * DEPTH)}, {', '.join(['[]'] * DEPTH)}]")
        == [{}] * DEPTH + [[]] * DEPTH
    )


def _nested(levels: int, *, objects_from: int = 0) -> str:
    """JSON that nests ``levels`` deep, each level an array, or from the level
    ``objects_from`` (counted from 1; 0 for none) an object holding the next."""
    text = "1"
    for level in range(levels, 0, -1):
        text = f'{{"x": {text}}}' if objects_from and level >= objects_from else f"[{text}]"
    return text


@pytest.mark.parametrize("objects_from", [0, 1, 2, 33])
def test_json_is_read_down_to_the_depth_and_no_further(objects_from: int) -> None:
    """The depth counts levels of arrays and objects, mixed or not; Python's reader is not
    what limits it (it reads 20,000 deep on Linux)."""
    read_json(_nested(DEPTH, objects_from=objects_from))
    with pytest.raises(ValueError):
        read_json(_nested(DEPTH + 1, objects_from=objects_from))


def test_brackets_in_strings_nest_nothing() -> None:
    """Nor do they close anything, and an escaped quote ends no string."""
    text = "[" * 100 + '\\"' + "]" * 100
    assert read_json(f'["{text}", {{"]]{text}": "{{{{"}}]') == [
        text.replace("\\", ""),
        {f"]]{text.replace('\\', '')}": "{{"},
    ]
    with pytest.raises(Unreadable):
        read_json(_nested(DEPTH + 1).replace("1", '"]]]"'))
    # An escaped quote ends no string, and an escaped backslash escapes no quote.
    for string in ('"\\""', '"\\\\"'):
        with pytest.raises(Unreadable):
            read_json(f"[{string}, {_nested(DEPTH)}]")


def test_the_depth_counts_levels_in_every_branch() -> None:
    """The deepest branch comes last, behind branches that reach the limit and no further."""
    within = ",".join([_nested(DEPTH - 1)] * 3)
    read_json(f"[{within}]")
    with pytest.raises(ValueError):
        read_json(f"[{within},{_nested(DEPTH)}]")


def test_a_string_of_escaped_quotes_is_read_in_linear_time() -> None:
    """Each escaped quote could start a string, as a pattern sees it: one that tried them all
    took 6.4 s for 20,000 of them on Windows (2026-11-19), and these take milliseconds."""
    started = time.monotonic()
    with pytest.raises(Unreadable):
        read_json('"' + '\\"' * 20_000)
    assert time.monotonic() - started < 1


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
