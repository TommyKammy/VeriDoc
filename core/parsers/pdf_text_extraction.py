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
            text_page = page.get_text("dict")
            for block in text_page.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = span.get("text", "").strip()
                        bbox_values = span.get("bbox")
                        if not text or bbox_values is None:
                            continue
                        x0, y0, x1, y1 = (float(value) for value in bbox_values)
                        fragments.append(
                            TextFragment(
                                text=text,
                                page_number=page_index,
                                bbox=TextBBox(
                                    x=x0,
                                    y=y0,
                                    width=max(0.0, x1 - x0),
                                    height=max(0.0, y1 - y0),
                                ),
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


def _load_fitz() -> Any:
    try:
        import fitz
    except ImportError as exc:
        raise MissingPdfExtractorDependency(
            "PyMuPDF is required for PDF bbox extraction; install evaluation "
            "dependencies with `python3 -m pip install -r requirements-pdf-eval.txt`."
        ) from exc
    return fitz
