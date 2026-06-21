from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zipfile import ZIP_STORED, ZipFile, ZipInfo


FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def render_docx_from_ir(document_ir: Mapping[str, Any], output_path: str | Path) -> None:
    """Render a minimal deterministic DOCX package from Document IR v0."""
    document = _mapping(document_ir.get("document"), "document")
    title = _text(document.get("title"))
    blocks = _blocks(document_ir)
    body_parts = [_docx_paragraph(title, style="Heading1")]
    body_parts.extend(_docx_block(block) for block in blocks)
    body_parts.append("<w:sectPr/>")

    parts = [
        (
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
""",
        ),
        (
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
""",
        ),
        (
            "word/document.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    {"".join(body_parts)}
  </w:body>
</w:document>
""",
        ),
    ]
    _write_zip(output_path, parts)


def render_xlsx_from_ir(document_ir: Mapping[str, Any], output_path: str | Path) -> None:
    """Render a minimal deterministic XLSX package from Document IR v0."""
    document = _mapping(document_ir.get("document"), "document")
    title = _text(document.get("title"))
    rows = [
        [_text_cell("A1", title)],
        [],
        [
            _text_cell("A3", "Block ID"),
            _text_cell("B3", "Label"),
            _text_cell("C3", "Value"),
            _text_cell("D3", "Value type"),
        ],
    ]
    current_row = 4
    for block in _blocks(document_ir):
        if _text(block.get("type")) != "field":
            continue
        label, value = _split_field(_text(block.get("text")))
        rendered_value, value_type = _typed_xlsx_value(value)
        rows.append(
            [
                _text_cell(f"A{current_row}", _text(block.get("id"))),
                _text_cell(f"B{current_row}", label),
                rendered_value(f"C{current_row}"),
                _text_cell(f"D{current_row}", value_type),
            ]
        )
        current_row += 1

    dimension = f"A1:D{max(current_row - 1, 3)}"
    sheet_rows = "\n".join(_xlsx_row(index + 1, cells) for index, cells in enumerate(rows) if cells)
    parts = [
        (
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>
""",
        ),
        (
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
""",
        ),
        (
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Document IR" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
""",
        ),
        (
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
""",
        ),
        (
            "xl/worksheets/sheet1.xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="{dimension}"/>
  <sheetData>
{sheet_rows}
  </sheetData>
</worksheet>
""",
        ),
    ]
    _write_zip(output_path, parts)


def _blocks(document_ir: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    blocks = document_ir.get("blocks")
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes)):
        raise ValueError("document_ir.blocks must be a list")
    for block in blocks:
        if not isinstance(block, Mapping):
            raise ValueError("document_ir.blocks entries must be objects")
    return blocks


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"document_ir.{label} must be an object")
    return value


def _docx_block(block: Mapping[str, Any]) -> str:
    kind = _text(block.get("type"))
    style = "Heading1" if kind == "heading" else None
    return _docx_paragraph(_text(block.get("text")), style=style)


def _docx_paragraph(text: str, *, style: str | None = None) -> str:
    style_xml = "" if style is None else f'<w:pPr><w:pStyle w:val="{escape(style)}"/></w:pPr>'
    return f"<w:p>{style_xml}<w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def _split_field(text: str) -> tuple[str, str]:
    label, separator, value = text.partition(":")
    if not separator:
        return "", text
    return label.strip(), value.strip()


def _typed_xlsx_value(value: str) -> tuple[Any, str]:
    if _is_plain_number(value):
        return (lambda ref: _number_cell(ref, value)), "number"
    return (lambda ref: _text_cell(ref, value)), "text"


def _is_plain_number(value: str) -> bool:
    if not value or value.strip() != value:
        return False
    if value.startswith("+"):
        return False
    if len(value) > 1 and value.startswith("0") and not value.startswith("0."):
        return False
    try:
        Decimal(value)
    except InvalidOperation:
        return False
    return any(character.isdigit() for character in value)


def _xlsx_row(row_index: int, cells: Sequence[str]) -> str:
    return f'    <row r="{row_index}">{"".join(cells)}</row>'


def _text_cell(ref: str, value: str) -> str:
    return f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'


def _number_cell(ref: str, value: str) -> str:
    return f'<c r="{ref}"><v>{escape(value)}</v></c>'


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _write_zip(output_path: str | Path, parts: Iterable[tuple[str, str]]) -> None:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", ZIP_STORED) as archive:
        for name, content in parts:
            info = ZipInfo(filename=name, date_time=FIXED_ZIP_TIMESTAMP)
            info.compress_type = ZIP_STORED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content.encode("utf-8"))
