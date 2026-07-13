from __future__ import annotations

import json
import unittest
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

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
POC_EVALUATION_MANIFEST_PATH = REPO_ROOT / "datasets" / "poc_evaluation_manifest_v1.json"
MVP_EVALUATION_MANIFEST_PATH = REPO_ROOT / "datasets" / "mvp_evaluation_manifest_v1.json"
GOLD_LABELS_PATH = REPO_ROOT / "datasets" / "gold" / "high_risk_labels_v0.json"
TEMPLATE_REGRESSION_PATH = REPO_ROOT / "datasets" / "gold" / "template_regression_v0.json"
REQUIRED_DOCX_PACKAGE_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
    "word/_rels/document.xml.rels",
}
REQUIRED_XLSX_PACKAGE_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "xl/workbook.xml",
    "xl/_rels/workbook.xml.rels",
}
CONTENT_TYPE_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
PACKAGE_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OFFICE_DOCUMENT_RELATIONSHIP = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)


class DatasetFixturesTest(unittest.TestCase):
    def test_mvp_evaluation_manifest_fixes_representative_cases(self) -> None:
        fixture_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        fixtures_by_id = {
            fixture["id"]: fixture for fixture in fixture_manifest["fixtures"]
        }
        mvp_manifest = json.loads(
            MVP_EVALUATION_MANIFEST_PATH.read_text(encoding="utf-8")
        )

        self.assertEqual(
            "veridoc-mvp-evaluation-dataset/v1", mvp_manifest["schema_version"]
        )
        self.assertEqual("fixed_for_mvp", mvp_manifest["selection_status"])
        self.assertEqual(
            "datasets/fixtures/manifest.json", mvp_manifest["fixture_manifest"]
        )
        self.assertEqual(
            "public_synthetic_or_anonymized_only", mvp_manifest["source_policy"]
        )
        self.assertFalse(mvp_manifest["confidential_source_documents_allowed"])

        expected_categories = {
            "word",
            "excel",
            "text_pdf",
            "scanned_pdf",
            "record_pdf",
        }
        cases = mvp_manifest["cases"]
        self.assertEqual(expected_categories, set(mvp_manifest["required_categories"]))
        self.assertEqual(expected_categories, {case["category"] for case in cases})
        self.assertEqual(len(expected_categories), len(cases))
        self.assertEqual(len(cases), len({case["id"] for case in cases}))

        expected_source_types = {
            "word": "word",
            "excel": "excel",
            "text_pdf": "text_pdf",
            "scanned_pdf": "scanned_pdf",
            "record_pdf": "record_excerpt",
        }

        for case in cases:
            with self.subTest(case=case["id"]):
                fixture = fixtures_by_id[case["fixture_id"]]
                fixture_path = Path(case["fixture_path"])

                self.assertFalse(fixture_path.is_absolute())
                self.assertEqual(fixture["path"], case["fixture_path"])
                self.assertTrue((REPO_ROOT / fixture_path).is_file())
                self.assertEqual(expected_source_types[case["category"]], fixture["source_type"])
                self.assertIn(fixture["anonymization"], {"synthetic", "anonymized"})
                self.assertEqual("public", fixture["confidentiality"])
                self.assertTrue(fixture["public_review_safe"])
                self.assertIn(
                    case["conversion_mode"],
                    {"word_to_excel", "excel_to_word", "pdf_to_word", "pdf_to_excel"},
                )
                self.assertIsInstance(case["expected_artifacts"], list)
                self.assertGreaterEqual(len(case["expected_artifacts"]), 1)
                for artifact in case["expected_artifacts"]:
                    self.assertIn(artifact["type"], {"docx", "xlsx"})
                    self.assertIsInstance(artifact["expectations"], list)
                    self.assertGreaterEqual(len(artifact["expectations"]), 1)
                self.assertIsInstance(case["expected_warnings"], list)
                self.assertIsInstance(case["review_focus"], list)
                self.assertGreaterEqual(len(case["review_focus"]), 1)
                if case["category"] == "scanned_pdf":
                    self.assertTrue(fixture["scanned_pdf_representative"])
                    scanned_pdf = (REPO_ROOT / fixture_path).read_bytes()
                    self.assertIn(b"/Subtype /Image", scanned_pdf)
                    self.assertNotIn(b"/Font", scanned_pdf)

    def test_poc_evaluation_manifest_defines_representative_safe_dataset(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        fixture_ids = {fixture["id"] for fixture in manifest["fixtures"]}
        fixtures_by_id = {fixture["id"]: fixture for fixture in manifest["fixtures"]}
        poc_manifest = json.loads(POC_EVALUATION_MANIFEST_PATH.read_text(encoding="utf-8"))

        self.assertEqual("veridoc-poc-evaluation-dataset/v1", poc_manifest["schema_version"])
        self.assertEqual("datasets/fixtures/manifest.json", poc_manifest["fixture_manifest"])
        self.assertEqual("public_synthetic_or_anonymized_only", poc_manifest["source_policy"])
        self.assertFalse(poc_manifest["confidential_source_documents_allowed"])
        self.assertEqual(
            {"word", "excel", "text_pdf", "scanned_pdf", "record_pdf"},
            set(poc_manifest["required_categories"]),
        )

        expected_ranges = {
            "word": (5, 10),
            "excel": (5, 10),
            "text_pdf": (10, 10),
            "scanned_pdf": (3, 5),
            "record_pdf": (3, 5),
        }
        category_counts = {category: 0 for category in expected_ranges}
        usable_fixture_paths_by_category: dict[str, set[str]] = {
            category: set() for category in expected_ranges
        }
        real_fixture_links = set()

        for sample in poc_manifest["samples"]:
            with self.subTest(sample=sample["id"]):
                category = sample["category"]
                category_counts[category] += 1
                self.assertIn(sample["dataset_status"], {"usable_fixture", "manifest_placeholder"})
                self.assertIn(sample["source_classification"], {"public", "synthetic", "anonymized"})
                self.assertIn(sample["conversion_mode"], {"word_to_excel", "excel_to_word", "pdf_to_word", "pdf_to_excel"})
                self.assertIsInstance(sample["evaluation_focus"], list)
                self.assertGreaterEqual(len(sample["evaluation_focus"]), 1)
                self.assertIsInstance(sample["expected_warning_or_review_focus"], list)
                self.assertGreaterEqual(len(sample["expected_warning_or_review_focus"]), 1)

                fixture_id = sample.get("fixture_id")
                if sample["dataset_status"] == "usable_fixture":
                    self.assertIn(fixture_id, fixture_ids)
                    fixture = fixtures_by_id[fixture_id]
                    real_fixture_links.add(fixture_id)
                    if fixture["path"] is not None:
                        usable_fixture_paths_by_category[category].add(fixture["path"])
                    if category == "text_pdf":
                        self.assertEqual("pdf", fixture["format"])
                        self.assertTrue(fixture["path"].endswith(".pdf"))
                    if category == "record_pdf":
                        self.assertEqual("pdf", fixture["format"])
                        self.assertTrue(fixture["path"].endswith(".pdf"))
                        self.assertTrue(fixture.get("record_pdf_representative"))
                else:
                    self.assertIsNone(fixture_id)
                    self.assertIn(
                        sample["availability_reason"],
                        {
                            "pending_synthetic_or_anonymized_fixture",
                            "pending_public_fixture_labels",
                        },
                    )

        self.assertGreaterEqual(len(real_fixture_links), 1)
        self.assertTrue(
            usable_fixture_paths_by_category["record_pdf"].isdisjoint(
                usable_fixture_paths_by_category["text_pdf"]
            )
        )
        for category, (minimum, maximum) in expected_ranges.items():
            self.assertGreaterEqual(category_counts[category], minimum)
            self.assertLessEqual(category_counts[category], maximum)

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

    def test_manifest_docx_fixtures_are_reusable_ooxml_packages(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

        for fixture in manifest["fixtures"]:
            if fixture["path"] is None or fixture["format"] != "docx":
                continue

            relpath = Path(fixture["path"])
            with self.subTest(fixture=fixture["id"]):
                with ZipFile(REPO_ROOT / relpath) as archive:
                    package_parts = set(archive.namelist())
                    content_types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
                    package_relationships = ElementTree.fromstring(archive.read("_rels/.rels"))

                self.assertTrue(
                    REQUIRED_DOCX_PACKAGE_PARTS.issubset(package_parts),
                    msg=f"{relpath} is missing reusable DOCX package parts",
                )
                rel_defaults = {
                    node.attrib.get("Extension"): node.attrib.get("ContentType")
                    for node in content_types.findall(f"{CONTENT_TYPE_NS}Default")
                }
                overrides = {
                    node.attrib.get("PartName"): node.attrib.get("ContentType")
                    for node in content_types.findall(f"{CONTENT_TYPE_NS}Override")
                }
                office_document_targets = {
                    node.attrib.get("Target")
                    for node in package_relationships.findall(f"{PACKAGE_REL_NS}Relationship")
                    if node.attrib.get("Type") == OFFICE_DOCUMENT_RELATIONSHIP
                }

                self.assertEqual(
                    "application/vnd.openxmlformats-package.relationships+xml",
                    rel_defaults.get("rels"),
                )
                self.assertEqual(
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
                    overrides.get("/word/document.xml"),
                )
                self.assertIn("word/document.xml", office_document_targets)

    def test_manifest_xlsx_fixtures_are_reusable_ooxml_packages(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

        for fixture in manifest["fixtures"]:
            if fixture["path"] is None or fixture["format"] != "xlsx":
                continue

            relpath = Path(fixture["path"])
            with self.subTest(fixture=fixture["id"]):
                with ZipFile(REPO_ROOT / relpath) as archive:
                    package_parts = set(archive.namelist())
                    content_types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
                    package_relationships = ElementTree.fromstring(archive.read("_rels/.rels"))

                self.assertTrue(
                    REQUIRED_XLSX_PACKAGE_PARTS.issubset(package_parts),
                    msg=f"{relpath} is missing reusable XLSX package parts",
                )
                rel_defaults = {
                    node.attrib.get("Extension"): node.attrib.get("ContentType")
                    for node in content_types.findall(f"{CONTENT_TYPE_NS}Default")
                }
                overrides = {
                    node.attrib.get("PartName"): node.attrib.get("ContentType")
                    for node in content_types.findall(f"{CONTENT_TYPE_NS}Override")
                }
                office_document_targets = {
                    node.attrib.get("Target")
                    for node in package_relationships.findall(f"{PACKAGE_REL_NS}Relationship")
                    if node.attrib.get("Type") == OFFICE_DOCUMENT_RELATIONSHIP
                }

                self.assertEqual(
                    "application/vnd.openxmlformats-package.relationships+xml",
                    rel_defaults.get("rels"),
                )
                self.assertEqual(
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                    overrides.get("/xl/workbook.xml"),
                )
                self.assertIn("xl/workbook.xml", office_document_targets)

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
