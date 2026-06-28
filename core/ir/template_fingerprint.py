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


@dataclass(frozen=True)
class TemplateMappedField:
    field_id: str
    label: str
    output_key: str
    value: str | None
    confidence: float
    evidence: Mapping[str, Any]
    requires_review: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class TemplateFieldMappingResult:
    root_key: str
    fields: tuple[TemplateMappedField, ...]
    output: Mapping[str, Any]
    requires_review: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ParsedTableRows:
    rows: list[list[str]]
    allow_merged_column_candidates: bool
    preserve_column_positions: bool = False


@dataclass(frozen=True)
class _ExtractedTemplateFieldValue:
    value: str
    block: DocumentBlock
    confidence: float
    evidence_detail: Mapping[str, Any]


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
    optional_anchor_ids = _optional_anchor_ids(template_definition, required_anchor_ids)
    defined_anchor_ids = {
        anchor_id
        for anchor in anchors
        if (anchor_id := str(anchor.get("anchor_id") or ""))
    }
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
    for anchor_id in sorted(required_anchor_ids - defined_anchor_ids):
        missing_anchor_ids.append(anchor_id)
        warnings.append(f"template required anchor '{anchor_id}' is not defined")
        fail_closed = True
    for anchor in anchors:
        anchor_id = str(anchor.get("anchor_id") or "")
        match_score = _best_anchor_match_score(
            anchor,
            document_ir.blocks,
            parse_xlsx_cell_refs=document_ir.document.source_type == "xlsx",
        )
        is_required = _anchor_is_required(anchor, required_anchor_ids, optional_anchor_ids)
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
            if _anchor_is_required(anchor, required_anchor_ids, optional_anchor_ids)
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

    table_score, cap_below_known = _table_score(
        table_definitions,
        anchors,
        document_ir.blocks,
        warnings,
        source_type=document_ir.document.source_type,
    )
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


def apply_template_field_mapping(
    document_ir: DocumentIRV1, template_definition: Mapping[str, Any]
) -> TemplateFieldMappingResult:
    """Apply deterministic field mapping rules for a known template match.

    Missing values are represented as review-required mapped fields rather than
    confirmed empty strings.
    """

    template_match = match_template_fingerprint(document_ir, template_definition)
    output_mapping = _mapping(template_definition.get("output_mapping"))
    root_key = str(output_mapping.get("root_key") or "template_result")
    warnings: list[str] = []
    if template_match.classification is not TemplateMatchClassification.KNOWN:
        warnings.append("template field mapping requires known template classification")

    anchors = [_mapping(anchor) for anchor in _list_value(template_definition.get("anchors"))]
    output_keys_by_field_id = {
        str(mapping.get("field_id")): str(mapping.get("output_key"))
        for mapping in (_mapping(value) for value in _list_value(output_mapping.get("field_map")))
        if isinstance(mapping.get("field_id"), str) and isinstance(mapping.get("output_key"), str)
    }
    fields = [_mapping(value) for value in _list_value(template_definition.get("fields"))]
    output_keys_by_field_id_for_conflicts = {
        str(field.get("field_id") or ""): output_keys_by_field_id.get(str(field.get("field_id") or ""))
        or str(field.get("output_key") or field.get("field_id") or "")
        for field in fields
    }
    table_output_keys_by_id_for_conflicts = {
        f"table:{mapping.get('table_id')}": str(mapping.get("output_key") or "")
        for mapping in (_mapping(value) for value in _list_value(output_mapping.get("table_map")))
        if isinstance(mapping.get("table_id"), str) and isinstance(mapping.get("output_key"), str)
    }
    output_conflicts = _output_key_conflicts(
        {**output_keys_by_field_id_for_conflicts, **table_output_keys_by_id_for_conflicts}
    )
    for conflict_warnings in output_conflicts.values():
        warnings.extend(conflict_warnings)

    mapped_fields: list[TemplateMappedField] = []
    output: dict[str, Any] = {}
    for field in fields:
        field_id = str(field.get("field_id") or "")
        label = str(field.get("label") or field_id)
        output_key = output_keys_by_field_id.get(field_id) or str(field.get("output_key") or field_id)
        required = field.get("required") is True
        output_conflict_warnings = tuple(output_conflicts.get(output_key, ()))
        extracted = (
            _extract_template_field_value(document_ir, field, anchors, fields, template_definition)
            if template_match.classification is TemplateMatchClassification.KNOWN
            else None
        )
        if extracted is None:
            missing_warnings = (
                (f"template field '{field_id or '<unknown>'}' missing; requires review",)
                if required
                else ()
            )
            field_warnings = (*missing_warnings, *output_conflict_warnings)
            warnings.extend(field_warnings)
            mapped_fields.append(
                TemplateMappedField(
                    field_id=field_id,
                    label=label,
                    output_key=output_key,
                    value=None,
                    confidence=0.0,
                    evidence={},
                    requires_review=required or bool(output_conflict_warnings),
                    warnings=field_warnings,
                )
            )
            continue

        block_warnings = tuple(extracted.block.review.warnings)
        field_warnings = (*block_warnings, *output_conflict_warnings)
        requires_review = (
            extracted.block.review.requires_review
            or bool(block_warnings)
            or bool(output_conflict_warnings)
        )
        mapped_fields.append(
            TemplateMappedField(
                field_id=field_id,
                label=label,
                output_key=output_key,
                value=extracted.value,
                confidence=extracted.confidence,
                evidence={
                    "source_page": extracted.block.source_page,
                    "block_id": extracted.block.id,
                    "bbox": _bbox_evidence(extracted.block),
                    **extracted.evidence_detail,
                },
                requires_review=requires_review,
                warnings=field_warnings,
            )
        )
        if not requires_review:
            _set_output_value(output, output_key, extracted.value)

    result_warnings = tuple(dict.fromkeys([*template_match.warnings, *warnings]))
    return TemplateFieldMappingResult(
        root_key=root_key,
        fields=tuple(mapped_fields),
        output={root_key: output},
        requires_review=bool(result_warnings)
        or any(field.requires_review for field in mapped_fields),
        warnings=result_warnings,
    )


def _best_anchor_match_score(
    anchor: Mapping[str, Any], blocks: Sequence[DocumentBlock], *, parse_xlsx_cell_refs: bool
) -> float:
    matching_blocks = _blocks_matching_anchor(
        anchor,
        blocks,
        parse_xlsx_cell_refs=parse_xlsx_cell_refs,
    )
    if not matching_blocks:
        return 0.0
    scores = [
        _anchor_block_match_score(
            anchor,
            block,
            parse_xlsx_cell_refs=parse_xlsx_cell_refs,
        )
        for block in matching_blocks
    ]
    return max(scores, default=0.0)


def _blocks_matching_anchor(
    anchor: Mapping[str, Any], blocks: Sequence[DocumentBlock], *, parse_xlsx_cell_refs: bool
) -> list[DocumentBlock]:
    scope = _mapping(anchor.get("scope"))
    expected_text = str(anchor.get("text") or "")
    match_mode = str(anchor.get("match") or "normalized")
    return [
        block
        for block in blocks
        if _block_matches_anchor_scope(anchor, block, scope)
        and _anchor_block_match_score(
            anchor,
            block,
            expected_text,
            match_mode,
            parse_xlsx_cell_refs=parse_xlsx_cell_refs,
        )
        > 0.0
    ]


def _anchor_block_match_score(
    anchor: Mapping[str, Any],
    block: DocumentBlock,
    expected_text: str | None = None,
    match_mode: str | None = None,
    *,
    parse_xlsx_cell_refs: bool,
) -> float:
    expected = str(anchor.get("text") or "") if expected_text is None else expected_text
    mode = str(anchor.get("match") or "normalized") if match_mode is None else match_mode
    if str(anchor.get("kind") or "") == "table_header":
        if block.type != "table":
            return 0.0
        return _table_anchor_match_score(
            block.text,
            expected,
            mode,
            parse_xlsx_cell_refs=parse_xlsx_cell_refs,
        )
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
    *,
    source_type: str,
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
        table_blocks = (
            _blocks_matching_anchor(
                anchor,
                blocks,
                parse_xlsx_cell_refs=source_type == "xlsx",
            )
            if anchor
            else []
        )
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
                _table_required_column_score(
                    block.text,
                    anchor,
                    required_columns,
                    parse_xlsx_cell_refs=source_type == "xlsx",
                    allow_merged_column_candidates=_block_allows_merged_column_candidates(
                        block, source_type
                    ),
                ),
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


def _anchor_is_required(
    anchor: Mapping[str, Any], required_anchor_ids: set[str], optional_anchor_ids: set[str]
) -> bool:
    anchor_id = str(anchor.get("anchor_id") or "")
    kind = str(anchor.get("kind") or "")
    if anchor_id in required_anchor_ids:
        return True
    if anchor_id in optional_anchor_ids:
        return False
    return kind in {"heading", "table_header"}


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


def _optional_anchor_ids(
    template_definition: Mapping[str, Any], required_anchor_ids: set[str]
) -> set[str]:
    anchor_ids: set[str] = set()
    for field in (_mapping(value) for value in _list_value(template_definition.get("fields"))):
        if field.get("required") is True:
            continue
        source = _mapping(field.get("source"))
        anchor_id = source.get("anchor_id")
        if isinstance(anchor_id, str) and anchor_id.strip() and anchor_id not in required_anchor_ids:
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


def _extract_template_field_value(
    document_ir: DocumentIRV1,
    field: Mapping[str, Any],
    anchors: Sequence[Mapping[str, Any]],
    fields: Sequence[Mapping[str, Any]],
    template_definition: Mapping[str, Any],
) -> _ExtractedTemplateFieldValue | None:
    source = _mapping(field.get("source"))
    anchor_id = str(source.get("anchor_id") or "")
    direction = str(source.get("direction") or "")
    anchor = next((anchor for anchor in anchors if str(anchor.get("anchor_id") or "") == anchor_id), None)
    if anchor is None:
        return None

    anchor_blocks = _blocks_matching_anchor(
        anchor,
        document_ir.blocks,
        parse_xlsx_cell_refs=document_ir.document.source_type == "xlsx",
    )
    if not anchor_blocks:
        return None
    if direction == "table_cell":
        return _extract_template_table_cell(document_ir, field, anchor, anchor_blocks, template_definition)
    return _extract_template_nearby_field(document_ir, field, anchor, anchor_blocks, fields, direction)


def _extract_template_nearby_field(
    document_ir: DocumentIRV1,
    field: Mapping[str, Any],
    anchor: Mapping[str, Any],
    anchor_blocks: Sequence[DocumentBlock],
    fields: Sequence[Mapping[str, Any]],
    direction: str,
) -> _ExtractedTemplateFieldValue | None:
    label = str(field.get("label") or field.get("field_id") or "")
    anchor_text = str(anchor.get("text") or "")
    stop_markers = _field_stop_markers(field, fields)
    if direction in {"same_block", "right"}:
        for block in anchor_blocks:
            value = _field_value_from_text(block.text, label, anchor_text, stop_markers=stop_markers)
            if value:
                return _template_value(block, value, 0.98, direction=direction)
        if direction == "right":
            for anchor_block in anchor_blocks:
                for block in _right_side_blocks(document_ir.blocks, anchor_block):
                    value = _field_value_from_text(block.text, label, anchor_text, stop_markers=stop_markers)
                    if value is None:
                        value = block.text.strip() or None
                    if value:
                        return _template_value(block, value, 0.94, direction=direction)
        return None

    if direction != "below":
        return None

    ordered_blocks = sorted(
        document_ir.blocks,
        key=lambda block: (block.source_page, block.bbox.y, block.bbox.x, block.id),
    )
    for anchor_block in anchor_blocks:
        for block in ordered_blocks:
            if block.id == anchor_block.id:
                continue
            if block.source_page != anchor_block.source_page:
                continue
            if block.bbox.y < anchor_block.bbox.y:
                continue
            value = _field_value_from_text(
                block.text,
                label,
                anchor_text,
                stop_markers=stop_markers,
                allow_anchor_fallback=False,
            )
            if value is None:
                value = _field_value_from_below_label_anchor(
                    block.text,
                    label,
                    anchor_text,
                    stop_markers=stop_markers,
                )
            if value:
                return _template_value(block, value, 0.95, direction=direction)
    return None


def _extract_template_table_cell(
    document_ir: DocumentIRV1,
    field: Mapping[str, Any],
    anchor: Mapping[str, Any],
    anchor_blocks: Sequence[DocumentBlock],
    template_definition: Mapping[str, Any],
) -> _ExtractedTemplateFieldValue | None:
    label = str(field.get("label") or field.get("field_id") or "")
    normalized_label = _normalized_column_name(label)
    if not normalized_label:
        return None
    table_definition = _field_mapping_table_definition(anchor, template_definition)
    required_columns = [str(column) for column in _list_value(table_definition.get("required_columns"))]
    matching_blocks = _field_mapping_table_blocks(document_ir, anchor, anchor_blocks, template_definition)
    for block in matching_blocks:
        parsed_rows = _parsed_table_rows(
            block.text,
            parse_xlsx_cell_refs=document_ir.document.source_type == "xlsx",
        )
        header_index = _table_header_row_index(
            parsed_rows.rows,
            anchor,
            normalized_label,
            required_columns=required_columns,
            allow_merged_column_candidates=(
                parsed_rows.allow_merged_column_candidates
                and _block_allows_merged_column_candidates(block, document_ir.document.source_type)
            ),
        )
        if header_index is None:
            continue
        header, column_offset = _table_header_candidate_row(
            parsed_rows.rows,
            header_index,
            anchor,
            preserve_column_positions=parsed_rows.preserve_column_positions,
        )
        column_index = _row_column_index(
            header,
            normalized_label,
            allow_merged_column_candidates=(
                parsed_rows.allow_merged_column_candidates
                and _block_allows_merged_column_candidates(block, document_ir.document.source_type)
            ),
        )
        if column_index is None:
            continue
        actual_column_index = column_offset + column_index
        for row_index, row in enumerate(parsed_rows.rows[header_index + 1 :], start=header_index + 1):
            value = _table_cell_value_at_physical_column(row, actual_column_index)
            if value:
                return _template_value(
                    block,
                    value,
                    0.90,
                    direction="table_cell",
                    row_index=row_index,
                    column_index=actual_column_index,
                    column_label=label,
                )
    return None


def _field_mapping_table_blocks(
    document_ir: DocumentIRV1,
    anchor: Mapping[str, Any],
    anchor_blocks: Sequence[DocumentBlock],
    template_definition: Mapping[str, Any],
) -> Sequence[DocumentBlock]:
    table_definition = _field_mapping_table_definition(anchor, template_definition)
    required_columns = [str(column) for column in _list_value(table_definition.get("required_columns"))]
    if not required_columns:
        return anchor_blocks
    matching_blocks = [
        block
        for block in anchor_blocks
        if _table_required_column_score(
            block.text,
            anchor,
            required_columns,
            parse_xlsx_cell_refs=document_ir.document.source_type == "xlsx",
            allow_merged_column_candidates=_block_allows_merged_column_candidates(
                block, document_ir.document.source_type
            ),
        )
        >= 1.0
    ]
    return matching_blocks


def _field_mapping_table_definition(
    anchor: Mapping[str, Any], template_definition: Mapping[str, Any]
) -> Mapping[str, Any]:
    anchor_id = str(anchor.get("anchor_id") or "")
    return next(
        (
            _mapping(table)
            for table in _list_value(template_definition.get("tables"))
            if str(_mapping(table).get("anchor_id") or "") == anchor_id
        ),
        {},
    )


def _table_cell_value_at_physical_column(row: Sequence[str], column_index: int) -> str | None:
    if column_index >= len(row):
        return None
    value = str(row[column_index]).strip()
    return value or None


def _field_stop_markers(
    field: Mapping[str, Any], fields: Sequence[Mapping[str, Any]]
) -> tuple[str, ...]:
    field_id = str(field.get("field_id") or "")
    markers: list[str] = []
    for candidate in fields:
        candidate_id = str(candidate.get("field_id") or "")
        if candidate_id == field_id:
            continue
        for marker in (candidate.get("label"), candidate_id):
            marker_text = str(marker or "").strip()
            if marker_text:
                markers.append(marker_text)
    return tuple(dict.fromkeys(markers))


def _field_value_from_text(
    text: str,
    label: str,
    anchor_text: str,
    *,
    stop_markers: Sequence[str] = (),
    allow_anchor_fallback: bool = True,
) -> str | None:
    label_value = _value_after_marker(text, label, stop_markers=stop_markers)
    if label_value:
        return label_value
    if not allow_anchor_fallback:
        return None
    return _value_after_marker(text, anchor_text, stop_markers=stop_markers)


def _field_value_from_below_label_anchor(
    text: str,
    label: str,
    anchor_text: str,
    *,
    stop_markers: Sequence[str] = (),
) -> str | None:
    if _normalized_text(label) != _normalized_text(anchor_text):
        return None
    value = _value_before_next_marker(text.strip(), stop_markers).strip()
    if not value or _normalized_text(value) == _normalized_text(label):
        return None
    return value


def _value_after_marker(
    text: str, marker: str, *, stop_markers: Sequence[str] = ()
) -> str | None:
    normalized_marker = marker.casefold().strip()
    if not normalized_marker:
        return None
    for line in text.splitlines() or [text]:
        for match in re.finditer(re.escape(marker.strip()), line, flags=re.IGNORECASE):
            if not _marker_match_has_boundaries(line, match.start(), match.end()):
                continue
            value = _candidate_value_after_marker_match(line, match.end(), stop_markers)
            if value:
                return value
    return None


def _candidate_value_after_marker_match(
    line: str, marker_end: int, stop_markers: Sequence[str]
) -> str:
    value = line[marker_end:].strip()
    value = re.sub(r"^[\s:：=-]+", "", value).strip()
    return _value_before_next_marker(value, stop_markers)


def _value_before_next_marker(value: str, stop_markers: Sequence[str]) -> str:
    earliest_stop: int | None = None
    for marker in stop_markers:
        marker_text = marker.strip()
        if not marker_text:
            continue
        for match in re.finditer(re.escape(marker_text), value, flags=re.IGNORECASE):
            if not _marker_match_has_boundaries(value, match.start(), match.end()):
                continue
            earliest_stop = match.start() if earliest_stop is None else min(earliest_stop, match.start())
            break
    if earliest_stop is None:
        return value
    return re.sub(r"[\s:：=-]+$", "", value[:earliest_stop]).strip()


def _marker_match_has_boundaries(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return _is_marker_boundary(before) and _is_marker_boundary(after)


def _is_marker_boundary(value: str) -> bool:
    return not value or value.isspace() or (not value.isalnum() and value != "_")


def _template_value(
    block: DocumentBlock,
    value: str,
    rule_confidence: float,
    **evidence_detail: Any,
) -> _ExtractedTemplateFieldValue:
    return _ExtractedTemplateFieldValue(
        value=value,
        block=block,
        confidence=_bounded_score(block.confidence * rule_confidence),
        evidence_detail=evidence_detail,
    )


def _bbox_evidence(block: DocumentBlock) -> dict[str, Any]:
    return {
        "x": block.bbox.x,
        "y": block.bbox.y,
        "width": block.bbox.width,
        "height": block.bbox.height,
        "unit": block.bbox.unit,
        "origin": block.bbox.origin,
    }


def _set_output_value(output: dict[str, Any], output_key: str, value: str) -> None:
    path = [part for part in output_key.split(".") if part]
    if not path:
        return
    cursor = output
    for part in path[:-1]:
        next_value = cursor.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            cursor[part] = next_value
        cursor = next_value
    cursor[path[-1]] = value


def _output_key_conflicts(output_keys_by_field_id: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    path_entries = [
        (field_id or "<unknown>", output_key, tuple(part for part in output_key.split(".") if part))
        for field_id, output_key in output_keys_by_field_id.items()
        if output_key
    ]
    conflicts: dict[str, list[str]] = {}
    for index, (left_field_id, left_key, left_path) in enumerate(path_entries):
        if not left_path:
            continue
        for right_field_id, right_key, right_path in path_entries[index + 1 :]:
            if not right_path:
                continue
            if left_path == right_path:
                warning = (
                    f"template output_key '{left_key}' is shared by fields "
                    f"'{left_field_id}' and '{right_field_id}'; requires review"
                )
            elif _is_output_path_prefix(left_path, right_path) or _is_output_path_prefix(
                right_path, left_path
            ):
                warning = (
                    f"template output_key conflict between '{left_key}' and '{right_key}'; "
                    "requires review"
                )
            else:
                continue
            conflicts.setdefault(left_key, []).append(warning)
            conflicts.setdefault(right_key, []).append(warning)
    return {key: tuple(dict.fromkeys(warnings)) for key, warnings in conflicts.items()}


def _is_output_path_prefix(left: Sequence[str], right: Sequence[str]) -> bool:
    return len(left) < len(right) and tuple(right[: len(left)]) == tuple(left)


def _table_header_row_index(
    rows: Sequence[Sequence[str]],
    anchor: Mapping[str, Any],
    normalized_label: str,
    *,
    required_columns: Sequence[str] = (),
    allow_merged_column_candidates: bool,
) -> int | None:
    anchor_index = _first_table_anchor_row_index(rows, anchor)
    normalized_required_columns = {
        column for column in (_normalized_column_name(column) for column in required_columns) if column
    }
    if normalized_required_columns:
        for index, row in enumerate(rows[anchor_index:], start=anchor_index):
            candidate_row = _row_columns_excluding_anchor(row, anchor) if index == anchor_index else row
            if index != anchor_index and _looks_like_wrapped_header_fragment(candidate_row, rows, index):
                continue
            if (
                _row_required_column_score(
                    candidate_row,
                    normalized_required_columns,
                    allow_merged_column_candidates=allow_merged_column_candidates,
                )
                < 1.0
            ):
                continue
            if (
                _row_column_index(
                    candidate_row,
                    normalized_label,
                    allow_merged_column_candidates=allow_merged_column_candidates,
                )
                is not None
            ):
                return index
            return None
        return None
    for index, row in enumerate(rows[anchor_index:], start=anchor_index):
        candidate_row = _row_columns_excluding_anchor(row, anchor) if index == anchor_index else row
        if index != anchor_index and _looks_like_wrapped_header_fragment(candidate_row, rows, index):
            continue
        if _row_column_index(
            candidate_row,
            normalized_label,
            allow_merged_column_candidates=allow_merged_column_candidates,
        ) is not None:
            return index
    return None


def _table_header_candidate_row(
    rows: Sequence[Sequence[str]],
    header_index: int,
    anchor: Mapping[str, Any],
    *,
    preserve_column_positions: bool,
) -> tuple[Sequence[str], int]:
    row = rows[header_index]
    if header_index == _first_table_anchor_row_index(rows, anchor):
        anchor_columns, column_offset = _row_columns_excluding_anchor_with_offset(
            row, anchor, preserve_column_positions=preserve_column_positions
        )
        if anchor_columns:
            return anchor_columns, column_offset
    return row, 0


def _row_column_index(
    row: Sequence[str],
    normalized_label: str,
    *,
    allow_merged_column_candidates: bool,
) -> int | None:
    for index, cell in enumerate(row):
        if _normalized_column_name(cell) == normalized_label:
            return index
    if not allow_merged_column_candidates:
        return None
    merged = ""
    start_index = 0
    for index, cell in enumerate(row):
        if not merged:
            start_index = index
            merged = str(cell)
        else:
            merged = f"{merged} {cell}"
        if _normalized_column_name(merged) == normalized_label:
            return start_index
        if _normalized_column_name(merged) not in normalized_label:
            merged = str(cell)
            start_index = index
    return None


def _block_allows_merged_column_candidates(block: DocumentBlock, source_type: str) -> bool:
    return source_type != "pdf"


def _right_side_blocks(
    blocks: Sequence[DocumentBlock], anchor_block: DocumentBlock
) -> list[DocumentBlock]:
    return sorted(
        [
            block
            for block in blocks
            if block.id != anchor_block.id
            and block.source_page == anchor_block.source_page
            and block.bbox.x >= anchor_block.bbox.x + anchor_block.bbox.width
            and _blocks_vertically_overlap(block, anchor_block)
        ],
        key=lambda block: (block.bbox.x, block.bbox.y, block.id),
    )


def _blocks_vertically_overlap(left: DocumentBlock, right: DocumentBlock) -> bool:
    left_top = left.bbox.y
    left_bottom = left.bbox.y + left.bbox.height
    right_top = right.bbox.y
    right_bottom = right.bbox.y + right.bbox.height
    return min(left_bottom, right_bottom) > max(left_top, right_top)


def _table_anchor_match_score(
    value: str, expected_text: str, match_mode: str, *, parse_xlsx_cell_refs: bool
) -> float:
    return max(
        (
            _row_anchor_match_score(row, expected_text, match_mode)
            for row in _table_rows(value, parse_xlsx_cell_refs=parse_xlsx_cell_refs)
        ),
        default=0.0,
    )


def _table_required_column_score(
    value: str,
    anchor: Mapping[str, Any],
    required_columns: Sequence[str],
    *,
    parse_xlsx_cell_refs: bool,
    allow_merged_column_candidates: bool,
) -> float:
    normalized_required_columns = [_normalized_column_name(column) for column in required_columns]
    normalized_required_columns = [column for column in normalized_required_columns if column]
    if not normalized_required_columns:
        return 1.0

    parsed_rows = _parsed_table_rows(value, parse_xlsx_cell_refs=parse_xlsx_cell_refs)
    rows = parsed_rows.rows
    required_column_set = set(normalized_required_columns)
    candidate_rows = _table_required_column_candidate_rows(
        rows,
        anchor,
        required_column_set,
        allow_merged_column_candidates=(
            parsed_rows.allow_merged_column_candidates and allow_merged_column_candidates
        ),
    )
    if parsed_rows.allow_merged_column_candidates and allow_merged_column_candidates:
        candidate_rows.extend(_wrapped_cell_candidate_rows(rows, anchor, required_column_set))
    best_score = 0.0
    for row in candidate_rows:
        best_score = max(
            best_score,
            _row_required_column_score(
                row,
                required_column_set,
                allow_merged_column_candidates=(
                    parsed_rows.allow_merged_column_candidates and allow_merged_column_candidates
                ),
            ),
        )
    return best_score


def _table_required_column_candidate_rows(
    rows: Sequence[Sequence[str]],
    anchor: Mapping[str, Any],
    required_columns: set[str],
    *,
    allow_merged_column_candidates: bool,
) -> list[Sequence[str]]:
    anchor_index = _first_table_anchor_row_index(rows, anchor)
    candidate_rows: list[Sequence[str]] = []
    if anchor_index < len(rows):
        anchor_columns = _row_columns_excluding_anchor(rows[anchor_index], anchor)
        if anchor_columns:
            candidate_rows.append(anchor_columns)
            if (
                _row_required_column_score(
                    anchor_columns,
                    required_columns,
                    allow_merged_column_candidates=allow_merged_column_candidates,
                )
                >= 1.0
                or not _looks_like_same_row_table_note(anchor_columns, required_columns)
            ):
                return candidate_rows
    for row in rows[anchor_index + 1 :]:
        if len(row) == 1 and len(required_columns) > 1:
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


def _row_required_column_score(
    row: Sequence[str],
    required_columns: set[str],
    *,
    allow_merged_column_candidates: bool,
) -> float:
    if not required_columns:
        return 1.0
    column_names = _row_column_name_candidates(
        row,
        required_columns,
        allow_merged_column_candidates=allow_merged_column_candidates,
    )
    if not column_names:
        return 0.0
    matched_columns = sum(1 for column in required_columns if column in column_names)
    return matched_columns / len(required_columns)


def _row_column_name_candidates(
    row: Sequence[str],
    required_columns: set[str],
    *,
    allow_merged_column_candidates: bool,
) -> set[str]:
    column_names = {_normalized_column_name(cell) for cell in row}
    column_names.discard("")
    if not required_columns or not allow_merged_column_candidates:
        return column_names
    for start in range(len(row)):
        merged = ""
        for end in range(start, len(row)):
            merged = f"{merged} {row[end]}" if merged else str(row[end])
            normalized_merged = _normalized_column_name(merged)
            if normalized_merged in required_columns:
                column_names.add(normalized_merged)
    return column_names


def _looks_like_same_row_table_note(row: Sequence[str], required_columns: set[str]) -> bool:
    if len(row) != 1:
        return False
    normalized = _normalized_column_name(str(row[0]))
    return normalized not in required_columns and " " in normalized


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
    columns, _offset = _row_columns_excluding_anchor_with_offset(
        row, anchor, preserve_column_positions=False
    )
    if not any(_normalized_column_name(cell) for cell in columns):
        return []
    return columns


def _row_columns_excluding_anchor_with_offset(
    row: Sequence[str], anchor: Mapping[str, Any], *, preserve_column_positions: bool
) -> tuple[Sequence[str], int]:
    expected_text = str(anchor.get("text") or "")
    if not expected_text:
        return row, 0
    match_mode = str(anchor.get("match") or "normalized")
    for index, cell in enumerate(row):
        if _text_match_score(expected_text, cell, match_mode) > 0.0:
            return row[index + 1 :], index + 1 if preserve_column_positions or index > 0 else 0
    anchor_end = _anchor_cell_span_end_index(row, expected_text, match_mode)
    if anchor_end is not None:
        return row[anchor_end:], anchor_end if preserve_column_positions or anchor_end > 1 else 0
    return [], 0


def _looks_like_wrapped_header_fragment(
    row: Sequence[str], rows: Sequence[Sequence[str]], index: int
) -> bool:
    return len(row) == 1 and index + 1 < len(rows) and len(rows[index + 1]) > 1


def _anchor_cell_span_end_index(
    row: Sequence[str], expected_text: str, match_mode: str
) -> int | None:
    for start in range(len(row)):
        merged = ""
        for end in range(start, len(row)):
            merged = f"{merged}\t{row[end]}" if merged else str(row[end])
            if _text_match_score(expected_text, merged, match_mode) > 0.0:
                return end + 1
    return None


def _row_anchor_match_score(row: Sequence[str], expected_text: str, match_mode: str) -> float:
    row_text = "\t".join(row)
    return max(
        [_text_match_score(expected_text, row_text, match_mode)]
        + [_text_match_score(expected_text, cell, match_mode) for cell in row],
        default=0.0,
    )


def _row_matches_anchor(row: Sequence[str], anchor: Mapping[str, Any]) -> bool:
    expected_text = str(anchor.get("text") or "")
    if not expected_text:
        return False
    match_mode = str(anchor.get("match") or "normalized")
    return _row_anchor_match_score(row, expected_text, match_mode) > 0.0


def _table_rows(value: str, *, parse_xlsx_cell_refs: bool) -> list[list[str]]:
    return _parsed_table_rows(value, parse_xlsx_cell_refs=parse_xlsx_cell_refs).rows


def _parsed_table_rows(value: str, *, parse_xlsx_cell_refs: bool) -> _ParsedTableRows:
    if parse_xlsx_cell_refs:
        xlsx_rows = _xlsx_cell_rows(value)
        if xlsx_rows:
            return _ParsedTableRows(
                rows=xlsx_rows,
                allow_merged_column_candidates=False,
                preserve_column_positions=True,
            )
    return _ParsedTableRows(
        rows=[
            cells
            for line in value.splitlines()
            if (cells := _table_row_cells(line))
        ],
        allow_merged_column_candidates=True,
    )


def _table_row_cells(value: str) -> list[str]:
    cells = _split_table_row(value)
    if "\t" in value or "|" in value or "," in value:
        return cells if any(_normalized_column_name(cell) for cell in cells) else []
    return [cell for cell in cells if _normalized_column_name(cell)]


def _split_table_row(value: str) -> list[str]:
    if "\t" in value:
        return value.split("\t")
    if "|" in value:
        return value.split("|")
    if "," in value:
        return re.split(r"\s*,\s*", value)
    return re.split(r"\s{2,}", value)


def _xlsx_cell_rows(value: str) -> list[list[str]]:
    lines = [line for line in value.splitlines() if line.strip()]
    row_cells: dict[int, dict[int, str]] = {}
    saw_cell_ref = False
    last_cell_ref: tuple[int, int] | None = None
    for line_index, line in enumerate(lines):
        cell = _xlsx_cell_line(line)
        if cell is None:
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
    max_column = max((max(columns) for columns in row_cells.values() if columns), default=0)
    return [
        [columns.get(column, "") for column in range(1, max_column + 1)]
        for _row, columns in sorted(row_cells.items())
        if any(_normalized_column_name(cell) for cell in columns.values())
    ]


def _xlsx_cell_line(value: str) -> tuple[int, int, str] | None:
    match = re.match(r"^([A-Za-z]+)([1-9][0-9]*):(.*)$", value)
    if not match:
        return None
    column_letters, row_number, cell_value = match.groups()
    if cell_value.startswith(" "):
        cell_value = cell_value[1:]
    return int(row_number), _xlsx_column_index(column_letters), cell_value


def _xlsx_column_index(value: str) -> int:
    column_index = 0
    for character in value.upper():
        column_index = (column_index * 26) + (ord(character) - ord("A") + 1)
    return column_index
