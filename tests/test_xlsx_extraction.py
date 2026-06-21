from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from core.parsers.xlsx_extraction import extract_xlsx_structure


def _write_xlsx(path: Path) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>
""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Results" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
""",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <si><t>Item</t></si>
  <si><t>Mass</t></si>
</sst>
""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:C4"/>
  <sheetData>
    <row r="1">
      <c r="A1" t="s"><v>0</v></c>
      <c r="B1" t="s"><v>1</v></c>
      <c r="C1" t="inlineStr"><is><t>Status</t></is></c>
    </row>
    <row r="2">
      <c r="A2" t="inlineStr"><is><t>Sample A</t></is></c>
      <c r="B2"><v>12.5</v></c>
      <c r="C2" t="b"><v>1</v></c>
    </row>
    <row r="3">
      <c r="A3" t="str"><v>Formula text</v></c>
      <c r="B3" t="e"><v>#N/A</v></c>
      <c r="C3" t="d"><v>2026-06-21T00:00:00Z</v></c>
    </row>
    <row r="4">
      <c r="A4"><v>12345678901234567890</v></c>
      <c r="B4"><v>0.12345678901234567890</v></c>
    </row>
  </sheetData>
  <mergeCells count="1"><mergeCell ref="A3:C3"/></mergeCells>
</worksheet>
""",
        )


def test_extract_xlsx_structure_returns_cell_types_and_merged_ranges(tmp_path: Path) -> None:
    xlsx_path = tmp_path / "sample.xlsx"
    _write_xlsx(xlsx_path)

    result = extract_xlsx_structure(xlsx_path)

    assert result.source_path == str(xlsx_path)
    assert len(result.sheets) == 1
    sheet = result.sheets[0]
    assert sheet.name == "Results"
    assert sheet.dimension == "A1:C4"
    assert sheet.merged_ranges == ["A3:C3"]
    assert [(cell.ref, cell.value, cell.value_type) for cell in sheet.cells] == [
        ("A1", "Item", "shared_string"),
        ("B1", "Mass", "shared_string"),
        ("C1", "Status", "inline_string"),
        ("A2", "Sample A", "inline_string"),
        ("B2", Decimal("12.5"), "number"),
        ("C2", True, "boolean"),
        ("A3", "Formula text", "string"),
        ("B3", "#N/A", "error"),
        ("C3", "2026-06-21T00:00:00Z", "date"),
        ("A4", 12345678901234567890, "number"),
        ("B4", Decimal("0.12345678901234567890"), "number"),
    ]
