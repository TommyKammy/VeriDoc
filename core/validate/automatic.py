from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ValidationStatus(Enum):
    PASS = "pass"
    REQUIRES_REVIEW = "requires_review"
    BLOCK_AUTO_CONFIRM = "block_auto_confirm"


SUPPORTED_BBOX_UNITS = {"pt", "px", "mm"}


@dataclass(frozen=True)
class ValidationDecision:
    status: ValidationStatus
    auto_confirm_allowed: bool
    requires_review: bool
    failed_rules: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def validate_extracted_item(
    *, expected: Mapping[str, Any], actual: Mapping[str, Any]
) -> ValidationDecision:
    """Validate a single extracted item against its authoritative expected record."""

    failed_rules: list[str] = []
    warnings: list[str] = []

    if not _same_non_empty_string(expected.get("id"), actual.get("id")):
        failed_rules.append("scope_binding")
    if not _same_non_empty_string(expected.get("label_id"), actual.get("label_id")):
        failed_rules.append("scope_binding")
    for scope_key in ("fixture_id", "document_id", "block_id"):
        if scope_key in expected and not _same_non_empty_string(
            expected.get(scope_key), actual.get(scope_key)
        ):
            failed_rules.append("scope_binding")

    if not _values_match(expected.get("expected_value"), actual.get("value")):
        failed_rules.append("value_non_modification")

    if not _evidence_matches(expected.get("evidence"), actual.get("evidence")):
        failed_rules.append("provenance")

    if _number_expected(expected.get("expected_value")) and not _same_finite_number(
        expected.get("expected_value"), actual.get("value")
    ):
        failed_rules.append("numeric")

    auto_confirmed = actual.get("auto_confirmed", False)
    if not isinstance(auto_confirmed, bool):
        failed_rules.append("risk_gate")
        auto_confirmed = True

    if _has_malformed_review_flag(expected) or _has_malformed_review_flag(actual):
        failed_rules.append("risk_gate")
    if _has_missing_or_malformed_risk_level(expected) or _has_malformed_risk_level(actual):
        failed_rules.append("risk_gate")
    if not isinstance(expected.get("requires_review"), bool):
        failed_rules.append("risk_gate")

    explicit_review_required = _requires_review(expected) or _requires_review(actual)
    high_risk = _is_high_risk(expected) or _is_high_risk(actual)
    requires_review = explicit_review_required or high_risk
    if not requires_review and not _same_non_empty_string(
        expected.get("fixture_id"), actual.get("fixture_id")
    ):
        failed_rules.append("scope_binding")
    if requires_review:
        warnings.append("item requires human review")
    if requires_review and auto_confirmed:
        failed_rules.append("risk_gate")

    return _decision(failed_rules, warnings, requires_review)


def validate_table_consistency(
    expected_table: Mapping[str, Any], actual_table: Mapping[str, Any]
) -> ValidationDecision:
    failed_rules: list[str] = []
    warnings: list[str] = []

    if not _same_non_empty_string(expected_table.get("id"), actual_table.get("id")):
        failed_rules.append("table_consistency")
    if not _same_non_empty_string(
        expected_table.get("fixture_table_id"), expected_table.get("id")
    ):
        failed_rules.append("table_consistency")

    expected_cells = _cells_by_id(expected_table.get("cells"))
    actual_cells = _cells_by_id(actual_table.get("cells"))
    table_requires_review = False
    if expected_cells is None or actual_cells is None:
        failed_rules.append("table_consistency")
    else:
        missing_or_extra = set(expected_cells) != set(actual_cells)
        matching_cell_ids = set(expected_cells) & set(actual_cells)
        changed_text = any(
            not _same_non_blank_normalized_text(
                expected_cells[cell_id].get("text"),
                actual_cells[cell_id].get("text"),
            )
            for cell_id in matching_cell_ids
        )
        if missing_or_extra or changed_text:
            failed_rules.append("table_consistency")
        for cell_id in matching_cell_ids:
            expected_cell = expected_cells[cell_id]
            actual_cell = actual_cells[cell_id]
            expected_source = expected_cell.get("source")
            actual_source = actual_cell.get("source")
            if not _evidence_matches(expected_source, actual_source):
                failed_rules.append("provenance")

            auto_confirmed = actual_cell.get("auto_confirmed", False)
            if not isinstance(auto_confirmed, bool):
                failed_rules.append("risk_gate")
                auto_confirmed = True
            if _has_malformed_review_flag(expected_cell) or _has_malformed_review_flag(
                actual_cell
            ):
                failed_rules.append("risk_gate")
            if _has_malformed_risk_level(expected_cell) or _has_malformed_risk_level(
                actual_cell
            ):
                failed_rules.append("risk_gate")
            if not isinstance(expected_cell.get("requires_review"), bool):
                failed_rules.append("risk_gate")
            if _cell_requires_review(expected_cell) or _cell_requires_review(actual_cell):
                table_requires_review = True
                warnings.append("table cell requires human review")
                if auto_confirmed:
                    failed_rules.append("risk_gate")

    if failed_rules:
        warnings.append("table content requires human review")
    return _decision(failed_rules, warnings, bool(failed_rules) or table_requires_review)


def _decision(
    failed_rules: list[str], warnings: list[str], requires_review: bool
) -> ValidationDecision:
    unique_failed_rules = tuple(dict.fromkeys(failed_rules))
    unique_warnings = tuple(dict.fromkeys(warnings))
    if unique_failed_rules:
        return ValidationDecision(
            status=ValidationStatus.BLOCK_AUTO_CONFIRM,
            auto_confirm_allowed=False,
            requires_review=True,
            failed_rules=unique_failed_rules,
            warnings=unique_warnings,
        )
    if requires_review:
        return ValidationDecision(
            status=ValidationStatus.REQUIRES_REVIEW,
            auto_confirm_allowed=False,
            requires_review=True,
            warnings=unique_warnings,
        )
    return ValidationDecision(
        status=ValidationStatus.PASS,
        auto_confirm_allowed=True,
        requires_review=False,
        warnings=unique_warnings,
    )


def _same_non_empty_string(expected: object, actual: object) -> bool:
    return isinstance(expected, str) and bool(expected.strip()) and expected == actual


def _same_normalized_text(expected: object, actual: object) -> bool:
    if not isinstance(expected, str) or not isinstance(actual, str):
        return False
    return _normalized_text(expected) == _normalized_text(actual)


def _same_non_blank_normalized_text(expected: object, actual: object) -> bool:
    if not isinstance(expected, str) or not isinstance(actual, str):
        return False
    normalized_expected = _normalized_text(expected)
    normalized_actual = _normalized_text(actual)
    return bool(normalized_expected) and normalized_expected == normalized_actual


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


def _values_match(expected: object, actual: object) -> bool:
    if isinstance(expected, bool):
        return actual is expected
    if _number_expected(expected):
        return _same_finite_number(expected, actual)
    return _same_non_blank_normalized_text(expected, actual)


def _number_expected(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _same_finite_number(expected: object, actual: object) -> bool:
    if not _is_supported_finite_number(expected) or not _is_supported_finite_number(actual):
        return False
    return expected == actual


def _evidence_matches(expected: object, actual: object) -> bool:
    return _is_source_anchor(expected) and _is_source_anchor(actual) and expected == actual


def _is_source_anchor(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    source_page = value.get("source_page")
    if not isinstance(source_page, int) or isinstance(source_page, bool) or source_page <= 0:
        return False
    bbox = value.get("bbox")
    if not isinstance(bbox, Mapping):
        return False
    if "origin" in bbox and bbox.get("origin") != "top-left":
        return False
    if "unit" in bbox and bbox.get("unit") not in SUPPORTED_BBOX_UNITS:
        return False
    for key in ("x", "y", "width", "height"):
        coordinate = bbox.get(key)
        if not _is_supported_finite_number(coordinate):
            return False
    return (
        bbox["x"] >= 0
        and bbox["y"] >= 0
        and bbox["width"] > 0
        and bbox["height"] > 0
    )


def _is_supported_finite_number(value: object) -> bool:
    if not _number_expected(value):
        return False
    try:
        number = float(value)
    except OverflowError:
        return False
    return math.isfinite(number)


def _cell_requires_review(cell: Mapping[str, Any]) -> bool:
    return _requires_review(cell) or _is_high_risk(cell)


def _is_high_risk(record: Mapping[str, Any]) -> bool:
    return record.get("risk_level") == "high"


def _has_malformed_risk_level(record: Mapping[str, Any]) -> bool:
    if "risk_level" not in record:
        return False
    return record.get("risk_level") not in {"low", "medium", "high"}


def _has_missing_or_malformed_risk_level(record: Mapping[str, Any]) -> bool:
    return "risk_level" not in record or _has_malformed_risk_level(record)


def _requires_review(record: Mapping[str, Any]) -> bool:
    return record.get("requires_review") is True


def _has_malformed_review_flag(record: Mapping[str, Any]) -> bool:
    if "requires_review" not in record:
        return False
    value = record.get("requires_review")
    return not isinstance(value, bool)


def _cells_by_id(value: object) -> dict[str, Mapping[str, Any]] | None:
    if not isinstance(value, list) or not value:
        return None
    cells: dict[str, Mapping[str, Any]] = {}
    for cell in value:
        if not isinstance(cell, Mapping):
            return None
        cell_id = cell.get("id")
        if not isinstance(cell_id, str) or not cell_id.strip() or cell_id in cells:
            return None
        cells[cell_id] = cell
    return cells
