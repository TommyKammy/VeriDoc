from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


class _ElementIdParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name == "id" and value is not None:
                self.ids.add(value)


def _web_html() -> str:
    return Path("apps/web/index.html").read_text(encoding="utf-8")


def test_pdf_preview_surface_and_bbox_controls_are_present() -> None:
    parser = _ElementIdParser()
    parser.feed(_web_html())

    assert {
        "pdf-preview-panel",
        "preview-page-select",
        "pdf-page-surface",
        "pdf-source-frame",
        "bbox-layer",
        "bbox-invalid",
    }.issubset(parser.ids)


def test_bbox_overlay_guard_rejects_missing_or_invalid_source_coordinates() -> None:
    html = _web_html()

    assert "function validBbox(bbox, page)" in html
    assert 'bbox.origin !== "top-left"' in html
    assert "bbox.unit !== page.unit" in html
    assert "bbox.width > 0" in html
    assert "bbox.x + bbox.width <= page.width" in html
    assert "Skipped invalid bbox" in html


def test_review_item_can_jump_to_preview_bbox() -> None:
    html = _web_html()

    assert "function jumpToReviewItem(item)" in html
    assert "Jump to bbox" in html
    assert "state.previewPage = item.source_page" in html
