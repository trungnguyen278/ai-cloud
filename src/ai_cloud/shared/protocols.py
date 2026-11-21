# SPDX-License-Identifier: MIT
"""The protocols' JSON Schemas, read from ai-platform, which this repository pins at
``platform/`` (CONTRIBUTING.md, design rule 1).

Every body that crosses to a device or an app is checked against them (SEC-14). A
problem names where the body is wrong and the rule it breaks, never the value, which may
be a token.
"""

import json
import math
import os
import re
from collections.abc import Iterator, Mapping
from functools import cache
from itertools import accumulate
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError, ValidationError, best_match

PLATFORM_DIR_ENV = "AI_CLOUD_PLATFORM_DIR"


def ecma_end(pattern: str) -> str:
    r"""``pattern`` for Python's ``re``, whose ``$`` also matches before a last newline:
    JSON Schema's patterns are ECMA-262's, whose ``$`` matches at the very end only, as
    Python's ``\Z`` does. Only a ``$`` that ends the pattern, unescaped, is an anchor here;
    the protocols' patterns have no other. Classes such as ``\d`` differ too, on text beyond
    ASCII: ai-platform's ADR-0010 keeps them out of the protocols' patterns, and the tests
    check it (tests/shared/test_protocols.py)."""
    body = pattern.removesuffix("$")
    if body == pattern:
        return pattern
    backslashes = len(body) - len(body.rstrip("\\"))
    return pattern if backslashes % 2 else body + r"\Z"


def _pattern(
    validator: Any, pattern: str, instance: Any, _schema: Mapping[str, Any]
) -> Iterator[ValidationError]:
    """JSON Schema's ``pattern``, with its end read as ECMA-262 reads it."""
    if validator.is_type(instance, "string") and not re.search(ecma_end(pattern), instance):
        yield ValidationError("does not match the pattern")


_UNIQUE_ITEMS = Draft202012Validator.VALIDATORS["uniqueItems"]


def _unique_items(
    validator: Any, unique: bool, instance: Any, schema: Mapping[str, Any]
) -> Iterator[ValidationError]:
    """JSON Schema's ``uniqueItems``, on an array that its own schema's ``maxItems`` and
    ``items`` allow: any other fails those. jsonschema compares items of any type, each with
    every other when they cannot be sorted, and recursively: 4,000 small objects took 17 s,
    and items nested 330 deep raised RecursionError (measured 2026-11-12, jsonschema 4.26.0).
    The tests check that the protocols bound every such array (tests/shared/test_protocols.py).
    """
    if not validator.is_type(instance, "array"):
        return
    if len(instance) > schema.get("maxItems", len(instance)):
        return
    items = schema.get("items")
    if items is not None and any(
        next(validator.descend(item, items), None) is not None for item in instance
    ):
        return
    yield from _UNIQUE_ITEMS(validator, unique, instance, schema)


_Validator = validators.extend(
    Draft202012Validator, {"pattern": _pattern, "uniqueItems": _unique_items}
)


class Unreadable(ValueError):
    """JSON that the cloud does not read: its message says why, and names no value."""


# How deep arrays and objects nest at most, counting the outermost: no side sends deeper,
# and every side reads at least this deep (ai-platform's ADR-0014, decision 3; the cloud
# link 3.1 and the app API 1.6). The protocols fix it, so that a device knows how deep the
# cloud reads, and reading deeper would serve no message.
MAX_DEPTH = 64


def read_json(text: str | bytes) -> Any:
    """``text`` as JSON that every side can hold (ai-platform's ADR-0014): ``Unreadable`` if
    it is not JSON, or if it holds
    - bytes that are not UTF-8, the only encoding of I-JSON (RFC 7493, 2.1);
    - arrays and objects nested more than ``MAX_DEPTH`` deep, counted on the text before it
      is parsed: ``json`` recurses as deep as they nest, and its own limit is where the C
      stack ends, 11,331 deep on Windows and past 20,000 on Linux (measured 2026-11-12,
      Python 3.14.5), so the answer would depend on the machine;
    - a name repeated in an object, which I-JSON forbids (RFC 7493, 2.3), and ADR-0014 lets
      a receiver refuse (decision 4). ``json`` keeps the last of them, and drops the others
      unread, which could hide what the cloud must refuse. Go's ``encoding/json/v2`` refuses
      them too ("A new experimental Go API for JSON", go.dev, 2025-09-09);
    - a number that is not finite: NaN and the infinities, which JSON lacks, or one past a
      double's range, such as ``1e400``, which Python would read as an infinity, or the same
      number written as an integer, which Python holds and JavaScript's reader takes for an
      infinity: the answer depends on the number, not on how it is written;
    - U+0000 or a lone surrogate, in a name or a string: JSON's escapes spell both, UTF-8
      cannot encode the second, and PostgreSQL stores neither.

    The rest of what I-JSON forbids, the cloud holds as it comes: noncharacters, and numbers
    more precise than a double, such as integers past 2**53. ADR-0014 lets a receiver take
    them (decision 4).
    """
    try:
        if isinstance(text, bytes):
            text = text.decode()
        if _depth(text) > MAX_DEPTH:
            raise Unreadable(_TOO_DEEP)
        value = json.loads(
            text,
            object_pairs_hook=_object,
            parse_constant=_not_finite,
            parse_float=_finite,
            parse_int=_integer,
        )
    except Unreadable:
        raise
    except UnicodeError:
        # Its own message names the byte: a value.
        raise Unreadable("it is not UTF-8") from None
    except ValueError:
        # JSON's own messages name places, and Python's for a long integer its length: the
        # cloud's own says enough.
        raise Unreadable("it is not JSON") from None
    _check_held(value)
    return value


_TOO_DEEP = "its arrays and objects nest deeper than the cloud reads"
# A JSON string, from its quote to its closing one (RFC 8259, 7): what it holds is no
# structure. Without its closing quote, it runs to the end of the text, which is no JSON
# then, so that each quote is read once: a pattern that required the closing quote would
# try again from each escaped quote, and take quadratic time.
_STRING = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"?')
_NOT_BRACKET = re.compile(r"[^\[\]{}]+")
_STEP = {"[": 1, "{": 1, "]": -1, "}": -1}


def _depth(text: str) -> int:
    """How deep ``text`` nests its arrays and objects: the most brackets open at once, outside
    its strings. Where ``text`` is no JSON, ``json`` stops before any deeper bracket."""
    brackets = _NOT_BRACKET.sub("", _STRING.sub("", text))
    return max(accumulate(map(_STEP.__getitem__, brackets)), default=0)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = dict(pairs)
    if len(value) < len(pairs):
        raise Unreadable("an object repeats a name")
    return value


# What no side holds (ADR-0014): U+0000, and a surrogate, which in a str of Python is a lone
# one, for a pair is read as the character it encodes.
_UNHELD = re.compile("[\x00\ud800-\udfff]")


def _check_held(value: Any) -> None:
    """``Unreadable`` if ``value`` holds a name or a string that no side holds."""
    waiting = [value]
    while waiting:
        item = waiting.pop()
        if isinstance(item, str):
            _check_text(item)
        elif isinstance(item, dict):
            for name in item:
                _check_text(name)
            waiting.extend(item.values())
        elif isinstance(item, list):
            waiting.extend(item)


def _check_text(text: str) -> None:
    if _UNHELD.search(text) is not None:
        raise Unreadable("it holds U+0000 or a lone surrogate, which no side holds")


_PAST_RANGE = "a number is past a double's range"


def _finite(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise Unreadable(_PAST_RANGE)
    return number


def _integer(text: str) -> int:
    # float() reads any number of digits, where int() takes at most sys.int_info's 4,300.
    if not math.isfinite(float(text)):
        raise Unreadable(_PAST_RANGE)
    return int(text)


def _not_finite(_: str) -> Any:
    raise Unreadable("a number is not finite")


def platform_dir() -> Path:
    """``$AI_CLOUD_PLATFORM_DIR`` if set, otherwise this checkout's ``platform/protocols``."""
    if value := os.environ.get(PLATFORM_DIR_ENV):
        return Path(value)
    # src/ai_cloud/shared/protocols.py -> platform/protocols
    return Path(__file__).resolve().parents[3] / "platform" / "protocols"


class Schema:
    """One definition of a protocol's schema, checked when it is made."""

    def __init__(self, name: str, schema: Mapping[str, Any]) -> None:
        self.name = name
        try:
            _Validator.check_schema(schema)
        except SchemaError as error:
            raise ValueError(f"{name} is not a valid JSON Schema: {error.message}") from None
        self._validator = _Validator(schema)

    def problem(self, instance: Any) -> str | None:
        """What is wrong with ``instance``, or ``None`` if it matches: the first rule that it
        breaks, in the schema's order. jsonschema's ``is_valid`` stops there too, where its
        ``best_match`` would weigh every error first, one for each item of a 1 MiB array
        (75,000 of them took 3 to 5 s, ai-platform's dev log, 2026-11-20). Ajv also returns
        after the first error unless told otherwise, and its security guidance says not to
        collect them all in production (``allErrors``, ajv.js.org, read 2026-11-21)."""
        error = next(self._validator.iter_errors(instance), None)
        # best_match descends into an anyOf's or a oneOf's own errors, which it holds already.
        return None if error is None else _describe(best_match([error]))


@cache
def protocol_schema(protocol: str, definition: str) -> Schema:
    """``definition`` of ``protocol``'s schema, as ``platform/protocols/<protocol>/schema.json``
    has it; ``ValueError`` if it cannot be read. One is made for each definition, for it takes
    checking the whole schema against JSON Schema's own."""
    schema = _read(protocol)
    if definition not in schema.get("$defs", {}):
        raise ValueError(f"the {protocol} protocol's schema has no {definition}")
    return Schema(
        f"platform/protocols/{protocol}/schema.json#/$defs/{definition}",
        {**schema, "$ref": f"#/$defs/{definition}"},
    )


@cache
def _read(protocol: str) -> dict[str, Any]:
    path = platform_dir() / protocol / "schema.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read the {protocol} protocol's schema: {error}") from None


def _describe(error: ValidationError) -> str:
    """Where ``error`` is and which rule it breaks: jsonschema's own messages quote the
    value."""
    where = ".".join(str(part) for part in error.absolute_path) or "the body"
    match error.validator:
        case "required":
            # It quotes the missing property's name, which the schema defines.
            return f"{where}: {error.message}"
        case "type":
            return f"{where}: must be of type {error.validator_value}"
        case _:
            return f"{where}: breaks {error.validator} {json.dumps(error.validator_value)}"
