from __future__ import annotations

import hashlib
from pathlib import Path

from core.parsers.docx_extraction import extract_docx_structure
from core.parsers.xlsx_extraction import extract_xlsx_structure
from core.render.ooxml import render_docx_from_ir, render_xlsx_from_ir


def _sample_ir() -> dict[str, object]:
    return {
        "schema_version": "document-ir/v0",
        "document": {
            "id": "sample-document-001",
            "title": "Synthetic batch record excerpt",
            "source_type": "pdf",
        },
        "pages": [
            {
                "page_number": 1,
                "width": 595.0,
                "height": 842.0,
                "unit": "pt",
            }
        ],
        "blocks": [
            {
                "id": "block-001",
                "type": "heading",
                "text": "Manufacturing Record",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 72.0, "y": 64.0, "width": 240.0, "height": 24.0},
                    "extractor": {"name": "synthetic-fixture", "version": "0.1.0"},
                    "confidence": 0.98,
                    "requires_review": False,
                },
            },
            {
                "id": "block-002",
                "type": "field",
                "text": "Lot Number: SAMPLE-LOT-001",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 72.0, "y": 112.0, "width": 180.0, "height": 18.0},
                    "extractor": {"name": "synthetic-fixture", "version": "0.1.0"},
                    "confidence": 0.86,
                    "requires_review": True,
                },
            },
            {
                "id": "block-003",
                "type": "field",
                "text": "Assay Result: 12.5",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 72.0, "y": 136.0, "width": 160.0, "height": 18.0},
                    "extractor": {"name": "synthetic-fixture", "version": "0.1.0"},
                    "confidence": 0.91,
                    "requires_review": False,
                },
            },
        ],
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_same_ir_renders_deterministic_docx_and_xlsx_with_typed_cells(tmp_path: Path) -> None:
    document_ir = _sample_ir()
    first_docx = tmp_path / "first.docx"
    second_docx = tmp_path / "second.docx"
    first_xlsx = tmp_path / "first.xlsx"
    second_xlsx = tmp_path / "second.xlsx"

    render_docx_from_ir(document_ir, first_docx)
    render_docx_from_ir(document_ir, second_docx)
    render_xlsx_from_ir(document_ir, first_xlsx)
    render_xlsx_from_ir(document_ir, second_xlsx)

    assert first_docx.read_bytes() == second_docx.read_bytes()
    assert first_xlsx.read_bytes() == second_xlsx.read_bytes()
    assert _sha256(first_docx) == _sha256(second_docx)
    assert _sha256(first_xlsx) == _sha256(second_xlsx)

    docx = extract_docx_structure(first_docx)
    assert [(block.kind, block.text) for block in docx.blocks] == [
        ("heading", "Synthetic batch record excerpt"),
        ("heading", "Manufacturing Record"),
        ("paragraph", "Lot Number: SAMPLE-LOT-001"),
        ("paragraph", "Assay Result: 12.5"),
    ]

    xlsx = extract_xlsx_structure(first_xlsx)
    assert len(xlsx.sheets) == 1
    assert xlsx.sheets[0].name == "Document IR"
    typed_cells = {
        cell.ref: (cell.value, cell.value_type)
        for cell in xlsx.sheets[0].cells
        if cell.ref in {
            "A4",
            "B4",
            "C4",
            "D4",
            "A5",
            "B5",
            "C5",
            "D5",
            "A6",
            "B6",
            "C6",
            "D6",
        }
    }
    assert typed_cells == {
        "A4": ("block-001", "inline_string"),
        "B4": ("heading", "inline_string"),
        "C4": ("Manufacturing Record", "inline_string"),
        "D4": ("text", "inline_string"),
        "A5": ("block-002", "inline_string"),
        "B5": ("Lot Number", "inline_string"),
        "C5": ("SAMPLE-LOT-001", "inline_string"),
        "D5": ("text", "inline_string"),
        "A6": ("block-003", "inline_string"),
        "B6": ("Assay Result", "inline_string"),
        "C6": ("12.5", "number"),
        "D6": ("number", "inline_string"),
    }


def test_xlsx_renders_non_field_blocks_as_document_content_rows(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Mixed content"},
        "blocks": [
            {"id": "heading-1", "type": "heading", "text": "Section 1"},
            {"id": "paragraph-1", "type": "paragraph", "text": "Observed value summary"},
            {"id": "list-item-1", "type": "list_item", "text": "First check"},
            {"id": "table-1", "type": "table", "text": "A\tB\n1\t2"},
        ],
    }
    output_path = tmp_path / "mixed.xlsx"

    render_xlsx_from_ir(document_ir, output_path)

    xlsx = extract_xlsx_structure(output_path)
    cells = {(cell.ref, cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert ("A4", "heading-1", "inline_string") in cells
    assert ("B4", "heading", "inline_string") in cells
    assert ("C4", "Section 1", "inline_string") in cells
    assert ("A5", "paragraph-1", "inline_string") in cells
    assert ("B5", "paragraph", "inline_string") in cells
    assert ("C5", "Observed value summary", "inline_string") in cells
    assert ("A6", "list-item-1", "inline_string") in cells
    assert ("B6", "list_item", "inline_string") in cells
    assert ("C6", "First check", "inline_string") in cells
    assert ("A7", "table-1", "inline_string") in cells
    assert ("B7", "table", "inline_string") in cells
    assert ("C7", "A\tB\n1\t2", "inline_string") in cells


def test_renderer_sanitizes_xml_invalid_text_before_writing_ooxml(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Control\fTitle"},
        "blocks": [
            {"id": "block\v1", "type": "paragraph", "text": "Alpha\vBeta"},
            {"id": "block-2", "type": "field", "text": "Code: A\f01"},
        ],
    }
    docx_path = tmp_path / "sanitized.docx"
    xlsx_path = tmp_path / "sanitized.xlsx"

    render_docx_from_ir(document_ir, docx_path)
    render_xlsx_from_ir(document_ir, xlsx_path)

    docx = extract_docx_structure(docx_path)
    xlsx = extract_xlsx_structure(xlsx_path)

    assert [(block.kind, block.text) for block in docx.blocks] == [
        ("heading", "Control Title"),
        ("paragraph", "Alpha Beta"),
        ("paragraph", "Code: A 01"),
    ]
    cells = {cell.ref: cell.value for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == "block 1"
    assert cells["C4"] == "Alpha Beta"
    assert cells["C5"] == "A 01"


def test_xlsx_numeric_detection_preserves_code_like_values_as_text(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Numeric boundaries"},
        "blocks": [
            {"id": "safe-integer", "type": "field", "text": "Safe Integer: 1000"},
            {"id": "safe-decimal", "type": "field", "text": "Safe Decimal: -12.50"},
            {"id": "underscore", "type": "field", "text": "Code: 1_000"},
            {"id": "full-width", "type": "field", "text": "Code: １２３"},
            {"id": "nan-prefix", "type": "field", "text": "Code: NaN123"},
            {"id": "negative-leading-zero", "type": "field", "text": "Code: -01"},
        ],
    }
    output_path = tmp_path / "numeric-boundaries.xlsx"

    render_xlsx_from_ir(document_ir, output_path)

    xlsx = extract_xlsx_structure(output_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["C4"] == (1000, "number")
    assert cells["C5"] == ("-12.50", "number")
    assert cells["C6"] == ("1_000", "inline_string")
    assert cells["C7"] == ("１２３", "inline_string")
    assert cells["C8"] == ("NaN123", "inline_string")
    assert cells["C9"] == ("-01", "inline_string")
