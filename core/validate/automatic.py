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

    explicit_review_required = expected.get("requires_review") is True
    high_risk = expected.get("risk_level") == "high"
    requires_review = explicit_review_required or high_risk
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

    expected_cells = _cells_by_id(expected_table.get("cells"))
    actual_cells = _cells_by_id(actual_table.get("cells"))
    if expected_cells is None or actual_cells is None:
        failed_rules.append("table_consistency")
    else:
        missing_or_extra = set(expected_cells) != set(actual_cells)
        changed_text = any(
            _normalized_text(expected_cells[cell_id].get("text"))
            != _normalized_text(actual_cells[cell_id].get("text"))
            for cell_id in set(expected_cells) & set(actual_cells)
        )
        if missing_or_extra or changed_text:
            failed_rules.append("table_consistency")

    if failed_rules:
        warnings.append("table content requires human review")
    return _decision(failed_rules, warnings, bool(failed_rules))


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
    return isinstance(expected, str) and bool(expected) and expected == actual


def _normalized_text(value: object) -> str:
    return " ".join(str(value).split())


def _values_match(expected: object, actual: object) -> bool:
    if isinstance(expected, bool):
        return actual is expected
    if _number_expected(expected):
        return _same_finite_number(expected, actual)
    return _normalized_text(expected) == _normalized_text(actual)


def _number_expected(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _same_finite_number(expected: object, actual: object) -> bool:
    if not _number_expected(expected) or not _number_expected(actual):
        return False
    expected_number = float(expected)
    actual_number = float(actual)
    return math.isfinite(expected_number) and math.isfinite(actual_number) and expected == actual


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
    for key in ("x", "y", "width", "height"):
        coordinate = bbox.get(key)
        if not _number_expected(coordinate) or not math.isfinite(float(coordinate)):
            return False
    return bbox["width"] > 0 and bbox["height"] > 0


def _cells_by_id(value: object) -> dict[str, Mapping[str, Any]] | None:
    if not isinstance(value, list):
        return None
    cells: dict[str, Mapping[str, Any]] = {}
    for cell in value:
        if not isinstance(cell, Mapping):
            return None
        cell_id = cell.get("id")
        if not isinstance(cell_id, str) or not cell_id or cell_id in cells:
            return None
        cells[cell_id] = cell
    return cells
