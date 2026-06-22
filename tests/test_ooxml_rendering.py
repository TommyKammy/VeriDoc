from __future__ import annotations

import hashlib
from pathlib import Path
from zipfile import ZipFile

import pytest

from core.llm.conversion_plan import validate_conversion_plan
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
        "C5": (" SAMPLE-LOT-001", "inline_string"),
        "D5": ("text", "inline_string"),
        "A6": ("block-003", "inline_string"),
        "B6": ("Assay Result", "inline_string"),
        "C6": ("12.5", "number"),
        "D6": ("number", "inline_string"),
    }


def test_renderers_apply_plan_table_merges_and_source_annotations(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Planned table render"},
        "blocks": [
            {
                "id": "table-1",
                "type": "table",
                "text": "Batch Summary\t\nLot\tSAMPLE-LOT-001\nAssay\t12.5",
                "rows": [
                    ["Batch Summary", ""],
                    ["Lot", "SAMPLE-LOT-001"],
                    ["Assay", "12.5"],
                ],
                "value_metadata": {
                    "source_page": 2,
                    "bbox": {"x": 10, "y": 20, "width": 200, "height": 48},
                    "confidence": 0.9,
                    "requires_review": False,
                },
            },
            {"id": "paragraph-1", "type": "paragraph", "text": "QA review complete"},
        ],
    }
    conversion_plan = {
        "schema_version": 1,
        "source_kind": "excel_workbook",
        "operations": [
            {
                "id": "extract-summary-table",
                "action": "extract_table",
                "inputs": ["table-1"],
                "output": "table-1",
                "rationale": "Preserve directly extracted table structure.",
            }
        ],
        "constraints": {"external_transmission": False},
    }
    render_plan = {
        "table_merges": [{"block_id": "table-1", "range": "A4:B4"}],
        "source_annotations": [
            {"block_id": "table-1", "text": "Source page 2"},
            {"block_id": "table-1", "text": "Lab worksheet A"},
            {"block_id": "paragraph-1", "text": "QA note"},
        ],
    }
    docx_path = tmp_path / "planned.docx"
    xlsx_path = tmp_path / "planned.xlsx"

    validate_conversion_plan(conversion_plan)
    render_docx_from_ir(
        document_ir,
        docx_path,
        conversion_plan=conversion_plan,
        render_plan=render_plan,
    )
    render_xlsx_from_ir(
        document_ir,
        xlsx_path,
        conversion_plan=conversion_plan,
        render_plan=render_plan,
    )

    xlsx = extract_xlsx_structure(xlsx_path)
    assert xlsx.sheets[0].merged_ranges == ["A4:B4"]
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A5"] == ("Lot", "inline_string")
    assert cells["B5"] == ("SAMPLE-LOT-001", "inline_string")
    assert cells["B6"] == ("12.5", "number")
    assert cells["A7"] == ("paragraph-1", "inline_string")

    with ZipFile(docx_path) as docx_archive:
        docx_names = set(docx_archive.namelist())
        document_relationships = docx_archive.read("word/_rels/document.xml.rels").decode("utf-8")
        comments_xml = docx_archive.read("word/comments.xml").decode("utf-8")
    assert "word/comments.xml" in docx_names
    assert "relationships/comments" in document_relationships
    assert "Source page 2" in comments_xml
    assert "Lab worksheet A" in comments_xml
    assert "QA note" in comments_xml

    with ZipFile(xlsx_path) as xlsx_archive:
        xlsx_names = set(xlsx_archive.namelist())
        content_types = xlsx_archive.read("[Content_Types].xml").decode("utf-8")
        sheet_xml = xlsx_archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
        sheet_relationships = xlsx_archive.read("xl/worksheets/_rels/sheet1.xml.rels").decode("utf-8")
        comments_xml = xlsx_archive.read("xl/comments1.xml").decode("utf-8")
        vml_xml = xlsx_archive.read("xl/drawings/vmlDrawing1.vml").decode("utf-8")
    assert "xl/comments1.xml" in xlsx_names
    assert "xl/drawings/vmlDrawing1.vml" in xlsx_names
    assert 'Default Extension="vml"' in content_types
    assert '<mergeCell ref="A4:B4"/>' in sheet_xml
    assert '<legacyDrawing r:id="rId2"/>' in sheet_xml
    assert "relationships/comments" in sheet_relationships
    assert "relationships/vmlDrawing" in sheet_relationships
    assert 'Target="../drawings/vmlDrawing1.vml"' in sheet_relationships
    assert "Source page 2" in comments_xml
    assert "Lab worksheet A" in comments_xml
    assert "QA note" in comments_xml
    assert vml_xml.count('<x:ClientData ObjectType="Note">') == 2
    assert '<x:ClientData ObjectType="Note">' in vml_xml
    assert "<x:Row>3</x:Row>" in vml_xml
    assert "<x:Row>6</x:Row>" in vml_xml
    assert "<x:Column>0</x:Column>" in vml_xml


def test_xlsx_offsets_table_merges_to_rendered_table_start_row(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Offset table render"},
        "blocks": [
            {"id": "summary-1", "type": "paragraph", "text": "Summary before table"},
            {
                "id": "table-1",
                "type": "table",
                "text": "Batch Summary\t\nLot\tSAMPLE-LOT-001",
                "rows": [["Batch Summary", ""], ["Lot", "SAMPLE-LOT-001"]],
            },
        ],
    }
    output_path = tmp_path / "offset-table.xlsx"

    render_xlsx_from_ir(
        document_ir,
        output_path,
        render_plan={"table_merges": [{"block_id": "table-1", "range": "A4:B4"}]},
    )

    xlsx = extract_xlsx_structure(output_path)
    assert xlsx.sheets[0].merged_ranges == ["A5:B5"]
    cells = {cell.ref: cell.value for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == "summary-1"
    assert cells["A5"] == "Batch Summary"


def test_renderers_reject_table_merges_for_non_table_blocks(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Invalid merge target"},
        "blocks": [
            {"id": "paragraph-1", "type": "paragraph", "text": "Not a table"},
        ],
    }
    docx_output = tmp_path / "invalid-merge.docx"
    xlsx_output = tmp_path / "invalid-merge.xlsx"

    with pytest.raises(ValueError, match="must reference a table block"):
        render_docx_from_ir(
            document_ir,
            docx_output,
            render_plan={"table_merges": [{"block_id": "paragraph-1", "range": "A4:B4"}]},
        )
    assert not docx_output.exists()

    with pytest.raises(ValueError, match="must reference a table block"):
        render_xlsx_from_ir(
            document_ir,
            xlsx_output,
            render_plan={"table_merges": [{"block_id": "paragraph-1", "range": "A4:B4"}]},
        )
    assert not xlsx_output.exists()


@pytest.mark.parametrize("merge_range", ["A1:B1", "A4:B6", "A4:C4", "B4:A4"])
def test_renderers_reject_table_merges_outside_their_table_grid(
    tmp_path: Path,
    merge_range: str,
) -> None:
    document_ir = {
        "document": {"title": "Bounded merge target"},
        "blocks": [
            {
                "id": "table-1",
                "type": "table",
                "rows": [["Header", ""], ["Lot", "SAMPLE-LOT-001"]],
            },
            {"id": "paragraph-1", "type": "paragraph", "text": "After table"},
        ],
    }
    output_path = tmp_path / f"invalid-merge-{merge_range.replace(':', '-')}.xlsx"
    docx_output_path = tmp_path / f"invalid-merge-{merge_range.replace(':', '-')}.docx"

    with pytest.raises(ValueError, match="range must stay within the table grid"):
        render_docx_from_ir(
            document_ir,
            docx_output_path,
            render_plan={"table_merges": [{"block_id": "table-1", "range": merge_range}]},
        )
    assert not docx_output_path.exists()

    with pytest.raises(ValueError, match="range must stay within the table grid"):
        render_xlsx_from_ir(
            document_ir,
            output_path,
            render_plan={"table_merges": [{"block_id": "table-1", "range": merge_range}]},
        )
    assert not output_path.exists()


def test_renderers_reject_render_directives_inside_conversion_plan(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Invalid render location"},
        "blocks": [
            {"id": "table-1", "type": "table", "text": "A\tB"},
        ],
    }
    conversion_plan = {
        "schema_version": 1,
        "source_kind": "excel_workbook",
        "operations": [
            {
                "id": "extract-table",
                "action": "extract_table",
                "inputs": ["table-1"],
                "output": "table-1",
                "rationale": "Preserve directly extracted table structure.",
            }
        ],
        "constraints": {"external_transmission": False},
        "render": {"table_merges": [{"block_id": "table-1", "range": "A4:B4"}]},
    }

    with pytest.raises(ValueError, match="conversion_plan\\.render is not supported"):
        render_docx_from_ir(document_ir, tmp_path / "invalid-render.docx", conversion_plan=conversion_plan)
    with pytest.raises(ValueError, match="conversion_plan\\.render is not supported"):
        render_xlsx_from_ir(document_ir, tmp_path / "invalid-render.xlsx", conversion_plan=conversion_plan)


def test_xlsx_renders_non_field_blocks_as_document_content_rows(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Mixed content"},
        "blocks": [
            {"id": "heading-1", "type": "heading", "text": "001"},
            {"id": "paragraph-1", "type": "paragraph", "text": "12.5"},
            {"id": "list-item-1", "type": "list_item", "text": "-01"},
            {"id": "table-1", "type": "table", "text": "A\tB\n1\t2"},
        ],
    }
    output_path = tmp_path / "mixed.xlsx"

    render_xlsx_from_ir(document_ir, output_path)

    xlsx = extract_xlsx_structure(output_path)
    cells = {(cell.ref, cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert ("A4", "heading-1", "inline_string") in cells
    assert ("B4", "heading", "inline_string") in cells
    assert ("C4", "001", "inline_string") in cells
    assert ("D4", "text", "inline_string") in cells
    assert ("A5", "paragraph-1", "inline_string") in cells
    assert ("B5", "paragraph", "inline_string") in cells
    assert ("C5", "12.5", "inline_string") in cells
    assert ("D5", "text", "inline_string") in cells
    assert ("A6", "list-item-1", "inline_string") in cells
    assert ("B6", "list_item", "inline_string") in cells
    assert ("C6", "-01", "inline_string") in cells
    assert ("D6", "text", "inline_string") in cells
    assert ("A7", "table-1", "inline_string") in cells
    assert ("B7", "table", "inline_string") in cells
    assert ("C7", "A\tB\n1\t2", "inline_string") in cells
    assert ("D7", "text", "inline_string") in cells


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
    assert cells["C5"] == " A 01"


def test_docx_renders_table_blocks_as_tables(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Tables"},
        "blocks": [
            {"id": "table-1", "type": "table", "text": "Header\tValue\nCode\t001"},
        ],
    }
    output_path = tmp_path / "table.docx"

    render_docx_from_ir(document_ir, output_path)

    docx = extract_docx_structure(output_path)
    assert [(block.kind, block.text, block.rows) for block in docx.blocks] == [
        ("heading", "Tables", None),
        ("table", "Header\tValue\nCode\t001", [["Header", "Value"], ["Code", "001"]]),
    ]


def test_docx_preserves_boundary_spaces_in_text_runs(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "  Boundary title  "},
        "blocks": [
            {"id": "paragraph-1", "type": "paragraph", "text": " leading paragraph "},
            {"id": "table-1", "type": "table", "text": " Left \tRight \n  Code\t001 "},
        ],
    }
    output_path = tmp_path / "boundary-spaces.docx"

    render_docx_from_ir(document_ir, output_path)

    with ZipFile(output_path) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert '<w:t xml:space="preserve">  Boundary title  </w:t>' in document_xml
    assert '<w:t xml:space="preserve"> leading paragraph </w:t>' in document_xml
    assert '<w:t xml:space="preserve"> Left </w:t>' in document_xml
    assert '<w:t xml:space="preserve">Right </w:t>' in document_xml

    docx = extract_docx_structure(output_path)
    assert [(block.kind, block.text, block.rows) for block in docx.blocks] == [
        ("heading", "  Boundary title  ", None),
        ("paragraph", " leading paragraph ", None),
        ("table", " Left \tRight \n  Code\t001 ", [[" Left ", "Right "], ["  Code", "001 "]]),
    ]


def test_docx_sanitizes_table_text_before_splitting_rows(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Table controls"},
        "blocks": [
            {"id": "table-1", "type": "table", "text": "Head\fA\tValue\nA\vB"},
        ],
    }
    output_path = tmp_path / "table-controls.docx"

    render_docx_from_ir(document_ir, output_path)

    docx = extract_docx_structure(output_path)
    assert [(block.kind, block.text, block.rows) for block in docx.blocks] == [
        ("heading", "Table controls", None),
        ("table", "Head A\tValue\nA B", [["Head A", "Value"], ["A B"]]),
    ]


def test_docx_table_splits_only_on_ir_row_delimiters(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Table row delimiters"},
        "blocks": [
            {"id": "table-1", "type": "table", "text": "A\u2028B\tC\u0085D\nE\tF"},
        ],
    }
    output_path = tmp_path / "table-row-delimiters.docx"

    render_docx_from_ir(document_ir, output_path)

    docx = extract_docx_structure(output_path)
    assert [(block.kind, block.text, block.rows) for block in docx.blocks] == [
        (
            "heading",
            "Table row delimiters",
            None,
        ),
        (
            "table",
            "A\u2028B\tC\u0085D\nE\tF",
            [["A\u2028B", "C\u0085D"], ["E", "F"]],
        ),
    ]


def test_docx_table_normalizes_crlf_row_delimiters(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Table CRLF delimiters"},
        "blocks": [
            {"id": "table-1", "type": "table", "text": "A\tB\r\nC\tD\rE\tF"},
        ],
    }
    output_path = tmp_path / "table-crlf-delimiters.docx"

    render_docx_from_ir(document_ir, output_path)

    docx = extract_docx_structure(output_path)
    assert [(block.kind, block.text, block.rows) for block in docx.blocks] == [
        ("heading", "Table CRLF delimiters", None),
        ("table", "A\tB\nC\tD\nE\tF", [["A", "B"], ["C", "D"], ["E", "F"]]),
    ]


def test_docx_renders_list_items_with_list_semantics(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Checklist"},
        "blocks": [
            {"id": "step-1", "type": "list_item", "text": "Verify batch number"},
        ],
    }
    output_path = tmp_path / "list-item.docx"

    render_docx_from_ir(document_ir, output_path)

    with ZipFile(output_path) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert "<w:numPr>" in document_xml

    docx = extract_docx_structure(output_path)
    assert [(block.kind, block.text, block.rows) for block in docx.blocks] == [
        ("heading", "Checklist", None),
        ("list_item", "Verify batch number", None),
    ]


def test_docx_package_defines_heading_style(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Styled title"},
        "blocks": [
            {"id": "heading-1", "type": "heading", "text": "Styled heading"},
        ],
    }
    output_path = tmp_path / "styled-heading.docx"

    render_docx_from_ir(document_ir, output_path)

    with ZipFile(output_path) as archive:
        names = set(archive.namelist())
        content_types = archive.read("[Content_Types].xml").decode("utf-8")
        relationships = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        styles_xml = archive.read("word/styles.xml").decode("utf-8")

    assert "word/styles.xml" in names
    assert (
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    ) in content_types
    assert (
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"'
    ) in relationships
    assert '<w:style w:type="paragraph" w:styleId="Heading1">' in styles_xml
    assert '<w:name w:val="heading 1"/>' in styles_xml


def test_ooxml_renderers_reject_unsupported_block_types(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Unsupported"},
        "blocks": [
            {"id": "unknown-1", "type": "unknown", "text": "Should not render"},
        ],
    }

    with pytest.raises(ValueError, match="unsupported document_ir\\.blocks type: 'unknown'"):
        render_docx_from_ir(document_ir, tmp_path / "unsupported.docx")
    with pytest.raises(ValueError, match="unsupported document_ir\\.blocks type: 'unknown'"):
        render_xlsx_from_ir(document_ir, tmp_path / "unsupported.xlsx")


def test_docx_encodes_tabs_and_line_breaks_as_run_elements(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Run elements"},
        "blocks": [
            {"id": "paragraph-1", "type": "paragraph", "text": "Alpha\tBeta\nGamma\r\nDelta"},
        ],
    }
    output_path = tmp_path / "runs.docx"

    render_docx_from_ir(document_ir, output_path)

    with ZipFile(output_path) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert "<w:tab/>" in document_xml
    assert document_xml.count("<w:br/>") == 2
    assert "Alpha\tBeta" not in document_xml
    assert "Beta\nGamma" not in document_xml

    docx = extract_docx_structure(output_path)
    assert docx.blocks[1].text == "Alpha\tBeta\nGamma\nDelta"


def test_renderer_escapes_carriage_returns_before_xml_normalization(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Carriage\rTitle"},
        "blocks": [
            {"id": "block\r1", "type": "field", "text": "Code: A\r01"},
        ],
    }
    output_path = tmp_path / "carriage.xlsx"

    render_xlsx_from_ir(document_ir, output_path)

    with ZipFile(output_path) as archive:
        sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert "Carriage&#13;Title" in sheet_xml
    assert "block&#13;1" in sheet_xml
    assert "A&#13;01" in sheet_xml

    xlsx = extract_xlsx_structure(output_path)
    cells = {cell.ref: cell.value for cell in xlsx.sheets[0].cells}
    assert cells["A1"] == "Carriage\rTitle"
    assert cells["A4"] == "block\r1"
    assert cells["C4"] == " A\r01"


def test_xlsx_field_values_preserve_boundary_spaces(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Field boundary spaces"},
        "blocks": [
            {"id": "code", "type": "field", "text": "Code: A "},
        ],
    }
    output_path = tmp_path / "field-boundary-spaces.xlsx"

    render_xlsx_from_ir(document_ir, output_path)

    with ZipFile(output_path) as archive:
        sheet_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
    assert '<t xml:space="preserve"> A </t>' in sheet_xml

    xlsx = extract_xlsx_structure(output_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["B4"] == ("Code", "inline_string")
    assert cells["C4"] == (" A ", "inline_string")


def test_ooxml_zip_entries_use_stable_host_system(tmp_path: Path) -> None:
    document_ir = _sample_ir()
    docx_path = tmp_path / "stable.docx"
    xlsx_path = tmp_path / "stable.xlsx"

    render_docx_from_ir(document_ir, docx_path)
    render_xlsx_from_ir(document_ir, xlsx_path)

    for package_path in (docx_path, xlsx_path):
        with ZipFile(package_path) as archive:
            assert {info.create_system for info in archive.infolist()} == {0}


def test_xlsx_numeric_detection_preserves_code_like_values_as_text(tmp_path: Path) -> None:
    document_ir = {
        "document": {"title": "Numeric boundaries"},
        "blocks": [
            {"id": "safe-integer", "type": "field", "text": "Safe Integer: 1000"},
            {"id": "max-precision", "type": "field", "text": "Max Precision: 123456789012345"},
            {
                "id": "oversized-identifier",
                "type": "field",
                "text": "Identifier: 1234567890123456",
            },
            {"id": "safe-decimal", "type": "field", "text": "Safe Decimal: -12.50"},
            {
                "id": "high-precision-decimal",
                "type": "field",
                "text": "High Precision: 0.1234567890123456",
            },
            {
                "id": "small-decimal",
                "type": "field",
                "text": "Concentration: 0.000000000000001",
            },
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
    assert cells["C5"] == (123456789012345, "number")
    assert cells["C6"] == (" 1234567890123456", "inline_string")
    assert cells["C7"] == ("-12.50", "number")
    assert cells["C8"] == (" 0.1234567890123456", "inline_string")
    assert cells["C9"] == ("0.000000000000001", "number")
    assert cells["C10"] == (" 1_000", "inline_string")
    assert cells["C11"] == (" １２３", "inline_string")
    assert cells["C12"] == (" NaN123", "inline_string")
    assert cells["C13"] == (" -01", "inline_string")
