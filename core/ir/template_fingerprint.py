from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.ir.document_ir_v1 import DocumentBlock, DocumentIRV1


KNOWN_TEMPLATE_THRESHOLD = 0.95
CAUTION_TEMPLATE_THRESHOLD = 0.80
FAIL_CLOSED_MAX_SCORE = 0.79
INCOMPLETE_REQUIRED_COLUMNS_MAX_SCORE = 0.94


class TemplateMatchClassification(Enum):
    KNOWN = "known_template"
    CAUTION = "caution"
    UNKNOWN = "unknown_template"


@dataclass(frozen=True)
class TemplateFingerprintMatch:
    score: float
    classification: TemplateMatchClassification
    requires_review: bool
    warnings: tuple[str, ...] = ()
    matched_anchor_ids: tuple[str, ...] = ()
    missing_anchor_ids: tuple[str, ...] = ()


def classify_template_match(score: float) -> TemplateMatchClassification:
    """Classify a normalized template match score using Phase3 thresholds."""
    bounded_score = _bounded_score(score)
    if bounded_score >= KNOWN_TEMPLATE_THRESHOLD:
        return TemplateMatchClassification.KNOWN
    if bounded_score >= CAUTION_TEMPLATE_THRESHOLD:
        return TemplateMatchClassification.CAUTION
    return TemplateMatchClassification.UNKNOWN


def match_template_fingerprint(
    document_ir: DocumentIRV1, template_definition: Mapping[str, Any]
) -> TemplateFingerprintMatch:
    """Compare Document IR structure against a template fingerprint.

    The fingerprint intentionally uses only existing IR/template signals:
    declared pages, anchor text/scope, table anchor presence, and required table
    columns. Missing prerequisite signals cap the score below the caution band.
    """

    anchors = [_mapping(anchor) for anchor in _list_value(template_definition.get("anchors"))]
    table_definitions = [_mapping(table) for table in _list_value(template_definition.get("tables"))]
    required_anchor_ids = _required_anchor_ids(template_definition, table_definitions)
    page_numbers = {page.page_number for page in document_ir.pages}
    warnings: list[str] = []
    fail_closed = False

    if not document_ir.pages:
        warnings.append("document pages missing; template fingerprint requires review")
        fail_closed = True
    if not anchors:
        warnings.append("template anchors missing; template fingerprint requires review")
        fail_closed = True

    matched_anchor_ids: list[str] = []
    missing_anchor_ids: list[str] = []
    anchor_scores: list[float] = []
    for anchor in anchors:
        anchor_id = str(anchor.get("anchor_id") or "")
        match_score = _best_anchor_match_score(anchor, document_ir.blocks)
        is_required = _anchor_is_required(anchor, required_anchor_ids)
        if is_required or match_score > 0.0:
            anchor_scores.append(match_score)
        if match_score > 0.0:
            matched_anchor_ids.append(anchor_id)
        else:
            missing_anchor_ids.append(anchor_id)
            if is_required:
                warnings.append(f"template anchor '{anchor_id or '<unknown>'}' missing from document")
                fail_closed = True

    scoped_pages = {
        page
        for page in (
            _scope_page(_mapping(anchor.get("scope")))
            for anchor in anchors
            if _anchor_is_required(anchor, required_anchor_ids)
        )
        if page is not None
    }
    page_score = 1.0
    if scoped_pages:
        matched_pages = sum(1 for page in scoped_pages if page in page_numbers)
        page_score = matched_pages / len(scoped_pages)
        if matched_pages != len(scoped_pages):
            warnings.append("document pages do not satisfy template anchor scopes")
            fail_closed = True

    table_score, cap_below_known = _table_score(table_definitions, anchors, document_ir.blocks, warnings)
    warnings.extend(_document_ir_review_warnings(document_ir))

    score = _weighted_average(
        (
            (sum(anchor_scores) / len(anchor_scores), 0.60) if anchor_scores else (0.0, 0.60),
            (page_score, 0.20),
            (table_score, 0.20),
        )
    )
    if fail_closed:
        score = min(score, FAIL_CLOSED_MAX_SCORE)
    elif cap_below_known:
        score = min(score, INCOMPLETE_REQUIRED_COLUMNS_MAX_SCORE)
    score = _bounded_score(score)
    classification = classify_template_match(score)
    requires_review = bool(warnings) or classification is not TemplateMatchClassification.KNOWN
    return TemplateFingerprintMatch(
        score=score,
        classification=classification,
        requires_review=requires_review,
        warnings=tuple(dict.fromkeys(warnings)),
        matched_anchor_ids=tuple(anchor_id for anchor_id in matched_anchor_ids if anchor_id),
        missing_anchor_ids=tuple(anchor_id for anchor_id in missing_anchor_ids if anchor_id),
    )


def _best_anchor_match_score(anchor: Mapping[str, Any], blocks: Sequence[DocumentBlock]) -> float:
    matching_blocks = _blocks_matching_anchor(anchor, blocks)
    if not matching_blocks:
        return 0.0
    scores = [_anchor_block_match_score(anchor, block) for block in matching_blocks]
    return max(scores, default=0.0)


def _blocks_matching_anchor(anchor: Mapping[str, Any], blocks: Sequence[DocumentBlock]) -> list[DocumentBlock]:
    scope = _mapping(anchor.get("scope"))
    expected_text = str(anchor.get("text") or "")
    match_mode = str(anchor.get("match") or "normalized")
    return [
        block
        for block in blocks
        if _block_matches_anchor_scope(anchor, block, scope)
        and _anchor_block_match_score(anchor, block, expected_text, match_mode) > 0.0
    ]


def _anchor_block_match_score(
    anchor: Mapping[str, Any],
    block: DocumentBlock,
    expected_text: str | None = None,
    match_mode: str | None = None,
) -> float:
    expected = str(anchor.get("text") or "") if expected_text is None else expected_text
    mode = str(anchor.get("match") or "normalized") if match_mode is None else match_mode
    if str(anchor.get("kind") or "") == "table_header":
        if block.type != "table":
            return 0.0
        return _table_anchor_match_score(block.text, expected, mode)
    return _text_match_score(expected, block.text, mode)


def _block_matches_anchor_scope(
    anchor: Mapping[str, Any], block: DocumentBlock, scope: Mapping[str, Any]
) -> bool:
    if str(anchor.get("kind") or "") == "table_header" and block.type != "table":
        return False
    page = _scope_page(scope)
    if page is not None and block.source_page != page:
        return False
    block_types = [str(value) for value in _list_value(scope.get("block_types"))]
    return not block_types or block.type in block_types


def _table_score(
    table_definitions: Sequence[Mapping[str, Any]],
    anchors: Sequence[Mapping[str, Any]],
    blocks: Sequence[DocumentBlock],
    warnings: list[str],
) -> tuple[float, bool]:
    if not table_definitions:
        return 1.0, False

    anchors_by_id = {str(anchor.get("anchor_id") or ""): anchor for anchor in anchors}
    scores: list[float] = []
    cap_below_known = False
    for table in table_definitions:
        table_id = str(table.get("table_id") or "<unknown>")
        anchor_id = str(table.get("anchor_id") or "")
        anchor = anchors_by_id.get(anchor_id, {})
        table_blocks = _blocks_matching_anchor(anchor, blocks) if anchor else []
        if not table_blocks:
            warnings.append(f"template table '{table_id}' missing from document")
            scores.append(0.0)
            continue

        required_columns = [str(column) for column in _list_value(table.get("required_columns"))]
        if not required_columns:
            scores.append(1.0)
            continue

        best_column_score = 0.0
        for block in table_blocks:
            best_column_score = max(
                best_column_score,
                _table_required_column_score(block.text, anchor, required_columns),
            )
        if best_column_score < 1.0:
            warnings.append(f"template table '{table_id}' required columns incomplete")
            cap_below_known = True
        scores.append(best_column_score)
    return sum(scores) / len(scores), cap_below_known


def _text_match_score(expected: str, actual: str, match_mode: str) -> float:
    if match_mode == "exact":
        return 1.0 if expected and actual and expected == actual else 0.0
    normalized_expected = _normalized_text(expected)
    normalized_actual = _normalized_text(actual)
    if not normalized_expected or not normalized_actual:
        return 0.0
    if match_mode == "contains":
        return 1.0 if normalized_expected in normalized_actual else 0.0
    return 1.0 if normalized_expected == normalized_actual else 0.0


def _anchor_is_required(anchor: Mapping[str, Any], required_anchor_ids: set[str]) -> bool:
    anchor_id = str(anchor.get("anchor_id") or "")
    kind = str(anchor.get("kind") or "")
    return kind in {"heading", "table_header"} or anchor_id in required_anchor_ids


def _required_anchor_ids(
    template_definition: Mapping[str, Any], table_definitions: Sequence[Mapping[str, Any]]
) -> set[str]:
    anchor_ids = {
        str(table.get("anchor_id"))
        for table in table_definitions
        if isinstance(table.get("anchor_id"), str) and str(table.get("anchor_id")).strip()
    }
    for field in (_mapping(value) for value in _list_value(template_definition.get("fields"))):
        if field.get("required") is not True:
            continue
        source = _mapping(field.get("source"))
        anchor_id = source.get("anchor_id")
        if isinstance(anchor_id, str) and anchor_id.strip():
            anchor_ids.add(anchor_id)
    return anchor_ids


def _scope_page(value: Mapping[str, Any]) -> int | None:
    page = value.get("page")
    if isinstance(page, int) and not isinstance(page, bool) and page >= 1:
        return page
    return None


def _weighted_average(values: Sequence[tuple[float, float]]) -> float:
    total_weight = sum(weight for _, weight in values)
    if total_weight <= 0:
        return 0.0
    return sum(_bounded_score(value) * weight for value, weight in values) / total_weight


def _bounded_score(score: float) -> float:
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
        return 0.0
    return min(1.0, max(0.0, float(score)))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _normalized_column_name(value: str) -> str:
    return _normalized_text(re.sub(r"[_-]+", " ", value))


def _document_ir_review_warnings(document_ir: DocumentIRV1) -> list[str]:
    warnings = list(document_ir.warnings)
    for block in document_ir.blocks:
        if block.review.warnings:
            warnings.extend(block.review.warnings)
        elif block.review.requires_review:
            warnings.append(f"document block '{block.id}' requires review")
    return warnings


def _table_anchor_match_score(value: str, expected_text: str, match_mode: str) -> float:
    return max(
        (_row_anchor_match_score(row, expected_text, match_mode) for row in _table_rows(value)),
        default=0.0,
    )


def _table_required_column_score(
    value: str, anchor: Mapping[str, Any], required_columns: Sequence[str]
) -> float:
    normalized_required_columns = [_normalized_column_name(column) for column in required_columns]
    normalized_required_columns = [column for column in normalized_required_columns if column]
    if not normalized_required_columns:
        return 1.0

    rows = _table_rows(value)
    candidate_rows = _table_required_column_candidate_rows(rows, anchor, len(normalized_required_columns))
    candidate_rows.extend(
        _wrapped_cell_candidate_rows(rows, anchor, set(normalized_required_columns))
    )
    best_score = 0.0
    for row in candidate_rows:
        column_names = {_normalized_column_name(cell) for cell in row}
        column_names.discard("")
        if not column_names:
            continue
        matched_columns = sum(1 for column in normalized_required_columns if column in column_names)
        best_score = max(best_score, matched_columns / len(normalized_required_columns))
    return best_score


def _table_required_column_candidate_rows(
    rows: Sequence[Sequence[str]], anchor: Mapping[str, Any], required_column_count: int
) -> list[Sequence[str]]:
    anchor_index = _first_table_anchor_row_index(rows, anchor)
    candidate_rows: list[Sequence[str]] = []
    if anchor_index < len(rows):
        anchor_columns = _row_columns_excluding_anchor(rows[anchor_index], anchor)
        if anchor_columns:
            candidate_rows.append(anchor_columns)
    for row in rows[anchor_index + 1 :]:
        if len(row) == 1 and required_column_count > 1:
            continue
        candidate_rows.append(row)
        break
    return candidate_rows


def _wrapped_cell_candidate_rows(
    rows: Sequence[Sequence[str]], anchor: Mapping[str, Any], required_columns: set[str]
) -> list[Sequence[str]]:
    anchor_index = _first_table_anchor_row_index(rows, anchor)
    candidates: list[Sequence[str]] = []
    for index in range(anchor_index, len(rows) - 1):
        row = rows[index]
        next_row = rows[index + 1]
        if len(row) < 2 or len(next_row) < 2:
            continue
        joined_cell = f"{row[-1]}\n{next_row[0]}"
        if _normalized_column_name(joined_cell) not in required_columns:
            continue
        combined_row = [*row[:-1], joined_cell, *next_row[1:]]
        if _row_matches_anchor(combined_row, anchor):
            anchor_columns = _row_columns_excluding_anchor(combined_row, anchor)
            if anchor_columns:
                candidates.append(anchor_columns)
        else:
            candidates.append(combined_row)
    return candidates


def _first_table_anchor_row_index(rows: Sequence[Sequence[str]], anchor: Mapping[str, Any]) -> int:
    expected_text = str(anchor.get("text") or "")
    if not expected_text:
        return 0
    match_mode = str(anchor.get("match") or "normalized")
    for index, row in enumerate(rows):
        if _row_anchor_match_score(row, expected_text, match_mode) > 0.0:
            return index
    return 0


def _row_columns_excluding_anchor(row: Sequence[str], anchor: Mapping[str, Any]) -> Sequence[str]:
    expected_text = str(anchor.get("text") or "")
    if not expected_text:
        return row
    match_mode = str(anchor.get("match") or "normalized")
    for index, cell in enumerate(row):
        if _text_match_score(expected_text, cell, match_mode) > 0.0:
            return [*row[:index], *row[index + 1 :]]
    return []


def _row_anchor_match_score(row: Sequence[str], expected_text: str, match_mode: str) -> float:
    row_text = "\t".join(row)
    first_cell = row[0] if row else ""
    return max(
        _text_match_score(expected_text, row_text, match_mode),
        _text_match_score(expected_text, first_cell, match_mode),
    )


def _row_matches_anchor(row: Sequence[str], anchor: Mapping[str, Any]) -> bool:
    expected_text = str(anchor.get("text") or "")
    if not expected_text:
        return False
    match_mode = str(anchor.get("match") or "normalized")
    return _row_anchor_match_score(row, expected_text, match_mode) > 0.0


def _table_rows(value: str) -> list[list[str]]:
    xlsx_rows = _xlsx_cell_rows(value)
    if xlsx_rows:
        return xlsx_rows
    return [
        cells
        for line in value.splitlines()
        if (cells := _table_row_cells(line))
    ]


def _table_row_cells(value: str) -> list[str]:
    return [
        cell
        for cell in _split_table_row(value)
        if _normalized_column_name(cell)
    ]


def _split_table_row(value: str) -> list[str]:
    if "\t" in value:
        return value.split("\t")
    if "|" in value:
        return value.split("|")
    if "," in value:
        return re.split(r"\s*,\s*", value)
    return re.split(r"\s{2,}", value)


def _xlsx_cell_rows(value: str) -> list[list[str]]:
    row_cells: dict[int, dict[int, str]] = {}
    saw_cell_ref = False
    last_cell_ref: tuple[int, int] | None = None
    for line in value.splitlines():
        if not line.strip():
            continue
        cell = _xlsx_cell_line(line)
        if cell is None:
            if _looks_like_delimited_table_row(line):
                return []
            if last_cell_ref is None:
                return []
            row_index, column_index = last_cell_ref
            row_cells[row_index][column_index] = (
                f"{row_cells[row_index][column_index]}\n{line}"
            )
            continue
        saw_cell_ref = True
        row_index, column_index, cell_value = cell
        if not _normalized_column_name(cell_value):
            last_cell_ref = None
            continue
        row_cells.setdefault(row_index, {})[column_index] = cell_value
        last_cell_ref = (row_index, column_index)
    if not saw_cell_ref:
        return []
    return [
        [cell for _column, cell in sorted(columns.items())]
        for _row, columns in sorted(row_cells.items())
        if columns
    ]


def _xlsx_cell_line(value: str) -> tuple[int, int, str] | None:
    match = re.match(r"^([A-Za-z]+)([1-9][0-9]*):(.*)$", value)
    if not match:
        return None
    column_letters, row_number, cell_value = match.groups()
    if cell_value.startswith(" "):
        cell_value = cell_value[1:]
    return int(row_number), _xlsx_column_index(column_letters), cell_value


def _looks_like_delimited_table_row(value: str) -> bool:
    return "\t" in value or "|" in value


def _xlsx_column_index(value: str) -> int:
    column_index = 0
    for character in value.upper():
        column_index = (column_index * 26) + (ord(character) - ord("A") + 1)
    return column_index
