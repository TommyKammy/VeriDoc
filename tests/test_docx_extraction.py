from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from core.parsers.docx_extraction import extract_docx_structure


def _write_docx(path: Path, document_xml: str) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
""",
        )
        archive.writestr("word/document.xml", document_xml)


def test_extract_docx_structure_returns_headings_paragraphs_and_tables(tmp_path: Path) -> None:
    docx_path = tmp_path / "sample.docx"
    _write_docx(
        docx_path,
        """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Batch Summary</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t>Reviewed </w:t></w:r>
      <w:r><w:tab/></w:r>
      <w:r><w:t>document</w:t></w:r>
      <w:r><w:br/></w:r>
      <w:r><w:t>Line two</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Field</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Lot</w:t><w:tab/><w:t>ID</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>L-001</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc>
          <w:p><w:r><w:t>First paragraph</w:t></w:r></w:p>
          <w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p>
        </w:tc>
        <w:tc><w:p/></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p/></w:tc>
        <w:tc><w:p/></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
""",
    )

    result = extract_docx_structure(docx_path)

    assert result.source_path == str(docx_path)
    assert [(block.kind, block.text) for block in result.blocks] == [
        ("heading", "Batch Summary"),
        ("paragraph", "Reviewed \tdocument\nLine two"),
        ("table", "Field\tValue\nLot\tID\tL-001\nFirst paragraph\nSecond paragraph\t\n\t"),
    ]
    assert result.blocks[0].style == "Heading1"
    assert result.blocks[2].rows == [
        ["Field", "Value"],
        ["Lot\tID", "L-001"],
        ["First paragraph\nSecond paragraph", ""],
        ["", ""],
    ]


def test_extract_docx_structure_ignores_removed_numbering(tmp_path: Path) -> None:
    docx_path = tmp_path / "removed-numbering.docx"
    _write_docx(
        docx_path,
        """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:numPr><w:numId w:val="0"/></w:numPr></w:pPr>
      <w:r><w:t>Plain paragraph</w:t></w:r>
    </w:p>
    <w:p>
      <w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr>
      <w:r><w:t>Numbered paragraph</w:t></w:r>
    </w:p>
  </w:body>
</w:document>
""",
    )

    result = extract_docx_structure(docx_path)

    assert [(block.kind, block.text) for block in result.blocks] == [
        ("paragraph", "Plain paragraph"),
        ("list_item", "Numbered paragraph"),
    ]


def test_extract_docx_structure_flags_merged_table_cells(tmp_path: Path) -> None:
    docx_path = tmp_path / "merged-table.docx"
    _write_docx(
        docx_path,
        """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:tcPr><w:gridSpan w:val="2"/></w:tcPr>
          <w:p><w:r><w:t>Merged Header</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Lot</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>0007</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
""",
    )

    result = extract_docx_structure(docx_path)

    assert result.blocks[0].requires_review is True
    assert result.blocks[0].warnings == [
        "DOCX table contains merged cells; xlsx artifact requires review"
    ]


def test_extract_docx_structure_does_not_flag_grid_span_one_as_merge(tmp_path: Path) -> None:
    docx_path = tmp_path / "unmerged-grid-span.docx"
    _write_docx(
        docx_path,
        """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:tcPr><w:gridSpan w:val="1"/></w:tcPr>
          <w:p><w:r><w:t>Lot</w:t></w:r></w:p>
        </w:tc>
        <w:tc><w:p><w:r><w:t>Result</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>0007</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Pass</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
""",
    )

    result = extract_docx_structure(docx_path)

    assert result.blocks[0].requires_review is False
    assert not result.blocks[0].warnings
