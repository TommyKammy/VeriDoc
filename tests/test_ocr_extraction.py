from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    import pymupdf as fitz
except ImportError:
    if os.environ.get("VERIDOC_REQUIRE_PDF_EVAL_DEPS") == "1":
        raise
    pytest.skip("PyMuPDF eval dependency is not installed", allow_module_level=True)

from core.parsers.ocr_extraction import (
    OcrEngineUnavailable,
    OcrRawRegion,
    compare_ocr_extractors,
    extract_scanned_pdf_ocr,
)


class FakeOcrAdapter:
    name = "fake-tesseract"
    version = "0.test"

    def recognize(self, image_png: bytes, *, page_number: int) -> list[OcrRawRegion]:
        assert image_png.startswith(b"\x89PNG")
        assert page_number == 1
        return [
            OcrRawRegion(
                text="LOT-001",
                bbox=(10, 12, 70, 26),
                confidence=91.5,
            )
        ]


class LowConfidenceAdapter:
    name = "low-confidence"
    version = "0.test"

    def recognize(self, image_png: bytes, *, page_number: int) -> list[OcrRawRegion]:
        assert image_png.startswith(b"\x89PNG")
        return [
            OcrRawRegion(
                text="unclear",
                bbox=(8, 8, 48, 22),
                confidence=41.0,
            )
        ]


class MissingAdapter:
    name = "missing-engine"
    version = None

    def recognize(self, image_png: bytes, *, page_number: int) -> list[OcrRawRegion]:
        raise OcrEngineUnavailable("missing OCR runtime")


def _write_image_only_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=180, height=90)
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 48), 1)
    pixmap.clear_with(255)
    page.insert_image(fitz.Rect(20, 20, 140, 68), pixmap=pixmap)
    document.save(path)
    document.close()


def test_extract_scanned_pdf_ocr_returns_text_bbox_and_confidence_for_image_pdf(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "scan.pdf"
    _write_image_only_pdf(pdf_path)

    with fitz.open(pdf_path) as document:
        assert document[0].get_text().strip() == ""

    result = extract_scanned_pdf_ocr(pdf_path, adapter=FakeOcrAdapter())

    assert result.source_path == str(pdf_path)
    assert result.engine == "fake-tesseract"
    assert len(result.pages) == 1
    page = result.pages[0]
    assert page.page_number == 1
    assert page.width_px > 0
    assert page.height_px > 0
    assert page.status == "ok"
    assert page.low_confidence_count == 0
    region = page.regions[0]
    assert region.text == "LOT-001"
    assert region.confidence == 91.5
    assert region.bbox.unit == "px"
    assert region.bbox.origin == "top-left"
    assert region.bbox.x == 10
    assert region.bbox.y == 12
    assert region.bbox.width == 60
    assert region.bbox.height == 14
    assert region.low_confidence is False


def test_extract_scanned_pdf_ocr_marks_low_confidence_without_promoting_success(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "low-confidence.pdf"
    _write_image_only_pdf(pdf_path)

    result = extract_scanned_pdf_ocr(
        pdf_path,
        adapter=LowConfidenceAdapter(),
        min_confidence=80.0,
    )

    page = result.pages[0]
    assert page.status == "low-confidence"
    assert page.low_confidence_count == 1
    assert page.regions[0].low_confidence is True


def test_compare_ocr_extractors_reports_engine_failures_without_success(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "missing.pdf"
    _write_image_only_pdf(pdf_path)

    candidates = compare_ocr_extractors(pdf_path, adapters=[MissingAdapter()])

    assert candidates == [
        {
            "name": "missing-engine",
            "version": None,
            "status": "not-installed",
            "region_count": 0,
            "low_confidence_count": 0,
            "supports_bbox": True,
            "supports_confidence": True,
            "notes": "missing OCR runtime",
        }
    ]
