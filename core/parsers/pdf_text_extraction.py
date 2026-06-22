from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TextBBox:
    x: float
    y: float
    width: float
    height: float
    unit: str = "pt"
    origin: str = "top-left"


@dataclass(frozen=True)
class TextFragment:
    text: str
    page_number: int
    bbox: TextBBox
    extractor: str


@dataclass(frozen=True)
class PdfPageText:
    page_number: int
    width_pt: float
    height_pt: float
    fragments: list[TextFragment]


@dataclass(frozen=True)
class PdfTextExtraction:
    source_path: str
    extractor: str
    pages: list[PdfPageText]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExtractorCandidate:
    name: str
    version: str | None
    supports_bbox: bool
    status: str
    fragment_count: int
    notes: str


class MissingPdfExtractorDependency(RuntimeError):
    """Raised when an optional PDF extraction dependency is unavailable."""


def parse_text_pdf_to_document_ir(
    pdf_path: str | Path,
    *,
    document_id: str | None = None,
) -> dict[str, Any]:
    """Convert a text PDF into the minimal Document IR v0 shape."""
    extraction = extract_pdf_text(pdf_path)
    if not extraction.pages:
        raise ValueError("PDF text extraction produced no pages")

    source = Path(pdf_path)
    ir_pages = [
        {
            "page_number": page.page_number,
            "width": page.width_pt,
            "height": page.height_pt,
            "unit": "pt",
        }
        for page in extraction.pages
    ]
    blocks: list[dict[str, Any]] = []

    for page in extraction.pages:
        page_lines = _group_fragments_by_line(page.fragments)
        if not page_lines:
            blocks.append(
                _review_required_empty_page_block(page, extraction, block_index=len(blocks) + 1)
            )
            continue
        for line_group in _group_table_lines(page_lines):
            block_type = "table" if _is_table_line_group(line_group) else "paragraph"
            text = "\n".join(_line_text(line) for line in line_group)
            bbox = _union_text_bboxes(fragment for line in line_group for fragment in line)
            if bbox is None:
                continue
            block_id = f"block-{len(blocks) + 1:03d}"
            blocks.append(
                {
                    "id": block_id,
                    "type": block_type,
                    "text": text,
                    "value_metadata": {
                        "source_page": page.page_number,
                        "bbox": _bbox_to_ir(bbox),
                        "extractor": {
                            "name": (
                                "pymupdf-text-table-heuristic"
                                if block_type == "table"
                                else extraction.extractor
                            ),
                            "version": _package_version("PyMuPDF") or "unknown",
                        },
                        "confidence": 0.6 if block_type == "table" else 0.9,
                        "requires_review": block_type == "table",
                    },
                }
            )

    if not ir_pages:
        raise ValueError("PDF text extraction produced no pages")
    if not blocks:
        raise ValueError("PDF text extraction produced no document blocks")

    document_ir = {
        "schema_version": "document-ir/v0",
        "document": {
            "id": document_id or source.stem,
            "title": source.name,
            "source_type": "pdf",
        },
        "pages": ir_pages,
        "blocks": blocks,
    }
    return document_ir


def _review_required_empty_page_block(
    page: PdfPageText,
    extraction: PdfTextExtraction,
    *,
    block_index: int,
) -> dict[str, Any]:
    return {
        "id": f"block-{block_index:03d}",
        "type": "paragraph",
        "text": "PDF text extraction produced no text blocks for this page.",
        "value_metadata": {
            "source_page": page.page_number,
            "bbox": {
                "x": 0.0,
                "y": 0.0,
                "width": page.width_pt,
                "height": page.height_pt,
            },
            "extractor": {
                "name": extraction.extractor,
                "version": _package_version("PyMuPDF") or "unknown",
            },
            "confidence": 0.0,
            "requires_review": True,
        },
    }


def extract_pdf_text(pdf_path: str | Path) -> PdfTextExtraction:
    """Extract text spans with page numbers and top-left-origin point bboxes."""
    source = Path(pdf_path)
    if not source.is_file():
        raise FileNotFoundError(f"PDF file not found: {source}")
    fitz = _load_fitz()

    pages: list[PdfPageText] = []
    try:
        document = fitz.open(source)
    except Exception as exc:  # pragma: no cover - exact PyMuPDF exception varies by file damage.
        raise ValueError(f"failed to open PDF: {source}") from exc

    try:
        for page_index, page in enumerate(document, start=1):
            page_rect = page.cropbox
            fragments: list[TextFragment] = []
            text_page = page.get_text("dict", flags=_text_dict_flags(fitz))
            for block in text_page.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "")
                        bbox_values = span.get("bbox")
                        if not text.strip() or bbox_values is None:
                            continue
                        bbox = _normalize_bbox(
                            bbox_values,
                            page_width=float(page_rect.width),
                            page_height=float(page_rect.height),
                        )
                        if bbox is None:
                            continue
                        fragments.append(
                            TextFragment(
                                text=text,
                                page_number=page_index,
                                bbox=bbox,
                                extractor="pymupdf",
                            )
                        )
            pages.append(
                PdfPageText(
                    page_number=page_index,
                    width_pt=float(page_rect.width),
                    height_pt=float(page_rect.height),
                    fragments=fragments,
                )
            )
    finally:
        document.close()

    return PdfTextExtraction(source_path=str(source), extractor="pymupdf", pages=pages)


def compare_pdf_text_extractors(pdf_path: str | Path) -> list[ExtractorCandidate]:
    """Return a minimal candidate comparison for the Phase0 PDF extraction spike."""
    candidates: list[ExtractorCandidate] = []

    try:
        result = extract_pdf_text(pdf_path)
        candidates.append(
            ExtractorCandidate(
                name="pymupdf",
                version=_package_version("PyMuPDF"),
                supports_bbox=True,
                status="ok",
                fragment_count=sum(len(page.fragments) for page in result.pages),
                notes=(
                    "Provides text spans with page geometry and bbox coordinates "
                    "in unrotated PDF text coordinates."
                ),
            )
        )
    except MissingPdfExtractorDependency as exc:
        candidates.append(
            ExtractorCandidate(
                name="pymupdf",
                version=None,
                supports_bbox=True,
                status="not-installed",
                fragment_count=0,
                notes=str(exc),
            )
        )
    except Exception as exc:
        candidates.append(
            ExtractorCandidate(
                name="pymupdf",
                version=_package_version("PyMuPDF"),
                supports_bbox=True,
                status="failed",
                fragment_count=0,
                notes=str(exc),
            )
        )

    candidates.append(_compare_pypdf(pdf_path))
    return candidates


def _compare_pypdf(pdf_path: str | Path) -> ExtractorCandidate:
    try:
        import pypdf
    except ImportError:
        return ExtractorCandidate(
            name="pypdf",
            version=None,
            supports_bbox=False,
            status="not-installed",
            fragment_count=0,
            notes="Text extraction candidate only; bbox extraction is not available in this spike.",
        )

    try:
        reader = pypdf.PdfReader(str(pdf_path))
        fragment_count = sum(1 for page in reader.pages if (page.extract_text() or "").strip())
        return ExtractorCandidate(
            name="pypdf",
            version=_package_version("pypdf"),
            supports_bbox=False,
            status="text-only",
            fragment_count=fragment_count,
            notes="Extracts text but does not provide reliable fragment-level bboxes here.",
        )
    except Exception as exc:
        return ExtractorCandidate(
            name="pypdf",
            version=_package_version("pypdf"),
            supports_bbox=False,
            status="failed",
            fragment_count=0,
            notes=str(exc),
        )


def _package_version(package_name: str) -> str | None:
    try:
        return metadata.version(package_name)
    except metadata.PackageNotFoundError:
        return None


def _normalize_bbox(
    bbox_values: Any,
    *,
    page_width: float,
    page_height: float,
) -> TextBBox | None:
    x0, y0, x1, y1 = (float(value) for value in bbox_values)
    clipped_x0 = min(max(x0, 0.0), page_width)
    clipped_y0 = min(max(y0, 0.0), page_height)
    clipped_x1 = min(max(x1, 0.0), page_width)
    clipped_y1 = min(max(y1, 0.0), page_height)
    width = clipped_x1 - clipped_x0
    height = clipped_y1 - clipped_y0
    if width <= 0 or height <= 0:
        return None
    return TextBBox(
        x=clipped_x0,
        y=clipped_y0,
        width=width,
        height=height,
    )


def _group_fragments_by_line(fragments: list[TextFragment]) -> list[list[TextFragment]]:
    lines: list[list[TextFragment]] = []
    sorted_fragments = sorted(fragments, key=lambda fragment: (fragment.bbox.y, fragment.bbox.x))

    for fragment in sorted_fragments:
        for line in reversed(lines):
            if _belongs_to_line(fragment, line):
                line.append(fragment)
                line.sort(key=lambda item: item.bbox.x)
                break
        else:
            lines.append([fragment])

    return lines


def _belongs_to_line(fragment: TextFragment, line: list[TextFragment]) -> bool:
    reference = line[0].bbox
    max_height = max(reference.height, fragment.bbox.height)
    if abs(_bbox_center_y(fragment.bbox) - _bbox_center_y(reference)) > max_height * 0.25:
        return False
    return (
        _horizontal_gap_to_line(fragment, line)
        <= max_height * 2.0
    )


def _bbox_center_y(bbox: TextBBox) -> float:
    return bbox.y + (bbox.height / 2.0)


def _horizontal_gap_to_line(fragment: TextFragment, line: list[TextFragment]) -> float:
    fragment_left = fragment.bbox.x
    fragment_right = fragment.bbox.x + fragment.bbox.width
    gaps: list[float] = []
    for existing in line:
        existing_left = existing.bbox.x
        existing_right = existing.bbox.x + existing.bbox.width
        if fragment_right < existing_left:
            gaps.append(existing_left - fragment_right)
        elif existing_right < fragment_left:
            gaps.append(fragment_left - existing_right)
        else:
            return 0.0
    return min(gaps) if gaps else 0.0


def _group_table_lines(lines: list[list[TextFragment]]) -> list[list[list[TextFragment]]]:
    groups: list[list[list[TextFragment]]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_table_line(line):
            table_lines = [line]
            index += 1
            while (
                index < len(lines)
                and _is_table_line(lines[index])
                and _are_vertically_adjacent_table_lines(table_lines[-1], lines[index])
            ):
                table_lines.append(lines[index])
                index += 1
            groups.append(table_lines)
        else:
            groups.append([line])
            index += 1
    return groups


def _is_table_line_group(line_group: list[list[TextFragment]]) -> bool:
    return bool(line_group) and all(_is_table_line(line) for line in line_group)


def _are_vertically_adjacent_table_lines(
    previous_line: list[TextFragment],
    next_line: list[TextFragment],
) -> bool:
    previous_bbox = _union_text_bboxes(previous_line)
    next_bbox = _union_text_bboxes(next_line)
    if previous_bbox is None or next_bbox is None:
        return False
    vertical_gap = next_bbox.y - (previous_bbox.y + previous_bbox.height)
    max_height = max(previous_bbox.height, next_bbox.height)
    rows_progress_down_page = (
        next_bbox.y > previous_bbox.y
        and _bbox_center_y(next_bbox) > _bbox_center_y(previous_bbox)
    )
    if not rows_progress_down_page:
        return False
    return -max_height * 0.75 <= vertical_gap <= max_height


def _is_table_line(line: list[TextFragment]) -> bool:
    return "\t" in _line_text(line)


def _line_text(line: list[TextFragment]) -> str:
    if not line:
        return ""

    ordered = sorted(line, key=lambda fragment: fragment.bbox.x)
    text = ordered[0].text
    previous = ordered[0]
    for fragment in ordered[1:]:
        gap = fragment.bbox.x - (previous.bbox.x + previous.bbox.width)
        if _needs_inserted_space(text, fragment.text, gap, previous, fragment):
            text += " "
        text += fragment.text
        previous = fragment
    return text


def _needs_inserted_space(
    left_text: str,
    right_text: str,
    gap: float,
    left_fragment: TextFragment,
    right_fragment: TextFragment,
) -> bool:
    if gap <= max(left_fragment.bbox.height, right_fragment.bbox.height) * 0.15:
        return False
    if not left_text or not right_text:
        return False
    return not left_text[-1].isspace() and not right_text[0].isspace()


def _union_text_bboxes(fragments: Any) -> TextBBox | None:
    fragment_list = list(fragments)
    if not fragment_list:
        return None
    x0 = min(fragment.bbox.x for fragment in fragment_list)
    y0 = min(fragment.bbox.y for fragment in fragment_list)
    x1 = max(fragment.bbox.x + fragment.bbox.width for fragment in fragment_list)
    y1 = max(fragment.bbox.y + fragment.bbox.height for fragment in fragment_list)
    return TextBBox(x=x0, y=y0, width=x1 - x0, height=y1 - y0)


def _bbox_to_ir(bbox: TextBBox) -> dict[str, float]:
    return {
        "x": bbox.x,
        "y": bbox.y,
        "width": bbox.width,
        "height": bbox.height,
    }


def _load_fitz() -> Any:
    try:
        import pymupdf as fitz
    except ImportError as exc:
        raise MissingPdfExtractorDependency(
            "PyMuPDF is required for PDF bbox extraction; install evaluation "
            "dependencies with `python3 -m pip install -r requirements-pdf-eval.txt`."
        ) from exc
    return fitz


def _text_dict_flags(fitz: Any) -> int:
    flags = int(fitz.TEXTFLAGS_DICT)
    return flags & ~int(fitz.TEXT_PRESERVE_IMAGES)
