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


def read_json(text: str | bytes, max_depth: int) -> Any:
    """``text`` as JSON, holding only what the protocols' values may (ai-platform's ADR-0004):
    ``ValueError`` if it is not JSON, or if it holds
    - a number that is not finite: NaN and the infinities, which JSON lacks, or one past a
      double's range, such as ``1e400``, which Python would read as an infinity;
    - a lone surrogate, which JSON's escapes can spell and UTF-8 cannot: nothing can store it;
    - arrays and objects nested more than ``max_depth`` deep, which RFC 8259 lets a reader
      limit (section 9). ``json`` has a limit of its own, which the C stack sets: it raised
      RecursionError 11,331 deep on Windows, and read 20,000 deep on Linux (measured
      2026-11-12, Python 3.14.5); ``max_depth`` makes it the same everywhere.
    """
    try:
        value = json.loads(text, parse_constant=_not_finite, parse_float=_finite)
        if _nests_deeper(value, max_depth):
            raise RecursionError
        json.dumps(value, ensure_ascii=False).encode()
    except RecursionError:
        raise ValueError("the JSON nests deeper than the cloud reads") from None
    except UnicodeError:
        # Its own message names the character or the byte: a value.
        raise ValueError("the JSON is not Unicode text") from None
    return value


def _nests_deeper(value: Any, depth: int) -> bool:
    """Whether ``value`` holds an array or an object within ``depth`` others: level by level,
    for recursion is what the limit keeps the cloud from."""
    level = [value]
    for _ in range(depth):
        level = [
            child
            for item in level
            if isinstance(item, dict | list)
            for child in (item.values() if isinstance(item, dict) else item)
        ]
    return any(isinstance(item, dict | list) for item in level)


def _finite(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("a number is past a double's range")
    return number


def _not_finite(_: str) -> Any:
    raise ValueError("a number is not finite")


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
        """What is wrong with ``instance``, or ``None`` if it matches."""
        error = best_match(self._validator.iter_errors(instance))
        return None if error is None else _describe(error)


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
