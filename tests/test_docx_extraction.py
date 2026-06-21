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
      <w:r><w:t>document</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Field</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Lot</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>L-001</w:t></w:r></w:p></w:tc>
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
        ("paragraph", "Reviewed document"),
        ("table", "Field\tValue\nLot\tL-001"),
    ]
    assert result.blocks[0].style == "Heading1"
    assert result.blocks[2].rows == [["Field", "Value"], ["Lot", "L-001"]]
