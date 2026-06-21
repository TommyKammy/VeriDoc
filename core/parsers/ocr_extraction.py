from __future__ import annotations

import csv
import os
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
            if not extraction.pages or statuses == {"no-text"}:
                status = "no-text"
            elif "low-confidence" in statuses:
                status = "low-confidence"
            else:
                status = "ok"
            candidates.append(
                OcrCandidate(
                    name=adapter.name,
                    version=adapter.version,
                    status=status,
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
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / f"page-{page_number}.png"
            image_path.write_bytes(image_png)
            proc = subprocess.run(
                [binary, str(image_path), "stdout", "--psm", "6", "tsv"],
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

    def __init__(
        self,
        *,
        text_detection_model_dir: str | Path | None = None,
        text_recognition_model_dir: str | Path | None = None,
    ) -> None:
        self.text_detection_model_dir = _optional_path(
            text_detection_model_dir
            or os.environ.get("VERIDOC_PADDLEOCR_TEXT_DETECTION_MODEL_DIR")
        )
        self.text_recognition_model_dir = _optional_path(
            text_recognition_model_dir
            or os.environ.get("VERIDOC_PADDLEOCR_TEXT_RECOGNITION_MODEL_DIR")
        )

    @property
    def version(self) -> str | None:
        try:
            import paddleocr
        except ImportError:
            return None
        return getattr(paddleocr, "__version__", None)

    def recognize(self, image_png: bytes, *, page_number: int) -> list[OcrRawRegion]:
        self._require_local_models()
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise OcrEngineUnavailable("paddleocr package is not installed") from exc

        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / f"page-{page_number}.png"
            image_path.write_bytes(image_png)
            engine = self._create_engine(PaddleOCR)
            if hasattr(engine, "predict"):
                raw_result = engine.predict(str(image_path))
            else:
                raw_result = engine.ocr(str(image_path), cls=False)
        return _parse_paddle_result(raw_result)

    def _require_local_models(self) -> None:
        missing = []
        for env_name, model_dir in (
            ("VERIDOC_PADDLEOCR_TEXT_DETECTION_MODEL_DIR", self.text_detection_model_dir),
            ("VERIDOC_PADDLEOCR_TEXT_RECOGNITION_MODEL_DIR", self.text_recognition_model_dir),
        ):
            if model_dir is None or not model_dir.is_dir():
                missing.append(env_name)
        if missing:
            joined = ", ".join(missing)
            raise OcrEngineUnavailable(
                "paddleocr local model directories are required to avoid model downloads; "
                f"set {joined}"
            )

    def _create_engine(self, paddle_ocr: Any) -> Any:
        try:
            return paddle_ocr(
                lang="en",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                text_detection_model_dir=str(self.text_detection_model_dir),
                text_recognition_model_dir=str(self.text_recognition_model_dir),
            )
        except TypeError:
            return paddle_ocr(
                use_angle_cls=False,
                lang="en",
                det_model_dir=str(self.text_detection_model_dir),
                rec_model_dir=str(self.text_recognition_model_dir),
            )


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
    pages = raw_result if isinstance(raw_result, list) else [raw_result]
    for page_result in pages:
        result_data = _paddle_result_data(page_result)
        if result_data is not None:
            regions.extend(_parse_paddle_result_data(result_data))
            continue
        lines = page_result if isinstance(page_result, list) else []
        for line in lines:
            if not isinstance(line, (list, tuple)) or len(line) < 2:
                continue
            points, text_score = line[0], line[1]
            if not isinstance(text_score, (list, tuple)) or len(text_score) < 2:
                continue
            text = str(text_score[0])
            confidence = _parse_confidence(text_score[1], scale_unit_interval=True)
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


def _parse_paddle_result_data(result_data: dict[str, Any]) -> list[OcrRawRegion]:
    texts = _as_list(result_data.get("rec_texts"))
    scores = _as_list(result_data.get("rec_scores"))
    polygons = _as_list(result_data.get("rec_polys")) or _as_list(result_data.get("dt_polys"))
    boxes = _as_list(result_data.get("rec_boxes")) or _as_list(result_data.get("dt_boxes"))
    regions: list[OcrRawRegion] = []
    for index, text_value in enumerate(texts):
        text = str(text_value)
        bbox = _paddle_bbox(polygons[index] if index < len(polygons) else None)
        if bbox is None:
            bbox = _paddle_bbox(boxes[index] if index < len(boxes) else None)
        if bbox is None:
            continue
        confidence = _parse_confidence(
            scores[index] if index < len(scores) else None,
            scale_unit_interval=True,
        )
        regions.append(OcrRawRegion(text=text, bbox=bbox, confidence=confidence))
    return regions


def _paddle_result_data(page_result: Any) -> dict[str, Any] | None:
    candidates: list[Any] = []
    if isinstance(page_result, dict):
        candidates.append(page_result)
    json_value = getattr(page_result, "json", None)
    if callable(json_value):
        try:
            candidates.append(json_value())
        except TypeError:
            pass
    elif json_value is not None:
        candidates.append(json_value)
    res_value = getattr(page_result, "res", None)
    if res_value is not None:
        candidates.append(res_value)

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        result_data = candidate.get("res", candidate)
        if isinstance(result_data, dict):
            return result_data
    return None


def _paddle_bbox(value: Any) -> tuple[float, float, float, float] | None:
    values = _to_plain_list(value)
    if values is None:
        return None
    try:
        value_count = len(values)
    except TypeError:
        return None
    if value_count == 4 and all(isinstance(item, (int, float)) for item in values):
        x0, y0, x1, y1 = (float(item) for item in values)
        return (x0, y0, x1, y1)
    try:
        xs = [float(point[0]) for point in values]
        ys = [float(point[1]) for point in values]
    except (TypeError, ValueError, IndexError):
        return None
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _as_list(value: Any) -> list[Any]:
    values = _to_plain_list(value)
    return values if isinstance(values, list) else []


def _to_plain_list(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, tuple):
        return list(value)
    return value


def _optional_path(value: str | Path | None) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    return Path(value)


def _parse_confidence(value: Any, *, scale_unit_interval: bool = False) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if confidence < 0:
        return None
    if scale_unit_interval and 0.0 <= confidence <= 1.0:
        return confidence * 100.0
    return confidence
