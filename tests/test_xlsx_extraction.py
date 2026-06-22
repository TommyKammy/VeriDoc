from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from core.parsers.xlsx_extraction import extract_xlsx_structure


def _write_xlsx(
    path: Path,
    *,
    shared_strings_xml: str | None = None,
    sheet_xml: str | None = None,
) -> None:
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
            shared_strings_xml
            or """<?xml version="1.0" encoding="UTF-8"?>
<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <si><r><t>Item</t></r><rPh sb="0" eb="1"><t>phonetic</t></rPh></si>
  <si><t>Mass</t></si>
</sst>
""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            sheet_xml
            or """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:C4"/>
  <sheetData>
    <row r="1">
      <c r="A1" t="s"><v>0</v></c>
      <c r="B1" t="s"><v>1</v></c>
      <c r="C1" t="inlineStr"><is><r><t>Status</t></r><rPh sb="0" eb="1"><t>phonetic</t></rPh></is></c>
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
      <c r="C4" t="b"/>
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
        ("B2", "12.5", "number"),
        ("C2", True, "boolean"),
        ("A3", "Formula text", "string"),
        ("B3", "#N/A", "error"),
        ("C3", "2026-06-21T00:00:00Z", "date"),
        ("A4", 12345678901234567890, "number"),
        ("B4", "0.12345678901234567890", "number"),
        ("C4", None, "boolean"),
    ]
    as_dict = result.to_dict()
    assert as_dict["sheets"][0]["cells"][4]["value"] == "12.5"
    assert as_dict["sheets"][0]["cells"][10]["value"] == "0.12345678901234567890"
    json.dumps(as_dict)


def test_extract_xlsx_structure_rejects_negative_shared_string_index(tmp_path: Path) -> None:
    xlsx_path = tmp_path / "negative-shared-string.xlsx"
    _write_xlsx(
        xlsx_path,
        sheet_xml="""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="s"><v>-1</v></c></row>
  </sheetData>
</worksheet>
""",
    )

    with pytest.raises(ValueError, match="shared string index is invalid: -1"):
        extract_xlsx_structure(xlsx_path)


def test_extract_xlsx_structure_preserves_zero_padded_identifier_cells(tmp_path: Path) -> None:
    xlsx_path = tmp_path / "zero-padded-id.xlsx"
    _write_xlsx(
        xlsx_path,
        sheet_xml="""<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:B2"/>
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>Sample ID</t></is></c>
      <c r="B1" t="inlineStr"><is><t>Result</t></is></c>
    </row>
    <row r="2">
      <c r="A2" s="1"><v>123</v></c>
      <c r="B2"><v>12.5</v></c>
    </row>
  </sheetData>
</worksheet>
""",
    )
    with ZipFile(xlsx_path, "a", ZIP_DEFLATED) as archive:
        archive.writestr(
            "xl/styles.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="00000"/></numFmts>
  <cellXfs count="2">
    <xf numFmtId="0"/>
    <xf numFmtId="164" applyNumberFormat="1"/>
  </cellXfs>
</styleSheet>
""",
        )

    result = extract_xlsx_structure(xlsx_path)

    cells = {cell.ref: (cell.value, cell.value_type) for cell in result.sheets[0].cells}
    assert cells["A2"] == ("00123", "string")
    assert cells["B2"] == ("12.5", "number")
