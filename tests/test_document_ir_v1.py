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

    def test_validation_fails_closed_for_fractional_page_numbers(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/sample.pdf",
                "extractor": "pymupdf",
                "pages": [
                    {
                        "page_number": 1.5,
                        "width_pt": 595.0,
                        "height_pt": 842.0,
                        "fragments": [
                            {
                                "text": "fractional page",
                                "page_number": 1.5,
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
        self.assertIn("pages[0].page_number must be >= 1", result.errors)
        self.assertIn("blocks[0].source_page references undeclared page 0", result.errors)

    def test_validation_fails_closed_for_non_finite_page_dimensions(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/sample.pdf",
                "extractor": "pymupdf",
                "pages": [
                    {
                        "page_number": 1,
                        "width_pt": float("nan"),
                        "height_pt": 842.0,
                        "fragments": [
                            {
                                "text": "invalid page dimensions",
                                "page_number": 1,
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
        self.assertIn("pages[0] dimensions must be positive", result.errors)

    def test_validation_fails_closed_for_negative_bbox_dimensions(self) -> None:
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
                                "text": "negative bbox",
                                "page_number": 1,
                                "bbox": {"x": 72.0, "y": 80.0, "width": -120.0, "height": 18.0},
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
        self.assertIn("blocks[0].bbox dimensions must be non-negative", result.errors)

    def test_ocr_regions_convert_to_document_ir_v1_blocks(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/scanned.pdf",
                "engine": "tesseract",
                "pages": [
                    {
                        "page_number": 1,
                        "width_px": 1224,
                        "height_px": 1584,
                        "regions": [
                            {
                                "text": "LOT-001",
                                "page_number": 1,
                                "bbox": {
                                    "x": 10.0,
                                    "y": 12.0,
                                    "width": 60.0,
                                    "height": 14.0,
                                    "unit": "px",
                                    "origin": "top-left",
                                },
                                "confidence": 91.5,
                                "low_confidence": False,
                                "engine": "tesseract",
                            }
                        ],
                    }
                ],
            },
            document_id="scanned-pdf",
            title="Scanned PDF",
            source_type="pdf",
        )

        result = validate_document_ir_v1(document_ir)

        self.assertTrue(result.ok, result.errors)
        self.assertFalse(result.requires_review, result.warnings)
        self.assertEqual("LOT-001", document_ir.blocks[0].text)
        self.assertEqual("px", document_ir.pages[0].unit)
        self.assertEqual("px", document_ir.blocks[0].bbox.unit)
        self.assertEqual("tesseract", document_ir.blocks[0].extractor.name)
        self.assertEqual(0.915, document_ir.blocks[0].confidence)

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
