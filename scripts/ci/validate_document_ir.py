#!/usr/bin/env python3
"""Validate a Document IR or template-definition JSON file against a repository schema.

The project does not yet declare a Python dependency stack, so this validator
implements the small JSON Schema subset used by core/ir/document-ir-v0.schema.json.
It is intentionally strict for unknown keywords to keep the CI signal honest as
the schema evolves.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, NoReturn


ANNOTATION_KEYS = {"$schema", "$id", "title", "description"}
SUPPORTED_KEYS = {
    *ANNOTATION_KEYS,
    "additionalProperties",
    "const",
    "enum",
    "exclusiveMinimum",
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
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        if isinstance(value, float):
            return math.isfinite(value) and value.is_integer()
        return False
    if expected == "number":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        if isinstance(value, float):
            return math.isfinite(value)
        return False
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
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValidationError(
                f"{format_path(path)}: value must be greater than {schema['exclusiveMinimum']}"
            )
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


def validate_document_ir_consistency(document: dict[str, Any]) -> None:
    schema_version = document.get("schema_version")
    if schema_version == "document-ir/v1":
        validate_document_ir_v1_consistency(document)
        return
    validate_document_ir_v0_consistency(document)


def validate_template_definition_consistency(template: dict[str, Any]) -> None:
    anchors_by_id = _unique_template_items(template.get("anchors", []), "anchor_id", "$.anchors")
    anchor_ids = set(anchors_by_id)
    fields_by_id = _unique_template_items(template.get("fields", []), "field_id", "$.fields")
    field_ids = set(fields_by_id)
    tables_by_id = _unique_template_items(template.get("tables", []), "table_id", "$.tables")
    table_ids = set(tables_by_id)
    rules_by_id = _unique_template_items(template.get("validation_rules", []), "rule_id", "$.validation_rules")
    rule_ids = set(rules_by_id)

    _validate_template_risk_rank(template, fields_by_id, tables_by_id)

    for index, field in enumerate(template.get("fields", [])):
        source = field.get("source", {})
        anchor_id = source.get("anchor_id")
        if anchor_id not in anchor_ids:
            raise ValidationError(
                "$.fields"
                f"[{index}]"
                ".source.anchor_id: "
                f"references undeclared anchor {anchor_id!r}"
            )
        anchor = anchors_by_id[anchor_id]
        if source.get("direction") == "table_cell" and not _is_template_table_anchor(anchor):
            raise ValidationError(
                "$.fields"
                f"[{index}]"
                ".source.anchor_id: "
                f"table_cell source references non-table anchor {anchor_id!r}"
            )
        for rule_index, rule_id in enumerate(field.get("validation_rule_ids", [])):
            if rule_id not in rule_ids:
                raise ValidationError(
                    "$.fields"
                    f"[{index}]"
                    ".validation_rule_ids"
                    f"[{rule_index}]"
                    f": references undeclared validation rule {rule_id!r}"
                )
            rule = rules_by_id[rule_id]
            if rule.get("target") != field.get("field_id"):
                raise ValidationError(
                    "$.fields"
                    f"[{index}]"
                    ".validation_rule_ids"
                    f"[{rule_index}]"
                    f": validation rule {rule_id!r} targets {rule.get('target')!r}, "
                    f"not field {field.get('field_id')!r}"
                )

    for index, table in enumerate(template.get("tables", [])):
        anchor_id = table.get("anchor_id")
        if anchor_id not in anchor_ids:
            raise ValidationError(
                "$.tables"
                f"[{index}]"
                ".anchor_id: "
                f"references undeclared anchor {anchor_id!r}"
            )
        anchor = anchors_by_id[anchor_id]
        if not _is_template_table_anchor(anchor):
            raise ValidationError(
                "$.tables"
                f"[{index}]"
                ".anchor_id: "
                f"references non-table anchor {anchor_id!r}"
            )

    _validate_template_output_key_uniqueness(template)

    for index, rule in enumerate(template.get("validation_rules", [])):
        _validate_template_rule_operands(rule, index, fields_by_id)

    for index, field_mapping in enumerate(template.get("output_mapping", {}).get("field_map", [])):
        field_id = field_mapping.get("field_id")
        if field_id not in field_ids:
            raise ValidationError(
                "$.output_mapping.field_map"
                f"[{index}]"
                ".field_id: "
                f"references undeclared field {field_id!r}"
            )
        expected_output_key = fields_by_id[field_id].get("output_key")
        if field_mapping.get("output_key") != expected_output_key:
            raise ValidationError(
                "$.output_mapping.field_map"
                f"[{index}]"
                ".output_key: "
                f"must match field {field_id!r} output_key {expected_output_key!r}"
            )

    _validate_template_output_mapping_coverage(
        template.get("output_mapping", {}).get("field_map", []),
        field_ids,
        "field_id",
        "$.output_mapping.field_map",
    )

    for index, table_mapping in enumerate(template.get("output_mapping", {}).get("table_map", [])):
        table_id = table_mapping.get("table_id")
        if table_id not in table_ids:
            raise ValidationError(
                "$.output_mapping.table_map"
                f"[{index}]"
                ".table_id: "
                f"references undeclared table {table_id!r}"
            )
        expected_output_key = tables_by_id[table_id].get("output_key")
        if table_mapping.get("output_key") != expected_output_key:
            raise ValidationError(
                "$.output_mapping.table_map"
                f"[{index}]"
                ".output_key: "
                f"must match table {table_id!r} output_key {expected_output_key!r}"
            )

    _validate_template_output_mapping_coverage(
        template.get("output_mapping", {}).get("table_map", []),
        table_ids,
        "table_id",
        "$.output_mapping.table_map",
    )


def _unique_template_items(items: list[dict[str, Any]], key: str, path: str) -> dict[str, dict[str, Any]]:
    items_by_id: dict[str, dict[str, Any]] = {}
    ids: set[str] = set()
    for index, item in enumerate(items):
        item_id = item.get(key)
        if item_id in ids:
            raise ValidationError(f"{path}[{index}].{key}: duplicates {item_id!r}")
        ids.add(item_id)
        items_by_id[item_id] = item
    return items_by_id


def _is_template_table_anchor(anchor: dict[str, Any]) -> bool:
    block_types = anchor.get("scope", {}).get("block_types", [])
    return anchor.get("kind") == "table_header" and "table" in block_types


def _validate_template_output_key_uniqueness(template: dict[str, Any]) -> None:
    seen_output_keys: dict[str, str] = {}
    for section in ("fields", "tables"):
        for index, item in enumerate(template.get(section, [])):
            output_key = item.get("output_key")
            path = f"$.{section}[{index}].output_key"
            if output_key in seen_output_keys:
                raise ValidationError(
                    f"{path}: duplicates output_key {output_key!r} "
                    f"already declared at {seen_output_keys[output_key]}"
                )
            seen_output_keys[output_key] = path


def _validate_template_output_mapping_coverage(
    mappings: list[dict[str, Any]],
    expected_ids: set[str],
    key: str,
    path: str,
) -> None:
    mapped_ids: set[str] = set()
    for index, mapping in enumerate(mappings):
        mapped_id = mapping.get(key)
        if mapped_id in mapped_ids:
            raise ValidationError(f"{path}[{index}].{key}: duplicates mapping for {mapped_id!r}")
        mapped_ids.add(mapped_id)

    missing_ids = sorted(expected_ids - mapped_ids)
    if missing_ids:
        raise ValidationError(
            f"{path}: missing mapping(s) for "
            + ", ".join(repr(item_id) for item_id in missing_ids)
        )


def _validate_template_risk_rank(
    template: dict[str, Any],
    fields_by_id: dict[str, dict[str, Any]],
    tables_by_id: dict[str, dict[str, Any]],
) -> None:
    risk_rank = template.get("risk_rank", {})
    declared_levels = {
        level.get("level")
        for level in risk_rank.get("levels", [])
    }
    used_levels = {
        risk_rank.get("default_level"),
        *risk_rank.get("review_required_levels", []),
        *(field.get("risk_level") for field in fields_by_id.values()),
        *(table.get("risk_level") for table in tables_by_id.values()),
    }
    missing_levels = sorted(level for level in used_levels if level not in declared_levels)
    if missing_levels:
        raise ValidationError(
            "$.risk_rank.levels: "
            "missing level definition(s) for "
            + ", ".join(repr(level) for level in missing_levels)
        )


def _validate_template_rule_operands(
    rule: dict[str, Any],
    index: int,
    fields_by_id: dict[str, dict[str, Any]],
) -> None:
    field_ids = set(fields_by_id)
    target = rule.get("target")
    if target not in field_ids:
        raise ValidationError(
            "$.validation_rules"
            f"[{index}]"
            ".target: "
            f"references undeclared field {target!r}"
        )

    rule_type = rule.get("rule_type")
    declared_type = fields_by_id[target].get("value_type")
    if rule_type == "type":
        if "expected_type" not in rule:
            raise ValidationError(f"$.validation_rules[{index}].expected_type: required for type rule")
        expected_type = rule.get("expected_type")
        if expected_type != declared_type:
            raise ValidationError(
                "$.validation_rules"
                f"[{index}]"
                ".expected_type: "
                f"{expected_type!r} does not match field {target!r} value_type {declared_type!r}"
            )
    if rule_type == "range":
        if declared_type != "number":
            raise ValidationError(
                "$.validation_rules"
                f"[{index}]"
                f": range rule target {target!r} requires number field, got {declared_type!r}"
            )
        if "minimum" not in rule and "maximum" not in rule:
            raise ValidationError(f"$.validation_rules[{index}]: range rule requires minimum or maximum")
        if "minimum" in rule and "maximum" in rule and rule["minimum"] > rule["maximum"]:
            raise ValidationError(f"$.validation_rules[{index}]: minimum cannot exceed maximum")
    if rule_type == "allowed_values":
        if "allowed_values" not in rule:
            raise ValidationError(f"$.validation_rules[{index}].allowed_values: required for allowed_values rule")
        for value_index, value in enumerate(rule.get("allowed_values", [])):
            if not _template_value_matches_type(value, declared_type):
                raise ValidationError(
                    "$.validation_rules"
                    f"[{index}]"
                    ".allowed_values"
                    f"[{value_index}]"
                    f": value {value!r} cannot match field {target!r} value_type {declared_type!r}"
                )
    if rule_type == "cross_field":
        related_target = rule.get("related_target")
        if related_target is None:
            raise ValidationError(f"$.validation_rules[{index}].related_target: required for cross_field rule")
        if related_target not in field_ids:
            raise ValidationError(
                "$.validation_rules"
                f"[{index}]"
                ".related_target: "
                f"references undeclared field {related_target!r}"
            )
        if "operator" not in rule:
            raise ValidationError(f"$.validation_rules[{index}].operator: required for cross_field rule")
        operator = rule.get("operator")
        related_type = fields_by_id[related_target].get("value_type")
        if operator in {"before", "before_or_equal", "after", "after_or_equal"}:
            if declared_type != "date" or related_type != "date":
                raise ValidationError(
                    "$.validation_rules"
                    f"[{index}]"
                    f": operator {operator!r} requires date fields, got {declared_type!r} and {related_type!r}"
                )
        elif operator in {"less_than", "less_than_or_equal", "greater_than", "greater_than_or_equal"}:
            if declared_type != "number" or related_type != "number":
                raise ValidationError(
                    "$.validation_rules"
                    f"[{index}]"
                    f": operator {operator!r} requires number fields, got {declared_type!r} and {related_type!r}"
                )
        elif declared_type != related_type:
            raise ValidationError(
                "$.validation_rules"
                f"[{index}]"
                f": operator {operator!r} requires matching field types, got {declared_type!r} and {related_type!r}"
            )


def _template_value_matches_type(value: Any, value_type: str) -> bool:
    if value_type in {"string", "date"}:
        return isinstance(value, str)
    if value_type == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "enum":
        return isinstance(value, (str, int, float, bool))
    return False


def validate_document_ir_v0_consistency(document: dict[str, Any]) -> None:
    pages_by_number: dict[int | float, dict[str, Any]] = {}
    for index, page in enumerate(document["pages"]):
        page_number = page["page_number"]
        if page_number in pages_by_number:
            raise ValidationError(
                "$.pages"
                f"[{index}]"
                ".page_number: "
                f"duplicates page number {page_number!r}"
            )
        pages_by_number[page_number] = page

    declared_pages_text = ", ".join(str(page) for page in sorted(pages_by_number))

    for index, block in enumerate(document["blocks"]):
        value_metadata = block["value_metadata"]
        source_page = value_metadata["source_page"]
        if source_page not in pages_by_number:
            raise ValidationError(
                "$.blocks"
                f"[{index}]"
                ".value_metadata.source_page: "
                f"references undeclared page {source_page!r}"
                f" (declared pages: {declared_pages_text})"
            )
        page = pages_by_number[source_page]
        bbox = value_metadata["bbox"]
        if bbox["x"] + bbox["width"] > page["width"]:
            raise ValidationError(
                "$.blocks"
                f"[{index}]"
                ".value_metadata.bbox: "
                f"extends past page {source_page!r} width {page['width']!r}"
            )
        if bbox["y"] + bbox["height"] > page["height"]:
            raise ValidationError(
                "$.blocks"
                f"[{index}]"
                ".value_metadata.bbox: "
                f"extends past page {source_page!r} height {page['height']!r}"
            )


def validate_document_ir_v1_consistency(document: dict[str, Any]) -> None:
    pages_by_number: dict[int | float, dict[str, Any]] = {}
    for index, page in enumerate(document["pages"]):
        page_number = page["page_number"]
        if page_number in pages_by_number:
            raise ValidationError(
                "$.pages"
                f"[{index}]"
                ".page_number: "
                f"duplicates page number {page_number!r}"
            )
        pages_by_number[page_number] = page

    declared_pages_text = ", ".join(str(page) for page in sorted(pages_by_number))

    for index, block in enumerate(document["blocks"]):
        source_page = block["source_page"]
        if source_page not in pages_by_number:
            raise ValidationError(
                "$.blocks"
                f"[{index}]"
                ".source_page: "
                f"references undeclared page {source_page!r}"
                f" (declared pages: {declared_pages_text})"
            )
        page = pages_by_number[source_page]
        bbox = block["bbox"]
        if bbox["unit"] != page["unit"]:
            raise ValidationError(
                "$.blocks"
                f"[{index}]"
                ".bbox.unit: "
                f"must match page {source_page!r} unit {page['unit']!r}"
            )
        if bbox["x"] + bbox["width"] > page["width"]:
            raise ValidationError(
                "$.blocks"
                f"[{index}]"
                ".bbox: "
                f"extends past page {source_page!r} width {page['width']!r}"
            )
        if bbox["y"] + bbox["height"] > page["height"]:
            raise ValidationError(
                "$.blocks"
                f"[{index}]"
                ".bbox: "
                f"extends past page {source_page!r} height {page['height']!r}"
            )


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as file:
        return json.load(file, parse_constant=reject_json_constant)


def reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def validate_consistency(schema: dict[str, Any], document: dict[str, Any], schema_path: Path) -> None:
    schema_id = str(schema.get("$id", ""))
    if schema_path.name == "template-definition.schema.json" or schema_id.endswith(
        "/template-definition.schema.json"
    ):
        validate_template_definition_consistency(document)
        return

    validate_document_ir_consistency(document)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--document", type=Path, required=True)
    args = parser.parse_args()

    try:
        schema = load_json(args.schema)
        document = load_json(args.document)
        validate(schema, document)
        validate_consistency(schema, document, args.schema)
    except (OSError, ValueError, json.JSONDecodeError, ValidationError) as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1

    print("Validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
