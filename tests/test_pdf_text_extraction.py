from __future__ import annotations

from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

from core.parsers.pdf_text_extraction import extract_pdf_text


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
