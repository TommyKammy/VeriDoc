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
        if cell.ref in {"A4", "B4", "C4", "D4", "A5", "B5", "C5", "D5"}
    }
    assert typed_cells == {
        "A4": ("block-002", "inline_string"),
        "B4": ("Lot Number", "inline_string"),
        "C4": ("SAMPLE-LOT-001", "inline_string"),
        "D4": ("text", "inline_string"),
        "A5": ("block-003", "inline_string"),
        "B5": ("Assay Result", "inline_string"),
        "C5": ("12.5", "number"),
        "D5": ("number", "inline_string"),
    }
