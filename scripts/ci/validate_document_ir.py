#!/usr/bin/env python3
"""Validate a Document IR JSON file against the repository schema.

The project does not yet declare a Python dependency stack, so this validator
implements the small JSON Schema subset used by core/ir/document-ir-v0.schema.json.
It is intentionally strict for unknown keywords to keep the CI signal honest as
the schema evolves.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ANNOTATION_KEYS = {"$schema", "$id", "title", "description"}
SUPPORTED_KEYS = {
    *ANNOTATION_KEYS,
    "additionalProperties",
    "const",
    "enum",
    "items",
    "maximum",
    "minimum",
    "minItems",
    "properties",
    "required",
    "type",
}


class ValidationError(ValueError):
    """Raised when a document does not satisfy the schema."""


def type_matches(expected: str, value: Any) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    raise ValidationError(f"unsupported schema type: {expected}")


def format_path(path: tuple[str, ...]) -> str:
    return "$" + "".join(path)


def validate(schema: dict[str, Any], value: Any, path: tuple[str, ...] = ()) -> None:
    unknown = set(schema) - SUPPORTED_KEYS
    if unknown:
        raise ValidationError(f"{format_path(path)}: unsupported schema keyword(s): {', '.join(sorted(unknown))}")

    if "const" in schema and value != schema["const"]:
        raise ValidationError(f"{format_path(path)}: expected constant {schema['const']!r}")

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(item) for item in schema["enum"])
        raise ValidationError(f"{format_path(path)}: expected one of {allowed}")

    if "type" in schema:
        expected_types = schema["type"]
        if isinstance(expected_types, str):
            expected_types = [expected_types]
        if not any(type_matches(expected, value) for expected in expected_types):
            raise ValidationError(f"{format_path(path)}: expected type {schema['type']!r}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(f"{format_path(path)}: value is below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(f"{format_path(path)}: value is above maximum {schema['maximum']}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValidationError(f"{format_path(path)}: expected at least {schema['minItems']} item(s)")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                validate(item_schema, item, (*path, f"[{index}]"))

    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise ValidationError(f"{format_path(path)}: missing required key(s): {', '.join(missing)}")

        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ValidationError(f"{format_path(path)}: unexpected key(s): {', '.join(extra)}")

        for key, property_schema in properties.items():
            if key in value:
                validate(property_schema, value[key], (*path, f".{key}"))


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--document", type=Path, required=True)
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        document = load_json(args.document)
        validate(schema, document)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(f"Document IR validation failed: {exc}", file=sys.stderr)
        return 1

    print("Document IR validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
