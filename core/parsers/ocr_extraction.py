from __future__ import annotations

import csv
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from io import StringIO
from pathlib import Path
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class OcrBBox:
    x: float
    y: float
    width: float
    height: float
    unit: str = "px"
    origin: str = "top-left"


@dataclass(frozen=True)
class OcrRawRegion:
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float | None


@dataclass(frozen=True)
class OcrRegion:
    text: str
    page_number: int
    bbox: OcrBBox
    confidence: float | None
    low_confidence: bool
    engine: str


@dataclass(frozen=True)
class OcrPage:
    page_number: int
    width_px: int
    height_px: int
    regions: list[OcrRegion]
    status: str
    low_confidence_count: int


@dataclass(frozen=True)
class OcrExtraction:
    source_path: str
    engine: str
    pages: list[OcrPage]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OcrCandidate:
    name: str
    version: str | None
    status: str
    region_count: int
    low_confidence_count: int
    supports_bbox: bool
    supports_confidence: bool
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class OcrEngineUnavailable(RuntimeError):
    """Raised when a local OCR engine or runtime is unavailable."""


class OcrAdapter(Protocol):
    name: str
    version: str | None

    def recognize(self, image_png: bytes, *, page_number: int) -> list[OcrRawRegion]:
        """Return OCR text regions for one page raster."""


def extract_scanned_pdf_ocr(
    pdf_path: str | Path,
    *,
    adapter: OcrAdapter | None = None,
    min_confidence: float = 70.0,
    zoom: float = 2.0,
) -> OcrExtraction:
    """Rasterize a PDF locally and extract OCR text regions with confidence."""
    source = Path(pdf_path)
    if not source.is_file():
        raise FileNotFoundError(f"PDF file not found: {source}")
    fitz = _load_fitz()
    ocr_adapter = adapter or TesseractCliAdapter()

    pages: list[OcrPage] = []
    try:
        document = fitz.open(source)
    except Exception as exc:  # pragma: no cover - exact PyMuPDF exception varies by file damage.
        raise ValueError(f"failed to open PDF: {source}") from exc

    try:
        for page_index, page in enumerate(document, start=1):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            image_png = pixmap.tobytes("png")
            raw_regions = ocr_adapter.recognize(image_png, page_number=page_index)
            regions = _normalize_regions(
                raw_regions,
                page_number=page_index,
                page_width=pixmap.width,
                page_height=pixmap.height,
                engine=ocr_adapter.name,
                min_confidence=min_confidence,
            )
            low_confidence_count = sum(1 for region in regions if region.low_confidence)
            pages.append(
                OcrPage(
                    page_number=page_index,
                    width_px=pixmap.width,
                    height_px=pixmap.height,
                    regions=regions,
                    status=_page_status(regions, low_confidence_count),
                    low_confidence_count=low_confidence_count,
                )
            )
    finally:
        document.close()

    return OcrExtraction(source_path=str(source), engine=ocr_adapter.name, pages=pages)


def compare_ocr_extractors(
    pdf_path: str | Path,
    *,
    adapters: Sequence[OcrAdapter] | None = None,
    min_confidence: float = 70.0,
) -> list[dict[str, Any]]:
    """Return fail-closed local OCR candidate results for the Phase0 spike."""
    candidates: list[OcrCandidate] = []
    for adapter in adapters or default_ocr_adapters():
        try:
            extraction = extract_scanned_pdf_ocr(
                pdf_path,
                adapter=adapter,
                min_confidence=min_confidence,
            )
        except OcrEngineUnavailable as exc:
            candidates.append(
                OcrCandidate(
                    name=adapter.name,
                    version=adapter.version,
                    status="not-installed",
                    region_count=0,
                    low_confidence_count=0,
                    supports_bbox=True,
                    supports_confidence=True,
                    notes=str(exc),
                )
            )
        except Exception as exc:
            candidates.append(
                OcrCandidate(
                    name=adapter.name,
                    version=adapter.version,
                    status="failed",
                    region_count=0,
                    low_confidence_count=0,
                    supports_bbox=True,
                    supports_confidence=True,
                    notes=str(exc),
                )
            )
        else:
            region_count = sum(len(page.regions) for page in extraction.pages)
            low_confidence_count = sum(page.low_confidence_count for page in extraction.pages)
            statuses = {page.status for page in extraction.pages}
            candidates.append(
                OcrCandidate(
                    name=adapter.name,
                    version=adapter.version,
                    status="low-confidence" if "low-confidence" in statuses else "ok",
                    region_count=region_count,
                    low_confidence_count=low_confidence_count,
                    supports_bbox=True,
                    supports_confidence=True,
                    notes="Local OCR candidate; no external service call is made by this adapter.",
                )
            )
    return [candidate.to_dict() for candidate in candidates]


def default_ocr_adapters() -> list[OcrAdapter]:
    return [TesseractCliAdapter(), PaddleOcrAdapter()]


class TesseractCliAdapter:
    name = "tesseract"

    @property
    def version(self) -> str | None:
        binary = shutil.which("tesseract")
        if binary is None:
            return None
        proc = subprocess.run(
            [binary, "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.splitlines()[0].replace("tesseract ", "", 1).strip() or None

    def recognize(self, image_png: bytes, *, page_number: int) -> list[OcrRawRegion]:
        binary = shutil.which("tesseract")
        if binary is None:
            raise OcrEngineUnavailable("tesseract executable is not installed")
        with tempfile.NamedTemporaryFile(suffix=".png") as image_file:
            image_file.write(image_png)
            image_file.flush()
            proc = subprocess.run(
                [binary, image_file.name, "stdout", "--psm", "6", "tsv"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "tesseract OCR failed")
        return _parse_tesseract_tsv(proc.stdout)


class PaddleOcrAdapter:
    name = "paddleocr"

    @property
    def version(self) -> str | None:
        try:
            import paddleocr
        except ImportError:
            return None
        return getattr(paddleocr, "__version__", None)

    def recognize(self, image_png: bytes, *, page_number: int) -> list[OcrRawRegion]:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise OcrEngineUnavailable("paddleocr package is not installed") from exc

        with tempfile.NamedTemporaryFile(suffix=".png") as image_file:
            image_file.write(image_png)
            image_file.flush()
            engine = PaddleOCR(use_angle_cls=False, lang="en")
            raw_result = engine.ocr(image_file.name, cls=False)
        return _parse_paddle_result(raw_result)


def _load_fitz() -> Any:
    try:
        import pymupdf as fitz
    except ImportError as exc:
        raise OcrEngineUnavailable("PyMuPDF is required to rasterize PDFs for OCR") from exc
    return fitz


def _normalize_regions(
    raw_regions: Sequence[OcrRawRegion],
    *,
    page_number: int,
    page_width: int,
    page_height: int,
    engine: str,
    min_confidence: float,
) -> list[OcrRegion]:
    regions: list[OcrRegion] = []
    for raw_region in raw_regions:
        text = raw_region.text
        if not text.strip():
            continue
        bbox = _normalize_bbox(raw_region.bbox, page_width=page_width, page_height=page_height)
        if bbox is None:
            continue
        confidence = raw_region.confidence
        low_confidence = confidence is None or confidence < min_confidence
        regions.append(
            OcrRegion(
                text=text,
                page_number=page_number,
                bbox=bbox,
                confidence=confidence,
                low_confidence=low_confidence,
                engine=engine,
            )
        )
    return regions


def _normalize_bbox(
    bbox_values: tuple[float, float, float, float],
    *,
    page_width: int,
    page_height: int,
) -> OcrBBox | None:
    x0, y0, x1, y1 = (float(value) for value in bbox_values)
    clipped_x0 = min(max(x0, 0.0), float(page_width))
    clipped_y0 = min(max(y0, 0.0), float(page_height))
    clipped_x1 = min(max(x1, 0.0), float(page_width))
    clipped_y1 = min(max(y1, 0.0), float(page_height))
    width = clipped_x1 - clipped_x0
    height = clipped_y1 - clipped_y0
    if width <= 0 or height <= 0:
        return None
    return OcrBBox(x=clipped_x0, y=clipped_y0, width=width, height=height)


def _page_status(regions: Sequence[OcrRegion], low_confidence_count: int) -> str:
    if not regions:
        return "no-text"
    if low_confidence_count:
        return "low-confidence"
    return "ok"


def _parse_tesseract_tsv(tsv: str) -> list[OcrRawRegion]:
    regions: list[OcrRawRegion] = []
    reader = csv.DictReader(StringIO(tsv), delimiter="\t")
    for row in reader:
        text = row.get("text", "")
        if not text.strip():
            continue
        try:
            left = float(row.get("left", "0"))
            top = float(row.get("top", "0"))
            width = float(row.get("width", "0"))
            height = float(row.get("height", "0"))
        except ValueError:
            continue
        confidence = _parse_confidence(row.get("conf"))
        regions.append(
            OcrRawRegion(
                text=text,
                bbox=(left, top, left + width, top + height),
                confidence=confidence,
            )
        )
    return regions


def _parse_paddle_result(raw_result: Any) -> list[OcrRawRegion]:
    regions: list[OcrRawRegion] = []
    pages = raw_result if isinstance(raw_result, list) else []
    for page_result in pages:
        lines = page_result if isinstance(page_result, list) else []
        for line in lines:
            if not isinstance(line, (list, tuple)) or len(line) < 2:
                continue
            points, text_score = line[0], line[1]
            if not isinstance(text_score, (list, tuple)) or len(text_score) < 2:
                continue
            text = str(text_score[0])
            confidence = _parse_confidence(text_score[1])
            try:
                xs = [float(point[0]) for point in points]
                ys = [float(point[1]) for point in points]
            except (TypeError, ValueError, IndexError):
                continue
            regions.append(
                OcrRawRegion(
                    text=text,
                    bbox=(min(xs), min(ys), max(xs), max(ys)),
                    confidence=confidence,
                )
            )
    return regions


def _parse_confidence(value: Any) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if confidence < 0:
        return None
    if 0.0 <= confidence <= 1.0:
        return confidence * 100.0
    return confidence
