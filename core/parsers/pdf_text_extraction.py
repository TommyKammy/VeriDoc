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
