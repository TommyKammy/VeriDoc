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
    anchor_score = 0.0
    for anchor in anchors:
        anchor_id = str(anchor.get("anchor_id") or "")
        match_score = _best_anchor_match_score(anchor, document_ir.blocks)
        anchor_score += match_score
        if match_score > 0.0:
            matched_anchor_ids.append(anchor_id)
        else:
            missing_anchor_ids.append(anchor_id)
            warnings.append(f"template anchor '{anchor_id or '<unknown>'}' missing from document")
            if _anchor_is_required(anchor, required_anchor_ids):
                fail_closed = True

    scoped_pages = {
        page
        for page in (_scope_page(_mapping(anchor.get("scope"))) for anchor in anchors)
        if page is not None
    }
    page_score = 1.0
    if scoped_pages:
        matched_pages = sum(1 for page in scoped_pages if page in page_numbers)
        page_score = matched_pages / len(scoped_pages)
        if matched_pages != len(scoped_pages):
            warnings.append("document pages do not satisfy template anchor scopes")
            fail_closed = True

    table_score = _table_score(table_definitions, anchors, document_ir.blocks, warnings)

    score = _weighted_average(
        (
            (anchor_score / len(anchors), 0.60) if anchors else (0.0, 0.60),
            (page_score, 0.20),
            (table_score, 0.20),
        )
    )
    if fail_closed:
        score = min(score, FAIL_CLOSED_MAX_SCORE)
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
    scores = [
        _text_match_score(str(anchor.get("text") or ""), block.text, str(anchor.get("match") or "normalized"))
        for block in matching_blocks
    ]
    return max(scores, default=0.0)


def _blocks_matching_anchor(anchor: Mapping[str, Any], blocks: Sequence[DocumentBlock]) -> list[DocumentBlock]:
    scope = _mapping(anchor.get("scope"))
    expected_text = str(anchor.get("text") or "")
    match_mode = str(anchor.get("match") or "normalized")
    return [
        block
        for block in blocks
        if _block_matches_scope(block, scope)
        and _text_match_score(expected_text, block.text, match_mode) > 0.0
    ]


def _block_matches_scope(block: DocumentBlock, scope: Mapping[str, Any]) -> bool:
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
) -> float:
    if not table_definitions:
        return 1.0

    anchors_by_id = {str(anchor.get("anchor_id") or ""): anchor for anchor in anchors}
    scores: list[float] = []
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
            column_names = _table_column_names(block.text)
            matched_columns = sum(
                1 for column in required_columns if _normalized_text(column) in column_names
            )
            best_column_score = max(best_column_score, matched_columns / len(required_columns))
        if best_column_score < 1.0:
            warnings.append(f"template table '{table_id}' required columns incomplete")
        scores.append(best_column_score)
    return sum(scores) / len(scores)


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


def _table_column_names(value: str) -> set[str]:
    columns: set[str] = set()
    for line in value.splitlines():
        for cell in re.split(r"\t+|\s*\|\s*|\s*,\s*|\s{2,}", line):
            normalized_cell = _normalized_text(cell)
            if normalized_cell:
                columns.add(normalized_cell)
    return columns
