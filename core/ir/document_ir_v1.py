from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import math
from typing import Any, List, Optional


SCHEMA_VERSION = "document-ir/v1"
SOURCE_TYPES = {"pdf", "docx", "xlsx", "unknown"}
BLOCK_TYPES = {"heading", "paragraph", "table", "field", "list_item"}


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

    for page_index, page_data in enumerate(_list_value(data.get("pages")), start=1):
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
        if page.page_number < 1:
            errors.append(f"pages[{index}].page_number must be >= 1")
            continue
        if page.page_number in pages_by_number:
            errors.append(f"pages[{index}].page_number duplicates page {page.page_number}")
        pages_by_number[page.page_number] = page
        if page.width <= 0 or page.height <= 0:
            errors.append(f"pages[{index}] dimensions must be positive")

    for index, block in enumerate(document_ir.blocks):
        if block.type not in BLOCK_TYPES:
            errors.append(f"blocks[{index}].type is unsupported: {block.type}")
        page = pages_by_number.get(block.source_page)
        if page is None:
            errors.append(f"blocks[{index}].source_page references undeclared page {block.source_page}")
            continue
        if block.bbox.width < 0 or block.bbox.height < 0:
            errors.append(f"blocks[{index}].bbox dimensions must be non-negative")
        if _bbox_outside_page(block.bbox, page):
            errors.append(f"blocks[{index}].bbox extends past page {block.source_page}")
        if block.confidence < 0 or block.confidence > 1:
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
    confidence = _confidence_value(
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

    if data.get("confidence") is None and data.get("engine"):
        confidence = 0.0
        requires_review = True
        review_warnings.append(f"blocks[{block_index - 1}].confidence missing; block marked requires_review")
    if data.get("low_confidence") is True:
        requires_review = True
        review_warnings.append(f"blocks[{block_index - 1}].low confidence; block marked requires_review")

    return DocumentBlock(
        id=f"block-{block_index:04d}",
        type=_block_type(text),
        text=text,
        source_page=source_page,
        bbox=bbox,
        extractor=ExtractorRef(name=str(data.get("extractor") or data.get("engine") or fallback_extractor)),
        confidence=confidence,
        review=ReviewState(requires_review=requires_review, warnings=review_warnings),
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


def _finite_float_value(value: Any, *, default: float) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return default
    return converted if math.isfinite(converted) else default


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


def _confidence_value(value: Any, *, default: float, scale_percent: bool) -> float:
    if value is None:
        return default
    confidence = _finite_float_value(value, default=-1.0)
    if confidence < 0:
        return 0.0
    if scale_percent:
        confidence = confidence / 100.0
    return confidence


def _bbox_value(value: Any) -> Optional[BoundingBox]:
    data = _to_mapping(value)
    if not data:
        return None
    if not {"x", "y", "width", "height"}.issubset(data):
        return None
    return BoundingBox(
        x=_finite_float_value(data["x"], default=0.0),
        y=_finite_float_value(data["y"], default=0.0),
        width=_finite_float_value(data["width"], default=0.0),
        height=_finite_float_value(data["height"], default=0.0),
        unit=str(data.get("unit") or "pt"),
        origin=str(data.get("origin") or "top-left"),
    )


def _block_type(text: str) -> str:
    return "paragraph"


def _bbox_outside_page(bbox: BoundingBox, page: DocumentPage) -> bool:
    return bbox.x < 0 or bbox.y < 0 or bbox.x + bbox.width > page.width or bbox.y + bbox.height > page.height
