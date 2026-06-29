from __future__ import annotations

import math

from core.validate.automatic import (
    GMP_REVIEW_REQUIRED_CATEGORIES,
    ValidationStatus,
    validate_extracted_item,
    validate_table_consistency,
)


def _evidence() -> dict[str, object]:
    return {
        "source_page": 1,
        "bbox": {
            "x": 72.0,
            "y": 112.0,
            "width": 180.0,
            "height": 18.0,
        },
    }


def _expected_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": "gold-001",
        "label_id": "lot_number",
        "expected_value": "SAMPLE-LOT-001",
        "risk_level": "high",
        "requires_review": True,
        "evidence": _evidence(),
    }
    item.update(overrides)
    return item


def _actual_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": "gold-001",
        "label_id": "lot_number",
        "value": "SAMPLE-LOT-001",
        "auto_confirmed": False,
        "confidence": 0.95,
        "evidence": _evidence(),
    }
    item.update(overrides)
    return item


def test_high_risk_item_cannot_be_auto_confirmed_even_when_value_matches() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(),
        actual=_actual_item(auto_confirmed=True),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_changed_value_fails_closed_and_requires_review() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(),
        actual=_actual_item(value="SAMPLE-LOT-002"),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "value_non_modification" in decision.failed_rules


def test_missing_or_mismatched_provenance_blocks_auto_confirm() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(),
        actual=_actual_item(evidence={"source_page": 1}),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "provenance" in decision.failed_rules


def test_low_confidence_item_cannot_be_auto_confirmed_even_with_matching_source() -> None:
    expected = _expected_item(
        risk_level="medium",
        requires_review=False,
        fixture_id="fixture-001",
        document_id="doc-001",
        block_id="block-001",
    )

    decision = validate_extracted_item(
        expected=expected,
        actual=_actual_item(
            auto_confirmed=True,
            confidence=0.42,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
    assert "item confidence requires human review" in decision.warnings


def test_low_confidence_item_does_not_skip_mandatory_scope_binding() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="summary_note",
            expected_value="Reviewed note",
            risk_level="medium",
            requires_review=False,
        ),
        actual=_actual_item(
            label_id="summary_note",
            value="Reviewed note",
            confidence=0.42,
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "scope_binding" in decision.failed_rules
    assert "item confidence requires human review" in decision.warnings


def test_item_scope_binding_requires_matching_document_and_block_ids() -> None:
    expected = _expected_item(
        risk_level="medium",
        requires_review=False,
        document_id="doc-001",
        block_id="block-001",
    )

    missing_scope = validate_extracted_item(expected=expected, actual=_actual_item())
    wrong_scope = validate_extracted_item(
        expected=expected,
        actual=_actual_item(document_id="doc-001", block_id="block-002"),
    )

    assert missing_scope.auto_confirm_allowed is False
    assert missing_scope.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "scope_binding" in missing_scope.failed_rules
    assert wrong_scope.auto_confirm_allowed is False
    assert wrong_scope.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "scope_binding" in wrong_scope.failed_rules


def test_item_scope_binding_rejects_whitespace_only_scope_identifiers() -> None:
    expected = _expected_item(
        risk_level="medium",
        requires_review=False,
        document_id=" ",
        block_id="block-001",
    )

    decision = validate_extracted_item(
        expected=expected,
        actual=_actual_item(document_id=" ", block_id="block-001"),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "scope_binding" in decision.failed_rules


def test_item_scope_binding_requires_matching_fixture_id() -> None:
    expected = _expected_item(
        risk_level="medium",
        requires_review=False,
        fixture_id="fixture-001",
        document_id="doc-001",
        block_id="block-001",
    )

    decision = validate_extracted_item(
        expected=expected,
        actual=_actual_item(
            fixture_id="fixture-002",
            document_id="doc-001",
            block_id="block-001",
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "scope_binding" in decision.failed_rules


def test_missing_expected_item_fixture_id_blocks_auto_confirm() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="summary_note",
            expected_value="Reviewed note",
            risk_level="medium",
            requires_review=False,
        ),
        actual=_actual_item(label_id="summary_note", value="Reviewed note"),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "scope_binding" in decision.failed_rules


def test_missing_expected_item_document_or_block_scope_blocks_auto_confirm() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="summary_note",
            expected_value="Reviewed note",
            risk_level="medium",
            requires_review=False,
            fixture_id="fixture-001",
        ),
        actual=_actual_item(
            label_id="summary_note",
            value="Reviewed note",
            fixture_id="fixture-001",
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "scope_binding" in decision.failed_rules


def test_numeric_rule_rejects_non_finite_or_stringified_numbers() -> None:
    expected = _expected_item(expected_value=12.5, risk_level="medium", requires_review=False)

    stringified = validate_extracted_item(expected=expected, actual=_actual_item(value="12.5"))
    non_finite = validate_extracted_item(expected=expected, actual=_actual_item(value=math.nan))

    assert stringified.auto_confirm_allowed is False
    assert non_finite.auto_confirm_allowed is False
    assert "numeric" in stringified.failed_rules
    assert "numeric" in non_finite.failed_rules


def test_numeric_rule_blocks_oversized_integer_without_crashing() -> None:
    expected = _expected_item(expected_value=1, risk_level="medium", requires_review=False)

    decision = validate_extracted_item(
        expected=expected,
        actual=_actual_item(value=10**400),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "numeric" in decision.failed_rules


def test_string_value_rejects_numeric_actual_value() -> None:
    expected = _expected_item(
        expected_value="123",
        risk_level="medium",
        requires_review=False,
    )

    decision = validate_extracted_item(expected=expected, actual=_actual_item(value=123))

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "value_non_modification" in decision.failed_rules


def test_actual_review_flag_blocks_item_auto_confirm_without_failures() -> None:
    expected = _expected_item(risk_level="medium", requires_review=False)

    decision = validate_extracted_item(
        expected=expected,
        actual=_actual_item(requires_review=True, auto_confirmed=False),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.REQUIRES_REVIEW
    assert decision.requires_review is True
    assert decision.failed_rules == ()


def test_actual_high_risk_item_blocks_auto_confirm() -> None:
    expected = _expected_item(risk_level="medium", requires_review=False)

    decision = validate_extracted_item(
        expected=expected,
        actual=_actual_item(risk_level="high", auto_confirmed=True),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_critical_risk_item_cannot_be_auto_confirmed() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(risk_level="critical", requires_review=False),
        actual=_actual_item(auto_confirmed=True),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_gmp_review_required_categories_are_explicit_and_not_auto_confirmed() -> None:
    assert GMP_REVIEW_REQUIRED_CATEGORIES == frozenset(
        {
            "lot_number",
            "item",
            "date_time",
            "numeric_value",
            "specification",
            "judgment",
            "person",
            "correction",
            "deviation",
        }
    )

    for category in sorted(GMP_REVIEW_REQUIRED_CATEGORIES):
        decision = validate_extracted_item(
            expected=_expected_item(
                risk_level="medium",
                requires_review=False,
                gmp_review_category=category,
            ),
            actual=_actual_item(auto_confirmed=True),
        )

        assert decision.auto_confirm_allowed is False, category
        assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM, category
        assert decision.requires_review is True, category
        assert "risk_gate" in decision.failed_rules, category


def test_suffixed_gmp_category_aliases_are_not_auto_confirmed() -> None:
    cases = (
        ("release_status_1", "Approved"),
        ("Batch No. 1", "SAMPLE-LOT-001"),
        ("prepared_by_2", "Alex Reviewer"),
    )

    for label_id, value in cases:
        decision = validate_extracted_item(
            expected=_expected_item(
                label_id=label_id,
                expected_value=value,
                risk_level="medium",
                requires_review=False,
            ),
            actual=_actual_item(
                label_id=label_id,
                value=value,
                auto_confirmed=True,
            ),
        )

        assert decision.auto_confirm_allowed is False, label_id
        assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM, label_id
        assert decision.requires_review is True, label_id
        assert "risk_gate" in decision.failed_rules, label_id


def test_camel_case_gmp_aliases_are_not_auto_confirmed() -> None:
    cases = (
        ("batchLotNumber", "SAMPLE-LOT-001"),
        ("preparedBy", "Alex Reviewer"),
        ("releaseStatus", "Approved"),
    )

    for label_id, value in cases:
        decision = validate_extracted_item(
            expected=_expected_item(
                label_id=label_id,
                expected_value=value,
                risk_level="medium",
                requires_review=False,
            ),
            actual=_actual_item(
                label_id=label_id,
                value=value,
                auto_confirmed=True,
            ),
        )

        assert decision.auto_confirm_allowed is False, label_id
        assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM, label_id
        assert decision.requires_review is True, label_id
        assert "risk_gate" in decision.failed_rules, label_id


def test_invalid_explicit_gmp_category_blocks_auto_confirm() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(
            risk_level="medium",
            requires_review=False,
            gmp_review_category="not-a-gmp-category",
        ),
        actual=_actual_item(auto_confirmed=True),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_gmp_review_required_conditions_are_not_auto_confirmed() -> None:
    expected = _expected_item(
        risk_level="medium",
        requires_review=False,
        gmp_review_category="lot_number",
        extraction_engine="template-v1",
    )

    cases = (
        ("low_confidence", _actual_item(auto_confirmed=True, confidence=0.79)),
        ("ocr_derived", _actual_item(auto_confirmed=True, extraction_method="ocr")),
        (
            "engine_mismatch",
            _actual_item(auto_confirmed=True, extraction_engine="llm-repair-v1"),
        ),
        ("missing_source", _actual_item(auto_confirmed=True, evidence={})),
        ("llm_involved", _actual_item(auto_confirmed=True, llm_involved=True)),
    )

    for name, actual in cases:
        decision = validate_extracted_item(expected=expected, actual=actual)

        assert decision.auto_confirm_allowed is False, name
        assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM, name
        assert decision.requires_review is True, name
        assert "risk_gate" in decision.failed_rules, name


def test_ocr_region_engine_marks_item_as_ocr_derived() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="summary_note",
            expected_value="Reviewed note",
            risk_level="medium",
            requires_review=False,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
        ),
        actual=_actual_item(
            label_id="summary_note",
            value="Reviewed note",
            auto_confirmed=True,
            engine="fake-tesseract",
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
    assert "ocr-derived item requires human review" in decision.warnings


def test_scanned_pdf_ocr_source_kind_marks_item_as_ocr_derived() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="summary_note",
            expected_value="Reviewed note",
            risk_level="medium",
            requires_review=False,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
        ),
        actual=_actual_item(
            label_id="summary_note",
            value="Reviewed note",
            auto_confirmed=True,
            source_kind="scanned_pdf_ocr",
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
    assert "ocr-derived item requires human review" in decision.warnings


def test_scanned_pdf_source_type_marks_item_as_ocr_derived() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="summary_note",
            expected_value="Reviewed note",
            risk_level="medium",
            requires_review=False,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
        ),
        actual=_actual_item(
            label_id="summary_note",
            value="Reviewed note",
            auto_confirmed=True,
            source_type="scanned_pdf",
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
    assert "ocr-derived item requires human review" in decision.warnings


def test_nested_extractor_metadata_mismatch_blocks_item_auto_confirm() -> None:
    source = _evidence()
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="summary_note",
            expected_value="Reviewed note",
            risk_level="medium",
            requires_review=False,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            evidence=source,
            value_metadata={"extractor": {"name": "pymupdf-text"}},
        ),
        actual=_actual_item(
            label_id="summary_note",
            value="Reviewed note",
            auto_confirmed=True,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            evidence=source,
            value_metadata={"extractor": {"name": "pymupdf-text-table-heuristic"}},
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
    assert "extraction engine mismatch requires human review" in decision.warnings


def test_common_date_label_id_aliases_require_review() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="manufacturing_date",
            expected_value="2026-01-01",
            risk_level="medium",
            requires_review=False,
        ),
        actual=_actual_item(
            label_id="manufacturing_date",
            value="2026-01-01",
            auto_confirmed=True,
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_common_lot_label_aliases_require_review() -> None:
    for label_id in ("Lot No.", "Lot ID", "batch.lot_number"):
        decision = validate_extracted_item(
            expected=_expected_item(
                label_id=label_id,
                expected_value="SAMPLE-LOT-001",
                risk_level="medium",
                requires_review=False,
            ),
            actual=_actual_item(
                label_id=label_id,
                value="SAMPLE-LOT-001",
                auto_confirmed=True,
            ),
        )

        assert decision.auto_confirm_allowed is False, label_id
        assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM, label_id
        assert decision.requires_review is True, label_id
        assert "risk_gate" in decision.failed_rules, label_id


def test_numeric_value_type_requires_review() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="actual_yield",
            expected_value="98.5",
            risk_level="medium",
            requires_review=False,
            value_type="number",
        ),
        actual=_actual_item(
            label_id="actual_yield",
            value="98.5",
            auto_confirmed=True,
            value_type="number",
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_date_value_type_requires_review() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="effective_value",
            expected_value="2026-01-01",
            risk_level="medium",
            requires_review=False,
            value_type="date",
        ),
        actual=_actual_item(
            label_id="effective_value",
            value="2026-01-01",
            auto_confirmed=True,
            value_type="date",
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_status_label_id_alias_requires_review_as_judgment() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="release_status",
            expected_value="Approved",
            risk_level="medium",
            requires_review=False,
        ),
        actual=_actual_item(
            label_id="release_status",
            value="Approved",
            auto_confirmed=True,
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_status_field_id_alias_requires_review_as_judgment() -> None:
    scope = {
        "fixture_id": "fixture-001",
        "document_id": "doc-001",
        "block_id": "block-001",
    }
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="release-template-field",
            field_id="release_status",
            label="Status",
            expected_value="Approved",
            risk_level="medium",
            requires_review=False,
            **scope,
        ),
        actual=_actual_item(
            label_id="release-template-field",
            field_id="release_status",
            label="Status",
            value="Approved",
            auto_confirmed=True,
            **scope,
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_category_required_item_without_scope_is_review_only() -> None:
    cases = (
        ("judgment_alias", {"label_id": "release_status"}, "Approved"),
        ("lot_alias", {"label_id": "lot_number"}, "SAMPLE-LOT-001"),
        ("explicit_category", {"gmp_review_category": "lot_number"}, "SAMPLE-LOT-001"),
    )

    for case_name, metadata, value in cases:
        decision = validate_extracted_item(
            expected=_expected_item(
                expected_value=value,
                risk_level="medium",
                requires_review=False,
                **metadata,
            ),
            actual=_actual_item(
                value=value,
                auto_confirmed=False,
                **metadata,
            ),
        )

        assert decision.auto_confirm_allowed is False, case_name
        assert decision.status is ValidationStatus.REQUIRES_REVIEW, case_name
        assert decision.requires_review is True, case_name
        assert decision.failed_rules == (), case_name


def test_value_metadata_source_comparison_ignores_non_source_fields() -> None:
    source = _evidence()
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="summary_note",
            expected_value="Reviewed note",
            risk_level="medium",
            requires_review=False,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            value_metadata={
                "source_page": source["source_page"],
                "bbox": source["bbox"],
                "extractor": {"name": "pymupdf-text"},
                "confidence_model": "baseline",
            },
        ),
        actual=_actual_item(
            label_id="summary_note",
            value="Reviewed note",
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            value_metadata={
                "source_page": source["source_page"],
                "bbox": source["bbox"],
                "extractor": {"name": "pymupdf-text"},
                "confidence_model": "rerun",
            },
        ),
    )

    assert decision.auto_confirm_allowed is True
    assert decision.status is ValidationStatus.PASS
    assert "provenance" not in decision.failed_rules


def test_value_metadata_extractor_mismatch_blocks_without_provenance_failure() -> None:
    source = _evidence()
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="summary_note",
            expected_value="Reviewed note",
            risk_level="medium",
            requires_review=False,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            value_metadata={
                "source_page": source["source_page"],
                "bbox": source["bbox"],
                "extractor": {"name": "pymupdf-text"},
            },
        ),
        actual=_actual_item(
            label_id="summary_note",
            value="Reviewed note",
            auto_confirmed=True,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            value_metadata={
                "source_page": source["source_page"],
                "bbox": source["bbox"],
                "extractor": {"name": "pymupdf-text-table-heuristic"},
            },
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert decision.failed_rules == ("risk_gate",)
    assert "extraction engine mismatch requires human review" in decision.warnings


def test_actual_only_extraction_engine_does_not_create_mismatch() -> None:
    source = _evidence()
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="summary_note",
            expected_value="Reviewed note",
            risk_level="medium",
            requires_review=False,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            evidence=source,
        ),
        actual=_actual_item(
            label_id="summary_note",
            value="Reviewed note",
            auto_confirmed=True,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            evidence=source,
            extraction_engine="pymupdf-text",
        ),
    )

    assert decision.auto_confirm_allowed is True
    assert decision.status is ValidationStatus.PASS
    assert decision.requires_review is False
    assert decision.failed_rules == ()
    assert "extraction engine mismatch requires human review" not in decision.warnings


def test_generic_non_ocr_engine_does_not_mark_item_ocr_derived() -> None:
    source = _evidence()
    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="summary_note",
            expected_value="Reviewed note",
            risk_level="medium",
            requires_review=False,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            evidence=source,
        ),
        actual=_actual_item(
            label_id="summary_note",
            value="Reviewed note",
            auto_confirmed=True,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            evidence=source,
            engine="pymupdf-text",
        ),
    )

    assert decision.auto_confirm_allowed is True
    assert decision.status is ValidationStatus.PASS
    assert decision.requires_review is False
    assert decision.failed_rules == ()
    assert "ocr-derived item requires human review" not in decision.warnings


def test_malformed_item_risk_level_blocks_auto_confirm() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(risk_level="todo", requires_review=False),
        actual=_actual_item(auto_confirmed=False),
    )
    actual_malformed = validate_extracted_item(
        expected=_expected_item(risk_level="medium", requires_review=False),
        actual=_actual_item(risk_level=True, auto_confirmed=False),
    )
    actual_unhashable = validate_extracted_item(
        expected=_expected_item(risk_level="medium", requires_review=False),
        actual=_actual_item(risk_level=["high"], auto_confirmed=False),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
    assert actual_malformed.auto_confirm_allowed is False
    assert actual_malformed.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "risk_gate" in actual_malformed.failed_rules
    assert actual_unhashable.auto_confirm_allowed is False
    assert actual_unhashable.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "risk_gate" in actual_unhashable.failed_rules


def test_missing_expected_item_risk_level_blocks_auto_confirm() -> None:
    expected = _expected_item(requires_review=False)
    del expected["risk_level"]

    decision = validate_extracted_item(expected=expected, actual=_actual_item())

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_malformed_item_review_flag_blocks_auto_confirm() -> None:
    expected = _expected_item(risk_level="medium", requires_review="true")

    decision = validate_extracted_item(expected=expected, actual=_actual_item())
    actual_malformed = validate_extracted_item(
        expected=_expected_item(risk_level="medium", requires_review=False),
        actual=_actual_item(requires_review="true"),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
    assert actual_malformed.auto_confirm_allowed is False
    assert actual_malformed.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "risk_gate" in actual_malformed.failed_rules


def test_null_item_review_flag_blocks_auto_confirm() -> None:
    expected_null = validate_extracted_item(
        expected=_expected_item(risk_level="medium", requires_review=None),
        actual=_actual_item(),
    )
    actual_null = validate_extracted_item(
        expected=_expected_item(risk_level="medium", requires_review=False),
        actual=_actual_item(requires_review=None),
    )

    assert expected_null.auto_confirm_allowed is False
    assert expected_null.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert expected_null.requires_review is True
    assert "risk_gate" in expected_null.failed_rules
    assert actual_null.auto_confirm_allowed is False
    assert actual_null.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "risk_gate" in actual_null.failed_rules


def test_missing_expected_item_review_flag_blocks_auto_confirm() -> None:
    expected = _expected_item(risk_level="medium")
    del expected["requires_review"]

    decision = validate_extracted_item(expected=expected, actual=_actual_item())

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_blank_expected_item_value_blocks_auto_confirm() -> None:
    decision = validate_extracted_item(
        expected=_expected_item(
            expected_value=" ",
            risk_level="medium",
            requires_review=False,
        ),
        actual=_actual_item(value=""),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "value_non_modification" in decision.failed_rules


def test_matching_negative_source_coordinates_block_auto_confirm() -> None:
    off_page_source = {
        "source_page": 1,
        "bbox": {"x": -1.0, "y": 112.0, "width": 180.0, "height": 18.0},
    }
    expected = _expected_item(
        risk_level="medium",
        requires_review=False,
        evidence=off_page_source,
    )

    decision = validate_extracted_item(
        expected=expected,
        actual=_actual_item(evidence=off_page_source),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "provenance" in decision.failed_rules


def test_non_top_left_source_origin_blocks_auto_confirm() -> None:
    bottom_left_source = {
        "source_page": 1,
        "bbox": {
            "origin": "bottom_left",
            "x": 72.0,
            "y": 112.0,
            "width": 180.0,
            "height": 18.0,
        },
    }
    expected = _expected_item(
        risk_level="medium",
        requires_review=False,
        fixture_id="fixture-001",
        document_id="doc-001",
        block_id="block-001",
        evidence=bottom_left_source,
    )

    decision = validate_extracted_item(
        expected=expected,
        actual=_actual_item(
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            evidence=bottom_left_source,
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "provenance" in decision.failed_rules


def test_documented_top_left_source_origin_allows_auto_confirm() -> None:
    top_left_source = {
        "source_page": 1,
        "bbox": {
            "origin": "top-left",
            "unit": "pt",
            "x": 72.0,
            "y": 112.0,
            "width": 180.0,
            "height": 18.0,
        },
    }

    decision = validate_extracted_item(
        expected=_expected_item(
            label_id="summary_note",
            risk_level="medium",
            requires_review=False,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            evidence=top_left_source,
        ),
        actual=_actual_item(
            label_id="summary_note",
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            evidence=top_left_source,
        ),
    )

    assert decision.auto_confirm_allowed is True
    assert decision.status is ValidationStatus.PASS
    assert "provenance" not in decision.failed_rules


def test_unsupported_source_bbox_unit_blocks_auto_confirm() -> None:
    unsupported_unit_source = {
        "source_page": 1,
        "bbox": {
            "origin": "top-left",
            "unit": "inch",
            "x": 72.0,
            "y": 112.0,
            "width": 180.0,
            "height": 18.0,
        },
    }

    decision = validate_extracted_item(
        expected=_expected_item(
            risk_level="medium",
            requires_review=False,
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            evidence=unsupported_unit_source,
        ),
        actual=_actual_item(
            fixture_id="fixture-001",
            document_id="doc-001",
            block_id="block-001",
            evidence=unsupported_unit_source,
        ),
    )

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "provenance" in decision.failed_rules


def test_table_consistency_rejects_missing_cells_and_wrong_text() -> None:
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "cells": [
            {"id": "table-001-r1-c1", "text": "Lot number", "requires_review": False},
            {
                "id": "table-001-r1-c2",
                "text": "SAMPLE-LOT-001",
                "requires_review": False,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {"id": "table-001-r1-c1", "text": "Lot number changed"},
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "table_consistency" in decision.failed_rules
    assert "provenance" in decision.failed_rules
    assert decision.requires_review is True


def test_table_cells_enforce_provenance_and_review_gates() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "risk_level": "medium",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": True,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": {"source_page": 1},
                "auto_confirmed": True,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "provenance" in decision.failed_rules
    assert "risk_gate" in decision.failed_rules


def test_table_cells_apply_gmp_condition_gates() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "Reviewed note",
                "source": source,
                "requires_review": False,
                "risk_level": "medium",
                "extraction_engine": "template-v1",
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "Reviewed note",
                "source": source,
                "auto_confirmed": True,
                "engine": "fake-tesseract",
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
    assert "table cell ocr-derived item requires human review" in decision.warnings
    assert (
        "table cell extraction engine mismatch requires human review"
        in decision.warnings
    )


def test_table_level_metadata_applies_gmp_condition_gates_to_cells() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "risk_level": "medium",
        "source_type": "scanned_pdf",
        "extraction_engine": "template-v1",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "Reviewed note",
                "source": source,
                "requires_review": False,
                "risk_level": "low",
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "source_type": "scanned_pdf",
        "extraction_engine": "llm-repair-v1",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "Reviewed note",
                "source": source,
                "confidence": 0.95,
                "auto_confirmed": True,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
    assert "table cell ocr-derived item requires human review" in decision.warnings
    assert (
        "table cell extraction engine mismatch requires human review"
        in decision.warnings
    )


def test_table_level_risk_requires_review_for_matching_cells() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "risk_level": "high",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": False,
                "risk_level": "medium",
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "auto_confirmed": True,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_table_level_requires_review_blocks_auto_confirmed_cells() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "requires_review": True,
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "Reviewed note",
                "source": source,
                "requires_review": False,
                "risk_level": "low",
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "Reviewed note",
                "source": source,
                "auto_confirmed": True,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_malformed_table_risk_level_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "risk_level": "urgent",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "Reviewed note",
                "source": source,
                "requires_review": False,
                "risk_level": "low",
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "Reviewed note",
                "source": source,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)
    expected_table["risk_level"] = ["high"]
    unhashable_decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
    assert unhashable_decision.auto_confirm_allowed is False
    assert unhashable_decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert unhashable_decision.requires_review is True
    assert "risk_gate" in unhashable_decision.failed_rules


def test_table_required_columns_require_cell_review() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "risk_level": "medium",
        "required_columns": ["check", "result", "reviewer"],
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "Identity",
                "source": source,
                "requires_review": False,
                "risk_level": "low",
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "Identity",
                "source": source,
                "auto_confirmed": True,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_malformed_table_required_columns_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "risk_level": "medium",
        "required_columns": "result",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "Identity",
                "source": source,
                "requires_review": False,
                "risk_level": "low",
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "Identity",
                "source": source,
                "auto_confirmed": True,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)
    expected_table["required_columns"] = []
    empty_columns_decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
    assert empty_columns_decision.auto_confirm_allowed is False
    assert empty_columns_decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert empty_columns_decision.requires_review is True
    assert "risk_gate" in empty_columns_decision.failed_rules


def test_table_cell_missing_expected_source_blocks_auto_confirm() -> None:
    expected_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "requires_review": False,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": _evidence(),
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "provenance" in decision.failed_rules


def test_table_cell_requires_review_blocks_auto_confirm_even_without_failures() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "risk_level": "medium",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": True,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.REQUIRES_REVIEW
    assert decision.requires_review is True
    assert decision.failed_rules == ()


def test_actual_table_cell_requires_review_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "risk_level": "medium",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": False,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": True,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.REQUIRES_REVIEW
    assert decision.requires_review is True
    assert decision.failed_rules == ()


def test_malformed_table_cell_review_flag_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": "true",
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_null_actual_table_cell_review_flag_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": False,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": None,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_malformed_table_cell_risk_level_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": False,
                "risk_level": "todo",
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_actual_table_cell_malformed_gmp_category_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "risk_level": "medium",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": False,
                "risk_level": "low",
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "gmp_review_category": "not-a-gmp-category",
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules


def test_table_cell_text_rejects_numeric_actual_value() -> None:
    expected_table = {
        "id": "table-001",
        "cells": [
            {"id": "table-001-r1-c1", "text": "123", "requires_review": False},
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {"id": "table-001-r1-c1", "text": 123},
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert "table_consistency" in decision.failed_rules


def test_empty_expected_table_cells_block_auto_confirm() -> None:
    expected_table = {"id": "table-001", "cells": []}
    actual_table = {"id": "table-001", "cells": []}

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "table_consistency" in decision.failed_rules


def test_whitespace_only_table_cell_id_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-001",
        "cells": [
            {
                "id": "   ",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": False,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "   ",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "table_consistency" in decision.failed_rules


def test_mismatched_expected_table_fixture_binding_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "fixture_table_id": "table-002",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "requires_review": False,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "table_consistency" in decision.failed_rules


def test_blank_table_cell_text_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "  ",
                "source": source,
                "requires_review": False,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "",
                "source": source,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "table_consistency" in decision.failed_rules


def test_current_head_review_examples_fail_closed() -> None:
    source = _evidence()
    expected_scoped_item = _expected_item(
        risk_level="medium",
        requires_review=False,
        document_id="doc-001",
        block_id="block-001",
    )

    cases = [
        (
            "document_block_scope",
            validate_extracted_item(
                expected=expected_scoped_item,
                actual=_actual_item(document_id="doc-002", block_id="block-001"),
            ),
            "scope_binding",
        ),
        (
            "empty_expected_table_cells",
            validate_table_consistency(
                {"id": "table-001", "cells": []},
                {"id": "table-001", "cells": []},
            ),
            "table_consistency",
        ),
        (
            "blank_table_cell_text",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": " ",
                            "source": source,
                            "requires_review": False,
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "",
                            "source": source,
                        },
                    ],
                },
            ),
            "table_consistency",
        ),
        (
            "missing_expected_cell_review_flag",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                        },
                    ],
                },
            ),
            "risk_gate",
        ),
        (
            "fixture_scope",
            validate_extracted_item(
                expected=_expected_item(
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    fixture_id="fixture-002",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "scope_binding",
        ),
        (
            "missing_fixture_table_id",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                            "requires_review": False,
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                        },
                    ],
                },
            ),
            "table_consistency",
        ),
        (
            "missing_fixture_scope",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="summary_note",
                    expected_value="Reviewed note",
                    risk_level="medium",
                    requires_review=False,
                ),
                actual=_actual_item(label_id="summary_note", value="Reviewed note"),
            ),
            "scope_binding",
        ),
        (
            "missing_document_block_scope",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="summary_note",
                    expected_value="Reviewed note",
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                ),
                actual=_actual_item(
                    label_id="summary_note",
                    value="Reviewed note",
                    fixture_id="fixture-001",
                ),
            ),
            "scope_binding",
        ),
        (
            "whitespace_only_table_cell_id",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "fixture_table_id": "table-001",
                    "cells": [
                        {
                            "id": " ",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                            "requires_review": False,
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": " ",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                        },
                    ],
                },
            ),
            "table_consistency",
        ),
        (
            "non_top_left_source_origin",
            validate_extracted_item(
                expected=_expected_item(
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                    evidence={
                        "source_page": 1,
                        "bbox": {
                            "origin": "bottom_left",
                            "x": 72.0,
                            "y": 112.0,
                            "width": 180.0,
                            "height": 18.0,
                        },
                    },
                ),
                actual=_actual_item(
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                    evidence={
                        "source_page": 1,
                        "bbox": {
                            "origin": "bottom_left",
                            "x": 72.0,
                            "y": 112.0,
                            "width": 180.0,
                            "height": 18.0,
                        },
                    },
                ),
            ),
            "provenance",
        ),
        (
            "unsupported_bbox_unit",
            validate_extracted_item(
                expected=_expected_item(
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                    evidence={
                        "source_page": 1,
                        "bbox": {
                            "origin": "top-left",
                            "unit": "inch",
                            "x": 72.0,
                            "y": 112.0,
                            "width": 180.0,
                            "height": 18.0,
                        },
                    },
                ),
                actual=_actual_item(
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                    evidence={
                        "source_page": 1,
                        "bbox": {
                            "origin": "top-left",
                            "unit": "inch",
                            "x": 72.0,
                            "y": 112.0,
                            "width": 180.0,
                            "height": 18.0,
                        },
                    },
                ),
            ),
            "provenance",
        ),
        (
            "mismatched_fixture_table_id",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "fixture_table_id": "table-002",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                            "requires_review": False,
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "SAMPLE-LOT-001",
                            "source": source,
                        },
                    ],
                },
            ),
            "table_consistency",
        ),
        (
            "missing_expected_item_review_flag",
            validate_extracted_item(
                expected={
                    "id": "gold-001",
                    "label_id": "lot_number",
                    "expected_value": "SAMPLE-LOT-001",
                    "risk_level": "medium",
                    "evidence": source,
                },
                actual=_actual_item(),
            ),
            "risk_gate",
        ),
        (
            "blank_expected_item_value",
            validate_extracted_item(
                expected=_expected_item(
                    expected_value=" ",
                    risk_level="medium",
                    requires_review=False,
                ),
                actual=_actual_item(value=""),
            ),
            "value_non_modification",
        ),
        (
            "malformed_risk_level",
            validate_extracted_item(
                expected=_expected_item(risk_level="todo", requires_review=False),
                actual=_actual_item(),
            ),
            "risk_gate",
        ),
        (
            "missing_expected_risk_level",
            validate_extracted_item(
                expected={
                    "id": "gold-001",
                    "label_id": "lot_number",
                    "expected_value": "SAMPLE-LOT-001",
                    "requires_review": False,
                    "evidence": source,
                },
                actual=_actual_item(),
            ),
            "risk_gate",
        ),
        (
            "whitespace_only_scope_identifier",
            validate_extracted_item(
                expected=_expected_item(
                    risk_level="medium",
                    requires_review=False,
                    document_id=" ",
                    block_id="block-001",
                ),
                actual=_actual_item(document_id=" ", block_id="block-001"),
            ),
            "scope_binding",
        ),
        (
            "current_thread_release_status_field_id",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="release-template-field",
                    field_id="release_status",
                    label="Status",
                    expected_value="Approved",
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    label_id="release-template-field",
                    field_id="release_status",
                    label="Status",
                    value="Approved",
                    auto_confirmed=True,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "risk_gate",
        ),
        (
            "current_thread_release_status_with_qualifier_and_suffix",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="release-template-field",
                    field_id="final_release_status_code",
                    label="Status",
                    expected_value="Approved",
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    label_id="release-template-field",
                    field_id="final_release_status_code",
                    label="Status",
                    value="Approved",
                    auto_confirmed=True,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "risk_gate",
        ),
        (
            "current_thread_prepared_by_label_id",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="prepared_by",
                    label="Prepared By",
                    expected_value="Alex Reviewer",
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    label_id="prepared_by",
                    label="Prepared By",
                    value="Alex Reviewer",
                    auto_confirmed=True,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "risk_gate",
        ),
        (
            "current_thread_qualified_person_alias_label_id",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="qa_reviewer_name",
                    label="QA Reviewer",
                    expected_value="Alex Reviewer",
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    label_id="qa_reviewer_name",
                    label="QA Reviewer",
                    value="Alex Reviewer",
                    auto_confirmed=True,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "risk_gate",
        ),
        (
            "current_thread_date_value_type",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="generic-field",
                    expected_value="2026-01-01",
                    risk_level="medium",
                    requires_review=False,
                    value_type="date",
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    label_id="generic-field",
                    value="2026-01-01",
                    auto_confirmed=True,
                    value_type="date",
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "risk_gate",
        ),
        (
            "current_thread_table_requires_review",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "fixture_table_id": "table-001",
                    "requires_review": True,
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "Reviewed note",
                            "source": source,
                            "requires_review": False,
                            "risk_level": "low",
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "Reviewed note",
                            "source": source,
                            "auto_confirmed": True,
                        },
                    ],
                },
            ),
            "risk_gate",
        ),
        (
            "current_thread_table_auto_confirmed_review_table",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "fixture_table_id": "table-001",
                    "requires_review": True,
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "Reviewed note",
                            "source": source,
                            "requires_review": False,
                            "risk_level": "low",
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "auto_confirmed": True,
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "Reviewed note",
                            "source": source,
                            "auto_confirmed": False,
                        },
                    ],
                },
            ),
            "risk_gate",
        ),
        (
            "current_thread_malformed_table_risk_level",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "fixture_table_id": "table-001",
                    "risk_level": True,
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "Reviewed note",
                            "source": source,
                            "requires_review": False,
                            "risk_level": "low",
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "Reviewed note",
                            "source": source,
                            "auto_confirmed": False,
                        },
                    ],
                },
            ),
            "risk_gate",
        ),
        (
            "current_thread_numeric_required_column",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "fixture_table_id": "table-001",
                    "risk_level": "medium",
                    "required_columns": ["final_actual_yield_value"],
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "98.5",
                            "source": source,
                            "requires_review": False,
                            "risk_level": "low",
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "98.5",
                            "source": source,
                            "auto_confirmed": True,
                        },
                    ],
                },
            ),
            "risk_gate",
        ),
        (
            "current_thread_numeric_typed_value",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="generic-field",
                    expected_value=98.5,
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    label_id="generic-field",
                    value=98.5,
                    auto_confirmed=True,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "risk_gate",
        ),
        (
            "current_thread_numeric_actual_value",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="generic-field",
                    expected_value="98.5",
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    label_id="generic-field",
                    value=98.5,
                    auto_confirmed=True,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "risk_gate",
        ),
        (
            "current_thread_string_extractor_mismatch",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="summary_note",
                    expected_value="Reviewed note",
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                    extractor="pymupdf",
                ),
                actual=_actual_item(
                    label_id="summary_note",
                    value="Reviewed note",
                    auto_confirmed=True,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                    extractor="llm-repair-v1",
                ),
            ),
            "risk_gate",
        ),
        (
            "current_thread_lot_alias_label_id",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="Batch No.",
                    expected_value="SAMPLE-LOT-001",
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    label_id="Batch No.",
                    value="SAMPLE-LOT-001",
                    auto_confirmed=True,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "risk_gate",
        ),
        (
            "current_thread_deviation_alias_with_suffix",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="qa_deviation_id_value",
                    expected_value="DEV-001",
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    label_id="qa_deviation_id_value",
                    value="DEV-001",
                    auto_confirmed=True,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "risk_gate",
        ),
        (
            "current_thread_missing_table_risk_level",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "fixture_table_id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "Reviewed note",
                            "source": source,
                            "requires_review": False,
                            "risk_level": "low",
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "Reviewed note",
                            "source": source,
                            "confidence": 0.95,
                            "auto_confirmed": True,
                        },
                    ],
                },
            ),
            "risk_gate",
        ),
        (
            "current_thread_slash_separated_category",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="generic-field",
                    expected_value="2026-01-01",
                    risk_level="medium",
                    requires_review=False,
                    gmp_review_category="date/time",
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    label_id="generic-field",
                    value="2026-01-01",
                    auto_confirmed=True,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "risk_gate",
        ),
        (
            "current_thread_malformed_table_cell_gmp_category",
            validate_table_consistency(
                {
                    "id": "table-001",
                    "fixture_table_id": "table-001",
                    "risk_level": "medium",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "Reviewed note",
                            "source": source,
                            "requires_review": False,
                            "risk_level": "low",
                            "gmp_review_category": "not-a-gmp-category",
                        },
                    ],
                },
                {
                    "id": "table-001",
                    "cells": [
                        {
                            "id": "table-001-r1-c1",
                            "text": "Reviewed note",
                            "source": source,
                            "auto_confirmed": False,
                        },
                    ],
                },
            ),
            "risk_gate",
        ),
        (
            "current_thread_actual_malformed_gmp_category",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="generic-field",
                    expected_value="Reviewed note",
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    label_id="generic-field",
                    value="Reviewed note",
                    auto_confirmed=False,
                    gmp_review_category="not-a-gmp-category",
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "risk_gate",
        ),
        (
            "current_thread_qualified_lot_alias_label_id",
            validate_extracted_item(
                expected=_expected_item(
                    label_id="qc_lot_number",
                    expected_value="SAMPLE-LOT-001",
                    risk_level="medium",
                    requires_review=False,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
                actual=_actual_item(
                    label_id="qc_lot_number",
                    value="SAMPLE-LOT-001",
                    auto_confirmed=True,
                    fixture_id="fixture-001",
                    document_id="doc-001",
                    block_id="block-001",
                ),
            ),
            "risk_gate",
        ),
    ]

    for case_name, decision, failed_rule in cases:
        assert decision.auto_confirm_allowed is False, case_name
        assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM, case_name
        assert decision.requires_review is True, case_name
        assert failed_rule in decision.failed_rules, case_name


def test_missing_expected_table_cell_review_flag_blocks_auto_confirm() -> None:
    source = _evidence()
    expected_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
            },
        ],
    }
    actual_table = {
        "id": "table-001",
        "cells": [
            {
                "id": "table-001-r1-c1",
                "text": "SAMPLE-LOT-001",
                "source": source,
                "auto_confirmed": False,
            },
        ],
    }

    decision = validate_table_consistency(expected_table, actual_table)

    assert decision.auto_confirm_allowed is False
    assert decision.status is ValidationStatus.BLOCK_AUTO_CONFIRM
    assert decision.requires_review is True
    assert "risk_gate" in decision.failed_rules
