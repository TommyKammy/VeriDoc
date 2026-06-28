from __future__ import annotations

import json
import unittest
from pathlib import Path

from core.ir.document_ir_v1 import (
    BoundingBox,
    DocumentBlock,
    DocumentIRV1,
    DocumentInfo,
    DocumentPage,
    ExtractorRef,
    ReviewState,
)
from core.ir.template_fingerprint import (
    TemplateMatchClassification,
    apply_template_field_mapping,
    match_template_fingerprint,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "datasets" / "fixtures" / "manifest.json"
GOLD_LABELS_PATH = REPO_ROOT / "datasets" / "gold" / "high_risk_labels_v0.json"
TEMPLATE_REGRESSION_PATH = REPO_ROOT / "datasets" / "gold" / "template_regression_v0.json"


class DatasetFixturesTest(unittest.TestCase):
    def test_manifest_defines_public_fixture_policy_and_source_slots(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

        policy = manifest["policy"]
        self.assertTrue(policy["public_only"])
        self.assertFalse(policy["confidential_source_documents_allowed"])
        self.assertEqual("datasets/fixtures", policy["allowed_fixture_root"])
        self.assertEqual("datasets/gold", policy["gold_root"])

        source_types = {fixture["source_type"] for fixture in manifest["fixtures"]}
        self.assertEqual(set(manifest["required_source_types"]), source_types)

        for fixture in manifest["fixtures"]:
            self.assertEqual("public", fixture["confidentiality"])
            self.assertTrue(fixture["public_review_safe"])
            self.assertIn(
                fixture["anonymization"],
                {"synthetic", "anonymized", "pending_synthetic_fixture"},
            )
            if fixture["path"] is not None:
                relpath = Path(fixture["path"])
                self.assertFalse(relpath.is_absolute(), msg=f"fixture path must be repo-relative: {relpath}")
                self.assertTrue((REPO_ROOT / relpath).is_file(), msg=f"missing fixture file: {relpath}")

    def test_high_risk_gold_labels_are_anchored_to_public_fixtures(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        gold = json.loads(GOLD_LABELS_PATH.read_text(encoding="utf-8"))

        self.assertTrue(gold["scope"]["public_only"])
        self.assertFalse(gold["scope"]["confidential_source_documents_allowed"])
        self.assertFalse(gold["scope"]["production_or_gmp_claim"])

        fixtures_by_id = {fixture["id"]: fixture for fixture in manifest["fixtures"]}
        taxonomy_by_id = {label["id"]: label for label in gold["label_taxonomy"]}

        self.assertGreaterEqual(len(gold["items"]), 1)
        for item in gold["items"]:
            fixture = fixtures_by_id[item["fixture_id"]]
            label = taxonomy_by_id[item["label_id"]]

            self.assertEqual("public", fixture["confidentiality"])
            self.assertTrue(fixture["public_review_safe"])
            self.assertEqual("high", item["risk_level"])
            self.assertEqual("high", label["risk_level"])
            self.assertTrue(item["requires_review"])
            self.assertRegex(item["block_id"], r"^block-[0-9]{3}$")
            self.assertIn("source_page", item["evidence"])
            self.assertIn("bbox", item["evidence"])

    def test_template_regression_goldens_cover_representative_public_templates(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        gold = json.loads(TEMPLATE_REGRESSION_PATH.read_text(encoding="utf-8"))

        self.assertEqual("veridoc-template-regression/v0", gold["schema_version"])
        self.assertEqual("datasets/fixtures/manifest.json", gold["dataset_manifest"])
        self.assertTrue(gold["scope"]["synthetic_or_anonymized_only"])
        self.assertFalse(gold["scope"]["real_confidential_records_included"])

        fixtures_by_id = {fixture["id"]: fixture for fixture in manifest["fixtures"]}
        template_cases = gold["template_cases"]
        self.assertGreaterEqual(len(template_cases), 2)

        for case in template_cases:
            with self.subTest(case=case["id"]):
                fixture = fixtures_by_id[case["fixture_id"]]
                self.assertIn(fixture["anonymization"], {"synthetic", "anonymized"})
                self.assertEqual("public", fixture["confidentiality"])
                self.assertTrue(fixture["public_review_safe"])
                self.assertEqual("template_regression", fixture["source_type"])

                fixture_path = Path(fixture["path"])
                self.assertFalse(fixture_path.is_absolute())
                fixture_data = json.loads((REPO_ROOT / fixture_path).read_text(encoding="utf-8"))
                template = fixture_data["template_definition"]
                document = self.document_ir_from_fixture(fixture_data["document_ir"])

                fingerprint = match_template_fingerprint(document, template)
                expected = case["expected"]
                self.assertEqual(
                    TemplateMatchClassification(expected["classification"]),
                    fingerprint.classification,
                )
                self.assertGreaterEqual(fingerprint.score, expected["minimum_score"])

                mapping = apply_template_field_mapping(document, template)
                mapped_values = {field.field_id: field.value for field in mapping.fields}
                self.assertEqual(expected["field_values"], mapped_values)
                self.assertEqual(expected["requires_review"], mapping.requires_review)
                for warning in expected["warnings"]:
                    self.assertIn(warning, mapping.warnings)

    def document_ir_from_fixture(self, data: dict[str, object]) -> DocumentIRV1:
        document = data["document"]
        pages = [
            DocumentPage(
                page_number=page["page_number"],
                width=page["width"],
                height=page["height"],
                unit=page.get("unit", "pt"),
            )
            for page in data["pages"]
        ]
        blocks = []
        for block in data["blocks"]:
            bbox = block["bbox"]
            extractor = block.get("extractor", {"name": "fixture", "version": "unknown"})
            if isinstance(extractor, dict):
                extractor_ref = ExtractorRef(
                    name=str(extractor.get("name", "fixture")),
                    version=str(extractor.get("version", "unknown")),
                )
            else:
                extractor_ref = ExtractorRef(name=str(extractor))
            review = block.get("review", {})
            blocks.append(
                DocumentBlock(
                    id=block["id"],
                    type=block["type"],
                    text=block["text"],
                    source_page=block["source_page"],
                    bbox=BoundingBox(
                        x=bbox["x"],
                        y=bbox["y"],
                        width=bbox["width"],
                        height=bbox["height"],
                        unit=bbox.get("unit", "pt"),
                        origin=bbox.get("origin", "top-left"),
                    ),
                    extractor=extractor_ref,
                    confidence=block.get("confidence", 0.99),
                    review=ReviewState(
                        requires_review=review.get("requires_review", False),
                        warnings=review.get("warnings", []),
                    ),
                )
            )
        return DocumentIRV1(
            schema_version=data["schema_version"],
            document=DocumentInfo(
                id=document["id"],
                title=document["title"],
                source_type=document["source_type"],
            ),
            pages=pages,
            blocks=blocks,
            warnings=data.get("warnings", []),
        )


if __name__ == "__main__":
    unittest.main()
