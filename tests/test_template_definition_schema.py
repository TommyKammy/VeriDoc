from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from scripts.ci.validate_document_ir import ValidationError, validate


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
        validate(self.load_schema(), self.load_sample())

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


if __name__ == "__main__":
    unittest.main()
