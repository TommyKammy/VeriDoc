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
    source = Path(pdf_path)
    blocks: list[dict[str, Any]] = []

    for page in extraction.pages:
        page_lines = _group_fragments_by_line(page.fragments)
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

    if not blocks and extraction.pages:
        first_page = extraction.pages[0]
        blocks.append(
            {
                "id": "block-001",
                "type": "paragraph",
                "text": "PDF text extraction produced no text blocks.",
                "value_metadata": {
                    "source_page": first_page.page_number,
                    "bbox": {
                        "x": 0.0,
                        "y": 0.0,
                        "width": first_page.width_pt,
                        "height": first_page.height_pt,
                    },
                    "extractor": {
                        "name": extraction.extractor,
                        "version": _package_version("PyMuPDF") or "unknown",
                    },
                    "confidence": 0.0,
                    "requires_review": True,
                },
            }
        )

    return {
        "schema_version": "document-ir/v0",
        "document": {
            "id": document_id or source.stem,
            "title": source.name,
            "source_type": "pdf",
        },
        "pages": [
            {
                "page_number": page.page_number,
                "width": page.width_pt,
                "height": page.height_pt,
                "unit": "pt",
            }
            for page in extraction.pages
        ],
        "blocks": blocks,
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
            reference = line[0].bbox
            tolerance = max(reference.height, fragment.bbox.height) * 0.75
            if abs(fragment.bbox.y - reference.y) <= tolerance:
                line.append(fragment)
                line.sort(key=lambda item: item.bbox.x)
                break
        else:
            lines.append([fragment])

    return lines


def _group_table_lines(lines: list[list[TextFragment]]) -> list[list[list[TextFragment]]]:
    groups: list[list[list[TextFragment]]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_table_line(line):
            table_lines = [line]
            index += 1
            while index < len(lines) and _is_table_line(lines[index]):
                table_lines.append(lines[index])
                index += 1
            groups.append(table_lines)
        else:
            groups.append([line])
            index += 1
    return groups


def _is_table_line_group(line_group: list[list[TextFragment]]) -> bool:
    return len(line_group) > 1 and all(_is_table_line(line) for line in line_group)


def _is_table_line(line: list[TextFragment]) -> bool:
    return "\t" in _line_text(line)


def _line_text(line: list[TextFragment]) -> str:
    return "".join(fragment.text for fragment in line)


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
