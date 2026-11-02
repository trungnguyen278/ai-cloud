# SPDX-License-Identifier: MIT
"""The protocols' JSON Schemas, read from ai-platform, which this repository pins at
``platform/`` (CONTRIBUTING.md, design rule 1).

Every body that crosses to a device or an app is checked against them (SEC-14). A
problem names where the body is wrong and the rule it breaks, never the value, which may
be a token.
"""

import json
import os
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError, best_match

PLATFORM_DIR_ENV = "AI_CLOUD_PLATFORM_DIR"


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
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            raise ValueError(f"{name} is not a valid JSON Schema: {error.message}") from None
        self._validator = Draft202012Validator(schema)

    def problem(self, instance: Any) -> str | None:
        """What is wrong with ``instance``, or ``None`` if it matches."""
        error = best_match(self._validator.iter_errors(instance))
        return None if error is None else _describe(error)


def protocol_schema(protocol: str, definition: str) -> Schema:
    """``definition`` of ``protocol``'s schema, as ``platform/protocols/<protocol>/schema.json``
    has it; ``ValueError`` if it cannot be read."""
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
