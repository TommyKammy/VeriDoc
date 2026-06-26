from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import math
from typing import Any, List, Optional


SCHEMA_VERSION = "document-ir/v1"
SOURCE_TYPES = {"pdf", "docx", "xlsx", "unknown"}
BLOCK_TYPES = {"heading", "paragraph", "table", "field", "list_item"}
UNITS = {"pt", "px", "mm"}
DEFAULT_PAGE_WIDTH_PT = 612.0
DEFAULT_PAGE_HEIGHT_PT = 792.0


@dataclass(frozen=True)
class BoundingBox:
    x: float
    y: float
    width: float
    height: float
    unit: str = "pt"
    origin: str = "top-left"


@dataclass(frozen=True)
class ExtractorRef:
    name: str
    version: str = "unknown"


@dataclass(frozen=True)
class DocumentInfo:
    id: str
    title: str
    source_type: str


@dataclass(frozen=True)
class DocumentPage:
    page_number: int
    width: float
    height: float
    unit: str = "pt"


@dataclass(frozen=True)
class ReviewState:
    requires_review: bool
    warnings: List[str]


@dataclass(frozen=True)
class DocumentBlock:
    id: str
    type: str
    text: str
    source_page: int
    bbox: BoundingBox
    extractor: ExtractorRef
    confidence: float
    review: ReviewState


@dataclass(frozen=True)
class DocumentIRV1:
    schema_version: str
    document: DocumentInfo
    pages: List[DocumentPage]
    blocks: List[DocumentBlock]
    warnings: List[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: List[str]
    warnings: List[str]
    requires_review: bool


def from_parser_output(
    parser_output: Any,
    *,
    document_id: str,
    title: str,
    source_type: str,
) -> DocumentIRV1:
    """Build the minimal Document IR v1 surface from Phase0 parser output."""
    data = _to_mapping(parser_output)
    parser_extractor = str(data.get("extractor") or "unknown")
    pages: List[DocumentPage] = []
    blocks: List[DocumentBlock] = []
    warnings: List[str] = []

    for page_index, page_data in enumerate(_parser_pages(data, source_type), start=1):
        page = _to_mapping(page_data)
        page_number = _page_number_value(page.get("page_number"), default=page_index)
        width, height, unit = _page_size(page)
        pages.append(DocumentPage(page_number=page_number, width=width, height=height, unit=unit))

        page_blocks = [*_list_value(page.get("fragments")), *_list_value(page.get("regions"))]
        for fragment in page_blocks:
            block = _block_from_fragment(
                fragment,
                block_index=len(blocks) + 1,
                fallback_page_number=page_number,
                fallback_extractor=parser_extractor,
            )
            blocks.append(block)
            warnings.extend(block.review.warnings)

    if not pages:
        warnings.append("parser_output.pages missing or empty")
    if not blocks:
        warnings.append("parser_output produced no text blocks")

    return DocumentIRV1(
        schema_version=SCHEMA_VERSION,
        document=DocumentInfo(id=document_id, title=title, source_type=source_type),
        pages=pages,
        blocks=blocks,
        warnings=warnings,
    )


def validate_document_ir_v1(document_ir: DocumentIRV1) -> ValidationResult:
    """Validate Document IR v1 and return fail-closed structural errors."""
    errors: List[str] = []
    warnings = list(document_ir.warnings)

    if document_ir.schema_version != SCHEMA_VERSION:
        errors.append("schema_version must be document-ir/v1")
    if not document_ir.document.id:
        errors.append("document.id is required")
    if document_ir.document.source_type not in SOURCE_TYPES:
        errors.append(f"document.source_type is unsupported: {document_ir.document.source_type}")
    if not document_ir.pages:
        errors.append("pages must contain at least one page")
    if not document_ir.blocks:
        warnings.append("blocks empty; document requires review")

    pages_by_number: dict[int, DocumentPage] = {}
    for index, page in enumerate(document_ir.pages):
        page_number_error = _page_identifier_error(page.page_number)
        if page_number_error is not None:
            errors.append(f"pages[{index}].page_number {page_number_error}")
            continue
        if page.page_number in pages_by_number:
            errors.append(f"pages[{index}].page_number duplicates page {page.page_number}")
        pages_by_number[page.page_number] = page
        if not _page_dimensions_are_finite(page) or page.width <= 0 or page.height <= 0:
            errors.append(f"pages[{index}] dimensions must be positive")
        if page.unit not in UNITS:
            errors.append(f"pages[{index}].unit is unsupported: {page.unit}")

    for index, block in enumerate(document_ir.blocks):
        if block.type not in BLOCK_TYPES:
            errors.append(f"blocks[{index}].type is unsupported: {block.type}")
        source_page_error = _page_identifier_error(block.source_page)
        if source_page_error is not None:
            errors.append(f"blocks[{index}].source_page {source_page_error}")
            continue
        page = pages_by_number.get(block.source_page)
        if page is None:
            errors.append(f"blocks[{index}].source_page references undeclared page {block.source_page}")
            continue
        if block.bbox.width < 0 or block.bbox.height < 0:
            errors.append(f"blocks[{index}].bbox dimensions must be non-negative")
        if not _bbox_values_are_finite(block.bbox):
            errors.append(f"blocks[{index}].bbox values must be finite numbers")
        if block.bbox.origin != "top-left":
            errors.append(f"blocks[{index}].bbox origin must be top-left")
        if block.bbox.unit not in UNITS:
            errors.append(f"blocks[{index}].bbox unit is unsupported: {block.bbox.unit}")
        if block.bbox.unit != page.unit:
            errors.append(f"blocks[{index}].bbox unit must match page {block.source_page} unit")
        if _bbox_outside_page(block.bbox, page):
            errors.append(f"blocks[{index}].bbox extends past page {block.source_page}")
        if not math.isfinite(block.confidence) or block.confidence < 0 or block.confidence > 1:
            errors.append(f"blocks[{index}].confidence must be between 0 and 1")
        warnings.extend(block.review.warnings)

    deduped_warnings = list(dict.fromkeys(warnings))
    requires_review = bool(errors) or bool(deduped_warnings) or any(
        block.review.requires_review for block in document_ir.blocks
    )
    return ValidationResult(
        ok=not errors,
        errors=errors,
        warnings=deduped_warnings,
        requires_review=requires_review,
    )


def _block_from_fragment(
    fragment: Any,
    *,
    block_index: int,
    fallback_page_number: int,
    fallback_extractor: str,
) -> DocumentBlock:
    data = _to_mapping(fragment)
    review_warnings: List[str] = []
    source_page = _page_number_value(data.get("page_number"), default=fallback_page_number)
    bbox = _bbox_value(data.get("bbox"))
    confidence, invalid_confidence = _confidence_value(
        data.get("confidence"),
        default=0.95,
        scale_percent=bool(data.get("engine")),
    )
    requires_review = False

    if bbox is None:
        bbox = BoundingBox(x=0.0, y=0.0, width=0.0, height=0.0)
        confidence = 0.0
        requires_review = True
        review_warnings.append(f"blocks[{block_index - 1}].bbox missing; block marked requires_review")

    text = str(data.get("text") or "")
    if not text.strip():
        confidence = 0.0
        requires_review = True
        review_warnings.append(f"blocks[{block_index - 1}].text empty; block marked requires_review")

    if data.get("missing_confidence") is True:
        confidence = 0.0
        requires_review = True
        review_warnings.append(f"blocks[{block_index - 1}].confidence missing; block marked requires_review")
    elif data.get("confidence") is None and data.get("engine"):
        confidence = 0.0
        requires_review = True
        review_warnings.append(f"blocks[{block_index - 1}].confidence missing; block marked requires_review")
    if invalid_confidence:
        confidence = 0.0
        requires_review = True
        review_warnings.append(f"blocks[{block_index - 1}].confidence invalid; block marked requires_review")
    if data.get("low_confidence") is True:
        requires_review = True
        review_warnings.append(f"blocks[{block_index - 1}].low confidence; block marked requires_review")
    if data.get("requires_review") is True:
        requires_review = True
        review_warnings.append(f"blocks[{block_index - 1}].parser marked block requires_review")

    return DocumentBlock(
        id=f"block-{block_index:04d}",
        type=_block_type(data, text),
        text=text,
        source_page=source_page,
        bbox=bbox,
        extractor=_extractor_ref(data, fallback_extractor),
        confidence=confidence,
        review=ReviewState(requires_review=requires_review, warnings=review_warnings),
    )


def _extractor_ref(data: dict[str, Any], fallback_extractor: str) -> ExtractorRef:
    extractor = data.get("extractor")
    if isinstance(extractor, dict):
        return ExtractorRef(
            name=str(extractor.get("name") or fallback_extractor),
            version=str(extractor.get("version") or "unknown"),
        )
    return ExtractorRef(
        name=str(extractor or data.get("engine") or fallback_extractor),
        version=str(data.get("extractor_version") or "unknown"),
    )


def _to_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        mapped = value.to_dict()
        if isinstance(mapped, dict):
            return mapped
    if is_dataclass(value):
        mapped = asdict(value)
        if isinstance(mapped, dict):
            return mapped
    return {}


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _parser_pages(data: dict[str, Any], source_type: str) -> list[Any]:
    pages = _list_value(data.get("pages"))
    if pages:
        return _list_value(adapt_document_ir_v0_blocks(data).get("pages"))
    if source_type == "docx":
        blocks = _list_value(data.get("blocks"))
        if blocks:
            return [
                {
                    "page_number": 1,
                    "width": DEFAULT_PAGE_WIDTH_PT,
                    "height": max(DEFAULT_PAGE_HEIGHT_PT, 72.0 + (24.0 * len(blocks))),
                    "unit": "pt",
                    "fragments": blocks,
                }
            ]
    if source_type == "xlsx":
        sheets = _list_value(data.get("sheets"))
        return [_xlsx_sheet_page(sheet_data, index) for index, sheet_data in enumerate(sheets, start=1)]
    if source_type == "pdf":
        table_pages = _pdf_table_report_pages(data)
        if table_pages:
            return table_pages
    return []


def adapt_document_ir_v0_blocks(parser_output: Any) -> dict[str, Any]:
    """Return parser output with top-level Document IR v0 blocks adapted into page fragments."""
    data = dict(_to_mapping(parser_output))
    pages = _list_value(data.get("pages"))
    if not pages:
        return data
    top_level_blocks = [_to_mapping(block) for block in _list_value(data.get("blocks"))]
    if not top_level_blocks:
        return data

    adapted_pages: list[dict[str, Any]] = []
    blocks_by_page: dict[int, list[dict[str, Any]]] = {}
    unmatched_blocks: list[dict[str, Any]] = []
    known_page_numbers = {
        _page_number_value(_to_mapping(page).get("page_number"), default=index)
        for index, page in enumerate(pages, start=1)
    }
    page_units_by_number = {
        _page_number_value(_to_mapping(page).get("page_number"), default=index): str(
            _to_mapping(page).get("unit") or "pt"
        )
        for index, page in enumerate(pages, start=1)
    }

    for block in top_level_blocks:
        metadata = _to_mapping(block.get("value_metadata"))
        source_page = _page_number_value(metadata.get("source_page"), default=0)
        fragment = _document_ir_v0_block_fragment(block, page_unit=page_units_by_number.get(source_page))
        if source_page in known_page_numbers:
            blocks_by_page.setdefault(source_page, []).append(fragment)
        else:
            unmatched_blocks.append(fragment)

    for index, page_data in enumerate(pages, start=1):
        page = dict(_to_mapping(page_data))
        existing_page_blocks = [*_list_value(page.get("fragments")), *_list_value(page.get("regions"))]
        if existing_page_blocks:
            adapted_pages.append(page)
            continue

        page_number = _page_number_value(page.get("page_number"), default=index)
        fragments = list(blocks_by_page.get(page_number, []))
        if index == 1:
            fragments.extend(unmatched_blocks)
        if fragments:
            page["fragments"] = fragments
        adapted_pages.append(page)
    data["pages"] = adapted_pages
    return data


def _document_ir_v0_block_fragment(
    block: dict[str, Any], *, page_unit: str | None = None
) -> dict[str, Any]:
    metadata = _to_mapping(block.get("value_metadata"))
    kind = str(block.get("type") or "paragraph")
    fragment: dict[str, Any] = {
        "kind": kind,
        "text": str(block.get("text") or ""),
    }
    if kind not in BLOCK_TYPES:
        fragment["preserve_invalid_type"] = True

    fragment["page_number"] = _page_number_value(metadata.get("source_page"), default=0)

    bbox = _to_mapping(metadata.get("bbox"))
    if bbox:
        if page_unit and "unit" not in bbox:
            bbox = {**bbox, "unit": page_unit}
        fragment["bbox"] = bbox

    confidence = metadata.get("confidence")
    if confidence is not None:
        fragment["confidence"] = confidence
    else:
        fragment["missing_confidence"] = True

    extractor = metadata.get("extractor")
    if isinstance(extractor, dict):
        fragment["extractor"] = dict(extractor)
    elif extractor is not None:
        fragment["extractor"] = str(extractor)

    if metadata.get("requires_review") is True:
        fragment["requires_review"] = True
    return fragment


def _xlsx_sheet_page(sheet_data: Any, page_number: int) -> dict[str, Any]:
    sheet = _to_mapping(sheet_data)
    cells = [_to_mapping(cell) for cell in _list_value(sheet.get("cells"))]
    text_lines = []
    for cell in cells:
        value = cell.get("value")
        if value is None or str(value) == "":
            continue
        ref = str(cell.get("ref") or "")
        text_lines.append(f"{ref}: {value}" if ref else str(value))
    text = "\n".join(text_lines) or str(sheet.get("name") or f"Sheet {page_number}")
    return {
        "page_number": page_number,
        "width": DEFAULT_PAGE_WIDTH_PT,
        "height": max(DEFAULT_PAGE_HEIGHT_PT, 72.0 + (18.0 * max(len(text_lines), 1))),
        "unit": "pt",
        "fragments": [{"kind": "table", "text": text, "extractor": "xlsx"}],
    }


def _pdf_table_report_pages(data: dict[str, Any]) -> list[Any]:
    candidates = [_to_mapping(candidate) for candidate in _list_value(data.get("candidates"))]
    if not candidates:
        return []
    selected_candidate = str(data.get("selected_candidate") or "")
    if selected_candidate:
        table_candidates = [
            candidate
            for candidate in candidates
            if candidate.get("status") == "ok"
            and _table_candidate_name(candidate) == selected_candidate
        ]
    else:
        table_candidates = [candidate for candidate in candidates if candidate.get("status") == "ok"]

    pages_by_number: dict[int, dict[str, Any]] = {}
    for candidate in table_candidates:
        candidate_name = _table_candidate_name(candidate)
        for table_data in _list_value(candidate.get("tables")):
            table = _to_mapping(table_data)
            page_number = _page_number_value(table.get("page_number"), default=1)
            page = pages_by_number.setdefault(
                page_number,
                {
                    "page_number": page_number,
                    "width": DEFAULT_PAGE_WIDTH_PT,
                    "height": DEFAULT_PAGE_HEIGHT_PT,
                    "unit": "pt",
                    "fragments": [],
                },
            )
            fragment: dict[str, Any] = {
                "kind": "table",
                "text": _table_rows_text(table.get("rows")),
                "page_number": page_number,
                "extractor": candidate_name,
            }
            bbox = _table_bbox(table)
            if bbox is not None:
                fragment["bbox"] = bbox
                page["width"] = max(page["width"], bbox["x"] + bbox["width"])
                page["height"] = max(page["height"], bbox["y"] + bbox["height"])
                page["unit"] = bbox["unit"]
            page["fragments"].append(fragment)
    return [pages_by_number[page_number] for page_number in sorted(pages_by_number)]


def _table_candidate_name(candidate: dict[str, Any]) -> str:
    extractor = str(candidate.get("extractor") or "unknown")
    flavor = str(candidate.get("flavor") or "table")
    return f"{extractor}:{flavor}"


def _table_rows_text(rows_value: Any) -> str:
    lines = []
    for row in _list_value(rows_value):
        cells = _list_value(row)
        lines.append("\t".join(str(cell) for cell in cells))
    return "\n".join(lines)


def _table_bbox(table: dict[str, Any]) -> Optional[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    for row in _list_value(table.get("cell_bboxes")):
        for cell_value in _list_value(row):
            cell = _to_mapping(cell_value)
            if cell:
                cells.append(cell)
    if not cells:
        return None

    units = {str(cell.get("unit") or "pt") for cell in cells}
    origins = {str(cell.get("origin") or "top-left") for cell in cells}
    if len(units) != 1 or origins != {"top-left"}:
        return None

    min_x = min(_required_finite_float_value(cell.get("x")) for cell in cells)
    min_y = min(_required_finite_float_value(cell.get("y")) for cell in cells)
    max_x = max(
        _required_finite_float_value(cell.get("x")) + _required_finite_float_value(cell.get("width"))
        for cell in cells
    )
    max_y = max(
        _required_finite_float_value(cell.get("y")) + _required_finite_float_value(cell.get("height"))
        for cell in cells
    )
    return {
        "x": min_x,
        "y": min_y,
        "width": max_x - min_x,
        "height": max_y - min_y,
        "unit": units.pop(),
        "origin": "top-left",
    }


def _page_number_value(value: Any, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return 0


def _is_integer_value(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _page_identifier_error(value: Any) -> Optional[str]:
    if not _is_integer_value(value):
        return "must be an integer"
    if value < 1:
        return "must be >= 1"
    return None


def _finite_float_value(value: Any, *, default: float) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return default
    return converted if math.isfinite(converted) else default


def _required_finite_float_value(value: Any) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return math.nan
    return converted if math.isfinite(converted) else math.nan


def _page_size(page: dict[str, Any]) -> tuple[float, float, str]:
    if "width_pt" in page or "height_pt" in page:
        return (
            _finite_float_value(page.get("width_pt"), default=0.0),
            _finite_float_value(page.get("height_pt"), default=0.0),
            "pt",
        )
    if "width_px" in page or "height_px" in page:
        return (
            _finite_float_value(page.get("width_px"), default=0.0),
            _finite_float_value(page.get("height_px"), default=0.0),
            "px",
        )
    return (
        _finite_float_value(page.get("width"), default=0.0),
        _finite_float_value(page.get("height"), default=0.0),
        str(page.get("unit") or "pt"),
    )


def _confidence_value(value: Any, *, default: float, scale_percent: bool) -> tuple[float, bool]:
    if value is None:
        return default, False
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0, True
    if not math.isfinite(confidence) or confidence < 0:
        return 0.0, True
    if scale_percent:
        confidence = confidence / 100.0
    return confidence, False


def _bbox_value(value: Any) -> Optional[BoundingBox]:
    data = _to_mapping(value)
    if not data:
        return None
    if not {"x", "y", "width", "height"}.issubset(data):
        return None
    return BoundingBox(
        x=_required_finite_float_value(data["x"]),
        y=_required_finite_float_value(data["y"]),
        width=_required_finite_float_value(data["width"]),
        height=_required_finite_float_value(data["height"]),
        unit=str(data.get("unit") or "pt"),
        origin=str(data.get("origin") or "top-left"),
    )


def _block_type(data: dict[str, Any], text: str) -> str:
    raw_type = str(data.get("type") or data.get("kind") or "")
    if raw_type in BLOCK_TYPES:
        return raw_type
    if raw_type and data.get("preserve_invalid_type") is True:
        return raw_type
    return "paragraph"


def _bbox_values_are_finite(bbox: BoundingBox) -> bool:
    return all(math.isfinite(value) for value in (bbox.x, bbox.y, bbox.width, bbox.height))


def _page_dimensions_are_finite(page: DocumentPage) -> bool:
    return math.isfinite(page.width) and math.isfinite(page.height)


def _bbox_outside_page(bbox: BoundingBox, page: DocumentPage) -> bool:
    return bbox.x < 0 or bbox.y < 0 or bbox.x + bbox.width > page.width or bbox.y + bbox.height > page.height
