from __future__ import annotations

from datetime import date, datetime
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
ALWAYS_REVIEW_RISK_LEVELS = frozenset({"high", "critical"})


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
class _TableHeaderCandidate:
    row_index: int
    header: Sequence[str]
    column_offset: int
    comparison_headers: tuple[Sequence[str], ...]


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
        warnings.append(_known_template_mapping_required_warning())
    match_requires_review = _template_match_requires_field_review(template_match)

    anchors = [_mapping(anchor) for anchor in _list_value(template_definition.get("anchors"))]
    output_keys_by_field_id = {
        str(mapping.get("field_id")): str(mapping.get("output_key"))
        for mapping in (_mapping(value) for value in _list_value(output_mapping.get("field_map")))
        if isinstance(mapping.get("field_id"), str) and isinstance(mapping.get("output_key"), str)
    }
    fields = [_mapping(value) for value in _list_value(template_definition.get("fields"))]
    output_conflicts = _output_key_conflicts(
        _field_and_table_output_keys_for_conflicts(fields, output_mapping, output_keys_by_field_id)
    )
    review_required_levels = _template_review_required_levels(template_definition)
    defined_risk_levels = _template_defined_risk_levels(template_definition)
    risk_rank_declared = "risk_rank" in template_definition
    for conflict_warnings in output_conflicts.values():
        warnings.extend(conflict_warnings)

    mapped_fields: list[TemplateMappedField] = []
    output: dict[str, Any] = {}
    extracted_by_field_id: dict[str, _ExtractedTemplateFieldValue | None] = {}
    if template_match.classification is TemplateMatchClassification.KNOWN:
        extracted_by_field_id = {
            str(field.get("field_id") or ""): _extract_template_field_value(
                document_ir,
                field,
                anchors,
                fields,
                template_definition,
            )
            for field in fields
        }
    reviewed_field_values_by_id = _reviewed_template_field_values_by_id(
        fields,
        extracted_by_field_id,
        template_definition,
        output_conflicts,
        output_keys_by_field_id,
        risk_rank_declared,
        defined_risk_levels,
        review_required_levels,
        match_requires_review,
    )
    for field in fields:
        field_id = str(field.get("field_id") or "")
        label = str(field.get("label") or field_id)
        output_key = output_keys_by_field_id.get(field_id) or str(field.get("output_key") or field_id)
        required = field.get("required") is True
        output_conflict_warnings = tuple(output_conflicts.get(output_key, ()))
        risk_warnings = _template_field_risk_warnings(
            field,
            risk_rank_declared=risk_rank_declared,
            defined_levels=defined_risk_levels,
            review_required_levels=review_required_levels,
        )
        extracted = extracted_by_field_id.get(field_id)
        if extracted is None:
            missing_warnings = (
                (f"template field '{field_id or '<unknown>'}' missing; requires review",)
                if required
                else ()
            )
            absent_risk_warnings = _template_absent_field_risk_warnings(
                field,
                risk_warnings,
                required=required,
                risk_rank_declared=risk_rank_declared,
                defined_levels=defined_risk_levels,
            )
            field_warnings = (*missing_warnings, *absent_risk_warnings, *output_conflict_warnings)
            warnings.extend(field_warnings)
            mapped_fields.append(
                TemplateMappedField(
                    field_id=field_id,
                    label=label,
                    output_key=output_key,
                    value=None,
                    confidence=0.0,
                    evidence={},
                    requires_review=required
                    or bool(absent_risk_warnings)
                    or bool(output_conflict_warnings),
                    warnings=field_warnings,
                )
            )
            continue

        block_warnings = tuple(extracted.block.review.warnings)
        validation_warnings = _template_field_value_validation_warnings(
            field,
            extracted.value,
            template_definition,
            reviewed_field_values_by_id,
        )
        match_warnings = _template_match_review_warnings(match_requires_review)
        field_warnings = (
            *match_warnings,
            *block_warnings,
            *risk_warnings,
            *validation_warnings,
            *output_conflict_warnings,
        )
        requires_review = _template_field_requires_review(
            match_requires_review or extracted.block.review.requires_review,
            match_warnings,
            block_warnings,
            risk_warnings,
            validation_warnings,
            output_conflict_warnings,
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
        if _template_match_allows_confirmed_output(
            match_requires_review
        ) and _mapped_field_output_is_confirmed(requires_review):
            _set_output_value(output, output_key, extracted.value)
        else:
            warnings.extend(field_warnings)

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


def _normalized_risk_level(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().casefold()


def _template_review_required_levels(template_definition: Mapping[str, Any]) -> set[str]:
    risk_rank = _mapping(template_definition.get("risk_rank"))
    return {
        normalized
        for level in _list_value(risk_rank.get("review_required_levels"))
        if (normalized := _normalized_risk_level(level))
    }


def _template_defined_risk_levels(template_definition: Mapping[str, Any]) -> set[str]:
    risk_rank = _mapping(template_definition.get("risk_rank"))
    return {
        normalized
        for level in (_mapping(level) for level in _list_value(risk_rank.get("levels")))
        if (normalized := _normalized_risk_level(level.get("level")))
    }


def _template_field_risk_warnings(
    field: Mapping[str, Any],
    *,
    risk_rank_declared: bool,
    defined_levels: set[str],
    review_required_levels: set[str],
) -> tuple[str, ...]:
    # GMP/data-integrity guard: high/critical risk always requires review. Once
    # a matrix is declared, missing or non-matrix risk also fails closed.
    field_id = str(field.get("field_id") or "<unknown>")
    raw_risk_level = field.get("risk_level")
    if raw_risk_level is not None and not isinstance(raw_risk_level, str):
        if not risk_rank_declared:
            return ()
        return (
            f"template field '{field_id}' risk_level '{raw_risk_level}' is not defined "
            "by template risk_rank; requires review",
        )
    risk_level = _normalized_risk_level(raw_risk_level)
    if not risk_rank_declared:
        if risk_level in ALWAYS_REVIEW_RISK_LEVELS:
            return (
                f"template field '{field_id}' risk_level '{raw_risk_level}' requires review",
            )
        return ()
    if not risk_level:
        return (f"template field '{field_id}' missing risk_level; requires review",)
    if risk_level not in defined_levels:
        return (
            f"template field '{field_id}' risk_level '{raw_risk_level}' is not defined "
            "by template risk_rank; requires review",
        )
    if risk_level in ALWAYS_REVIEW_RISK_LEVELS or risk_level in review_required_levels:
        return (
            f"template field '{field_id}' risk_level '{raw_risk_level}' requires review",
        )
    return ()


def _template_absent_field_risk_warnings(
    field: Mapping[str, Any],
    risk_warnings: Sequence[str],
    *,
    required: bool,
    risk_rank_declared: bool,
    defined_levels: set[str],
) -> tuple[str, ...]:
    if required:
        return tuple(risk_warnings)
    if not risk_rank_declared:
        return ()
    raw_risk_level = field.get("risk_level")
    risk_level = _normalized_risk_level(raw_risk_level)
    if not risk_level or risk_level not in defined_levels:
        return tuple(risk_warnings)
    return ()


def _template_field_requires_review(
    block_requires_review: bool, *warning_groups: Sequence[str]
) -> bool:
    return block_requires_review or any(bool(warnings) for warnings in warning_groups)


def _template_match_allows_confirmed_output(match_requires_review: bool) -> bool:
    return not match_requires_review


def _template_match_requires_field_review(template_match: TemplateFingerprintMatch) -> bool:
    return template_match.requires_review


def _known_template_mapping_required_warning() -> str:
    return "template field mapping requires known template classification"


def _template_match_review_warnings(match_requires_review: bool) -> tuple[str, ...]:
    if not match_requires_review:
        return ()
    return ("template match requires review; field output requires review",)


def _mapped_field_output_is_confirmed(requires_review: bool) -> bool:
    return not requires_review


def _template_field_value_validation_warnings(
    field: Mapping[str, Any],
    value: str,
    template_definition: Mapping[str, Any],
    field_values_by_id: Mapping[str, str],
    *,
    non_cross_field_rules: bool = True,
    cross_field_rules: bool = True,
) -> tuple[str, ...]:
    field_id = str(field.get("field_id") or "<unknown>")
    value_type = str(field.get("value_type") or "").strip()
    warnings: list[str] = []
    if non_cross_field_rules and value_type and not _value_matches_template_type(value, value_type):
        warnings.append(
            f"template field '{field_id}' value {value!r} does not match "
            f"value_type '{value_type}'; requires review"
        )
        return tuple(warnings)

    rules_by_id = {
        str(rule.get("rule_id") or ""): rule
        for rule in (_mapping(rule) for rule in _list_value(template_definition.get("validation_rules")))
    }
    for rule_id_value in _list_value(field.get("validation_rule_ids")):
        rule_id = str(rule_id_value or "")
        if not rule_id:
            continue
        rule = rules_by_id.get(rule_id)
        if rule is None:
            warnings.append(
                f"template field '{field_id}' references missing validation rule "
                f"'{rule_id}'; requires review"
            )
            continue
        rule_type = str(rule.get("rule_type") or "").strip().casefold()
        if rule_type == "cross_field" and not cross_field_rules:
            continue
        if rule_type != "cross_field" and not non_cross_field_rules:
            continue
        if not _value_satisfies_template_validation_rule(
            value,
            rule,
            value_type=value_type,
            field_values_by_id=field_values_by_id,
        ):
            warnings.append(
                f"template field '{field_id}' failed validation rule '{rule_id}'; "
                "requires review"
            )
    return tuple(dict.fromkeys(warnings))


def _reviewed_template_field_values_by_id(
    fields: Sequence[Mapping[str, Any]],
    extracted_by_field_id: Mapping[str, _ExtractedTemplateFieldValue | None],
    template_definition: Mapping[str, Any],
    output_conflicts: Mapping[str, Sequence[str]],
    output_keys_by_field_id: Mapping[str, str],
    risk_rank_declared: bool,
    defined_levels: set[str],
    review_required_levels: set[str],
    match_requires_review: bool,
) -> dict[str, str]:
    if match_requires_review:
        return {}
    reviewed_field_ids: set[str] = set()
    for field in fields:
        field_id = str(field.get("field_id") or "")
        extracted = extracted_by_field_id.get(field_id)
        if not field_id or extracted is None:
            continue
        output_key = output_keys_by_field_id.get(field_id) or str(field.get("output_key") or field_id)
        base_validation_warnings = _template_field_value_validation_warnings(
            field,
            extracted.value,
            template_definition,
            {},
            cross_field_rules=False,
        )
        if _template_field_requires_review(
            extracted.block.review.requires_review,
            tuple(extracted.block.review.warnings),
            _template_field_risk_warnings(
                field,
                risk_rank_declared=risk_rank_declared,
                defined_levels=defined_levels,
                review_required_levels=review_required_levels,
            ),
            base_validation_warnings,
            tuple(output_conflicts.get(output_key, ())),
        ):
            continue
        reviewed_field_ids.add(field_id)

    reviewed_values = {
        field_id: extracted_by_field_id[field_id].value
        for field_id in reviewed_field_ids
        if extracted_by_field_id[field_id] is not None
    }
    while True:
        failed_field_ids = set()
        for field in fields:
            field_id = str(field.get("field_id") or "")
            extracted = extracted_by_field_id.get(field_id)
            if field_id not in reviewed_values or extracted is None:
                continue
            cross_field_warnings = _template_field_value_validation_warnings(
                field,
                extracted.value,
                template_definition,
                reviewed_values,
                non_cross_field_rules=False,
            )
            if cross_field_warnings:
                failed_field_ids.add(field_id)
        if not failed_field_ids:
            return reviewed_values
        for field_id in failed_field_ids:
            reviewed_values.pop(field_id, None)


def _value_matches_template_type(value: str, value_type: str) -> bool:
    normalized_type = value_type.strip().casefold()
    if normalized_type in {"", "string", "enum"}:
        return True
    if normalized_type == "number":
        return _number_value(value) is not None
    if normalized_type == "date":
        return _date_value(value) is not None
    if normalized_type == "boolean":
        return value.strip().casefold() in {"true", "false", "yes", "no", "1", "0"}
    return False


def _coerced_template_validation_value(value: Any, value_type: str) -> Any | None:
    normalized_type = value_type.strip().casefold()
    if normalized_type == "number":
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)) and math.isfinite(float(value)):
            return float(value)
        return _number_value(str(value))
    if normalized_type == "date":
        if isinstance(value, datetime):
            return value
        return _date_value(str(value))
    if normalized_type == "boolean":
        if isinstance(value, bool):
            return value
        normalized_value = str(value).strip().casefold()
        if normalized_value in {"true", "yes", "1"}:
            return True
        if normalized_value in {"false", "no", "0"}:
            return False
    return None


def _number_value(value: str) -> float | None:
    stripped = re.sub(r"^([+-])\s+", r"\1", value.strip())
    if not _comma_grouping_is_valid_for_number(stripped):
        return None
    stripped = stripped.replace(",", "")
    if not _plain_number_format_is_valid(stripped):
        return _invalid_template_number_value()
    try:
        number = float(re.sub(r"^([+-])\s+", r"\1", stripped))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _comma_grouping_is_valid_for_number(value: str) -> bool:
    if "," not in value:
        return True
    return bool(
        re.fullmatch(
            r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d*)?(?:[eE][+-]?\d+)?",
            value,
        )
    )


def _plain_number_format_is_valid(value: str) -> bool:
    return bool(re.fullmatch(r"[+-]?\s*(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value))


def _invalid_template_number_value() -> None:
    return None


def _date_value(value: str) -> date | None:
    stripped = value.strip()
    normalized = _normalized_template_date_text(stripped)
    if normalized is None:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.date()


def _normalized_template_date_text(value: str) -> str | None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+-]+Z?)?", value):
        return None
    normalized = value.replace("Z", "+00:00")
    if len(normalized) == 10:
        return f"{normalized}T00:00:00"
    if " " in normalized and "T" not in normalized:
        return normalized.replace(" ", "T", 1)
    return normalized


def _value_satisfies_template_validation_rule(
    value: str,
    rule: Mapping[str, Any],
    *,
    value_type: str,
    field_values_by_id: Mapping[str, str],
) -> bool:
    rule_type = str(rule.get("rule_type") or "").strip().casefold()
    if rule_type == "required":
        return bool(value.strip())
    if rule_type == "type":
        return _value_matches_template_type(value, str(rule.get("expected_type") or ""))
    if rule_type == "range":
        number = _number_value(value)
        if number is None:
            return False
        minimum = rule.get("minimum")
        maximum = rule.get("maximum")
        if isinstance(minimum, (int, float)) and number < float(minimum):
            return False
        if isinstance(maximum, (int, float)) and number > float(maximum):
            return False
        return isinstance(minimum, (int, float)) or isinstance(maximum, (int, float))
    if rule_type == "allowed_values":
        return _value_matches_allowed_template_values(
            value,
            _list_value(rule.get("allowed_values")),
            value_type,
        )
    if rule_type == "cross_field":
        return _value_satisfies_cross_field_rule(
            value,
            rule,
            field_values_by_id,
            value_type=value_type,
        )
    return False


def _value_satisfies_cross_field_rule(
    value: str,
    rule: Mapping[str, Any],
    field_values_by_id: Mapping[str, str],
    *,
    value_type: str,
) -> bool:
    related_target = str(rule.get("related_target") or "")
    operator = str(rule.get("operator") or "").strip().casefold()
    related_value = field_values_by_id.get(related_target)
    if related_value is None:
        return False
    if operator in {"before", "before_or_equal", "after", "after_or_equal"}:
        left_date = _date_value(value)
        right_date = _date_value(related_value)
        if left_date is None or right_date is None:
            return False
        return _compare_template_values(left_date, right_date, operator)
    if operator in {"less_than", "less_than_or_equal", "greater_than", "greater_than_or_equal"}:
        left_number = _number_value(value)
        right_number = _number_value(related_value)
        if left_number is None or right_number is None:
            return False
        return _compare_template_values(left_number, right_number, operator)
    if operator == "equals":
        return _template_values_satisfy_typed_equality(
            value,
            related_value,
            value_type=value_type,
            expected_equal=True,
        )
    if operator == "not_equals":
        return _template_values_satisfy_typed_equality(
            value,
            related_value,
            value_type=value_type,
            expected_equal=False,
        )
    return False


def _value_matches_allowed_template_values(
    value: str, allowed_values: Sequence[Any], value_type: str
) -> bool:
    typed_value = _coerced_template_validation_value(value, value_type)
    if typed_value is not None:
        return any(
            typed_value == _coerced_template_validation_value(allowed, value_type)
            for allowed in allowed_values
        )
    normalized_value = value.strip().casefold()
    return any(str(allowed).strip().casefold() == normalized_value for allowed in allowed_values)


def _template_values_satisfy_typed_equality(
    value: str, related_value: str, *, value_type: str, expected_equal: bool
) -> bool:
    left = _coerced_template_validation_value(value, value_type)
    right = _coerced_template_validation_value(related_value, value_type)
    if left is not None or right is not None:
        if left is None or right is None:
            return False
        return (left == right) if expected_equal else (left != right)
    left_text = value.strip()
    right_text = related_value.strip()
    return (left_text == right_text) if expected_equal else (left_text != right_text)


def _compare_template_values(left: Any, right: Any, operator: str) -> bool:
    comparisons = {
        "before": lambda: left < right,
        "less_than": lambda: left < right,
        "before_or_equal": lambda: left <= right,
        "less_than_or_equal": lambda: left <= right,
        "after": lambda: left > right,
        "greater_than": lambda: left > right,
        "after_or_equal": lambda: left >= right,
        "greater_than_or_equal": lambda: left >= right,
    }
    comparison = comparisons.get(operator)
    if comparison is None:
        return False
    try:
        return bool(comparison())
    except TypeError:
        return _invalid_ordered_template_comparison()


def _invalid_ordered_template_comparison() -> bool:
    return False


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
    return _extract_template_nearby_field(
        document_ir,
        field,
        anchor,
        anchor_blocks,
        fields,
        anchors,
        direction,
    )


def _extract_template_nearby_field(
    document_ir: DocumentIRV1,
    field: Mapping[str, Any],
    anchor: Mapping[str, Any],
    anchor_blocks: Sequence[DocumentBlock],
    fields: Sequence[Mapping[str, Any]],
    anchors: Sequence[Mapping[str, Any]],
    direction: str,
) -> _ExtractedTemplateFieldValue | None:
    label = str(field.get("label") or field.get("field_id") or "")
    anchor_text = str(anchor.get("text") or "")
    stop_markers = (*_field_stop_markers(field, fields), *_anchor_stop_markers(anchor, anchors))
    if direction in {"same_block", "right"}:
        for block in anchor_blocks:
            value = _field_value_from_text(
                block.text,
                label,
                stop_markers=stop_markers,
            )
            if value:
                return _template_value(block, value, 0.98, direction=direction)
        if direction == "right":
            for anchor_block in anchor_blocks:
                for block in _right_side_blocks(document_ir.blocks, anchor_block):
                    if not _right_side_block_can_supply_unlabeled_value(block):
                        continue
                    value = _field_value_from_text(
                        block.text,
                        label,
                        stop_markers=stop_markers,
                    )
                    if value is None:
                        value = _right_side_fallback_value_from_block(
                            block,
                            label=label,
                            anchor_text=anchor_text,
                            stop_markers=stop_markers,
                        )
                        if value is _RIGHT_SIDE_STOP_SCAN:
                            break
                    if value:
                        return _template_value(
                            block,
                            value,
                            0.94,
                            direction=direction,
                        )
        return None

    if direction != "below":
        return None

    ordered_blocks = sorted(
        document_ir.blocks,
        key=lambda block: (block.source_page, block.bbox.y, block.bbox.x, block.id),
    )
    for anchor_block in anchor_blocks:
        for block in _below_scan_candidate_blocks(ordered_blocks, anchor_block, anchor):
            value = _below_field_value_from_block(
                block,
                field_label=label,
                anchor_text=anchor_text,
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
        header_candidate = _table_header_candidate(
            parsed_rows.rows,
            anchor,
            normalized_label,
            required_columns=required_columns,
            allow_merged_column_candidates=(
                parsed_rows.allow_merged_column_candidates
                and _block_allows_merged_column_candidates(block, document_ir.document.source_type)
            ),
            preserve_column_positions=parsed_rows.preserve_column_positions,
        )
        if header_candidate is None:
            continue
        column_span = _row_column_span(
            header_candidate.header,
            normalized_label,
            allow_merged_column_candidates=(
                parsed_rows.allow_merged_column_candidates
                and _block_allows_merged_column_candidates(block, document_ir.document.source_type)
            ),
        )
        if column_span is None:
            continue
        column_index, column_count = column_span
        actual_column_index = header_candidate.column_offset + column_index
        table_value = _first_table_body_value_at_physical_column_span(
            parsed_rows.rows,
            header_candidate.row_index,
            actual_column_index,
            column_count,
            header_candidate.comparison_headers,
        )
        if table_value is not None:
            row_index, value, value_column_index = table_value
            return _template_value(
                block,
                value,
                0.90,
                direction="table_cell",
                row_index=row_index,
                column_index=value_column_index,
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


def _table_cell_value_at_physical_column_span(
    row: Sequence[str], column_index: int, column_count: int
) -> str | None:
    if column_count <= 1:
        return _table_cell_value_at_physical_column(row, column_index)
    if column_index >= len(row):
        return None
    values = [
        str(cell).strip()
        for cell in row[column_index : column_index + column_count]
        if str(cell).strip()
    ]
    return " ".join(values) or None


def _table_body_values_at_physical_column(
    rows: Sequence[Sequence[str]],
    header_index: int,
    column_index: int,
    comparison_headers: Sequence[Sequence[str]],
) -> list[tuple[int, str | None, int]]:
    return [
        (
            row_index,
            _table_cell_value_at_physical_column(
                row,
                _table_body_physical_column_index(row, column_index, comparison_headers),
            ),
            _table_body_physical_column_index(row, column_index, comparison_headers),
        )
        for row_index, row in enumerate(rows[header_index + 1 :], start=header_index + 1)
        if _table_body_row_is_value_candidate(row, comparison_headers)
    ]


def _table_body_values_at_physical_column_span(
    rows: Sequence[Sequence[str]],
    header_index: int,
    column_index: int,
    column_count: int,
    comparison_headers: Sequence[Sequence[str]],
) -> list[tuple[int, str | None, int]]:
    return [
        (
            row_index,
            _table_cell_value_at_physical_column_span(
                row,
                _table_body_physical_column_index(row, column_index, comparison_headers),
                column_count,
            ),
            _table_body_physical_column_index(row, column_index, comparison_headers),
        )
        for row_index, row in enumerate(rows[header_index + 1 :], start=header_index + 1)
        if _table_body_row_is_value_candidate(row, comparison_headers)
    ]


def _table_body_physical_column_index(
    row: Sequence[str], column_index: int, comparison_headers: Sequence[Sequence[str]]
) -> int:
    leading_columns = _same_row_anchor_leading_column_count(comparison_headers)
    if leading_columns <= 0:
        return column_index
    if len(row) <= column_index + leading_columns:
        return column_index
    if any(str(cell).strip() for cell in row[:leading_columns]):
        return column_index
    return column_index + leading_columns


def _same_row_anchor_leading_column_count(
    comparison_headers: Sequence[Sequence[str]],
) -> int:
    if len(comparison_headers) < 2:
        return 0
    header = tuple(_normalized_column_name(cell) for cell in comparison_headers[0])
    full_row = tuple(_normalized_column_name(cell) for cell in comparison_headers[1])
    if not header or len(full_row) <= len(header):
        return 0
    leading_count = len(full_row) - len(header)
    if tuple(full_row[leading_count:]) != header:
        return 0
    return leading_count


def _table_body_row_is_value_candidate(
    row: Sequence[str], comparison_headers: Sequence[Sequence[str]]
) -> bool:
    return not _is_markdown_alignment_row(row) and not any(
        _table_row_repeats_header(row, header_row) for header_row in comparison_headers
    )


def _table_row_repeats_header(row: Sequence[str], header_row: Sequence[str]) -> bool:
    normalized_row = tuple(_normalized_column_name(cell) for cell in row)
    normalized_header = tuple(_normalized_column_name(cell) for cell in header_row)
    if not normalized_row or not normalized_header:
        return False
    if normalized_row == normalized_header:
        return True
    if len(normalized_row) > len(normalized_header):
        return normalized_row[-len(normalized_header) :] == normalized_header
    return False


def _first_table_body_value_at_physical_column(
    rows: Sequence[Sequence[str]],
    header_index: int,
    column_index: int,
    comparison_headers: Sequence[Sequence[str]],
) -> tuple[int, str, int] | None:
    for row_index, value, value_column_index in _table_body_values_at_physical_column(
        rows,
        header_index,
        column_index,
        comparison_headers,
    ):
        if value:
            return row_index, value, value_column_index
    return None


def _first_table_body_value_at_physical_column_span(
    rows: Sequence[Sequence[str]],
    header_index: int,
    column_index: int,
    column_count: int,
    comparison_headers: Sequence[Sequence[str]],
) -> tuple[int, str, int] | None:
    for row_index, value, value_column_index in _table_body_values_at_physical_column_span(
        rows,
        header_index,
        column_index,
        column_count,
        comparison_headers,
    ):
        if value:
            return row_index, value, value_column_index
    return None


def _field_and_table_output_keys_for_conflicts(
    fields: Sequence[Mapping[str, Any]],
    output_mapping: Mapping[str, Any],
    output_keys_by_field_id: Mapping[str, str],
) -> dict[str, str]:
    field_output_keys = {
        str(field.get("field_id") or ""): output_keys_by_field_id.get(str(field.get("field_id") or ""))
        or str(field.get("output_key") or field.get("field_id") or "")
        for field in fields
    }
    table_output_keys = {
        f"table:{mapping.get('table_id')}": str(mapping.get("output_key") or "")
        for mapping in (_mapping(value) for value in _list_value(output_mapping.get("table_map")))
        if isinstance(mapping.get("table_id"), str) and isinstance(mapping.get("output_key"), str)
    }
    return {**field_output_keys, **table_output_keys}


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


def _anchor_stop_markers(
    anchor: Mapping[str, Any], anchors: Sequence[Mapping[str, Any]]
) -> tuple[str, ...]:
    anchor_id = str(anchor.get("anchor_id") or "")
    markers: list[str] = []
    for candidate in anchors:
        if str(candidate.get("anchor_id") or "") == anchor_id:
            continue
        marker_text = str(candidate.get("text") or "").strip()
        if marker_text:
            markers.append(marker_text)
    return tuple(dict.fromkeys(markers))


def _field_value_from_text(
    text: str,
    label: str,
    *,
    stop_markers: Sequence[str] = (),
) -> str | None:
    return _value_after_marker(text, label, stop_markers=stop_markers)


def _right_side_block_can_supply_unlabeled_value(block: DocumentBlock) -> bool:
    return block.type not in {"heading", "table"}


def _right_side_block_starts_with_current_label(
    block: DocumentBlock,
    *,
    label: str,
    anchor_text: str,
) -> bool:
    return _text_starts_with_label_like_marker(
        block.text,
        (label,),
    ) or _text_starts_with_label_like_marker(
        block.text,
        (anchor_text,),
    )


def _right_side_block_starts_with_stop_marker(
    block: DocumentBlock, stop_markers: Sequence[str]
) -> bool:
    return _text_starts_with_label_like_marker(block.text, stop_markers)


_RIGHT_SIDE_STOP_SCAN = object()


def _right_side_fallback_value_from_block(
    block: DocumentBlock,
    *,
    label: str,
    anchor_text: str,
    stop_markers: Sequence[str] = (),
) -> str | object | None:
    if _right_side_block_starts_with_current_label(
        block,
        label=label,
        anchor_text=anchor_text,
    ):
        return None
    if _right_side_block_starts_with_stop_marker(block, stop_markers):
        return _RIGHT_SIDE_STOP_SCAN
    return _right_side_unlabeled_value_from_block(block, stop_markers=stop_markers)


def _right_side_unlabeled_value_from_block(
    block: DocumentBlock,
    *,
    stop_markers: Sequence[str] = (),
) -> str | None:
    if not _right_side_block_can_supply_unlabeled_value(block):
        return None
    value = _value_before_next_marker(block.text.strip(), stop_markers).strip()
    if _looks_like_label_or_note_line(value):
        return None
    return value or None


def _below_field_value_from_block(
    block: DocumentBlock,
    *,
    field_label: str,
    anchor_text: str,
    stop_markers: Sequence[str] = (),
) -> str | None:
    text = block.text
    value = _field_value_from_text(
        text,
        field_label,
        stop_markers=stop_markers,
    )
    if value is not None:
        return value
    return _field_value_from_label_anchor_below(
        block,
        field_label=field_label,
        anchor_text=anchor_text,
        stop_markers=stop_markers,
    )


def _below_scan_block_is_not_field_value(block: DocumentBlock) -> bool:
    return block.type in {"heading", "table"}


def _below_scan_candidate_blocks(
    ordered_blocks: Sequence[DocumentBlock],
    anchor_block: DocumentBlock,
    anchor: Mapping[str, Any],
) -> Sequence[DocumentBlock]:
    candidates = _below_scan_candidates_after_anchor(ordered_blocks, anchor_block)
    if str(anchor.get("kind") or "") != "label":
        scoped_candidates: list[DocumentBlock] = []
        for block in candidates:
            if _below_scan_block_is_not_field_value(block):
                break
            scoped_candidates.append(block)
        return scoped_candidates
    if not candidates:
        return []
    return _below_label_scan_candidates(candidates, anchor_block)


def _below_scan_candidates_after_anchor(
    ordered_blocks: Sequence[DocumentBlock], anchor_block: DocumentBlock
) -> list[DocumentBlock]:
    return [
        block
        for block in ordered_blocks
        if _below_scan_candidate_is_after_anchor(block, anchor_block)
    ]


def _below_scan_candidate_is_after_anchor(
    block: DocumentBlock, anchor_block: DocumentBlock
) -> bool:
    return (
        block.id != anchor_block.id
        and block.source_page == anchor_block.source_page
        and block.bbox.y > anchor_block.bbox.y
    )


def _below_label_scan_candidates(
    candidates: Sequence[DocumentBlock], anchor_block: DocumentBlock
) -> Sequence[DocumentBlock]:
    aligned_candidates = [
        block for block in candidates if _blocks_horizontally_overlap(block, anchor_block)
    ]
    if not aligned_candidates:
        return []
    first_below_block = aligned_candidates[0]
    if _below_scan_block_is_not_field_value(first_below_block):
        return []
    return [first_below_block]


def _field_value_from_label_anchor_below(
    block: DocumentBlock,
    *,
    field_label: str,
    anchor_text: str,
    stop_markers: Sequence[str] = (),
) -> str | None:
    if _normalized_text(field_label) != _normalized_text(anchor_text):
        return None
    raw_value = block.text.strip()
    if _text_starts_with_label_like_marker(raw_value, stop_markers):
        return None
    value = _value_before_next_marker(raw_value, stop_markers).strip()
    return _confirmed_unlabeled_below_value(block, value, field_label)


def _confirmed_unlabeled_below_value(
    block: DocumentBlock, value: str, field_label: str
) -> str | None:
    if not _unlabeled_below_value_can_be_confirmed(block, value, field_label):
        return None
    return value


def _unlabeled_below_value_can_be_confirmed(
    block: DocumentBlock, value: str, field_label: str
) -> bool:
    if block.type not in {"paragraph", "field"}:
        return False
    if not _looks_like_unlabeled_below_value_block(value):
        return False
    if not value or _normalized_text(value) == _normalized_text(field_label):
        return False
    if _looks_like_unlabeled_section_heading(value) and not _field_label_accepts_title_cased_value(
        field_label
    ):
        return False
    return True


def _value_after_marker(
    text: str, marker: str, *, stop_markers: Sequence[str] = ()
) -> str | None:
    normalized_marker = marker.casefold().strip()
    if not normalized_marker:
        return None
    lines = text.splitlines() or [text]
    for line_index, line in enumerate(lines):
        for match in re.finditer(_marker_match_pattern(marker), line, flags=re.IGNORECASE):
            if not _marker_match_has_boundaries(line, match.start(), match.end()):
                continue
            if not _marker_match_is_label_like_stop(line, match.start(), match.end(), marker):
                continue
            value = _value_after_marker_match_or_next_line(
                lines,
                line_index,
                line,
                match.end(),
                stop_markers,
            )
            if value:
                return value
    return None


def _candidate_value_after_marker_match(
    line: str, marker_end: int, stop_markers: Sequence[str]
) -> str:
    value = line[marker_end:].strip()
    value = re.sub(r"^[\s:：=]+", "", value).strip()
    if value.startswith("-") and not _looks_like_negative_number(value):
        value = value[1:].strip()
    return _value_before_next_marker(value, stop_markers)


def _value_after_marker_match_or_next_line(
    lines: Sequence[str],
    line_index: int,
    line: str,
    marker_end: int,
    stop_markers: Sequence[str],
) -> str | None:
    value = _candidate_value_after_marker_match(line, marker_end, stop_markers)
    if value:
        return value
    return _first_next_line_value(lines[line_index + 1 :], stop_markers)


def _first_next_line_value(lines: Sequence[str], stop_markers: Sequence[str]) -> str | None:
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        return _next_line_fallback_value(stripped, stop_markers)
    return None


def _next_line_fallback_value(line: str, stop_markers: Sequence[str]) -> str | None:
    value = _value_before_next_marker(line, stop_markers).strip()
    if _next_line_fallback_rejects_value(value):
        return None
    return value or None


def _next_line_fallback_rejects_value(value: str) -> bool:
    return _looks_like_label_or_note_line(value) or _looks_like_standalone_label_line(value)


def _value_before_next_marker(value: str, stop_markers: Sequence[str]) -> str:
    earliest_stop: int | None = None
    for marker in stop_markers:
        marker_text = marker.strip()
        if not marker_text:
            continue
        for match in re.finditer(_marker_match_pattern(marker_text), value, flags=re.IGNORECASE):
            if not _marker_match_has_boundaries(value, match.start(), match.end()):
                continue
            if not _marker_match_is_label_like_stop(value, match.start(), match.end(), marker_text):
                continue
            earliest_stop = _earlier_marker_stop_index(earliest_stop, match.start())
            break
    if earliest_stop is None:
        return value
    return re.sub(r"[\s:：=-]+$", "", value[:earliest_stop]).strip()


def _marker_match_pattern(marker: str) -> str:
    parts = [part for part in re.split(r"\s+", marker.strip()) if part]
    return r"\s+".join(re.escape(part) for part in parts)


def _earlier_marker_stop_index(current: int | None, candidate: int) -> int:
    return candidate if current is None else min(current, candidate)


def _looks_like_unlabeled_below_value_block(value: str) -> bool:
    nonempty_lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(nonempty_lines) != 1:
        return False
    line = nonempty_lines[0]
    if _looks_like_label_or_note_line(line):
        return False
    return "\t" not in line and "|" not in line


def _looks_like_label_or_note_line(value: str) -> bool:
    match = re.match(
        r"^([A-Za-z][A-Za-z0-9 /_.()'-]*(?:\s+[A-Za-z][A-Za-z0-9 /_.()'-]*){0,5})"
        r"\s*[:：=]\s*\S",
        value.strip(),
    )
    return bool(match and _looks_like_label_text(match.group(1)))


def _looks_like_standalone_label_line(value: str) -> bool:
    stripped = value.strip()
    if not stripped or re.search(r"[\d:：=|,\t]", stripped):
        return False
    words = [word.strip("._()'-/") for word in stripped.split() if word.strip("._()'-/")]
    if not 1 <= len(words) <= 5:
        return False
    if not all(re.fullmatch(r"[A-Za-z]+", word) for word in words):
        return False
    return _label_text_contains_label_token(stripped)


def _looks_like_label_text(value: str) -> bool:
    words = [word.casefold() for word in re.findall(r"[A-Za-z]+", value)]
    if not words:
        return False
    if len(words) > 1 and value.strip()[:1].isupper():
        return True
    return _label_text_contains_label_token(value)


def _label_text_contains_label_token(value: str) -> bool:
    words = [word.casefold() for word in re.findall(r"[A-Za-z]+", value)]
    label_tokens = {
        "approved",
        "authorized",
        "batch",
        "comment",
        "comments",
        "date",
        "disposition",
        "expiry",
        "lot",
        "manufacturing",
        "name",
        "no",
        "note",
        "notes",
        "number",
        "prepared",
        "review",
        "reviewed",
        "reviewer",
        "status",
        "yield",
    }
    return any(word in label_tokens for word in words)


def _field_label_accepts_title_cased_value(field_label: str) -> bool:
    normalized_label = _normalized_text(field_label)
    return any(
        token in normalized_label
        for token in (
            "prepared by",
            "reviewed by",
            "approved by",
            "authorized by",
            "name",
            "person",
            "organization",
            "organisation",
            "company",
            "supplier",
            "manufacturer",
        )
    )


def _looks_like_unlabeled_section_heading(value: str) -> bool:
    if any(char.isdigit() for char in value):
        return False
    if re.search(r"[^A-Za-z\s]", value):
        return False
    words = [word for word in value.split() if word]
    if len(words) < 2:
        return False
    return _all_words_are_title_cased(words)


def _all_words_are_title_cased(words: Sequence[str]) -> bool:
    return all(_word_is_title_cased(word) for word in words)


def _word_is_title_cased(word: str) -> bool:
    return word[:1].isupper()


def _looks_like_negative_number(value: str) -> bool:
    return bool(
        re.fullmatch(
            r"-\s*(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",
            value.strip(),
        )
    )


def _marker_match_is_label_like_stop(
    value: str, start: int, end: int, marker_text: str = ""
) -> bool:
    after = value[end:]
    if re.match(r"\s*[:：=]", after):
        return True
    before = value[:start].rstrip()
    if not before:
        return True
    if before[-1:] in {"\n", "\r", "|", ":", "：", "="}:
        return True
    previous_token = before.split()[-1] if before.split() else ""
    if re.search(r"[\d_-]", previous_token):
        return True
    return _marker_match_looks_like_adjacent_label(value, start, end, marker_text)


def _marker_match_looks_like_adjacent_label(
    value: str, start: int, end: int, marker_text: str
) -> bool:
    normalized_marker = " ".join(marker_text.strip().split())
    if len(re.sub(r"[^A-Za-z0-9]", "", normalized_marker)) < 5:
        return False
    if not normalized_marker[:1].isupper():
        return False
    before = value[:start].strip()
    after = value[end:].strip()
    if not before or not after:
        return False
    after_token = after.split()[0]
    return bool(after_token[:1].isupper() or re.search(r"\d", after_token))


def _text_starts_with_label_like_marker(text: str, stop_markers: Sequence[str]) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    for marker in stop_markers:
        marker_text = marker.strip()
        if not marker_text:
            continue
        match = re.match(_marker_match_pattern(marker_text), stripped, flags=re.IGNORECASE)
        if match is None:
            continue
        if not _marker_match_has_boundaries(stripped, match.start(), match.end()):
            continue
        if _marker_match_is_label_like_stop(stripped, match.start(), match.end(), marker_text):
            return True
    return False


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


def _table_header_candidate(
    rows: Sequence[Sequence[str]],
    anchor: Mapping[str, Any],
    normalized_label: str,
    *,
    required_columns: Sequence[str] = (),
    allow_merged_column_candidates: bool,
    preserve_column_positions: bool,
) -> _TableHeaderCandidate | None:
    anchor_index = _first_table_anchor_row_index(rows, anchor)
    normalized_required_columns = {
        column for column in (_normalized_column_name(column) for column in required_columns) if column
    }
    if normalized_required_columns:
        for candidate in _table_header_candidates_with_required_columns(
            rows,
            anchor,
            anchor_index,
            preserve_column_positions=preserve_column_positions,
            allow_merged_column_candidates=allow_merged_column_candidates,
        ):
            candidate_row = candidate.header
            index = candidate.row_index
            if index != anchor_index and _looks_like_wrapped_header_fragment(candidate_row, rows, index):
                continue
            if not _table_header_candidate_satisfies_required_columns(
                candidate_row,
                normalized_required_columns,
                allow_merged_column_candidates=allow_merged_column_candidates,
            ):
                continue
            if _table_header_candidate_contains_label(
                candidate_row,
                normalized_label,
                allow_merged_column_candidates=allow_merged_column_candidates,
            ):
                return candidate
            return None
        return None

    for index, candidate_row in _table_header_candidates_without_required_columns(rows, anchor_index, anchor):
        if _row_column_index(
            candidate_row,
            normalized_label,
            allow_merged_column_candidates=allow_merged_column_candidates,
        ) is not None:
            header, column_offset, comparison_headers = _table_header_candidate_details(
                rows,
                index,
                anchor,
                preserve_column_positions=preserve_column_positions,
            )
            return _TableHeaderCandidate(index, header, column_offset, comparison_headers)
    return None


def _table_header_candidate_contains_label(
    candidate_row: Sequence[str],
    normalized_label: str,
    *,
    allow_merged_column_candidates: bool,
) -> bool:
    return (
        _row_column_index(
            candidate_row,
            normalized_label,
            allow_merged_column_candidates=allow_merged_column_candidates,
        )
        is not None
    )


def _table_header_candidate_satisfies_required_columns(
    candidate_row: Sequence[str],
    normalized_required_columns: set[str],
    *,
    allow_merged_column_candidates: bool,
) -> bool:
    return (
        _row_required_column_score(
            candidate_row,
            normalized_required_columns,
            allow_merged_column_candidates=allow_merged_column_candidates,
        )
        >= 1.0
    )


def _table_header_candidates_with_required_columns(
    rows: Sequence[Sequence[str]],
    anchor: Mapping[str, Any],
    anchor_index: int,
    *,
    preserve_column_positions: bool,
    allow_merged_column_candidates: bool,
) -> list[_TableHeaderCandidate]:
    candidates: list[_TableHeaderCandidate] = []
    if anchor_index < len(rows):
        header, column_offset, comparison_headers = _table_header_candidate_details(
            rows,
            anchor_index,
            anchor,
            preserve_column_positions=preserve_column_positions,
        )
        if header:
            candidates.append(
                _TableHeaderCandidate(anchor_index, header, column_offset, comparison_headers)
            )
    if allow_merged_column_candidates:
        candidates.extend(
            _wrapped_cell_header_candidates(
                rows,
                anchor,
                preserve_column_positions=preserve_column_positions,
            )
        )
    for index, row in enumerate(rows[anchor_index + 1 :], start=anchor_index + 1):
        if _looks_like_wrapped_header_fragment(row, rows, index):
            continue
        candidates.append(_TableHeaderCandidate(index, row, 0, (row,)))
    return candidates


def _table_header_candidate_details(
    rows: Sequence[Sequence[str]],
    header_index: int,
    anchor: Mapping[str, Any],
    *,
    preserve_column_positions: bool,
) -> tuple[Sequence[str], int, tuple[Sequence[str], ...]]:
    row = rows[header_index]
    if header_index == _first_table_anchor_row_index(rows, anchor):
        anchor_columns, column_offset = _row_columns_excluding_anchor_with_offset(
            row, anchor, preserve_column_positions=preserve_column_positions
        )
        if anchor_columns:
            return anchor_columns, column_offset, (anchor_columns, row)
    return row, 0, (row,)


def _wrapped_cell_header_candidates(
    rows: Sequence[Sequence[str]],
    anchor: Mapping[str, Any],
    *,
    preserve_column_positions: bool,
) -> list[_TableHeaderCandidate]:
    anchor_index = _first_table_anchor_row_index(rows, anchor)
    candidates: list[_TableHeaderCandidate] = []
    for index in range(anchor_index, len(rows) - 1):
        row = rows[index]
        next_row = rows[index + 1]
        if len(row) < 2 or len(next_row) < 2:
            continue
        joined_cell = f"{row[-1]}\n{next_row[0]}"
        combined_row = [*row[:-1], joined_cell, *next_row[1:]]
        if _row_matches_anchor(combined_row, anchor):
            header, column_offset = _row_columns_excluding_anchor_with_offset(
                combined_row,
                anchor,
                preserve_column_positions=preserve_column_positions,
            )
            if header:
                candidates.append(
                    _TableHeaderCandidate(
                        index + 1,
                        header,
                        column_offset,
                        (header, combined_row, row, next_row),
                    )
                )
        else:
            candidates.append(
                _TableHeaderCandidate(
                    index + 1,
                    combined_row,
                    0,
                    (combined_row, row, next_row),
                )
            )
    return candidates


def _table_header_candidates_without_required_columns(
    rows: Sequence[Sequence[str]],
    anchor_index: int,
    anchor: Mapping[str, Any],
) -> list[tuple[int, Sequence[str]]]:
    candidates: list[tuple[int, Sequence[str]]] = []
    if anchor_index < len(rows):
        anchor_columns = _row_columns_excluding_anchor(rows[anchor_index], anchor)
        if anchor_columns:
            candidates.append((anchor_index, anchor_columns))
    for index in range(anchor_index + 1, len(rows)):
        row = rows[index]
        if _looks_like_wrapped_header_fragment(row, rows, index):
            continue
        candidates.append((index, row))
        break
    return candidates


def _row_column_index(
    row: Sequence[str],
    normalized_label: str,
    *,
    allow_merged_column_candidates: bool,
) -> int | None:
    span = _row_column_span(
        row,
        normalized_label,
        allow_merged_column_candidates=allow_merged_column_candidates,
    )
    return None if span is None else span[0]


def _row_column_span(
    row: Sequence[str],
    normalized_label: str,
    *,
    allow_merged_column_candidates: bool,
) -> tuple[int, int] | None:
    for index, cell in enumerate(row):
        if _normalized_column_name(cell) == normalized_label:
            return index, 1
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
            return start_index, index - start_index + 1
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
            and _blocks_share_text_row(block, anchor_block)
        ],
        key=lambda block: (block.bbox.x, block.bbox.y, block.id),
    )


def _blocks_share_text_row(left: DocumentBlock, right: DocumentBlock) -> bool:
    if not _blocks_vertically_overlap(left, right):
        return False
    tolerance = max(4.0, min(left.bbox.height, right.bbox.height) * 0.5)
    return abs(left.bbox.y - right.bbox.y) <= tolerance


def _blocks_vertically_overlap(left: DocumentBlock, right: DocumentBlock) -> bool:
    left_top = left.bbox.y
    left_bottom = left.bbox.y + left.bbox.height
    right_top = right.bbox.y
    right_bottom = right.bbox.y + right.bbox.height
    return min(left_bottom, right_bottom) > max(left_top, right_top)


def _blocks_horizontally_overlap(left: DocumentBlock, right: DocumentBlock) -> bool:
    left_start = left.bbox.x
    left_end = left.bbox.x + left.bbox.width
    right_start = right.bbox.x
    right_end = right.bbox.x + right.bbox.width
    return min(left_end, right_end) > max(left_start, right_start)


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
            return row[index + 1 :], _column_offset_after_anchor_cell(
                index,
                preserve_column_positions=preserve_column_positions,
            )
    anchor_span = _anchor_cell_span(row, expected_text, match_mode)
    if anchor_span is not None:
        anchor_start, anchor_end = anchor_span
        return row[anchor_end:], _column_offset_after_anchor_span(
            anchor_start,
            anchor_end,
            preserve_column_positions=preserve_column_positions,
        )
    return [], 0


def _column_offset_after_anchor_cell(
    anchor_cell_index: int, *, preserve_column_positions: bool
) -> int:
    if _same_row_anchor_offset_should_preserve_blank_columns(
        anchor_cell_index,
        preserve_column_positions=preserve_column_positions,
    ):
        return anchor_cell_index + 1
    return 0


def _same_row_anchor_offset_should_preserve_blank_columns(
    anchor_cell_index: int, *, preserve_column_positions: bool
) -> bool:
    return (
        preserve_column_positions
        or anchor_cell_index > 0
    )


def _column_offset_after_anchor_span(
    anchor_start_index: int, anchor_end_index: int, *, preserve_column_positions: bool
) -> int:
    if preserve_column_positions:
        return anchor_end_index
    return anchor_end_index if anchor_start_index > 0 else 0


def _looks_like_wrapped_header_fragment(
    row: Sequence[str], rows: Sequence[Sequence[str]], index: int
) -> bool:
    return len(row) == 1 and index + 1 < len(rows) and len(rows[index + 1]) > 1


def _anchor_cell_span(
    row: Sequence[str], expected_text: str, match_mode: str
) -> tuple[int, int] | None:
    for start in range(len(row)):
        merged = ""
        for end in range(start, len(row)):
            merged = f"{merged}\t{row[end]}" if merged else str(row[end])
            if _text_match_score(expected_text, merged, match_mode) > 0.0:
                return start, end + 1
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


def _is_markdown_alignment_row(row: Sequence[str]) -> bool:
    cells = [str(cell).strip() for cell in row if str(cell).strip()]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _split_table_row(value: str) -> list[str]:
    if "\t" in value:
        return value.split("\t")
    if "|" in value:
        cells = value.split("|")
        if cells and not cells[0].strip():
            cells = cells[1:]
        if cells and not cells[-1].strip():
            cells = cells[:-1]
        return cells
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
            if not saw_cell_ref and _is_xlsx_sheet_label_line(line):
                continue
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


def _is_xlsx_sheet_label_line(value: str) -> bool:
    return bool(re.fullmatch(r"Sheet:\s+.+", value.strip()))


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
