from __future__ import annotations

import json
import unittest

from core.ir.document_ir_v1 import from_parser_output, validate_document_ir_v1


SCHEMA_PATH = "core/ir/document-ir-v1.schema.json"


class DocumentIrV1Test(unittest.TestCase):
    def test_document_ir_v1_schema_exists_with_review_surface(self) -> None:
        with open(SCHEMA_PATH, encoding="utf-8") as file:
            schema = json.load(file)

        self.assertEqual("document-ir/v1", schema["properties"]["schema_version"]["const"])
        block_properties = schema["properties"]["blocks"]["items"]["properties"]
        self.assertIn("review", block_properties)
        self.assertIn("warnings", schema["properties"])

    def test_pdf_parser_output_converts_to_valid_ir_v1_with_warnings(self) -> None:
        parser_output = {
            "source_path": "fixtures/sample.pdf",
            "extractor": "pymupdf",
            "pages": [
                {
                    "page_number": 1,
                    "width_pt": 595.0,
                    "height_pt": 842.0,
                    "fragments": [
                        {
                            "text": "Batch Record",
                            "page_number": 1,
                            "bbox": {"x": 72.0, "y": 80.0, "width": 120.0, "height": 18.0},
                            "extractor": "pymupdf",
                        },
                        {
                            "text": "missing geometry",
                            "page_number": 1,
                            "extractor": "pymupdf",
                        },
                    ],
                }
            ],
        }

        document_ir = from_parser_output(
            parser_output,
            document_id="sample-pdf",
            title="Sample PDF",
            source_type="pdf",
        )
        result = validate_document_ir_v1(document_ir)

        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.requires_review)
        self.assertIn("blocks[1].bbox missing; block marked requires_review", result.warnings)
        self.assertEqual("document-ir/v1", document_ir.schema_version)
        self.assertEqual(["block-0001", "block-0002"], [block.id for block in document_ir.blocks])
        self.assertEqual("paragraph", document_ir.blocks[0].type)
        self.assertTrue(document_ir.blocks[1].review.requires_review)

    def test_validation_fails_closed_for_block_referencing_missing_page(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/sample.pdf",
                "extractor": "pymupdf",
                "pages": [
                    {
                        "page_number": 1,
                        "width_pt": 595.0,
                        "height_pt": 842.0,
                        "fragments": [
                            {
                                "text": "orphan fragment",
                                "page_number": 2,
                                "bbox": {"x": 72.0, "y": 80.0, "width": 120.0, "height": 18.0},
                                "extractor": "pymupdf",
                            }
                        ],
                    }
                ],
            },
            document_id="sample-pdf",
            title="Sample PDF",
            source_type="pdf",
        )

        result = validate_document_ir_v1(document_ir)

        self.assertFalse(result.ok)
        self.assertTrue(result.requires_review)
        self.assertIn("blocks[0].source_page references undeclared page 2", result.errors)

    def test_sample_document_ir_v1_json_matches_dataclass_shape(self) -> None:
        with open("core/ir/examples/sample-document-ir-v1.json", encoding="utf-8") as file:
            sample = json.load(file)

        document_ir = from_parser_output(
            sample["parser_output"],
            document_id=sample["document"]["id"],
            title=sample["document"]["title"],
            source_type=sample["document"]["source_type"],
        )
        result = validate_document_ir_v1(document_ir)

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(sample["expected_ir"], document_ir.to_dict())


if __name__ == "__main__":
    unittest.main()
