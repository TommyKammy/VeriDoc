from __future__ import annotations

import json
import unittest

from core.ir.document_ir_v1 import (
    BoundingBox,
    DocumentBlock,
    DocumentIRV1,
    DocumentInfo,
    DocumentPage,
    ExtractorRef,
    ReviewState,
    adapt_document_ir_v0_blocks,
    from_parser_output,
    validate_document_ir_v1,
)


SCHEMA_PATH = "core/ir/document-ir-v1.schema.json"


class DocumentIrV1Test(unittest.TestCase):
    def _document_ir_from_dataclasses(
        self,
        *,
        pages: list[DocumentPage],
        blocks: list[DocumentBlock] | None = None,
    ) -> DocumentIRV1:
        return DocumentIRV1(
            schema_version="document-ir/v1",
            document=DocumentInfo(id="sample", title="Sample", source_type="pdf"),
            pages=pages,
            blocks=blocks
            if blocks is not None
            else [
                DocumentBlock(
                    id="block-0001",
                    type="paragraph",
                    text="Batch Record",
                    source_page=1,
                    bbox=BoundingBox(x=72.0, y=80.0, width=120.0, height=18.0),
                    extractor=ExtractorRef(name="unit-test"),
                    confidence=0.95,
                    review=ReviewState(requires_review=False, warnings=[]),
                )
            ],
            warnings=[],
        )

    def test_document_ir_v1_schema_exists_with_review_surface(self) -> None:
        with open(SCHEMA_PATH, encoding="utf-8") as file:
            schema = json.load(file)

        self.assertEqual("document-ir/v1", schema["properties"]["schema_version"]["const"])
        block_properties = schema["properties"]["blocks"]["items"]["properties"]
        self.assertIn("review", block_properties)
        self.assertIn("rows", block_properties)
        self.assertIn("footnote", block_properties["type"]["enum"])
        self.assertIn("warnings", schema["properties"])

    def test_footnote_block_is_valid_document_ir_v1(self) -> None:
        document_ir = self._document_ir_from_dataclasses(
            pages=[DocumentPage(page_number=1, width=595.0, height=842.0)],
            blocks=[
                DocumentBlock(
                    id="footnote-0001",
                    type="footnote",
                    text="OCR source note.",
                    source_page=1,
                    bbox=BoundingBox(x=72.0, y=744.0, width=360.0, height=18.0),
                    extractor=ExtractorRef(name="unit-test"),
                    confidence=0.95,
                    review=ReviewState(requires_review=False, warnings=[]),
                )
            ],
        )

        result = validate_document_ir_v1(document_ir)

        self.assertTrue(result.ok, result.errors)
        self.assertFalse(result.requires_review, result.warnings)

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
        self.assertIn("blocks[0].source_page must be >= 1", result.errors)

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

    def test_validation_rejects_direct_non_finite_page_dimensions(self) -> None:
        document_ir = self._document_ir_from_dataclasses(
            pages=[DocumentPage(page_number=1, width=float("nan"), height=842.0)]
        )

        result = validate_document_ir_v1(document_ir)

        self.assertFalse(result.ok)
        self.assertIn("pages[0] dimensions must be positive", result.errors)

    def test_validation_rejects_direct_non_integer_page_numbers(self) -> None:
        document_ir = self._document_ir_from_dataclasses(
            pages=[DocumentPage(page_number=1.5, width=595.0, height=842.0)],
            blocks=[
                DocumentBlock(
                    id="block-0001",
                    type="paragraph",
                    text="Batch Record",
                    source_page=1.5,
                    bbox=BoundingBox(x=72.0, y=80.0, width=120.0, height=18.0),
                    extractor=ExtractorRef(name="unit-test"),
                    confidence=0.95,
                    review=ReviewState(requires_review=False, warnings=[]),
                )
            ],
        )

        result = validate_document_ir_v1(document_ir)

        self.assertFalse(result.ok)
        self.assertIn("pages[0].page_number must be an integer", result.errors)
        self.assertIn("blocks[0].source_page must be an integer", result.errors)

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

    def test_validation_fails_closed_for_invalid_bbox_numeric_values(self) -> None:
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
                                "text": "invalid bbox",
                                "page_number": 1,
                                "bbox": {"x": "left", "y": 80.0, "width": 120.0, "height": 18.0},
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
        self.assertIn("blocks[0].bbox values must be finite numbers", result.errors)

    def test_validation_fails_closed_for_non_top_left_bbox_origin(self) -> None:
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
                                "text": "bottom-origin bbox",
                                "page_number": 1,
                                "bbox": {
                                    "x": 72.0,
                                    "y": 80.0,
                                    "width": 120.0,
                                    "height": 18.0,
                                    "origin": "bottom-left",
                                },
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
        self.assertIn("blocks[0].bbox origin must be top-left", result.errors)

    def test_validation_fails_closed_for_bbox_unit_mismatch(self) -> None:
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
                                "text": "pixel bbox on point page",
                                "page_number": 1,
                                "bbox": {
                                    "x": 72.0,
                                    "y": 80.0,
                                    "width": 120.0,
                                    "height": 18.0,
                                    "unit": "px",
                                },
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
        self.assertIn("blocks[0].bbox unit must match page 1 unit", result.errors)

    def test_invalid_present_confidence_marks_block_for_review(self) -> None:
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
                                "text": "invalid confidence",
                                "page_number": 1,
                                "bbox": {"x": 72.0, "y": 80.0, "width": 120.0, "height": 18.0},
                                "confidence": "bad",
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

        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.requires_review)
        self.assertEqual(0.0, document_ir.blocks[0].confidence)
        self.assertTrue(document_ir.blocks[0].review.requires_review)
        self.assertIn("blocks[0].confidence invalid; block marked requires_review", result.warnings)

    def test_validation_rejects_direct_non_finite_confidence(self) -> None:
        document_ir = self._document_ir_from_dataclasses(
            pages=[DocumentPage(page_number=1, width=595.0, height=842.0)],
            blocks=[
                DocumentBlock(
                    id="block-0001",
                    type="paragraph",
                    text="Batch Record",
                    source_page=1,
                    bbox=BoundingBox(x=72.0, y=80.0, width=120.0, height=18.0),
                    extractor=ExtractorRef(name="unit-test"),
                    confidence=float("nan"),
                    review=ReviewState(requires_review=False, warnings=[]),
                )
            ],
        )

        result = validate_document_ir_v1(document_ir)

        self.assertFalse(result.ok)
        self.assertIn("blocks[0].confidence must be between 0 and 1", result.errors)

    def test_pdf_table_report_converts_selected_candidate_tables(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/sample.pdf",
                "selected_candidate": "pdfplumber:table",
                "candidates": [
                    {
                        "extractor": "camelot",
                        "flavor": "lattice",
                        "status": "failed",
                        "tables": [],
                    },
                    {
                        "extractor": "pdfplumber",
                        "flavor": "table",
                        "status": "ok",
                        "tables": [
                            {
                                "page_number": 2,
                                "rows": [["Field", "Value"], ["Lot", "L-001"]],
                                "cell_bboxes": [
                                    [
                                        {
                                            "x": 72.0,
                                            "y": 96.0,
                                            "width": 60.0,
                                            "height": 18.0,
                                            "unit": "pt",
                                            "origin": "top-left",
                                        },
                                        {
                                            "x": 132.0,
                                            "y": 96.0,
                                            "width": 90.0,
                                            "height": 18.0,
                                            "unit": "pt",
                                            "origin": "top-left",
                                        },
                                    ]
                                ],
                            }
                        ],
                    },
                ],
            },
            document_id="sample-pdf",
            title="Sample PDF",
            source_type="pdf",
        )

        result = validate_document_ir_v1(document_ir)

        self.assertTrue(result.ok, result.errors)
        self.assertFalse(result.requires_review, result.warnings)
        self.assertEqual([2], [page.page_number for page in document_ir.pages])
        self.assertEqual("table", document_ir.blocks[0].type)
        self.assertEqual("pdfplumber:table", document_ir.blocks[0].extractor.name)
        self.assertIn("Lot\tL-001", document_ir.blocks[0].text)
        self.assertEqual(72.0, document_ir.blocks[0].bbox.x)
        self.assertEqual(150.0, document_ir.blocks[0].bbox.width)

    def test_pdf_table_report_does_not_merge_unselected_candidates(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/sample.pdf",
                "selected_candidate": "camelot:lattice",
                "candidates": [
                    {
                        "extractor": "camelot",
                        "flavor": "lattice",
                        "status": "ok",
                        "tables": [
                            {
                                "page_number": 1,
                                "rows": [["Selected", "Only"]],
                                "cell_bboxes": [
                                    [
                                        {
                                            "x": 72.0,
                                            "y": 96.0,
                                            "width": 60.0,
                                            "height": 18.0,
                                            "unit": "pt",
                                            "origin": "top-left",
                                        }
                                    ]
                                ],
                            }
                        ],
                    },
                    {
                        "extractor": "pdfplumber",
                        "flavor": "table",
                        "status": "ok",
                        "tables": [
                            {
                                "page_number": 1,
                                "rows": [["Unselected", "Candidate"]],
                                "cell_bboxes": [
                                    [
                                        {
                                            "x": 132.0,
                                            "y": 96.0,
                                            "width": 90.0,
                                            "height": 18.0,
                                            "unit": "pt",
                                            "origin": "top-left",
                                        }
                                    ]
                                ],
                            }
                        ],
                    },
                ],
            },
            document_id="sample-pdf",
            title="Sample PDF",
            source_type="pdf",
        )

        result = validate_document_ir_v1(document_ir)

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(["camelot:lattice"], [block.extractor.name for block in document_ir.blocks])
        self.assertEqual(["Selected\tOnly"], [block.text for block in document_ir.blocks])

    def test_pdf_table_report_does_not_trust_bottom_left_cell_bboxes(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/sample.pdf",
                "selected_candidate": "camelot:lattice",
                "candidates": [
                    {
                        "extractor": "camelot",
                        "flavor": "lattice",
                        "status": "ok",
                        "tables": [
                            {
                                "page_number": 1,
                                "rows": [["Field", "Value"]],
                                "cell_bboxes": [
                                    [
                                        {
                                            "x": 72.0,
                                            "y": 96.0,
                                            "width": 60.0,
                                            "height": 18.0,
                                            "unit": "pt",
                                            "origin": "bottom-left",
                                        }
                                    ]
                                ],
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

        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.requires_review)
        self.assertEqual("Field\tValue", document_ir.blocks[0].text)
        self.assertIn("blocks[0].bbox missing; block marked requires_review", result.warnings)

    def test_docx_parser_output_converts_to_document_ir_v1_blocks(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/sample.docx",
                "blocks": [
                    {"kind": "heading", "text": "Batch Summary", "style": "Heading1"},
                    {"kind": "paragraph", "text": "Reviewed document"},
                    {"kind": "table", "text": "Field\tValue\nLot\tL-001", "rows": [["Field", "Value"], ["Lot", "L-001"]]},
                ],
            },
            document_id="sample-docx",
            title="Sample DOCX",
            source_type="docx",
        )

        result = validate_document_ir_v1(document_ir)

        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.requires_review)
        self.assertEqual([1], [page.page_number for page in document_ir.pages])
        self.assertEqual(["heading", "paragraph", "table"], [block.type for block in document_ir.blocks])
        self.assertIn("blocks[0].bbox missing; block marked requires_review", result.warnings)

    def test_document_ir_v0_top_level_block_warnings_are_preserved(self) -> None:
        document_ir = from_parser_output(
            {
                "schema_version": "document-ir/v0",
                "extractor": "docx-table-parser",
                "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "pt"}],
                "blocks": [
                    {
                        "id": "block-001",
                        "type": "table",
                        "text": "Field\tValue\nLot\t0007",
                        "warnings": [
                            "DOCX table contains merged cells; xlsx artifact requires review"
                        ],
                        "value_metadata": {
                            "source_page": 1,
                            "bbox": {"x": 10, "y": 20, "width": 120, "height": 32},
                            "extractor": {"name": "docx-table-parser", "version": "test"},
                            "confidence": 0.95,
                            "requires_review": False,
                        },
                    }
                ],
            },
            document_id="sample-docx",
            title="Sample DOCX",
            source_type="docx",
        )

        result = validate_document_ir_v1(document_ir)

        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.requires_review)
        self.assertIn(
            "DOCX table contains merged cells; xlsx artifact requires review",
            result.warnings,
        )
        self.assertEqual(
            ["DOCX table contains merged cells; xlsx artifact requires review"],
            document_ir.blocks[0].review.warnings,
        )

    def test_document_ir_v0_top_level_rows_merge_into_existing_fragments(self) -> None:
        parser_output = {
            "schema_version": "document-ir/v0",
            "extractor": {"name": "docx-root-parser", "version": "2"},
            "pages": [
                {
                    "page_number": 1,
                    "width": 320,
                    "height": 240,
                    "unit": "pt",
                    "fragments": [
                        {
                            "kind": "table",
                            "text": "Field\tValue\nLot\t0007",
                            "bbox": {"x": 10, "y": 20, "width": 120, "height": 32},
                            "confidence": 0.95,
                        }
                    ],
                }
            ],
            "blocks": [
                {
                    "type": "table",
                    "text": "Field\tValue\nLot\t0007",
                    "rows": [["Field", "Value"], ["Lot", "0007"]],
                    "warnings": [
                        "DOCX table contains merged cells; xlsx artifact requires review"
                    ],
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32},
                        "extractor": {"name": "docx-root-parser", "version": "legacy"},
                        "confidence": 0.95,
                    },
                }
            ],
        }

        adapted = adapt_document_ir_v0_blocks(parser_output)

        fragment = adapted["pages"][0]["fragments"][0]
        self.assertEqual([["Field", "Value"], ["Lot", "0007"]], fragment["rows"])
        self.assertEqual(
            {"name": "docx-root-parser", "version": "legacy"},
            fragment["extractor"],
        )
        self.assertEqual(
            ["DOCX table contains merged cells; xlsx artifact requires review"],
            fragment["warnings"],
        )

    def test_document_ir_v0_top_level_rows_match_existing_fragments_by_bbox(self) -> None:
        parser_output = {
            "schema_version": "document-ir/v0",
            "extractor": "docx-table-parser",
            "pages": [
                {
                    "page_number": 1,
                    "width": 320,
                    "height": 240,
                    "unit": "pt",
                    "fragments": [
                        {
                            "kind": "table",
                            "text": "Field\tValue\nLot\t0007",
                            "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                            "confidence": 0.95,
                        },
                        {
                            "kind": "table",
                            "text": "Field\tValue\nLot\t0007",
                            "bbox": {"x": 10, "y": 72, "width": 120, "height": 32, "unit": "pt"},
                            "confidence": 0.95,
                        },
                    ],
                }
            ],
            "blocks": [
                {
                    "type": "table",
                    "text": "Field\tValue\nLot\t0007",
                    "rows": [["Field", "Value"], ["Lot", "0099"]],
                    "warnings": ["second table warning"],
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 10, "y": 72, "width": 120, "height": 32, "unit": "pt"},
                        "extractor": {"name": "docx-table-parser", "version": "legacy"},
                        "confidence": 0.95,
                    },
                },
                {
                    "type": "table",
                    "text": "Field\tValue\nLot\t0007",
                    "rows": [["Field", "Value"], ["Lot", "0007"]],
                    "warnings": ["first table warning"],
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "extractor": {"name": "docx-table-parser", "version": "legacy"},
                        "confidence": 0.95,
                    },
                },
            ],
        }

        adapted = adapt_document_ir_v0_blocks(parser_output)

        first_fragment, second_fragment = adapted["pages"][0]["fragments"]
        self.assertEqual([["Field", "Value"], ["Lot", "0007"]], first_fragment["rows"])
        self.assertEqual(["first table warning"], first_fragment["warnings"])
        self.assertEqual([["Field", "Value"], ["Lot", "0099"]], second_fragment["rows"])
        self.assertEqual(["second table warning"], second_fragment["warnings"])

    def test_document_ir_v0_low_confidence_merges_into_existing_fragment(self) -> None:
        parser_output = {
            "schema_version": "document-ir/v0",
            "extractor": "scanned_pdf_ocr",
            "pages": [
                {
                    "page_number": 1,
                    "width": 320,
                    "height": 240,
                    "unit": "px",
                    "fragments": [
                        {
                            "kind": "field",
                            "text": "LOT-O0I",
                            "bbox": {"x": 10, "y": 20, "width": 70, "height": 18, "unit": "px"},
                            "confidence": 0.95,
                        }
                    ],
                }
            ],
            "blocks": [
                {
                    "type": "field",
                    "text": "LOT-O0I",
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 10, "y": 20, "width": 70, "height": 18, "unit": "px"},
                        "extractor": {"name": "scanned_pdf_ocr", "version": "0.test"},
                        "confidence": 0.41,
                        "low_confidence": True,
                    },
                }
            ],
        }

        adapted = adapt_document_ir_v0_blocks(parser_output)
        document_ir = from_parser_output(
            parser_output,
            document_id="scanned-batch-record",
            title="Scanned batch record",
            source_type="pdf",
        )

        fragment = adapted["pages"][0]["fragments"][0]
        self.assertIs(fragment["low_confidence"], True)
        self.assertEqual(0.41, fragment["confidence"])
        self.assertTrue(document_ir.blocks[0].review.requires_review)
        self.assertEqual(0.41, document_ir.blocks[0].confidence)
        self.assertEqual(
            ["blocks[0].low confidence; block marked requires_review"],
            document_ir.blocks[0].review.warnings,
        )

    def test_document_ir_v0_low_confidence_matches_untyped_existing_fragment(self) -> None:
        parser_output = {
            "schema_version": "document-ir/v0",
            "extractor": "scanned_pdf_ocr",
            "pages": [
                {
                    "page_number": 1,
                    "width": 320,
                    "height": 240,
                    "unit": "px",
                    "fragments": [
                        {
                            "text": "Scanned note",
                            "bbox": {"x": 10, "y": 20, "width": 70, "height": 18, "unit": "px"},
                        }
                    ],
                }
            ],
            "blocks": [
                {
                    "type": "paragraph",
                    "text": "Scanned note",
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 10, "y": 20, "width": 70, "height": 18, "unit": "px"},
                        "extractor": {"name": "scanned_pdf_ocr", "version": "0.test"},
                        "confidence": 0.39,
                        "low_confidence": True,
                    },
                }
            ],
        }

        adapted = adapt_document_ir_v0_blocks(parser_output)
        document_ir = from_parser_output(
            parser_output,
            document_id="scanned-note",
            title="Scanned note",
            source_type="pdf",
        )

        fragment = adapted["pages"][0]["fragments"][0]
        self.assertEqual("Scanned note", fragment["text"])
        self.assertIs(fragment["low_confidence"], True)
        self.assertEqual(0.39, fragment["confidence"])
        self.assertEqual("paragraph", document_ir.blocks[0].type)
        self.assertTrue(document_ir.blocks[0].review.requires_review)
        self.assertEqual(0.39, document_ir.blocks[0].confidence)

    def test_document_ir_v0_low_confidence_merge_keeps_normalized_confidence(self) -> None:
        parser_output = {
            "schema_version": "document-ir/v0",
            "extractor": "scanned_pdf_ocr",
            "pages": [
                {
                    "page_number": 1,
                    "width": 320,
                    "height": 240,
                    "unit": "px",
                    "fragments": [
                        {
                            "kind": "field",
                            "text": "LOT-O0I",
                            "bbox": {"x": 10, "y": 20, "width": 70, "height": 18, "unit": "px"},
                            "confidence": 91.5,
                            "engine": "tesseract",
                        }
                    ],
                }
            ],
            "blocks": [
                {
                    "type": "field",
                    "text": "LOT-O0I",
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 10, "y": 20, "width": 70, "height": 18, "unit": "px"},
                        "extractor": {"name": "tesseract", "version": "0.test"},
                        "confidence": 0.41,
                        "low_confidence": True,
                    },
                }
            ],
        }

        adapted = adapt_document_ir_v0_blocks(parser_output)
        document_ir = from_parser_output(
            parser_output,
            document_id="scanned-batch-record",
            title="Scanned batch record",
            source_type="pdf",
        )

        fragment = adapted["pages"][0]["fragments"][0]
        self.assertEqual(0.41, fragment["confidence"])
        self.assertNotIn("engine", fragment)
        self.assertEqual({"name": "tesseract", "version": "0.test"}, fragment["extractor"])
        self.assertEqual(0.41, document_ir.blocks[0].confidence)
        self.assertEqual("tesseract", document_ir.blocks[0].extractor.name)

    def test_document_ir_v0_review_merge_keeps_normalized_confidence(self) -> None:
        parser_output = {
            "schema_version": "document-ir/v0",
            "extractor": "scanned_pdf_ocr",
            "pages": [
                {
                    "page_number": 1,
                    "width": 320,
                    "height": 240,
                    "unit": "px",
                    "fragments": [
                        {
                            "kind": "field",
                            "text": "LOT-O0I",
                            "bbox": {"x": 10, "y": 20, "width": 70, "height": 18, "unit": "px"},
                            "engine": "tesseract",
                        }
                    ],
                }
            ],
            "blocks": [
                {
                    "type": "field",
                    "text": "LOT-O0I",
                    "warnings": ["OCR confidence requires review"],
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 10, "y": 20, "width": 70, "height": 18, "unit": "px"},
                        "extractor": {"name": "tesseract", "version": "0.test"},
                        "confidence": 0.6,
                        "requires_review": True,
                    },
                }
            ],
        }

        adapted = adapt_document_ir_v0_blocks(parser_output)
        document_ir = from_parser_output(
            parser_output,
            document_id="scanned-batch-record",
            title="Scanned batch record",
            source_type="pdf",
        )

        fragment = adapted["pages"][0]["fragments"][0]
        self.assertEqual(0.6, fragment["confidence"])
        self.assertNotIn("engine", fragment)
        self.assertEqual({"name": "tesseract", "version": "0.test"}, fragment["extractor"])
        self.assertEqual(0.6, document_ir.blocks[0].confidence)
        self.assertEqual("tesseract", document_ir.blocks[0].extractor.name)
        self.assertTrue(document_ir.blocks[0].review.requires_review)
        self.assertEqual(
            [
                "blocks[0].parser marked block requires_review",
                "OCR confidence requires review",
            ],
            document_ir.blocks[0].review.warnings,
        )

    def test_document_ir_v0_low_confidence_merge_preserves_missing_confidence(self) -> None:
        parser_output = {
            "schema_version": "document-ir/v0",
            "extractor": "scanned_pdf_ocr",
            "pages": [
                {
                    "page_number": 1,
                    "width": 320,
                    "height": 240,
                    "unit": "px",
                    "fragments": [
                        {
                            "kind": "field",
                            "text": "LOT-O0I",
                            "bbox": {"x": 10, "y": 20, "width": 70, "height": 18, "unit": "px"},
                            "confidence": 0.95,
                        }
                    ],
                }
            ],
            "blocks": [
                {
                    "type": "field",
                    "text": "LOT-O0I",
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 10, "y": 20, "width": 70, "height": 18, "unit": "px"},
                        "extractor": {"name": "scanned_pdf_ocr", "version": "0.test"},
                        "low_confidence": True,
                    },
                }
            ],
        }

        adapted = adapt_document_ir_v0_blocks(parser_output)
        document_ir = from_parser_output(
            parser_output,
            document_id="scanned-batch-record",
            title="Scanned batch record",
            source_type="pdf",
        )

        fragment = adapted["pages"][0]["fragments"][0]
        self.assertIs(fragment["low_confidence"], True)
        self.assertIs(fragment["missing_confidence"], True)
        self.assertNotIn("confidence", fragment)
        self.assertTrue(document_ir.blocks[0].review.requires_review)
        self.assertEqual(0.0, document_ir.blocks[0].confidence)
        self.assertEqual(
            [
                "blocks[0].confidence missing; block marked requires_review",
                "blocks[0].low confidence; block marked requires_review",
            ],
            document_ir.blocks[0].review.warnings,
        )

    def test_document_ir_v0_low_confidence_matches_kind_before_legacy_type(self) -> None:
        parser_output = {
            "schema_version": "document-ir/v0",
            "extractor": "scanned_pdf_ocr",
            "pages": [
                {
                    "page_number": 1,
                    "width": 320,
                    "height": 240,
                    "unit": "px",
                    "fragments": [
                        {
                            "kind": "field",
                            "type": "paragraph",
                            "text": "Lot: 0007",
                            "bbox": {"x": 10, "y": 20, "width": 70, "height": 18, "unit": "px"},
                            "confidence": 0.95,
                        }
                    ],
                }
            ],
            "blocks": [
                {
                    "type": "field",
                    "text": "Lot: 0007",
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 10, "y": 20, "width": 70, "height": 18, "unit": "px"},
                        "extractor": {"name": "scanned_pdf_ocr", "version": "0.test"},
                        "confidence": 0.42,
                        "low_confidence": True,
                    },
                }
            ],
        }

        adapted = adapt_document_ir_v0_blocks(parser_output)

        fragments = adapted["pages"][0]["fragments"]
        self.assertEqual(1, len(fragments))
        self.assertIs(fragments[0]["low_confidence"], True)
        self.assertEqual(0.42, fragments[0]["confidence"])

    def test_document_ir_v0_top_level_rows_do_not_override_existing_fragment_rows(
        self,
    ) -> None:
        parser_output = {
            "schema_version": "document-ir/v0",
            "extractor": "docx-root-parser",
            "pages": [
                {
                    "page_number": 1,
                    "width": 320,
                    "height": 240,
                    "unit": "pt",
                    "fragments": [
                        {
                            "kind": "table",
                            "text": "Field\tValue\nLot\t0007",
                            "extractor": "page-fragment-parser",
                            "rows": [["Field", "Value"], ["Lot", "0007"]],
                            "bbox": {"x": 10, "y": 20, "width": 120, "height": 32},
                            "confidence": 0.95,
                        }
                    ],
                }
            ],
            "blocks": [
                {
                    "type": "table",
                    "text": "Field\tValue\nLot\t0007",
                    "rows": [["stale", "grid"], ["wrong", "cells"]],
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32},
                        "extractor": {"name": "docx-root-parser", "version": "legacy"},
                        "confidence": 0.95,
                    },
                }
            ],
        }

        adapted = adapt_document_ir_v0_blocks(parser_output)

        self.assertEqual(
            [["Field", "Value"], ["Lot", "0007"]],
            adapted["pages"][0]["fragments"][0]["rows"],
        )
        self.assertEqual(
            "page-fragment-parser",
            adapted["pages"][0]["fragments"][0]["extractor"],
        )

    def test_document_ir_v0_top_level_block_engine_is_preserved(self) -> None:
        document_ir = from_parser_output(
            {
                "schema_version": "document-ir/v0",
                "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "pt"}],
                "blocks": [
                    {
                        "type": "table",
                        "engine": "docx-engine-parser",
                        "text": "Field\tValue\nLot\t0007",
                        "rows": [["Field", "Value"], ["Lot", "0007"]],
                        "value_metadata": {
                            "source_page": 1,
                            "bbox": {"x": 10, "y": 20, "width": 120, "height": 32},
                            "confidence": 0.95,
                        },
                    }
                ],
            },
            document_id="sample-docx",
            title="Sample DOCX",
            source_type="docx",
        )

        self.assertEqual("docx-engine-parser", document_ir.blocks[0].extractor.name)

    def test_xlsx_parser_output_converts_to_document_ir_v1_blocks(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/sample.xlsx",
                "sheets": [
                    {
                        "name": "Results",
                        "dimension": "A1:B2",
                        "cells": [
                            {"ref": "A1", "value": "Item", "value_type": "shared_string"},
                            {"ref": "B1", "value": "Mass", "value_type": "shared_string"},
                            {"ref": "A2", "value": "Sample A", "value_type": "inline_string"},
                            {"ref": "B2", "value": "12.5", "value_type": "number"},
                        ],
                        "merged_ranges": [],
                    }
                ],
            },
            document_id="sample-xlsx",
            title="Sample XLSX",
            source_type="xlsx",
        )

        result = validate_document_ir_v1(document_ir)

        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.requires_review)
        self.assertEqual([1], [page.page_number for page in document_ir.pages])
        self.assertEqual("table", document_ir.blocks[0].type)
        self.assertIn("A1: Item", document_ir.blocks[0].text)
        self.assertIn("B2: 12.5", document_ir.blocks[0].text)
        self.assertEqual(
            [["Sheet: Results"], ["Item", "Mass"], ["Sample A", "12.5"]],
            document_ir.blocks[0].rows,
        )

    def test_xlsx_parser_output_preserves_sparse_and_unreferenced_cells(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/sparse.xlsx",
                "sheets": [
                    {
                        "name": "Sparse",
                        "cells": [
                            {"ref": "A1", "value": "Top left"},
                            {"ref": "XFD1048576", "value": "Far edge"},
                            {"ref": "", "value": "No ref"},
                            {"ref": "not-a-cell", "value": "Bad ref"},
                        ],
                    }
                ],
            },
            document_id="sparse-xlsx",
            title="Sparse XLSX",
            source_type="xlsx",
        )

        result = validate_document_ir_v1(document_ir)

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(
            [["Sheet: Sparse"], ["Top left"], ["Far edge"], ["No ref"], ["Bad ref"]],
            document_ir.blocks[0].rows,
        )
        self.assertIn("XFD1048576: Far edge", document_ir.blocks[0].text)
        self.assertIn("Unreferenced cell: No ref", document_ir.blocks[0].text)

    def test_xlsx_parser_output_preserves_bounded_column_gaps(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/gapped.xlsx",
                "sheets": [
                    {
                        "name": "Gapped",
                        "cells": [
                            {"ref": "A1", "value": "A value"},
                            {"ref": "C1", "value": "C value"},
                            {"ref": "B2", "value": "B value"},
                            {"ref": "C2", "value": "C2 value"},
                        ],
                    }
                ],
            },
            document_id="gapped-xlsx",
            title="Gapped XLSX",
            source_type="xlsx",
        )

        self.assertEqual(
            [
                ["Sheet: Gapped"],
                ["A value", "", "C value"],
                ["", "B value", "C2 value"],
            ],
            document_ir.blocks[0].rows,
        )

    def test_xlsx_parser_output_preserves_leading_row_and_column_offsets(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/offset-table.xlsx",
                "sheets": [
                    {
                        "name": "Offset Table",
                        "cells": [
                            {"ref": "B2", "value": "ID"},
                            {"ref": "C2", "value": "Task"},
                            {"ref": "B3", "value": "00123"},
                            {"ref": "C3", "value": "Template review"},
                        ],
                    }
                ],
            },
            document_id="offset-table-xlsx",
            title="Offset Table XLSX",
            source_type="xlsx",
        )

        self.assertEqual(
            [
                ["Sheet: Offset Table"],
                ["", "", ""],
                ["", "ID", "Task"],
                ["", "00123", "Template review"],
            ],
            document_ir.blocks[0].rows,
        )

    def test_xlsx_parser_output_accepts_lowercase_cell_refs(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/lowercase-refs.xlsx",
                "sheets": [
                    {
                        "name": "Lowercase Refs",
                        "cells": [
                            {"ref": "a1", "value": "Left"},
                            {"ref": "c1", "value": "Right"},
                        ],
                    }
                ],
            },
            document_id="lowercase-refs-xlsx",
            title="Lowercase Refs XLSX",
            source_type="xlsx",
        )

        self.assertEqual(
            [
                ["Sheet: Lowercase Refs"],
                ["Left", "", "Right"],
            ],
            document_ir.blocks[0].rows,
        )
        self.assertIn("a1: Left", document_ir.blocks[0].text)
        self.assertIn("c1: Right", document_ir.blocks[0].text)

    def test_xlsx_parser_output_preserves_reasonable_wide_column_gaps(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/wide-gapped.xlsx",
                "sheets": [
                    {
                        "name": "Wide Gapped",
                        "cells": [
                            {"ref": "A1", "value": "Left"},
                            {"ref": "BM1", "value": "Right"},
                        ],
                    }
                ],
            },
            document_id="wide-gapped-xlsx",
            title="Wide Gapped XLSX",
            source_type="xlsx",
        )

        row = document_ir.blocks[0].rows[1]
        self.assertEqual(65, len(row))
        self.assertEqual("Left", row[0])
        self.assertEqual([""] * 63, row[1:64])
        self.assertEqual("Right", row[64])

    def test_xlsx_parser_output_preserves_bounded_row_gaps(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/row-gapped.xlsx",
                "sheets": [
                    {
                        "name": "Row Gapped",
                        "cells": [
                            {"ref": "A1", "value": "Header"},
                            {"ref": "A3", "value": "Footer"},
                            {"ref": "C3", "value": "Total"},
                        ],
                    }
                ],
            },
            document_id="row-gapped-xlsx",
            title="Row Gapped XLSX",
            source_type="xlsx",
        )

        self.assertEqual(
            [
                ["Sheet: Row Gapped"],
                ["Header", "", ""],
                ["", "", ""],
                ["Footer", "", "Total"],
            ],
            document_ir.blocks[0].rows,
        )

    def test_xlsx_parser_output_keeps_extreme_row_gaps_sparse(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/extreme-row-gap.xlsx",
                "sheets": [
                    {
                        "name": "Sparse Rows",
                        "cells": [
                            {"ref": "A1", "value": "Top"},
                            {"ref": "A1048576", "value": "Bottom"},
                        ],
                    }
                ],
            },
            document_id="sparse-row-xlsx",
            title="Sparse Row XLSX",
            source_type="xlsx",
        )

        self.assertEqual(
            [["Sheet: Sparse Rows"], ["Top"], ["Bottom"]],
            document_ir.blocks[0].rows,
        )

    def test_xlsx_parser_output_preserves_sparse_column_gaps_after_row_cap(self) -> None:
        document_ir = from_parser_output(
            {
                "source_path": "fixtures/sparse-row-column-gap.xlsx",
                "sheets": [
                    {
                        "name": "Sparse Columns",
                        "cells": [
                            {"ref": "A1", "value": "Top"},
                            {"ref": "C2000", "value": "Total"},
                        ],
                    }
                ],
            },
            document_id="sparse-row-column-gap-xlsx",
            title="Sparse Row Column Gap XLSX",
            source_type="xlsx",
        )

        self.assertEqual(
            [["Sheet: Sparse Columns"], ["Top", "", ""], ["", "", "Total"]],
            document_ir.blocks[0].rows,
        )

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
