from __future__ import annotations

import unittest
from typing import Any

from core.ir.document_ir_v1 import (
    BoundingBox,
    DocumentBlock,
    DocumentInfo,
    DocumentIRV1,
    DocumentPage,
    ExtractorRef,
    ReviewState,
    from_parser_output,
)
from core.ir.template_fingerprint import (
    TemplateMatchClassification,
    apply_template_field_mapping,
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

    def test_missing_required_field_anchor_fails_closed_to_low_confidence(self) -> None:
        template = self.template_definition()
        template["anchors"] = [
            *template["anchors"],
            {
                "anchor_id": "batch-number-label",
                "kind": "label",
                "text": "Batch No.",
                "match": "contains",
                "scope": {"page": 1, "block_types": ["paragraph"]},
            },
        ]
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-number-label", "direction": "same_block"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": ["batch-number-required"],
                "output_key": "batch.number",
            }
        ]

        result = match_template_fingerprint(
            self.document_with_blocks(paragraph_text="Lot No. BN-001"),
            template,
        )

        self.assertEqual(TemplateMatchClassification.UNKNOWN, result.classification)
        self.assertLess(result.score, 0.80)
        self.assertTrue(result.requires_review)
        self.assertIn("batch-number-label", result.missing_anchor_ids)

    def test_undefined_required_anchor_fails_closed_to_low_confidence(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "undefined-batch-label", "direction": "same_block"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": ["batch-number-required"],
                "output_key": "batch.number",
            }
        ]

        result = match_template_fingerprint(self.document_with_blocks(), template)

        self.assertEqual(TemplateMatchClassification.UNKNOWN, result.classification)
        self.assertLess(result.score, 0.80)
        self.assertTrue(result.requires_review)
        self.assertIn("undefined-batch-label", result.missing_anchor_ids)
        self.assertIn(
            "template required anchor 'undefined-batch-label' is not defined",
            result.warnings,
        )

    def test_undefined_table_anchor_fails_closed_to_low_confidence(self) -> None:
        template = self.template_definition()
        template["tables"][0]["anchor_id"] = "undefined-yield-table"

        result = match_template_fingerprint(self.document_with_blocks(), template)

        self.assertEqual(TemplateMatchClassification.UNKNOWN, result.classification)
        self.assertLess(result.score, 0.80)
        self.assertTrue(result.requires_review)
        self.assertIn("undefined-yield-table", result.missing_anchor_ids)
        self.assertIn(
            "template required anchor 'undefined-yield-table' is not defined",
            result.warnings,
        )

    def test_table_columns_are_scored_on_the_matched_anchor_block_only(self) -> None:
        template = self.template_definition()
        document = self.document_with_blocks(table_text="Yield Summary")
        document.blocks.append(
            self.block(
                "table",
                "Equipment Summary\nstep\texpected_yield\tactual_yield",
                y=240.0,
            )
        )

        result = match_template_fingerprint(document, template)

        self.assertNotEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertTrue(result.requires_review)
        self.assertIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_required_columns_match_whole_column_names(self) -> None:
        template = self.template_definition()

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text="Yield Summary\nsteps\texpected_yield_estimate\tvalid_actual_yield"
            ),
            template,
        )

        self.assertNotEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertTrue(result.requires_review)
        self.assertIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_required_columns_normalize_template_and_header_separators(self) -> None:
        template = self.template_definition()

        result = match_template_fingerprint(
            self.document_with_blocks(table_text="Yield Summary\nstep\texpected yield\tactual yield"),
            template,
        )

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertGreaterEqual(result.score, 0.95)
        self.assertFalse(result.requires_review)
        self.assertNotIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_incomplete_required_columns_are_capped_below_known(self) -> None:
        template = self.template_definition()
        template["tables"][0]["required_columns"] = [
            "step",
            "expected_yield",
            "actual_yield",
            "variance",
            "review_status",
        ]

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text=(
                    "Yield Summary\n"
                    "step\texpected_yield\tactual_yield\tvariance"
                )
            ),
            template,
        )

        self.assertEqual(TemplateMatchClassification.CAUTION, result.classification)
        self.assertGreaterEqual(result.score, 0.80)
        self.assertLess(result.score, 0.95)
        self.assertTrue(result.requires_review)
        self.assertIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_required_columns_must_come_from_header_row(self) -> None:
        template = self.template_definition()

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text=(
                    "Yield Summary\n"
                    "foo\tbar\tbaz\n"
                    "step\t1\t2\n"
                    "expected_yield\t95\t96\n"
                    "actual_yield\t94\t95"
                )
            ),
            template,
        )

        self.assertNotEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertTrue(result.requires_review)
        self.assertIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_data_row_does_not_satisfy_required_column_headers(self) -> None:
        template = self.template_definition()

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text=(
                    "Yield Summary\n"
                    "foo\tbar\tbaz\n"
                    "step\texpected_yield\tactual_yield"
                )
            ),
            template,
        )

        self.assertNotEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertTrue(result.requires_review)
        self.assertIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_data_row_after_incomplete_same_row_anchor_does_not_satisfy_headers(self) -> None:
        template = self.template_definition()

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text=(
                    "Yield Summary\tfoo\tbar\n"
                    "step\texpected_yield\tactual_yield"
                )
            ),
            template,
        )

        self.assertNotEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertTrue(result.requires_review)
        self.assertIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_same_row_table_note_does_not_hide_following_header(self) -> None:
        template = self.template_definition()

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text=(
                    "Yield Summary\tAll yields in %\n"
                    "step\texpected_yield\tactual_yield\n"
                    "blend\t95\t94"
                )
            ),
            template,
        )

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertFalse(result.requires_review)
        self.assertNotIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_absent_optional_anchor_does_not_lower_required_anchor_score(self) -> None:
        template = self.template_definition()
        template["anchors"] = [
            template["anchors"][0],
            {
                "anchor_id": "optional-review-note",
                "kind": "label",
                "text": "Review Note",
                "match": "contains",
                "scope": {"page": 1, "block_types": ["paragraph"]},
            },
        ]
        template["tables"] = []
        template["fields"] = [
            {
                "field_id": "review_note",
                "label": "Review Note",
                "value_type": "string",
                "source": {"anchor_id": "optional-review-note", "direction": "same_block"},
                "required": False,
                "risk_level": "low",
                "validation_rule_ids": [],
                "output_key": "review.note",
            }
        ]

        result = match_template_fingerprint(self.document_with_blocks(table_text=None), template)

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertGreaterEqual(result.score, 0.95)
        self.assertFalse(result.requires_review)
        self.assertIn("optional-review-note", result.missing_anchor_ids)
        self.assertNotIn("template anchor 'optional-review-note' missing from document", result.warnings)

    def test_optional_anchor_page_scope_does_not_fail_closed(self) -> None:
        template = self.template_definition()
        template["anchors"] = [
            *template["anchors"],
            {
                "anchor_id": "optional-review-note",
                "kind": "label",
                "text": "Review Note",
                "match": "contains",
                "scope": {"page": 2, "block_types": ["paragraph"]},
            },
        ]
        template["fields"] = [
            {
                "field_id": "review_note",
                "label": "Review Note",
                "value_type": "string",
                "source": {"anchor_id": "optional-review-note", "direction": "same_block"},
                "required": False,
                "risk_level": "low",
                "validation_rule_ids": [],
                "output_key": "review.note",
            }
        ]

        result = match_template_fingerprint(self.document_with_blocks(), template)

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertGreaterEqual(result.score, 0.95)
        self.assertFalse(result.requires_review)
        self.assertIn("optional-review-note", result.missing_anchor_ids)
        self.assertNotIn("template anchor 'optional-review-note' missing from document", result.warnings)

    def test_table_anchor_scope_requires_real_table_block(self) -> None:
        template = self.template_definition()
        template["anchors"][1]["scope"]["block_types"] = ["paragraph", "table"]

        result = match_template_fingerprint(
            self.document_with_blocks(
                paragraph_text="Yield Summary\nstep\texpected_yield\tactual_yield",
                table_text=None,
            ),
            template,
        )

        self.assertEqual(TemplateMatchClassification.UNKNOWN, result.classification)
        self.assertIn("yield-table", result.missing_anchor_ids)
        self.assertTrue(result.requires_review)

    def test_table_header_anchor_scans_rows_beyond_first_row(self) -> None:
        template = self.template_definition()
        template["anchors"] = [template["anchors"][1]]
        document = from_parser_output(
            {
                "source_path": "fixtures/sample.xlsx",
                "sheets": [
                    {
                        "name": "Results",
                        "dimension": "A1:C5",
                        "cells": [
                            {"ref": "A1", "value": "Workbook metadata", "value_type": "shared_string"},
                            {"ref": "A2", "value": "Prepared by QA", "value_type": "shared_string"},
                            {"ref": "A3", "value": "Yield Summary", "value_type": "shared_string"},
                            {"ref": "A4", "value": "step", "value_type": "shared_string"},
                            {"ref": "B4", "value": "expected_yield", "value_type": "shared_string"},
                            {"ref": "C4", "value": "actual_yield", "value_type": "shared_string"},
                            {"ref": "A5", "value": "blend", "value_type": "shared_string"},
                            {"ref": "B5", "value": "95", "value_type": "number"},
                            {"ref": "C5", "value": "94", "value_type": "number"},
                        ],
                        "merged_ranges": [],
                    }
                ],
            },
            document_id="sample-xlsx",
            title="Sample XLSX",
            source_type="xlsx",
        )

        result = match_template_fingerprint(document, template)

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("yield-table", result.missing_anchor_ids)
        self.assertNotIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_required_columns_scan_past_note_rows(self) -> None:
        template = self.template_definition()

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text=(
                    "Yield Summary\n"
                    "All yields in %\n"
                    "step\texpected_yield\tactual_yield\n"
                    "blend\t95\t94"
                )
            ),
            template,
        )

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertFalse(result.requires_review)
        self.assertNotIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_parser_review_state_is_preserved_for_known_template_matches(self) -> None:
        template = self.template_definition()
        document = self.document_with_blocks(table_review_warnings=["heuristic table requires review"])
        document.warnings.append("document-level parser warning")

        result = match_template_fingerprint(document, template)

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertGreaterEqual(result.score, 0.95)
        self.assertTrue(result.requires_review)
        self.assertIn("document-level parser warning", result.warnings)
        self.assertIn("heuristic table requires review", result.warnings)

    def test_warning_only_block_review_state_is_preserved(self) -> None:
        template = self.template_definition()
        document = self.document_with_blocks(
            table_review_warnings=["schema-valid warning-only block"],
            table_review_requires_review=False,
        )

        result = match_template_fingerprint(document, template)

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertTrue(result.requires_review)
        self.assertIn("schema-valid warning-only block", result.warnings)

    def test_exact_anchor_mode_does_not_normalize_case_or_whitespace(self) -> None:
        template = self.template_definition()
        anchors = template["anchors"]
        anchors[0]["match"] = "exact"

        result = match_template_fingerprint(
            self.document_with_blocks(heading_text=" batch production record "),
            template,
        )

        self.assertEqual(TemplateMatchClassification.UNKNOWN, result.classification)
        self.assertIn("batch-header", result.missing_anchor_ids)
        self.assertTrue(result.requires_review)

    def test_exact_table_anchor_matches_table_header_text(self) -> None:
        template = self.template_definition()
        template["anchors"][1]["match"] = "exact"

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text="Yield Summary\nstep\texpected_yield\tactual_yield"
            ),
            template,
        )

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("yield-table", result.missing_anchor_ids)
        self.assertNotIn("template table 'yield_summary' missing from document", result.warnings)

    def test_exact_table_anchor_mode_does_not_strip_cell_whitespace(self) -> None:
        template = self.template_definition()
        template["anchors"][1]["match"] = "exact"

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text=" Yield Summary \nstep\texpected_yield\tactual_yield"
            ),
            template,
        )

        self.assertEqual(TemplateMatchClassification.UNKNOWN, result.classification)
        self.assertIn("yield-table", result.missing_anchor_ids)
        self.assertTrue(result.requires_review)

    def test_single_column_required_table_header_is_supported(self) -> None:
        template = self.template_definition()
        template["anchors"][1]["text"] = "Lot History"
        template["tables"][0]["table_id"] = "lot_history"
        template["tables"][0]["required_columns"] = ["Lot Number"]

        result = match_template_fingerprint(
            self.document_with_blocks(table_text="Lot History\nLot Number\nBN-001"),
            template,
        )

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("template table 'lot_history' required columns incomplete", result.warnings)

    def test_required_columns_can_share_table_anchor_row(self) -> None:
        template = self.template_definition()

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text="Yield Summary\tstep\texpected_yield\tactual_yield\nblend\t95\t94"
            ),
            template,
        )

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_split_cell_table_anchor_can_share_header_row(self) -> None:
        template = self.template_definition()

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text="Yield\tSummary\tstep\texpected_yield\tactual_yield\nblend\t95\t94"
            ),
            template,
        )

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("yield-table", result.missing_anchor_ids)
        self.assertNotIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_tab_delimited_required_column_preserves_commas_inside_cells(self) -> None:
        template = self.template_definition()
        template["anchors"][1]["text"] = "Lot History"
        template["tables"][0]["table_id"] = "lot_history"
        template["tables"][0]["required_columns"] = ["Lot, Number", "Value"]

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text="Lot History\nLot, Number\tValue\nBN-001\treleased"
            ),
            template,
        )

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("template table 'lot_history' required columns incomplete", result.warnings)

    def test_coordinate_like_pdf_table_title_does_not_force_xlsx_rows(self) -> None:
        template = self.template_definition()

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text="A1: Yield Summary\nstep\texpected_yield\tactual_yield"
            ),
            template,
        )

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_coordinate_like_pdf_title_preserves_comma_or_space_delimited_rows(self) -> None:
        template = self.template_definition()

        for table_text in (
            "A1: Yield Summary\nstep,expected_yield,actual_yield",
            "A1: Yield Summary\nstep  expected_yield  actual_yield",
        ):
            with self.subTest(table_text=table_text):
                result = match_template_fingerprint(
                    self.document_with_blocks(table_text=table_text),
                    template,
                )

                self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
                self.assertNotIn(
                    "template table 'yield_summary' required columns incomplete",
                    result.warnings,
                )

    def test_docx_wrapped_header_cell_line_break_stays_in_header_row(self) -> None:
        template = self.template_definition()
        template["tables"][0]["required_columns"] = ["step", "expected yield", "actual yield"]

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text="Yield Summary\nstep\tExpected\nYield\tActual Yield",
                source_type="docx",
            ),
            template,
        )

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_docx_manual_tab_inside_header_cell_can_match_required_column(self) -> None:
        template = self.template_definition()
        template["anchors"][1]["text"] = "Lot History"
        template["tables"][0]["table_id"] = "lot_history"
        template["tables"][0]["required_columns"] = ["Lot ID", "Value"]

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text="Lot History\nLot\tID\tValue\nBN-001\t123",
                source_type="docx",
            ),
            template,
        )

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("template table 'lot_history' required columns incomplete", result.warnings)

    def test_strict_table_anchor_can_match_later_cell(self) -> None:
        template = self.template_definition()
        template["anchors"][1]["match"] = "normalized"

        result = match_template_fingerprint(
            self.document_with_blocks(
                table_text="Report\tYield Summary\nstep\texpected_yield\tactual_yield"
            ),
            template,
        )

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("yield-table", result.missing_anchor_ids)
        self.assertNotIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_exact_non_xlsx_coordinate_like_table_anchor_is_not_parsed_as_xlsx(self) -> None:
        template = self.template_definition()
        template["anchors"][1]["text"] = "A1: Yield Summary"
        template["anchors"][1]["match"] = "exact"

        result = match_template_fingerprint(
            self.document_with_blocks(table_text="A1: Yield Summary\nstep\texpected_yield\tactual_yield"),
            template,
        )

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("yield-table", result.missing_anchor_ids)
        self.assertNotIn("template table 'yield_summary' missing from document", result.warnings)

    def test_xlsx_cell_rows_are_reconstructed_for_required_columns(self) -> None:
        template = self.template_definition()
        template["anchors"] = [template["anchors"][1]]
        document = from_parser_output(
            {
                "source_path": "fixtures/sample.xlsx",
                "sheets": [
                    {
                        "name": "Results",
                        "dimension": "A1:C3",
                        "cells": [
                            {"ref": "A1", "value": "Yield Summary", "value_type": "shared_string"},
                            {"ref": "A2", "value": "step", "value_type": "shared_string"},
                            {"ref": "B2", "value": "expected_yield", "value_type": "shared_string"},
                            {"ref": "C2", "value": "actual_yield", "value_type": "shared_string"},
                            {"ref": "A3", "value": "blend", "value_type": "shared_string"},
                            {"ref": "B3", "value": "95", "value_type": "number"},
                            {"ref": "C3", "value": "94", "value_type": "number"},
                        ],
                        "merged_ranges": [],
                    }
                ],
            },
            document_id="sample-xlsx",
            title="Sample XLSX",
            source_type="xlsx",
        )

        result = match_template_fingerprint(document, template)

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_xlsx_header_cells_are_not_merged_to_satisfy_required_columns(self) -> None:
        template = self.template_definition()
        template["anchors"] = [template["anchors"][1]]
        document = from_parser_output(
            {
                "source_path": "fixtures/sample.xlsx",
                "sheets": [
                    {
                        "name": "Results",
                        "dimension": "A1:D3",
                        "cells": [
                            {"ref": "A1", "value": "Yield Summary", "value_type": "shared_string"},
                            {"ref": "A2", "value": "step", "value_type": "shared_string"},
                            {"ref": "B2", "value": "expected", "value_type": "shared_string"},
                            {"ref": "C2", "value": "yield", "value_type": "shared_string"},
                            {"ref": "D2", "value": "actual_yield", "value_type": "shared_string"},
                            {"ref": "A3", "value": "blend", "value_type": "shared_string"},
                            {"ref": "B3", "value": "95", "value_type": "number"},
                            {"ref": "C3", "value": "96", "value_type": "number"},
                            {"ref": "D3", "value": "94", "value_type": "number"},
                        ],
                        "merged_ranges": [],
                    }
                ],
            },
            document_id="sample-xlsx",
            title="Sample XLSX",
            source_type="xlsx",
        )

        result = match_template_fingerprint(document, template)

        self.assertNotEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertTrue(result.requires_review)
        self.assertIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_xlsx_wrapped_cell_text_does_not_abort_row_reconstruction(self) -> None:
        template = self.template_definition()
        template["anchors"] = [template["anchors"][1]]
        document = from_parser_output(
            {
                "source_path": "fixtures/sample.xlsx",
                "sheets": [
                    {
                        "name": "Results",
                        "dimension": "A1:C4",
                        "cells": [
                            {"ref": "A1", "value": "note\ncontinued", "value_type": "shared_string"},
                            {"ref": "A2", "value": "Yield Summary", "value_type": "shared_string"},
                            {"ref": "A3", "value": "step", "value_type": "shared_string"},
                            {"ref": "B3", "value": "expected_yield", "value_type": "shared_string"},
                            {"ref": "C3", "value": "actual_yield", "value_type": "shared_string"},
                            {"ref": "A4", "value": "blend", "value_type": "shared_string"},
                            {"ref": "B4", "value": "95", "value_type": "number"},
                            {"ref": "C4", "value": "94", "value_type": "number"},
                        ],
                        "merged_ranges": [],
                    }
                ],
            },
            document_id="sample-xlsx",
            title="Sample XLSX",
            source_type="xlsx",
        )

        result = match_template_fingerprint(document, template)

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_xlsx_delimited_wrapped_cell_text_does_not_abort_row_reconstruction(self) -> None:
        template = self.template_definition()
        template["anchors"] = [template["anchors"][1]]
        document = from_parser_output(
            {
                "source_path": "fixtures/sample.xlsx",
                "sheets": [
                    {
                        "name": "Results",
                        "dimension": "A1:C4",
                        "cells": [
                            {
                                "ref": "A1",
                                "value": "note\ncontinued, with comma",
                                "value_type": "shared_string",
                            },
                            {"ref": "A2", "value": "Yield Summary", "value_type": "shared_string"},
                            {"ref": "A3", "value": "step", "value_type": "shared_string"},
                            {"ref": "B3", "value": "expected_yield", "value_type": "shared_string"},
                            {"ref": "C3", "value": "actual_yield", "value_type": "shared_string"},
                            {"ref": "A4", "value": "blend", "value_type": "shared_string"},
                            {"ref": "B4", "value": "95", "value_type": "number"},
                            {"ref": "C4", "value": "94", "value_type": "number"},
                        ],
                        "merged_ranges": [],
                    }
                ],
            },
            document_id="sample-xlsx",
            title="Sample XLSX",
            source_type="xlsx",
        )

        result = match_template_fingerprint(document, template)

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_optional_heading_anchor_does_not_fail_closed_when_section_is_absent(self) -> None:
        template = self.template_definition()
        template["anchors"] = [
            *template["anchors"],
            {
                "anchor_id": "optional-review-section",
                "kind": "heading",
                "text": "Optional Review Section",
                "match": "normalized",
                "scope": {"page": 2, "block_types": ["heading"]},
            },
        ]
        template["fields"] = [
            {
                "field_id": "optional_review_note",
                "label": "Optional review note",
                "value_type": "string",
                "source": {"anchor_id": "optional-review-section", "direction": "same_block"},
                "required": False,
                "risk_level": "low",
                "validation_rule_ids": [],
                "output_key": "review.optional_note",
            }
        ]

        result = match_template_fingerprint(self.document_with_blocks(), template)

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertFalse(result.requires_review)
        self.assertNotIn(
            "template anchor 'optional-review-section' missing from document",
            result.warnings,
        )

    def test_xlsx_trailing_wrapped_cell_continuation_does_not_abort_row_reconstruction(self) -> None:
        template = self.template_definition()
        template["anchors"] = [template["anchors"][1]]
        document = from_parser_output(
            {
                "source_path": "fixtures/sample.xlsx",
                "sheets": [
                    {
                        "name": "Results",
                        "dimension": "A1:C4",
                        "cells": [
                            {"ref": "A1", "value": "Yield Summary", "value_type": "shared_string"},
                            {"ref": "A2", "value": "step", "value_type": "shared_string"},
                            {"ref": "B2", "value": "expected_yield", "value_type": "shared_string"},
                            {"ref": "C2", "value": "actual_yield", "value_type": "shared_string"},
                            {"ref": "A3", "value": "blend", "value_type": "shared_string"},
                            {"ref": "B3", "value": "95", "value_type": "number"},
                            {
                                "ref": "C3",
                                "value": "94\ncontinued, with comma",
                                "value_type": "shared_string",
                            },
                        ],
                        "merged_ranges": [],
                    }
                ],
            },
            document_id="sample-xlsx",
            title="Sample XLSX",
            source_type="xlsx",
        )

        result = match_template_fingerprint(document, template)

        self.assertEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertNotIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_pdf_parser_table_cells_are_not_merged_to_satisfy_required_columns(self) -> None:
        template = self.template_definition()
        template["anchors"] = [template["anchors"][1]]
        template["tables"][0]["required_columns"] = ["Lot ID", "Value"]
        document = from_parser_output(
            {
                "candidates": [
                    {
                        "extractor": "pdfplumber",
                        "flavor": "lattice",
                        "status": "ok",
                        "tables": [
                            {
                                "page_number": 1,
                                "rows": [
                                    ["Yield Summary"],
                                    ["Lot", "ID", "Value"],
                                    ["L-001", "A", "94"],
                                ],
                            }
                        ],
                    }
                ]
            },
            document_id="sample-pdf",
            title="Sample PDF",
            source_type="pdf",
        )

        result = match_template_fingerprint(document, template)

        self.assertNotEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertTrue(result.requires_review)
        self.assertIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_pdf_table_cells_are_not_merged_when_extractor_has_no_flavor(self) -> None:
        template = self.template_definition()
        template["tables"][0]["required_columns"] = ["Lot ID", "Value"]

        result = match_template_fingerprint(
            self.document_with_blocks(table_text="Yield Summary\nLot\tID\tValue\nL-001\tA\t94"),
            template,
        )

        self.assertNotEqual(TemplateMatchClassification.KNOWN, result.classification)
        self.assertTrue(result.requires_review)
        self.assertIn("template table 'yield_summary' required columns incomplete", result.warnings)

    def test_known_template_field_mapping_extracts_values_with_evidence(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            },
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            },
            {
                "field_id": "missing_required",
                "label": "Disposition",
                "value_type": "string",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.disposition",
            },
        ]
        template["output_mapping"] = {
            "format": "json",
            "root_key": "template_result",
            "field_map": [
                {"field_id": "batch_number", "output_key": "batch.number"},
                {"field_id": "actual_yield", "output_key": "batch.actual_yield"},
                {"field_id": "missing_required", "output_key": "batch.disposition"},
            ],
            "table_map": [],
        }
        document = self.document_with_blocks(
            paragraph_text="Batch No. BN-001\nManufacturing Date 2026-01-01",
            table_text=(
                "Yield Summary\tstep\texpected_yield\tactual_yield\n"
                "blend\t95\t94"
            ),
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("BN-001", mapped["batch_number"].value)
        self.assertEqual("94", mapped["actual_yield"].value)
        self.assertIsNone(mapped["missing_required"].value)
        self.assertTrue(mapped["missing_required"].requires_review)
        self.assertIn("template field 'missing_required' missing; requires review", result.warnings)
        self.assertEqual(
            {"template_result": {"batch": {"number": "BN-001", "actual_yield": "94"}}},
            result.output,
        )
        self.assertEqual("paragraph-120", mapped["batch_number"].evidence["block_id"])
        self.assertEqual("table-180", mapped["actual_yield"].evidence["block_id"])
        self.assertGreaterEqual(mapped["batch_number"].confidence, 0.90)

    def test_field_mapping_extracts_right_side_block_value(self) -> None:
        template = self.template_definition()
        template["anchors"].append(
            {
                "anchor_id": "batch-number-label",
                "kind": "label",
                "text": "Batch No.",
                "match": "contains",
                "scope": {"page": 1, "block_types": ["paragraph"]},
            }
        )
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-number-label", "direction": "right"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            }
        ]
        document = DocumentIRV1(
            schema_version="document-ir/v1",
            document=DocumentInfo(id="fixture", title="Fixture", source_type="pdf"),
            pages=[DocumentPage(page_number=1, width=612.0, height=792.0)],
            blocks=[
                self.block("heading", "Batch Production Record"),
                self.block("paragraph", "Batch No.", y=120.0, block_id="batch-label"),
                self.block("paragraph", "BN-001", x=280.0, y=120.0, block_id="batch-value"),
                self.block(
                    "table",
                    "Yield Summary\nstep\texpected_yield\tactual_yield",
                    y=180.0,
                ),
            ],
            warnings=[],
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("BN-001", mapped["batch_number"].value)
        self.assertEqual("batch-value", mapped["batch_number"].evidence["block_id"])
        self.assertEqual("right", mapped["batch_number"].evidence["direction"])

    def test_field_mapping_matches_short_markers_on_boundaries(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "number",
                "label": "No",
                "value_type": "string",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            }
        ]
        document = self.document_with_blocks(
            paragraph_text="Notes: do not extract this line\nNo: BN-001"
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("BN-001", mapped["number"].value)

    def test_table_cell_mapping_offsets_columns_after_same_row_anchor(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "Metadata\tYield Summary\tstep\texpected_yield\tactual_yield\n"
                "Batch A\tResults\tblend\t95\t94"
            )
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("94", mapped["actual_yield"].value)
        self.assertEqual(1, mapped["actual_yield"].evidence["row_index"])
        self.assertEqual(4, mapped["actual_yield"].evidence["column_index"])

    def test_table_cell_mapping_reports_actual_row_after_skipped_rows(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "Yield Summary\tstep\texpected_yield\tactual_yield\n"
                "blend\t95\n"
                "pack\t96\t93"
            )
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("93", mapped["actual_yield"].value)
        self.assertEqual(2, mapped["actual_yield"].evidence["row_index"])

    def test_table_cell_mapping_uses_table_block_that_satisfies_required_columns(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = DocumentIRV1(
            schema_version="document-ir/v1",
            document=DocumentInfo(id="fixture", title="Fixture", source_type="pdf"),
            pages=[DocumentPage(page_number=1, width=612.0, height=792.0)],
            blocks=[
                self.block("heading", "Batch Production Record"),
                self.block(
                    "table",
                    "Yield Summary\nactual_yield\nwrong-table-value",
                    y=180.0,
                    block_id="incomplete-yield-table",
                ),
                self.block(
                    "table",
                    "Yield Summary\nstep\texpected_yield\tactual_yield\nblend\t95\t94",
                    y=240.0,
                    block_id="complete-yield-table",
                ),
            ],
            warnings=[],
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("94", mapped["actual_yield"].value)
        self.assertEqual("complete-yield-table", mapped["actual_yield"].evidence["block_id"])

    def test_xlsx_table_cell_mapping_preserves_sparse_column_positions(self) -> None:
        template = self.template_definition()
        template["tables"][0]["required_columns"] = ["actual_yield"]
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            source_type="xlsx",
            table_text=(
                "A1: Yield Summary\n"
                "C1: actual_yield\n"
                "A2: do-not-use\n"
                "C2: 94"
            ),
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("94", mapped["actual_yield"].value)
        self.assertEqual(2, mapped["actual_yield"].evidence["column_index"])

    def test_xlsx_table_cell_mapping_uses_physical_columns_for_value_rows(self) -> None:
        template = self.template_definition()
        template["tables"][0]["required_columns"] = ["actual_yield"]
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            source_type="xlsx",
            table_text=(
                "A1: Yield Summary\n"
                "A2: step\n"
                "C2: actual_yield\n"
                "B3: SPACER\n"
                "C3: 94"
            ),
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("94", mapped["actual_yield"].value)
        self.assertEqual(2, mapped["actual_yield"].evidence["column_index"])

    def test_tabular_table_cell_mapping_preserves_blank_value_cells(self) -> None:
        template = self.template_definition()
        template["tables"][0]["required_columns"] = ["actual_yield"]
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "Yield Summary\tstep\texpected_yield\tactual_yield\tvariance\n"
                "blend\t95\t\t1"
            )
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertIsNone(mapped["actual_yield"].value)
        self.assertTrue(mapped["actual_yield"].requires_review)
        self.assertEqual({"template_result": {}}, result.output)

    def test_tabular_table_cell_mapping_preserves_blank_cells_before_value_column(self) -> None:
        template = self.template_definition()
        template["tables"][0]["required_columns"] = ["actual_yield"]
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "Yield Summary\tstep\texpected_yield\tactual_yield\tvariance\n"
                "blend\t\t94\t1"
            )
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("94", mapped["actual_yield"].value)
        self.assertEqual(2, mapped["actual_yield"].evidence["column_index"])

    def test_table_cell_mapping_skips_repeated_header_rows(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "Yield Summary\n"
                "step\texpected_yield\tactual_yield\n"
                "step\texpected_yield\tactual_yield\n"
                "blend\t95\t94"
            )
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("94", mapped["actual_yield"].value)
        self.assertFalse(mapped["actual_yield"].requires_review)
        self.assertEqual({"template_result": {"batch": {"actual_yield": "94"}}}, result.output)

    def test_review_required_extracted_value_is_not_confirmed_in_output(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "Yield Summary\tstep\texpected_yield\tactual_yield\n"
                "blend\t95\t94"
            ),
            table_review_warnings=["parser marked table uncertain"],
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("94", mapped["actual_yield"].value)
        self.assertTrue(mapped["actual_yield"].requires_review)
        self.assertEqual({"template_result": {}}, result.output)

    def test_below_field_mapping_does_not_extract_anchor_text_fallback(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "disposition",
                "label": "Disposition",
                "value_type": "string",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.disposition",
            }
        ]
        document = self.document_with_blocks(
            paragraph_text="Batch Production Record supplemental note"
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertIsNone(mapped["disposition"].value)
        self.assertTrue(mapped["disposition"].requires_review)
        self.assertIn("template field 'disposition' missing; requires review", result.warnings)

    def test_below_label_anchor_does_not_extract_unlabeled_table_block(self) -> None:
        template = self.template_definition()
        template["anchors"].append(
            {
                "anchor_id": "batch-number-label",
                "kind": "label",
                "text": "Batch No.",
                "match": "normalized",
                "scope": {"page": 1, "block_types": ["paragraph"]},
            }
        )
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-number-label", "direction": "below"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            }
        ]
        document = DocumentIRV1(
            schema_version="document-ir/v1",
            document=DocumentInfo(id="fixture", title="Fixture", source_type="pdf"),
            pages=[DocumentPage(page_number=1, width=612.0, height=792.0)],
            blocks=[
                self.block("heading", "Batch Production Record"),
                self.block("paragraph", "Batch No.", y=120.0, block_id="batch-label"),
                self.block(
                    "table",
                    "Yield Summary\nstep\texpected_yield\tactual_yield\nblend\t95\t94",
                    y=180.0,
                    block_id="yield-table-block",
                ),
            ],
            warnings=[],
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertIsNone(mapped["batch_number"].value)
        self.assertTrue(mapped["batch_number"].requires_review)
        self.assertEqual({"template_result": {}}, result.output)

    def test_below_label_anchor_skips_single_line_table_blocks(self) -> None:
        template = self.template_definition()
        template["anchors"].append(
            {
                "anchor_id": "batch-number-label",
                "kind": "label",
                "text": "Batch No.",
                "match": "normalized",
                "scope": {"page": 1, "block_types": ["paragraph"]},
            }
        )
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-number-label", "direction": "below"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            }
        ]
        document = DocumentIRV1(
            schema_version="document-ir/v1",
            document=DocumentInfo(id="fixture", title="Fixture", source_type="pdf"),
            pages=[DocumentPage(page_number=1, width=612.0, height=792.0)],
            blocks=[
                self.block("heading", "Batch Production Record"),
                self.block("paragraph", "Batch No.", y=120.0, block_id="batch-label"),
                self.block("table", "Yield Summary", y=180.0, block_id="yield-table-block"),
            ],
            warnings=[],
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertIsNone(mapped["batch_number"].value)
        self.assertTrue(mapped["batch_number"].requires_review)
        self.assertEqual({"template_result": {}}, result.output)

    def test_below_label_anchor_does_not_scan_past_non_value_block(self) -> None:
        template = self.template_definition()
        template["anchors"].append(
            {
                "anchor_id": "batch-number-label",
                "kind": "label",
                "text": "Batch No.",
                "match": "normalized",
                "scope": {"page": 1, "block_types": ["paragraph"]},
            }
        )
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-number-label", "direction": "below"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            }
        ]
        document = DocumentIRV1(
            schema_version="document-ir/v1",
            document=DocumentInfo(id="fixture", title="Fixture", source_type="pdf"),
            pages=[DocumentPage(page_number=1, width=612.0, height=792.0)],
            blocks=[
                self.block("heading", "Batch Production Record"),
                self.block("paragraph", "Batch No.", y=120.0, block_id="batch-label"),
                self.block(
                    "table",
                    "Yield Summary\nstep\texpected_yield\tactual_yield",
                    y=144.0,
                    block_id="yield-table-block",
                ),
                self.block("paragraph", "BN-001", y=180.0, block_id="later-value"),
            ],
            warnings=[],
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertIsNone(mapped["batch_number"].value)
        self.assertTrue(mapped["batch_number"].requires_review)
        self.assertEqual({"template_result": {}}, result.output)

    def test_below_label_anchor_rejects_known_anchor_text_as_value(self) -> None:
        template = self.template_definition()
        template["anchors"].append(
            {
                "anchor_id": "batch-number-label",
                "kind": "label",
                "text": "Batch No.",
                "match": "normalized",
                "scope": {"page": 1, "block_types": ["paragraph"]},
            }
        )
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-number-label", "direction": "below"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            }
        ]
        document = DocumentIRV1(
            schema_version="document-ir/v1",
            document=DocumentInfo(id="fixture", title="Fixture", source_type="pdf"),
            pages=[DocumentPage(page_number=1, width=612.0, height=792.0)],
            blocks=[
                self.block("heading", "Batch Production Record"),
                self.block("paragraph", "Batch No.", y=120.0, block_id="batch-label"),
                self.block("paragraph", "Yield Summary", y=144.0, block_id="yield-heading"),
            ],
            warnings=[],
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertIsNone(mapped["batch_number"].value)
        self.assertTrue(mapped["batch_number"].requires_review)
        self.assertEqual({"template_result": {}}, result.output)

    def test_below_label_anchor_rejects_section_heading_as_value(self) -> None:
        template = self.template_definition()
        template["anchors"].append(
            {
                "anchor_id": "batch-number-label",
                "kind": "label",
                "text": "Batch No.",
                "match": "normalized",
                "scope": {"page": 1, "block_types": ["paragraph"]},
            }
        )
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-number-label", "direction": "below"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            }
        ]
        document = DocumentIRV1(
            schema_version="document-ir/v1",
            document=DocumentInfo(id="fixture", title="Fixture", source_type="pdf"),
            pages=[DocumentPage(page_number=1, width=612.0, height=792.0)],
            blocks=[
                self.block("heading", "Batch Production Record"),
                self.block("paragraph", "Batch No.", y=120.0, block_id="batch-label"),
                self.block("paragraph", "Equipment Summary", y=144.0, block_id="section"),
            ],
            warnings=[],
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertIsNone(mapped["batch_number"].value)
        self.assertTrue(mapped["batch_number"].requires_review)
        self.assertEqual({"template_result": {}}, result.output)

    def test_below_field_mapping_reads_value_below_label_anchor(self) -> None:
        template = self.template_definition()
        template["anchors"].append(
            {
                "anchor_id": "batch-number-label",
                "kind": "label",
                "text": "Batch No.",
                "match": "normalized",
                "scope": {"page": 1, "block_types": ["paragraph"]},
            }
        )
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-number-label", "direction": "below"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            }
        ]
        document = DocumentIRV1(
            schema_version="document-ir/v1",
            document=DocumentInfo(id="fixture", title="Fixture", source_type="pdf"),
            pages=[DocumentPage(page_number=1, width=612.0, height=792.0)],
            blocks=[
                self.block("heading", "Batch Production Record"),
                self.block("paragraph", "Batch No.", y=120.0, block_id="batch-label"),
                self.block("paragraph", "BN-001", y=144.0, block_id="batch-value"),
                self.block(
                    "table",
                    "Yield Summary\nstep\texpected_yield\tactual_yield",
                    y=180.0,
                ),
            ],
            warnings=[],
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("BN-001", mapped["batch_number"].value)
        self.assertEqual("batch-value", mapped["batch_number"].evidence["block_id"])

    def test_field_mapping_stops_value_at_next_mapped_label(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            },
            {
                "field_id": "disposition",
                "label": "Disposition",
                "value_type": "string",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.disposition",
            },
        ]
        document = self.document_with_blocks(
            paragraph_text="Batch No. BN-001 Disposition Released"
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("BN-001", mapped["batch_number"].value)
        self.assertEqual("Released", mapped["disposition"].value)

    def test_field_mapping_stops_value_at_adjacent_same_line_label(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            },
            {
                "field_id": "manufacturing_date",
                "label": "Manufacturing Date",
                "value_type": "date",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.manufacturing_date",
            },
        ]
        document = self.document_with_blocks(
            paragraph_text="Batch No. BN-001 Manufacturing Date 2026-01-01"
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("BN-001", mapped["batch_number"].value)
        self.assertEqual("2026-01-01", mapped["manufacturing_date"].value)

    def test_field_mapping_does_not_stop_at_label_words_inside_value_text(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "comments",
                "label": "Comments",
                "value_type": "string",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.comments",
            },
            {
                "field_id": "date",
                "label": "Date",
                "value_type": "string",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": False,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.date",
            },
        ]
        document = self.document_with_blocks(paragraph_text="Comments: Expiry Date TBD")

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("Expiry Date TBD", mapped["comments"].value)
        self.assertIsNone(mapped["date"].value)
        self.assertEqual({"template_result": {"batch": {"comments": "Expiry Date TBD"}}}, result.output)

    def test_field_mapping_preserves_negative_value_sign_after_label(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "variance",
                "label": "Variance",
                "value_type": "number",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.variance",
            }
        ]
        document = self.document_with_blocks(paragraph_text="Variance: -1.5")

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("-1.5", mapped["variance"].value)
        self.assertEqual({"template_result": {"batch": {"variance": "-1.5"}}}, result.output)

    def test_field_mapping_preserves_spaced_negative_value_sign_after_label(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "variance",
                "label": "Variance",
                "value_type": "number",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.variance",
            }
        ]
        document = self.document_with_blocks(paragraph_text="Variance: - 1.5")

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("- 1.5", mapped["variance"].value)
        self.assertFalse(mapped["variance"].requires_review)
        self.assertEqual({"template_result": {"batch": {"variance": "- 1.5"}}}, result.output)

    def test_field_mapping_invalid_value_type_is_not_confirmed_in_output(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "Yield Summary\n"
                "step\texpected_yield\tactual_yield\n"
                "blend\t95\tN/A"
            )
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("N/A", mapped["actual_yield"].value)
        self.assertTrue(mapped["actual_yield"].requires_review)
        self.assertEqual({"template_result": {}}, result.output)
        self.assertIn(
            "template field 'actual_yield' value 'N/A' does not match value_type 'number'; requires review",
            result.warnings,
        )

    def test_field_mapping_validation_rule_failure_is_not_confirmed_in_output(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": ["actual-yield-range"],
                "output_key": "batch.actual_yield",
            }
        ]
        template["validation_rules"] = [
            {
                "rule_id": "actual-yield-range",
                "target": "actual_yield",
                "rule_type": "range",
                "severity": "error",
                "message": "Actual yield must stay within expected bounds.",
                "minimum": 0,
                "maximum": 100,
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "Yield Summary\n"
                "step\texpected_yield\tactual_yield\n"
                "blend\t95\t101"
            )
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("101", mapped["actual_yield"].value)
        self.assertTrue(mapped["actual_yield"].requires_review)
        self.assertEqual({"template_result": {}}, result.output)
        self.assertIn(
            "template field 'actual_yield' failed validation rule 'actual-yield-range'; "
            "requires review",
            result.warnings,
        )

    def test_right_side_fallback_stops_value_at_adjacent_label(self) -> None:
        template = self.template_definition()
        template["anchors"].append(
            {
                "anchor_id": "batch-number-label",
                "kind": "label",
                "text": "Batch No.",
                "match": "normalized",
                "scope": {"page": 1, "block_types": ["paragraph"]},
            }
        )
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-number-label", "direction": "right"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            },
            {
                "field_id": "manufacturing_date",
                "label": "Manufacturing Date",
                "value_type": "date",
                "source": {"anchor_id": "batch-number-label", "direction": "right"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.manufacturing_date",
            },
        ]
        document = DocumentIRV1(
            schema_version="document-ir/v1",
            document=DocumentInfo(id="fixture", title="Fixture", source_type="pdf"),
            pages=[DocumentPage(page_number=1, width=612.0, height=792.0)],
            blocks=[
                self.block("heading", "Batch Production Record"),
                self.block("paragraph", "Batch No.", y=120.0, block_id="batch-label"),
                self.block(
                    "paragraph",
                    "BN-001 Manufacturing Date 2026-01-01",
                    x=280.0,
                    y=120.0,
                    block_id="batch-value",
                ),
                self.block(
                    "table",
                    "Yield Summary\nstep\texpected_yield\tactual_yield",
                    y=180.0,
                ),
            ],
            warnings=[],
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("BN-001", mapped["batch_number"].value)

    def test_same_block_field_mapping_reads_value_on_next_line(self) -> None:
        template = self.template_definition()
        template["anchors"].append(
            {
                "anchor_id": "batch-number-label",
                "kind": "label",
                "text": "Batch No.",
                "match": "contains",
                "scope": {"page": 1, "block_types": ["paragraph"]},
            }
        )
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-number-label", "direction": "same_block"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            }
        ]
        document = self.document_with_blocks(paragraph_text="Batch No.\nBN-001")

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("BN-001", mapped["batch_number"].value)

    def test_field_mapping_rejects_conflicting_nested_output_paths(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch",
            },
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            },
        ]
        document = self.document_with_blocks(
            table_text=(
                "Yield Summary\tstep\texpected_yield\tactual_yield\n"
                "blend\t95\t94"
            )
        )

        result = apply_template_field_mapping(document, template)

        self.assertTrue(result.requires_review)
        self.assertEqual({"template_result": {}}, result.output)
        self.assertIn(
            "template output_key conflict between 'batch' and 'batch.actual_yield'; "
            "requires review",
            result.warnings,
        )
        self.assertTrue(all(field.requires_review for field in result.fields))

    def test_field_mapping_rejects_table_output_path_conflicts(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch",
            }
        ]
        template["output_mapping"] = {
            "format": "json",
            "root_key": "template_result",
            "field_map": [{"field_id": "batch_number", "output_key": "batch"}],
            "table_map": [{"table_id": "yield_summary", "output_key": "batch.yield_summary"}],
        }
        document = self.document_with_blocks()

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertTrue(result.requires_review)
        self.assertTrue(mapped["batch_number"].requires_review)
        self.assertEqual({"template_result": {}}, result.output)
        self.assertIn(
            "template output_key conflict between 'batch' and 'batch.yield_summary'; "
            "requires review",
            result.warnings,
        )

    def test_template_risk_review_levels_are_not_confirmed_in_output(self) -> None:
        template = self.template_definition()
        template["risk_rank"] = {"review_required_levels": ["high", "critical"]}
        template["fields"] = [
            {
                "field_id": "batch_number",
                "label": "Batch No.",
                "value_type": "string",
                "source": {"anchor_id": "batch-header", "direction": "below"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.number",
            }
        ]
        document = self.document_with_blocks()

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("BN-001", mapped["batch_number"].value)
        self.assertTrue(mapped["batch_number"].requires_review)
        self.assertEqual({"template_result": {}}, result.output)
        self.assertIn(
            "template field 'batch_number' risk_level 'high' requires review",
            result.warnings,
        )

    def test_table_cell_mapping_does_not_search_data_rows_for_optional_header(self) -> None:
        template = self.template_definition()
        template["tables"][0]["required_columns"] = ["step"]
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "Yield Summary\n"
                "step\texpected_yield\n"
                "actual_yield\tvariance\n"
                "94\t1"
            )
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertIsNone(mapped["actual_yield"].value)
        self.assertTrue(mapped["actual_yield"].requires_review)
        self.assertEqual({"template_result": {}}, result.output)

    def test_table_cell_mapping_skips_markdown_alignment_rows(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "Yield Summary\n"
                "| step | expected_yield | actual_yield |\n"
                "| :--- | ---: | ---: |\n"
                "| blend | 95 | 94 |"
            )
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("94", mapped["actual_yield"].value)
        self.assertEqual(3, mapped["actual_yield"].evidence["row_index"])

    def test_table_cell_mapping_does_not_offset_pipe_headers_by_outer_delimiters(self) -> None:
        template = self.template_definition()
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "| Yield Summary | step | expected_yield | actual_yield |\n"
                "| blend | 95 | 94 |"
            )
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("94", mapped["actual_yield"].value)
        self.assertEqual(2, mapped["actual_yield"].evidence["column_index"])

    def test_table_cell_mapping_skips_trimmed_repeated_headers_after_same_row_anchor(self) -> None:
        template = self.template_definition()
        template["tables"][0]["required_columns"] = ["step", "status"]
        template["fields"] = [
            {
                "field_id": "status",
                "label": "status",
                "value_type": "string",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "medium",
                "validation_rule_ids": [],
                "output_key": "batch.status",
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "Yield Summary\tstep\tstatus\n"
                "step\tstatus\n"
                "blend\treleased"
            )
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("released", mapped["status"].value)
        self.assertEqual(2, mapped["status"].evidence["row_index"])
        self.assertEqual(1, mapped["status"].evidence["column_index"])

    def test_table_cell_mapping_without_required_columns_does_not_promote_data_row_label(self) -> None:
        template = self.template_definition()
        template["tables"][0]["required_columns"] = []
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "Yield Summary\n"
                "step\texpected_yield\n"
                "actual_yield\tvariance\n"
                "94\t1"
            )
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertIsNone(mapped["actual_yield"].value)
        self.assertTrue(mapped["actual_yield"].requires_review)
        self.assertEqual({"template_result": {}}, result.output)

    def test_table_cell_mapping_ignores_wrapped_header_fragments_as_rows(self) -> None:
        template = self.template_definition()
        template["tables"][0]["required_columns"] = ["actual_yield"]
        template["fields"] = [
            {
                "field_id": "actual_yield",
                "label": "actual_yield",
                "value_type": "number",
                "source": {"anchor_id": "yield-table", "direction": "table_cell"},
                "required": True,
                "risk_level": "high",
                "validation_rule_ids": [],
                "output_key": "batch.actual_yield",
            }
        ]
        document = self.document_with_blocks(
            table_text=(
                "Yield Summary\n"
                "actual_yield\n"
                "blend\t94\n"
                "step\texpected_yield\tactual_yield\n"
                "blend\t95\t94"
            )
        )

        result = apply_template_field_mapping(document, template)
        mapped = {field.field_id: field for field in result.fields}

        self.assertEqual("94", mapped["actual_yield"].value)
        self.assertEqual(4, mapped["actual_yield"].evidence["row_index"])
        self.assertEqual(2, mapped["actual_yield"].evidence["column_index"])

    def template_definition(self) -> dict[str, Any]:
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
            "fields": [],
        }

    def document_with_blocks(
        self,
        heading_text: str | None = "Batch Production Record",
        paragraph_text: str | None = "Batch No. BN-001\nManufacturing Date 2026-01-01",
        table_text: str | None = "Yield Summary\nstep\texpected_yield\tactual_yield",
        *,
        pages: list[DocumentPage] | None = None,
        source_type: str = "pdf",
        table_review_warnings: list[str] | None = None,
        table_review_requires_review: bool | None = None,
    ) -> DocumentIRV1:
        blocks: list[DocumentBlock] = []
        if heading_text is not None:
            blocks.append(self.block("heading", heading_text))
        if paragraph_text is not None:
            blocks.append(self.block("paragraph", paragraph_text, y=120.0))
        if table_text is not None:
            blocks.append(
                self.block(
                    "table",
                    table_text,
                    y=180.0,
                    review_warnings=table_review_warnings,
                    review_requires_review=table_review_requires_review,
                )
            )
        return DocumentIRV1(
            schema_version="document-ir/v1",
            document=DocumentInfo(id="fixture", title="Fixture", source_type=source_type),
            pages=[DocumentPage(page_number=1, width=612.0, height=792.0)] if pages is None else pages,
            blocks=blocks,
            warnings=[],
        )

    def block(
        self,
        block_type: str,
        text: str,
        *,
        x: float = 72.0,
        y: float = 72.0,
        block_id: str | None = None,
        review_warnings: list[str] | None = None,
        review_requires_review: bool | None = None,
    ) -> DocumentBlock:
        warnings = review_warnings or []
        return DocumentBlock(
            id=block_id or f"{block_type}-{int(y)}",
            type=block_type,
            text=text,
            source_page=1,
            bbox=BoundingBox(x=x, y=y, width=180.0, height=24.0),
            extractor=ExtractorRef(name="fixture"),
            confidence=0.99,
            review=ReviewState(
                requires_review=bool(warnings)
                if review_requires_review is None
                else review_requires_review,
                warnings=warnings,
            ),
        )


if __name__ == "__main__":
    unittest.main()
