from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.ci.validate_document_ir import validate, validate_document_ir_consistency

try:
    import pymupdf as fitz
except ImportError:
    if os.environ.get("VERIDOC_REQUIRE_PDF_EVAL_DEPS") == "1":
        raise
    pytest.skip("PyMuPDF eval dependency is not installed", allow_module_level=True)

from core.parsers import pdf_text_extraction
from core.parsers.pdf_text_extraction import (
    PdfPageText,
    PdfTextExtraction,
    TextBBox,
    TextFragment,
    extract_pdf_text,
    parse_text_pdf_to_document_ir,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "core" / "ir" / "document-ir-v0.schema.json"


def _write_pdf(
    path: Path,
    pages: list[list[tuple[str, tuple[float, float]]]],
    *,
    rotation: int = 0,
) -> None:
    document = fitz.open()
    for page_fragments in pages:
        page = document.new_page(width=300, height=200)
        for text, point in page_fragments:
            page.insert_text(point, text, fontsize=12)
        if rotation:
            page.set_rotation(rotation)
    document.save(path)
    document.close()


def test_extract_pdf_text_returns_page_numbers_and_top_left_point_bboxes_for_three_samples(tmp_path: Path) -> None:
    samples = [
        ("single-page.pdf", [[("Alpha batch", (36, 48))]]),
        ("multi-fragment.pdf", [[("Lot", (42, 70)), ("Result", (110, 70))]]),
        ("multi-page.pdf", [[("Page one", (36, 52))], [("Page two", (40, 60))]]),
    ]

    for filename, pages in samples:
        pdf_path = tmp_path / filename
        _write_pdf(pdf_path, pages)

        result = extract_pdf_text(pdf_path)

        expected_text = [text for page in pages for text, _point in page]
        fragments = [fragment for page in result.pages for fragment in page.fragments]
        assert [fragment.text for fragment in fragments] == expected_text
        assert [page.page_number for page in result.pages] == list(range(1, len(pages) + 1))

        for page in result.pages:
            assert page.width_pt == 300
            assert page.height_pt == 200
            for fragment in page.fragments:
                assert fragment.page_number == page.page_number
                assert fragment.bbox.unit == "pt"
                assert fragment.bbox.origin == "top-left"
                assert fragment.bbox.x >= 0
                assert fragment.bbox.y >= 0
                assert fragment.bbox.width > 0
                assert fragment.bbox.height > 0
                assert fragment.bbox.x + fragment.bbox.width <= page.width_pt
                assert fragment.bbox.y + fragment.bbox.height <= page.height_pt


def test_extract_pdf_text_uses_unrotated_text_coordinate_space_for_rotated_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "rotated.pdf"
    _write_pdf(pdf_path, [[("Rotated text", (40, 60))]], rotation=90)

    with fitz.open(pdf_path) as document:
        rotated_rect = document[0].rect
        assert rotated_rect.width == 200
        assert rotated_rect.height == 300

    result = extract_pdf_text(pdf_path)

    page = result.pages[0]
    assert page.width_pt == 300
    assert page.height_pt == 200
    fragment = page.fragments[0]
    assert fragment.text == "Rotated text"
    assert fragment.bbox.x + fragment.bbox.width <= page.width_pt
    assert fragment.bbox.y + fragment.bbox.height <= page.height_pt


def test_extract_pdf_text_preserves_span_whitespace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "styled-boundary.pdf"
    _write_pdf(pdf_path, [[("placeholder", (36, 48))]])
    original_get_text = fitz.Page.get_text

    def get_text_with_boundary_space(page: fitz.Page, option: str, *args: object, **kwargs: object) -> object:
        if option == "dict":
            return {
                "blocks": [
                    {
                        "lines": [
                            {
                                "spans": [
                                    {"text": "Approved ", "bbox": (36, 40, 86, 52)},
                                    {"text": "By", "bbox": (86, 40, 100, 52)},
                                ]
                            }
                        ]
                    }
                ]
            }
        return original_get_text(page, option, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_text", get_text_with_boundary_space)

    result = extract_pdf_text(pdf_path)

    fragments = result.pages[0].fragments
    assert [fragment.text for fragment in fragments] == ["Approved ", "By"]


def test_extract_pdf_text_disables_image_payload_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "text-only-flags.pdf"
    _write_pdf(pdf_path, [[("Text only", (36, 48))]])
    captured_flags: list[int] = []
    original_get_text = fitz.Page.get_text

    def get_text_with_flag_capture(page: fitz.Page, option: str, *args: object, **kwargs: object) -> object:
        if option == "dict":
            captured_flags.append(kwargs["flags"])
            return {
                "blocks": [
                    {
                        "lines": [
                            {
                                "spans": [
                                    {"text": "Text only", "bbox": (36, 40, 86, 52)},
                                ]
                            }
                        ]
                    }
                ]
            }
        return original_get_text(page, option, *args, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_text", get_text_with_flag_capture)

    result = extract_pdf_text(pdf_path)

    assert [fragment.text for fragment in result.pages[0].fragments] == ["Text only"]
    assert captured_flags
    assert all(flags & fitz.TEXT_PRESERVE_IMAGES == 0 for flags in captured_flags)
    assert all(flags & fitz.TEXT_PRESERVE_WHITESPACE for flags in captured_flags)


def test_extract_pdf_text_clips_bboxes_to_crop_box_dimensions(tmp_path: Path) -> None:
    pdf_path = tmp_path / "cropped.pdf"
    document = fitz.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((45, 80), "Left", fontsize=12)
    page.insert_text((240, 80), "RightEdge", fontsize=12)
    page.set_cropbox(fitz.Rect(50, 40, 250, 180))
    document.save(pdf_path)
    document.close()

    result = extract_pdf_text(pdf_path)

    page_result = result.pages[0]
    assert page_result.width_pt == 200
    assert page_result.height_pt == 140
    assert page_result.fragments
    for fragment in page_result.fragments:
        assert fragment.bbox.x >= 0
        assert fragment.bbox.y >= 0
        assert fragment.bbox.width > 0
        assert fragment.bbox.height > 0
        assert fragment.bbox.x + fragment.bbox.width <= page_result.width_pt
        assert fragment.bbox.y + fragment.bbox.height <= page_result.height_pt


def test_parse_text_pdf_to_document_ir_emits_paragraphs_tables_and_coordinates(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "batch-record.pdf"
    _write_pdf(
        pdf_path,
        [
            [
                ("Manufacturing summary", (36, 48)),
                ("Batch was inspected before release.", (36, 78)),
                ("Lot\tResult", (36, 118)),
                ("A-001\tPass", (36, 138)),
            ]
        ],
    )

    document_ir = parse_text_pdf_to_document_ir(pdf_path, document_id="sample-pdf")

    assert document_ir["schema_version"] == "document-ir/v0"
    assert document_ir["document"] == {
        "id": "sample-pdf",
        "title": "batch-record.pdf",
        "source_type": "pdf",
    }
    assert document_ir["pages"] == [
        {"page_number": 1, "width": 300.0, "height": 200.0, "unit": "pt"}
    ]

    blocks = document_ir["blocks"]
    assert [(block["id"], block["type"], block["text"]) for block in blocks] == [
        ("block-001", "paragraph", "Manufacturing summary"),
        ("block-002", "paragraph", "Batch was inspected before release."),
        ("block-003", "table", "Lot\tResult\nA-001\tPass"),
    ]
    assert blocks[0]["value_metadata"]["requires_review"] is False
    assert blocks[2]["value_metadata"]["requires_review"] is True
    assert blocks[2]["value_metadata"]["extractor"]["name"] == "pymupdf-text-table-heuristic"

    for block in blocks:
        metadata = block["value_metadata"]
        assert metadata["source_page"] == 1
        assert metadata["confidence"] > 0
        bbox = metadata["bbox"]
        assert bbox["x"] >= 0
        assert bbox["y"] >= 0
        assert bbox["width"] > 0
        assert bbox["height"] > 0
        assert bbox["x"] + bbox["width"] <= document_ir["pages"][0]["width"]
        assert bbox["y"] + bbox["height"] <= document_ir["pages"][0]["height"]

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validate(schema, document_ir)
    validate_document_ir_consistency(document_ir)


def test_parse_text_pdf_to_document_ir_preserves_spaces_between_same_line_fragments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "split-line.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    extraction = PdfTextExtraction(
        source_path=str(pdf_path),
        extractor="pymupdf",
        pages=[
            PdfPageText(
                page_number=1,
                width_pt=300.0,
                height_pt=200.0,
                fragments=[
                    _fragment("Approved", page_number=1, x=36.0, y=40.0, width=52.0),
                    _fragment("By", page_number=1, x=92.0, y=40.0, width=12.0),
                ],
            )
        ],
    )
    monkeypatch.setattr(pdf_text_extraction, "extract_pdf_text", lambda _path: extraction)

    document_ir = parse_text_pdf_to_document_ir(pdf_path)

    assert [block["text"] for block in document_ir["blocks"]] == ["Approved By"]


def test_parse_text_pdf_to_document_ir_does_not_merge_far_same_baseline_fragments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "two-columns.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    extraction = PdfTextExtraction(
        source_path=str(pdf_path),
        extractor="pymupdf",
        pages=[
            PdfPageText(
                page_number=1,
                width_pt=300.0,
                height_pt=200.0,
                fragments=[
                    _fragment("Main header", page_number=1, x=36.0, y=40.0, width=70.0),
                    _fragment("Sidebar note", page_number=1, x=210.0, y=40.0, width=60.0),
                ],
            )
        ],
    )
    monkeypatch.setattr(pdf_text_extraction, "extract_pdf_text", lambda _path: extraction)

    document_ir = parse_text_pdf_to_document_ir(pdf_path)

    assert [(block["type"], block["text"]) for block in document_ir["blocks"]] == [
        ("paragraph", "Main header"),
        ("paragraph", "Sidebar note"),
    ]


def test_parse_text_pdf_to_document_ir_marks_empty_text_for_review(tmp_path: Path) -> None:
    pdf_path = tmp_path / "empty-page.pdf"
    document = fitz.open()
    document.new_page(width=300, height=200)
    document.save(pdf_path)
    document.close()

    document_ir = parse_text_pdf_to_document_ir(pdf_path)

    assert len(document_ir["blocks"]) == 1
    block = document_ir["blocks"][0]
    assert block["id"] == "block-001"
    assert block["type"] == "paragraph"
    assert block["text"] == "PDF text extraction produced no text blocks for this page."
    assert block["value_metadata"]["source_page"] == 1
    assert block["value_metadata"]["bbox"] == {
        "x": 0.0,
        "y": 0.0,
        "width": 300.0,
        "height": 200.0,
    }
    assert block["value_metadata"]["extractor"]["name"] == "pymupdf"
    assert block["value_metadata"]["confidence"] == 0.0
    assert block["value_metadata"]["requires_review"] is True

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validate(schema, document_ir)
    validate_document_ir_consistency(document_ir)


def test_parse_text_pdf_to_document_ir_marks_each_textless_page_for_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "mixed-textless.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    extraction = PdfTextExtraction(
        source_path=str(pdf_path),
        extractor="pymupdf",
        pages=[
            PdfPageText(
                page_number=1,
                width_pt=300.0,
                height_pt=200.0,
                fragments=[
                    _fragment("Readable page", page_number=1, x=36.0, y=40.0, width=78.0),
                ],
            ),
            PdfPageText(
                page_number=2,
                width_pt=300.0,
                height_pt=200.0,
                fragments=[],
            ),
            PdfPageText(
                page_number=3,
                width_pt=300.0,
                height_pt=200.0,
                fragments=[
                    _fragment("Readable again", page_number=3, x=36.0, y=40.0, width=84.0),
                ],
            ),
        ],
    )
    monkeypatch.setattr(pdf_text_extraction, "extract_pdf_text", lambda _path: extraction)

    document_ir = parse_text_pdf_to_document_ir(pdf_path)

    page_texts = [
        (block["text"], block["value_metadata"]["source_page"]) for block in document_ir["blocks"]
    ]
    assert page_texts == [
        ("Readable page", 1),
        ("PDF text extraction produced no text blocks for this page.", 2),
        ("Readable again", 3),
    ]
    textless_block = document_ir["blocks"][1]
    assert textless_block["value_metadata"]["confidence"] == 0.0
    assert textless_block["value_metadata"]["requires_review"] is True


def _fragment(
    text: str,
    *,
    page_number: int,
    x: float,
    y: float,
    width: float,
    height: float = 12.0,
) -> TextFragment:
    return TextFragment(
        text=text,
        page_number=page_number,
        bbox=TextBBox(x=x, y=y, width=width, height=height),
        extractor="pymupdf",
    )
