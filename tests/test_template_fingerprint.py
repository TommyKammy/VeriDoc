from __future__ import annotations

import unittest

from core.ir.document_ir_v1 import (
    BoundingBox,
    DocumentBlock,
    DocumentInfo,
    DocumentIRV1,
    DocumentPage,
    ExtractorRef,
    ReviewState,
)
from core.ir.template_fingerprint import (
    TemplateMatchClassification,
    classify_template_match,
    match_template_fingerprint,
)


class TemplateFingerprintTest(unittest.TestCase):
    def test_classification_thresholds_are_stable(self) -> None:
        self.assertEqual(TemplateMatchClassification.KNOWN, classify_template_match(0.95))
        self.assertEqual(TemplateMatchClassification.CAUTION, classify_template_match(0.80))
        self.assertEqual(TemplateMatchClassification.UNKNOWN, classify_template_match(0.79))

    def test_representative_documents_are_classified_by_template_fit(self) -> None:
        template = self.template_definition()

        known = match_template_fingerprint(self.document_with_blocks(), template)
        caution = match_template_fingerprint(
            self.document_with_blocks(table_text="Yield Summary"),
            template,
        )
        unknown = match_template_fingerprint(self.document_with_blocks("Certificate of Analysis"), template)

        self.assertEqual(TemplateMatchClassification.KNOWN, known.classification)
        self.assertGreaterEqual(known.score, 0.95)
        self.assertEqual(TemplateMatchClassification.CAUTION, caution.classification)
        self.assertGreaterEqual(caution.score, 0.80)
        self.assertLess(caution.score, 0.95)
        self.assertEqual(TemplateMatchClassification.UNKNOWN, unknown.classification)
        self.assertLess(unknown.score, 0.80)

    def test_missing_page_table_or_heading_signals_fail_closed_to_low_confidence(self) -> None:
        template = self.template_definition()

        no_pages = match_template_fingerprint(self.document_with_blocks(pages=[]), template)
        no_heading = match_template_fingerprint(
            self.document_with_blocks(heading_text=None, table_text="Yield Summary"),
            template,
        )
        no_table = match_template_fingerprint(
            self.document_with_blocks("Batch Production Record", table_text=None),
            template,
        )

        for result in (no_pages, no_heading, no_table):
            with self.subTest(result=result):
                self.assertEqual(TemplateMatchClassification.UNKNOWN, result.classification)
                self.assertLess(result.score, 0.80)
                self.assertTrue(result.requires_review)
                self.assertTrue(result.warnings)

    def template_definition(self) -> dict[str, object]:
        return {
            "template_id": "synthetic-batch-record-v1",
            "version": "1.0.0",
            "document_type": "batch_record",
            "anchors": [
                {
                    "anchor_id": "batch-header",
                    "kind": "heading",
                    "text": "Batch Production Record",
                    "match": "normalized",
                    "scope": {"page": 1, "block_types": ["heading"]},
                },
                {
                    "anchor_id": "yield-table",
                    "kind": "table_header",
                    "text": "Yield Summary",
                    "match": "contains",
                    "scope": {"page": 1, "block_types": ["table"]},
                },
            ],
            "tables": [
                {
                    "table_id": "yield_summary",
                    "anchor_id": "yield-table",
                    "required_columns": ["step", "expected_yield", "actual_yield"],
                }
            ],
        }

    def document_with_blocks(
        self,
        heading_text: str | None = "Batch Production Record",
        paragraph_text: str | None = "Batch No. BN-001\nManufacturing Date 2026-01-01",
        table_text: str | None = "Yield Summary\nstep\texpected_yield\tactual_yield",
        *,
        pages: list[DocumentPage] | None = None,
    ) -> DocumentIRV1:
        blocks: list[DocumentBlock] = []
        if heading_text is not None:
            blocks.append(self.block("heading", heading_text))
        if paragraph_text is not None:
            blocks.append(self.block("paragraph", paragraph_text, y=120.0))
        if table_text is not None:
            blocks.append(self.block("table", table_text, y=180.0))
        return DocumentIRV1(
            schema_version="document-ir/v1",
            document=DocumentInfo(id="fixture", title="Fixture", source_type="pdf"),
            pages=[DocumentPage(page_number=1, width=612.0, height=792.0)] if pages is None else pages,
            blocks=blocks,
            warnings=[],
        )

    def block(self, block_type: str, text: str, *, y: float = 72.0) -> DocumentBlock:
        return DocumentBlock(
            id=f"{block_type}-{int(y)}",
            type=block_type,
            text=text,
            source_page=1,
            bbox=BoundingBox(x=72.0, y=y, width=180.0, height=24.0),
            extractor=ExtractorRef(name="fixture"),
            confidence=0.99,
            review=ReviewState(requires_review=False, warnings=[]),
        )


if __name__ == "__main__":
    unittest.main()
