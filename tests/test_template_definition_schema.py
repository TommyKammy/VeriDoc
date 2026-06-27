from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from scripts.ci.validate_document_ir import ValidationError, validate, validate_template_definition_consistency


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "core" / "ir" / "template-definition.schema.json"
SAMPLE_PATH = REPO_ROOT / "core" / "ir" / "examples" / "sample-template-definition.json"


class TemplateDefinitionSchemaTest(unittest.TestCase):
    def load_schema(self) -> dict[str, object]:
        self.assertTrue(SCHEMA_PATH.is_file(), f"missing schema: {SCHEMA_PATH.relative_to(REPO_ROOT)}")
        return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def load_sample(self) -> dict[str, object]:
        self.assertTrue(SAMPLE_PATH.is_file(), f"missing sample: {SAMPLE_PATH.relative_to(REPO_ROOT)}")
        return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))

    def test_sample_template_definition_validates_against_schema(self) -> None:
        self.validate_template(self.load_sample())

    def validate_template(self, template: dict[str, object]) -> None:
        validate(self.load_schema(), template)
        validate_template_definition_consistency(template)

    def test_required_template_definition_sections_are_enforced(self) -> None:
        schema = self.load_schema()
        sample = self.load_sample()

        for key in (
            "template_id",
            "version",
            "document_type",
            "anchors",
            "fields",
            "tables",
            "risk_rank",
            "validation_rules",
            "output_mapping",
        ):
            invalid = copy.deepcopy(sample)
            invalid.pop(key)
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValidationError, "missing required key"):
                    validate(schema, invalid)

    def test_required_template_definition_sections_reject_type_mismatches(self) -> None:
        schema = self.load_schema()
        sample = self.load_sample()

        invalid_values = {
            "template_id": 102,
            "version": 1,
            "document_type": ["batch_record"],
            "anchors": {},
            "fields": {},
            "tables": {},
            "risk_rank": [],
            "validation_rules": {},
            "output_mapping": [],
        }
        for key, value in invalid_values.items():
            invalid = copy.deepcopy(sample)
            invalid[key] = value
            with self.subTest(key=key):
                with self.assertRaises(ValidationError):
                    validate(schema, invalid)

    def test_scope_block_types_follow_document_ir_v1_block_types(self) -> None:
        schema = self.load_schema()
        sample = self.load_sample()

        valid = copy.deepcopy(sample)
        valid["anchors"][0]["scope"]["block_types"] = ["footnote"]
        validate(schema, valid)

        invalid = copy.deepcopy(sample)
        invalid["anchors"][0]["scope"]["block_types"] = ["footer"]
        with self.assertRaisesRegex(ValidationError, "expected one of"):
            validate(schema, invalid)

    def test_field_source_anchor_ids_must_reference_declared_anchors(self) -> None:
        sample = self.load_sample()
        sample["fields"][0]["source"]["anchor_id"] = "missing-anchor"

        with self.assertRaisesRegex(ValidationError, "references undeclared anchor"):
            self.validate_template(sample)

    def test_risk_rank_levels_must_cover_used_levels(self) -> None:
        cases = (
            ("default_level", "medium", None),
            ("review_required_levels", "critical", None),
            ("field.risk_level", "low", ("fields", 0)),
            ("table.risk_level", "low", ("tables", 0)),
        )

        for name, removed_level, mutation_target in cases:
            sample = self.load_sample()
            if mutation_target is not None:
                section, index = mutation_target
                sample[section][index]["risk_level"] = removed_level
            sample["risk_rank"]["levels"] = [
                level
                for level in sample["risk_rank"]["levels"]
                if level["level"] != removed_level
            ]

            with self.subTest(name=name):
                with self.assertRaisesRegex(ValidationError, "risk_rank.levels"):
                    self.validate_template(sample)

    def test_field_validation_rule_ids_must_target_same_field(self) -> None:
        sample = self.load_sample()
        sample["fields"][1]["validation_rule_ids"] = ["batch-number-required"]

        with self.assertRaisesRegex(ValidationError, "not field 'manufacturing_date'"):
            self.validate_template(sample)

    def test_tables_must_reference_table_anchors(self) -> None:
        sample = self.load_sample()
        sample["tables"][0]["anchor_id"] = "batch-header"

        with self.assertRaisesRegex(ValidationError, "non-table anchor"):
            self.validate_template(sample)

        sample = self.load_sample()
        sample["anchors"][1]["scope"]["block_types"] = ["paragraph"]

        with self.assertRaisesRegex(ValidationError, "non-table anchor"):
            self.validate_template(sample)

    def test_output_mapping_keys_must_match_declared_output_keys(self) -> None:
        cases = (
            ("field_map", "output_mapping.field_map", "changed.batch_number"),
            ("table_map", "output_mapping.table_map", "changed.yield_summary"),
        )

        for section, expected_error, output_key in cases:
            sample = self.load_sample()
            sample["output_mapping"][section][0]["output_key"] = output_key

            with self.subTest(section=section):
                with self.assertRaisesRegex(ValidationError, expected_error):
                    self.validate_template(sample)

    def test_validation_rules_require_operands_for_executable_rule_types(self) -> None:
        for rule_type, expected_error in (
            ("type", "expected_type"),
            ("range", "minimum or maximum"),
            ("allowed_values", "allowed_values"),
            ("cross_field", "related_target"),
        ):
            sample = self.load_sample()
            sample["validation_rules"].append(
                {
                    "rule_id": f"{rule_type}-rule",
                    "target": "batch_number",
                    "rule_type": rule_type,
                    "severity": "error",
                    "message": f"{rule_type} rule requires operands.",
                }
            )

            with self.subTest(rule_type=rule_type):
                validate(self.load_schema(), sample)
                with self.assertRaisesRegex(ValidationError, expected_error):
                    validate_template_definition_consistency(sample)

    def test_validation_rule_operands_validate_when_present(self) -> None:
        sample = self.load_sample()
        sample["validation_rules"].extend(
            [
                {
                    "rule_id": "batch-number-type",
                    "target": "batch_number",
                    "rule_type": "type",
                    "severity": "error",
                    "message": "Batch number must be parsed as a string.",
                    "expected_type": "string",
                },
                {
                    "rule_id": "yield-range",
                    "target": "batch_number",
                    "rule_type": "range",
                    "severity": "warning",
                    "message": "Yield must stay within configured bounds.",
                    "minimum": 0,
                    "maximum": 100,
                },
                {
                    "rule_id": "batch-status-values",
                    "target": "batch_number",
                    "rule_type": "allowed_values",
                    "severity": "warning",
                    "message": "Batch status must be one of the configured values.",
                    "allowed_values": ["released", "quarantined"],
                },
                {
                    "rule_id": "date-order",
                    "target": "manufacturing_date",
                    "rule_type": "cross_field",
                    "severity": "error",
                    "message": "Manufacturing date must not follow release date.",
                    "related_target": "batch_number",
                    "operator": "before_or_equal",
                },
            ]
        )

        self.validate_template(sample)


if __name__ == "__main__":
    unittest.main()
