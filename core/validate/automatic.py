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
MIN_IMPORTANT_VALUE_CONFIDENCE = 0.8
SUPPORTED_RISK_LEVELS = {"low", "medium", "high", "critical"}
REVIEW_REQUIRED_RISK_LEVELS = {"high", "critical"}
GMP_REVIEW_REQUIRED_CATEGORIES = frozenset(
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
GMP_REVIEW_CATEGORY_ALIASES = {
    "batch_number": "lot_number",
    "lot": "lot_number",
    "lot_no": "lot_number",
    "product": "item",
    "material": "item",
    "component": "item",
    "date": "date_time",
    "approval_date": "date_time",
    "approved_at": "date_time",
    "collection_date": "date_time",
    "effective_date": "date_time",
    "expiration_date": "date_time",
    "expiry_date": "date_time",
    "manufacture_date": "date_time",
    "manufactured_at": "date_time",
    "manufacturing_date": "date_time",
    "mfg_date": "date_time",
    "production_date": "date_time",
    "review_date": "date_time",
    "reviewed_at": "date_time",
    "sample_date": "date_time",
    "test_date": "date_time",
    "time": "date_time",
    "timestamp": "date_time",
    "datetime": "date_time",
    "quantity": "numeric_value",
    "measurement": "numeric_value",
    "number": "numeric_value",
    "limit": "specification",
    "standard": "specification",
    "acceptance_criteria": "specification",
    "result": "judgment",
    "decision": "judgment",
    "disposition": "judgment",
    "status": "judgment",
    "release_status": "judgment",
    "operator": "person",
    "reviewer": "person",
    "approver": "person",
    "signature": "person",
    "change": "correction",
    "amendment": "correction",
    "nonconformance": "deviation",
    "oos": "deviation",
}
OCR_SOURCE_MARKERS = {"ocr", "optical_character_recognition"}
OCR_EXTRACTOR_NAME_MARKERS = ("ocr", "tesseract")


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

    if not _evidence_matches(_record_source_anchor(expected), _record_source_anchor(actual)):
        failed_rules.append("provenance")

    if _number_expected(expected.get("expected_value")) and not _same_finite_number(
        expected.get("expected_value"), actual.get("value")
    ):
        failed_rules.append("numeric")

    auto_confirmed = actual.get("auto_confirmed", False)
    if not isinstance(auto_confirmed, bool):
        failed_rules.append("risk_gate")
        auto_confirmed = True

    confidence_requires_review = _confidence_requires_review(actual.get("confidence"))
    if confidence_requires_review:
        warnings.append("item confidence requires human review")
        if auto_confirmed:
            failed_rules.append("risk_gate")

    if _has_malformed_review_flag(expected) or _has_malformed_review_flag(actual):
        failed_rules.append("risk_gate")
    if _has_missing_or_malformed_risk_level(expected) or _has_malformed_risk_level(actual):
        failed_rules.append("risk_gate")
    if not isinstance(expected.get("requires_review"), bool):
        failed_rules.append("risk_gate")

    explicit_review_required = _requires_review(expected) or _requires_review(actual)
    high_risk = _is_high_risk(expected) or _is_high_risk(actual)
    category_requires_review = _gmp_category_requires_review(
        expected
    ) or _gmp_category_requires_review(actual)
    condition_warnings = _gmp_condition_review_warnings(
        expected,
        actual,
        important_item=high_risk or category_requires_review or explicit_review_required,
    )
    warnings.extend(condition_warnings)
    requires_review = (
        explicit_review_required
        or high_risk
        or category_requires_review
        or confidence_requires_review
        or bool(condition_warnings)
    )
    scope_binding_required = not (explicit_review_required or high_risk)
    if scope_binding_required:
        for scope_key in ("fixture_id", "document_id", "block_id"):
            if not _same_non_empty_string(expected.get(scope_key), actual.get(scope_key)):
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
    if _has_malformed_review_flag(expected_table) or _has_malformed_review_flag(
        actual_table
    ):
        failed_rules.append("risk_gate")
    if _has_malformed_risk_level(expected_table) or _has_malformed_risk_level(
        actual_table
    ):
        failed_rules.append("risk_gate")
    table_explicit_review_required = _requires_review(expected_table) or _requires_review(
        actual_table
    )
    table_high_risk = _is_high_risk(expected_table) or _is_high_risk(actual_table)
    table_category_requires_review = _table_gmp_category_requires_review(
        expected_table
    ) or _table_gmp_category_requires_review(actual_table)

    expected_cells = _cells_by_id(expected_table.get("cells"))
    actual_cells = _cells_by_id(actual_table.get("cells"))
    table_requires_review = (
        table_explicit_review_required
        or table_high_risk
        or table_category_requires_review
    )
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
            expected_source = _record_source_anchor(expected_cell)
            actual_source = _record_source_anchor(actual_cell)
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
            confidence_requires_review = _confidence_requires_review(
                actual_cell.get("confidence")
            )
            if confidence_requires_review:
                warnings.append("table cell confidence requires human review")
            explicit_review_required = (
                table_explicit_review_required
                or _requires_review(expected_cell)
                or _requires_review(actual_cell)
            )
            high_risk = (
                table_high_risk
                or _is_high_risk(expected_cell)
                or _is_high_risk(actual_cell)
            )
            category_requires_review = (
                table_category_requires_review
                or _gmp_category_requires_review(expected_cell)
                or _gmp_category_requires_review(actual_cell)
            )
            condition_warnings = _gmp_condition_review_warnings(
                expected_cell,
                actual_cell,
                important_item=high_risk
                or category_requires_review
                or explicit_review_required,
            )
            warnings.extend(f"table cell {warning}" for warning in condition_warnings)
            cell_requires_review = (
                explicit_review_required
                or high_risk
                or category_requires_review
                or confidence_requires_review
                or bool(condition_warnings)
            )
            if cell_requires_review:
                table_requires_review = True
                warnings.append("table cell requires human review")
                if auto_confirmed:
                    failed_rules.append("risk_gate")
            if "risk_level" not in expected_table and auto_confirmed:
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


def _record_source_anchor(record: Mapping[str, Any]) -> object:
    if "evidence" in record:
        return record.get("evidence")
    if "source" in record:
        return record.get("source")
    value_metadata = record.get("value_metadata")
    if isinstance(value_metadata, Mapping) and (
        "source_page" in value_metadata or "bbox" in value_metadata
    ):
        return {
            "source_page": value_metadata.get("source_page"),
            "bbox": value_metadata.get("bbox"),
        }
    return None


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


def _confidence_requires_review(value: object) -> bool:
    if not _is_supported_finite_number(value):
        return True
    return float(value) < MIN_IMPORTANT_VALUE_CONFIDENCE or float(value) > 1


def _is_high_risk(record: Mapping[str, Any]) -> bool:
    return record.get("risk_level") in REVIEW_REQUIRED_RISK_LEVELS


def _has_malformed_risk_level(record: Mapping[str, Any]) -> bool:
    if "risk_level" not in record:
        return False
    return record.get("risk_level") not in SUPPORTED_RISK_LEVELS


def _has_missing_or_malformed_risk_level(record: Mapping[str, Any]) -> bool:
    return "risk_level" not in record or _has_malformed_risk_level(record)


def _requires_review(record: Mapping[str, Any]) -> bool:
    return record.get("requires_review") is True


def _has_malformed_review_flag(record: Mapping[str, Any]) -> bool:
    if "requires_review" not in record:
        return False
    value = record.get("requires_review")
    return not isinstance(value, bool)


def _gmp_category_requires_review(record: Mapping[str, Any]) -> bool:
    categories = (
        _normalized_category(record.get("gmp_review_category")),
        _normalized_category(record.get("field_category")),
        _normalized_category(record.get("field_id")),
        _normalized_category(record.get("label_id")),
        _normalized_category(record.get("label")),
        _category_from_value_type(record.get("value_type")),
        _category_from_record_value(record),
    )
    return any(category in GMP_REVIEW_REQUIRED_CATEGORIES for category in categories)


def _table_gmp_category_requires_review(table: Mapping[str, Any]) -> bool:
    if _gmp_category_requires_review(table):
        return True
    required_columns = table.get("required_columns")
    if not isinstance(required_columns, list):
        return False
    return any(
        _normalized_category(column) in GMP_REVIEW_REQUIRED_CATEGORIES
        for column in required_columns
    )


def _normalized_category(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = (
        value.strip().casefold().replace("-", "_").replace("/", "_").replace(" ", "_")
    )
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    alias = GMP_REVIEW_CATEGORY_ALIASES.get(normalized)
    if alias:
        return alias
    if normalized.endswith(("_date", "_time", "_timestamp", "_datetime")):
        return "date_time"
    return normalized


def _category_from_value_type(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized in {"number", "numeric", "integer", "int", "float", "decimal"}:
        return "numeric_value"
    if normalized in {"date", "datetime", "time", "timestamp"}:
        return "date_time"
    return ""


def _category_from_record_value(record: Mapping[str, Any]) -> str:
    if _number_expected(record.get("expected_value")) or _number_expected(
        record.get("value")
    ):
        return "numeric_value"
    return ""


def _gmp_condition_review_warnings(
    expected: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    important_item: bool,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if _ocr_derived(expected) or _ocr_derived(actual):
        warnings.append("ocr-derived item requires human review")
    if _extraction_engine_mismatch(expected, actual):
        warnings.append("extraction engine mismatch requires human review")
    if not _evidence_matches(_record_source_anchor(expected), _record_source_anchor(actual)):
        warnings.append("item source requires human review")
    if important_item and (_llm_involved(expected) or _llm_involved(actual)):
        warnings.append("llm-involved important item requires human review")
    return tuple(dict.fromkeys(warnings))


def _ocr_derived(record: Mapping[str, Any]) -> bool:
    for key in ("source_kind", "source_type", "extraction_method", "extractor_kind"):
        value = record.get(key)
        if isinstance(value, str) and value.strip().casefold() in OCR_SOURCE_MARKERS:
            return True
    engine = record.get("engine")
    if isinstance(engine, str) and engine.strip():
        return True
    extractor_name = _extractor_name(record)
    if extractor_name:
        normalized = extractor_name.strip().casefold()
        return any(marker in normalized for marker in OCR_EXTRACTOR_NAME_MARKERS)
    return False


def _extraction_engine_mismatch(
    expected: Mapping[str, Any], actual: Mapping[str, Any]
) -> bool:
    expected_engine = _extraction_engine(expected)
    actual_engine = _extraction_engine(actual)
    if expected_engine is None and actual_engine is None:
        return False
    return not _same_non_empty_string(expected_engine, actual_engine)


def _extraction_engine(record: Mapping[str, Any]) -> object:
    for key in ("extraction_engine", "extractor_engine", "engine"):
        value = record.get(key)
        if isinstance(value, str):
            return value
    return _extractor_name(record)


def _extractor_name(record: Mapping[str, Any]) -> str | None:
    extractor = record.get("extractor")
    if isinstance(extractor, str):
        return extractor
    if isinstance(extractor, Mapping):
        name = extractor.get("name")
        if isinstance(name, str):
            return name
    value_metadata = record.get("value_metadata")
    if isinstance(value_metadata, Mapping):
        extractor = value_metadata.get("extractor")
        if isinstance(extractor, str):
            return extractor
        if isinstance(extractor, Mapping):
            name = extractor.get("name")
            if isinstance(name, str):
                return name
    return None


def _llm_involved(record: Mapping[str, Any]) -> bool:
    return record.get("llm_involved") is True or record.get("llm_generated") is True


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
