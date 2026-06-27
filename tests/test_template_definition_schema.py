from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable

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

    def test_cli_applies_template_consistency_checks(self) -> None:
        sample = self.load_sample()
        sample["fields"][0]["source"]["anchor_id"] = "missing-anchor"

        result = self.run_template_cli(sample)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("references undeclared anchor", result.stderr)

    def test_cli_rejects_review_semantic_consistency_failures(self) -> None:
        def dangling_field_anchor(sample: dict[str, object]) -> None:
            sample["fields"][0]["source"]["anchor_id"] = "missing-anchor"

        def mismatched_field_rule_target(sample: dict[str, object]) -> None:
            sample["fields"][1]["validation_rule_ids"] = ["batch-number-required"]

        def table_cell_non_table_anchor(sample: dict[str, object]) -> None:
            sample["fields"][0]["source"]["direction"] = "table_cell"

        def table_uses_non_table_anchor(sample: dict[str, object]) -> None:
            sample["tables"][0]["anchor_id"] = "batch-header"

        def conflicting_output_mapping(sample: dict[str, object]) -> None:
            sample["output_mapping"]["field_map"][0]["output_key"] = "other.path"

        def duplicate_output_key(sample: dict[str, object]) -> None:
            sample["fields"][1]["output_key"] = "batch.number"
            sample["output_mapping"]["field_map"][1]["output_key"] = "batch.number"

        def missing_output_mapping(sample: dict[str, object]) -> None:
            sample["output_mapping"]["field_map"] = sample["output_mapping"]["field_map"][:1]

        def incomplete_risk_rank(sample: dict[str, object]) -> None:
            sample["risk_rank"]["levels"] = [
                level for level in sample["risk_rank"]["levels"] if level["level"] != "critical"
            ]

        def range_on_non_numeric_field(sample: dict[str, object]) -> None:
            sample["validation_rules"].append(
                {
                    "rule_id": "batch-number-range",
                    "target": "batch_number",
                    "rule_type": "range",
                    "severity": "warning",
                    "message": "Batch number must stay within numeric bounds.",
                    "minimum": 0,
                    "maximum": 100,
                }
            )

        def cross_field_incompatible_types(sample: dict[str, object]) -> None:
            sample["validation_rules"].append(
                {
                    "rule_id": "date-order",
                    "target": "manufacturing_date",
                    "rule_type": "cross_field",
                    "severity": "error",
                    "message": "Manufacturing date must not follow batch number.",
                    "related_target": "batch_number",
                    "operator": "before_or_equal",
                }
            )

        cases: tuple[tuple[str, Callable[[dict[str, object]], None], str], ...] = (
            ("dangling_field_anchor", dangling_field_anchor, "references undeclared anchor"),
            ("mismatched_field_rule_target", mismatched_field_rule_target, "not field 'manufacturing_date'"),
            ("table_cell_non_table_anchor", table_cell_non_table_anchor, "table_cell source references non-table anchor"),
            ("table_uses_non_table_anchor", table_uses_non_table_anchor, "non-table anchor"),
            ("conflicting_output_mapping", conflicting_output_mapping, "must match field 'batch_number' output_key"),
            ("duplicate_output_key", duplicate_output_key, "duplicates output_key"),
            ("missing_output_mapping", missing_output_mapping, "missing mapping"),
            ("incomplete_risk_rank", incomplete_risk_rank, "risk_rank.levels"),
            ("range_on_non_numeric_field", range_on_non_numeric_field, "requires number field"),
            ("cross_field_incompatible_types", cross_field_incompatible_types, "requires date fields"),
        )

        for name, mutate, expected_error in cases:
            sample = self.load_sample()
            mutate(sample)
            with self.subTest(name=name):
                result = self.run_template_cli(sample)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)

    def run_template_cli(self, template: dict[str, object]) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmpdir:
            document_path = Path(tmpdir) / "template.json"
            document_path.write_text(json.dumps(template), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/ci/validate_document_ir.py",
                    "--schema",
                    str(SCHEMA_PATH.relative_to(REPO_ROOT)),
                    "--document",
                    str(document_path),
                ],
                cwd=REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
        return result

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

    def test_table_cell_field_sources_must_reference_table_anchors(self) -> None:
        sample = self.load_sample()
        sample["fields"][0]["source"] = {
            "anchor_id": "yield-table",
            "direction": "table_cell",
        }
        self.validate_template(sample)

        sample = self.load_sample()
        sample["fields"][0]["source"]["direction"] = "table_cell"

        with self.assertRaisesRegex(ValidationError, "table_cell source references non-table anchor"):
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

    def test_output_mapping_must_cover_declared_outputs(self) -> None:
        cases = (
            ("field_map", "fields", "manufacturing_date", "output_mapping.field_map"),
            ("table_map", "tables", "yield_summary", "output_mapping.table_map"),
        )

        for mapping_section, declared_section, missing_id, expected_error in cases:
            sample = self.load_sample()
            sample["output_mapping"][mapping_section] = []
            if mapping_section == "field_map":
                sample["output_mapping"][mapping_section].append(
                    {
                        "field_id": sample[declared_section][0]["field_id"],
                        "output_key": sample[declared_section][0]["output_key"],
                    }
                )

            with self.subTest(mapping_section=mapping_section):
                with self.assertRaisesRegex(ValidationError, expected_error):
                    self.validate_template(sample)
                with self.assertRaisesRegex(ValidationError, missing_id):
                    self.validate_template(sample)

    def test_declared_output_keys_must_be_unique_across_fields_and_tables(self) -> None:
        cases = (
            ("field", "fields", 1, "batch.number", ("field_map", 1)),
            ("table", "tables", 0, "batch.number", ("table_map", 0)),
        )

        for name, section, index, output_key, mapping_target in cases:
            sample = self.load_sample()
            sample[section][index]["output_key"] = output_key
            mapping_section, mapping_index = mapping_target
            sample["output_mapping"][mapping_section][mapping_index]["output_key"] = output_key

            with self.subTest(name=name):
                with self.assertRaisesRegex(ValidationError, "duplicates output_key"):
                    self.validate_template(sample)

    def test_validation_rules_require_operands_for_executable_rule_types(self) -> None:
        for rule_type, expected_error in (
            ("type", "expected_type"),
            ("range", "minimum or maximum"),
            ("allowed_values", "allowed_values"),
            ("cross_field", "related_target"),
        ):
            sample = self.load_sample()
            if rule_type == "range":
                sample["fields"][0]["value_type"] = "number"
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

    def test_type_validation_rules_must_match_target_field_value_type(self) -> None:
        sample = self.load_sample()
        sample["validation_rules"].append(
            {
                "rule_id": "batch-number-type",
                "target": "batch_number",
                "rule_type": "type",
                "severity": "error",
                "message": "Batch number must be parsed as a number.",
                "expected_type": "number",
            }
        )

        with self.assertRaisesRegex(ValidationError, "does not match field 'batch_number' value_type 'string'"):
            self.validate_template(sample)

    def test_range_validation_rules_require_numeric_targets(self) -> None:
        sample = self.load_sample()
        sample["validation_rules"].append(
            {
                "rule_id": "batch-number-range",
                "target": "batch_number",
                "rule_type": "range",
                "severity": "warning",
                "message": "Batch number must stay within numeric bounds.",
                "minimum": 0,
                "maximum": 100,
            }
        )

        with self.assertRaisesRegex(ValidationError, "requires number field"):
            self.validate_template(sample)

    def test_allowed_values_must_match_target_field_value_type(self) -> None:
        sample = self.load_sample()
        sample["fields"][0]["value_type"] = "number"
        sample["validation_rules"].append(
            {
                "rule_id": "batch-number-values",
                "target": "batch_number",
                "rule_type": "allowed_values",
                "severity": "warning",
                "message": "Batch number must be an allowed number.",
                "allowed_values": ["released"],
            }
        )

        with self.assertRaisesRegex(ValidationError, "cannot match field 'batch_number' value_type 'number'"):
            self.validate_template(sample)

    def test_cross_field_ordering_rules_require_compatible_value_types(self) -> None:
        sample = self.load_sample()
        sample["validation_rules"].append(
            {
                "rule_id": "date-order",
                "target": "manufacturing_date",
                "rule_type": "cross_field",
                "severity": "error",
                "message": "Manufacturing date must not follow batch number.",
                "related_target": "batch_number",
                "operator": "before_or_equal",
            }
        )

        with self.assertRaisesRegex(ValidationError, "requires date fields"):
            self.validate_template(sample)

    def test_validation_rule_operands_validate_when_present(self) -> None:
        cases = (
            (
                {
                    "rule_id": "batch-number-type",
                    "target": "batch_number",
                    "rule_type": "type",
                    "severity": "error",
                    "message": "Batch number must be parsed as a string.",
                    "expected_type": "string",
                },
                None,
            ),
            (
                {
                    "rule_id": "yield-range",
                    "target": "batch_number",
                    "rule_type": "range",
                    "severity": "warning",
                    "message": "Yield must stay within configured bounds.",
                    "minimum": 0,
                    "maximum": 100,
                },
                "number",
            ),
            (
                {
                    "rule_id": "batch-status-values",
                    "target": "batch_number",
                    "rule_type": "allowed_values",
                    "severity": "warning",
                    "message": "Batch status must be one of the configured values.",
                    "allowed_values": ["released", "quarantined"],
                },
                None,
            ),
            (
                {
                    "rule_id": "date-order",
                    "target": "manufacturing_date",
                    "rule_type": "cross_field",
                    "severity": "error",
                    "message": "Manufacturing date must not follow release date.",
                    "related_target": "batch_number",
                    "operator": "before_or_equal",
                },
                "date",
            ),
        )

        for rule, batch_number_type in cases:
            sample = self.load_sample()
            if batch_number_type is not None:
                sample["fields"][0]["value_type"] = batch_number_type
            sample["validation_rules"].append(rule)

            with self.subTest(rule_id=rule["rule_id"]):
                self.validate_template(sample)


if __name__ == "__main__":
    unittest.main()
