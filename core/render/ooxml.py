from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zipfile import ZIP_STORED, ZipFile, ZipInfo


FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
MAX_EXACT_SPREADSHEET_DIGITS = 15
ASCII_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
XLSX_RANGE_RE = re.compile(r"[A-Z]+[1-9][0-9]*:[A-Z]+[1-9][0-9]*\Z")
XLSX_CELL_RE = re.compile(r"([A-Z]+)([1-9][0-9]*)\Z")
SUPPORTED_BLOCK_TYPES = {"field", "heading", "list_item", "paragraph", "table"}
EDITABLE_PDF_BLOCK_TYPES = SUPPORTED_BLOCK_TYPES | {"footnote"}


def render_docx_from_ir(
    document_ir: Mapping[str, Any],
    output_path: str | Path,
    *,
    conversion_plan: Mapping[str, Any] | None = None,
    render_plan: Mapping[str, Any] | None = None,
) -> None:
    """Render a minimal deterministic DOCX package from Document IR v0."""
    document = _mapping(document_ir.get("document"), "document")
    title = _text(document.get("title"))
    blocks = _blocks(document_ir)
    render_directives = _render_directives(conversion_plan, render_plan)
    source_annotations = _source_annotations_by_block(render_directives, blocks)
    _table_merges_by_block(render_directives, blocks)
    comment_ids = {
        block_id: index
        for index, block_id in enumerate(
            block_id for block_id in _block_ids(blocks) if block_id in source_annotations
        )
    }
    body_parts = [_docx_paragraph(title, style="Heading1")]
    body_parts.extend(
        _docx_block(block, comment_id=comment_ids.get(_text(block.get("id"))))
        for block in blocks
    )
    body_parts.append("<w:sectPr/>")

    comment_content_type = ""
    comment_relationship = ""
    comment_parts: list[tuple[str, str]] = []
    if comment_ids:
        comment_content_type = (
            '  <Override PartName="/word/comments.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>\n'
        )
        comment_relationship = (
            '  <Relationship Id="rId3" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" '
            'Target="comments.xml"/>\n'
        )
        comment_parts.append(
            (
                "word/comments.xml",
                _docx_comments_xml(
                    [
                        (comment_ids[block_id], _joined_annotation_text(texts))
                        for block_id, texts in source_annotations.items()
                        if block_id in comment_ids
                    ]
                ),
            )
        )

    parts = [
        (
            "[Content_Types].xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
{comment_content_type.rstrip()}
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
            "word/_rels/document.xml.rels",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
{comment_relationship.rstrip()}
</Relationships>
""",
        ),
        (
            "word/styles.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
  </w:style>
</w:styles>
""",
        ),
        (
            "word/numbering.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/>
      <w:numFmt w:val="bullet"/>
      <w:lvlText w:val="&#8226;"/>
    </w:lvl>
  </w:abstractNum>
  <w:num w:numId="1">
    <w:abstractNumId w:val="0"/>
  </w:num>
</w:numbering>
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
    ] + comment_parts
    _write_zip(output_path, parts)


def render_editable_docx_from_pdf_ir(
    document_ir: Mapping[str, Any],
    output_path: str | Path,
) -> None:
    """Reconstruct PDF-derived Document IR v1 as editable DOCX structure."""
    _validate_editable_pdf_ir(document_ir)
    document = _mapping(document_ir.get("document"), "document")
    title = _text(document.get("title"))
    blocks = _editable_pdf_blocks(document_ir)
    body_blocks = [block for block in blocks if _text(block.get("type")) != "footnote"]
    footnote_blocks = [block for block in blocks if _text(block.get("type")) == "footnote"]
    comment_ids = {_text(block.get("id")): index for index, block in enumerate(blocks)}
    footnote_ids = {_text(block.get("id")): index for index, block in enumerate(footnote_blocks, start=1)}

    body_parts = [_docx_paragraph(title, style="Heading1")]
    body_parts.extend(
        _docx_block(block, comment_id=comment_ids.get(_text(block.get("id"))))
        for block in body_blocks
    )
    body_parts.extend(
        _docx_footnote_reference_paragraph(
            footnote_ids[_text(block.get("id"))],
            comment_id=comment_ids.get(_text(block.get("id"))),
        )
        for block in footnote_blocks
    )
    body_parts.append("<w:sectPr/>")

    comments = [
        (comment_ids[_text(block.get("id"))], _editable_pdf_block_comment(block))
        for block in blocks
    ]
    parts = [
        (
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
  <Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>
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
            "word/_rels/document.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>
</Relationships>
""",
        ),
        ("word/styles.xml", _docx_styles_xml()),
        ("word/numbering.xml", _docx_numbering_xml()),
        (
            "word/comments.xml",
            _docx_comments_xml(comments),
        ),
        (
            "word/footnotes.xml",
            _docx_footnotes_xml(
                [
                    (footnote_ids[_text(block.get("id"))], _text(block.get("text")))
                    for block in footnote_blocks
                ]
            ),
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


def render_xlsx_from_ir(
    document_ir: Mapping[str, Any],
    output_path: str | Path,
    *,
    conversion_plan: Mapping[str, Any] | None = None,
    render_plan: Mapping[str, Any] | None = None,
) -> None:
    """Render a minimal deterministic XLSX package from Document IR v0."""
    document = _mapping(document_ir.get("document"), "document")
    title = _text(document.get("title"))
    blocks = _blocks(document_ir)
    render_directives = _render_directives(conversion_plan, render_plan)
    source_annotations = _source_annotations_by_block(render_directives, blocks)
    table_merges = _table_merges_by_block(render_directives, blocks)
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
    max_column_count = 4
    comment_refs: dict[str, str] = {}
    merge_ranges: list[str] = []
    for block in blocks:
        block_id = _text(block.get("id"))
        kind = _text(block.get("type"))
        text = _text(block.get("text"))
        if kind == "table" and _should_render_xlsx_table_grid(block, table_merges):
            if block_id in source_annotations:
                comment_refs[block_id] = f"A{current_row}"
            table_start_row = current_row
            table_rows = _docx_table_rows(block)
            for row_offset, table_row in enumerate(table_rows):
                row_index = current_row + row_offset
                rows.append(
                    [
                        _typed_xlsx_cell(_cell_ref(column_index + 1, row_index), cell)
                        for column_index, cell in enumerate(table_row)
                    ]
                )
                max_column_count = max(max_column_count, len(table_row))
            current_row += len(table_rows)
            merge_ranges.extend(
                _offset_xlsx_range(merge_range, table_start_row - 4)
                for merge_range in table_merges.get(block_id, [])
            )
            continue
        if kind == "field":
            label, value = _split_field(text)
            rendered_value, value_type = _typed_xlsx_value(value)
            value_cell = rendered_value(f"C{current_row}")
        else:
            label, value = kind, text
            value_cell = _text_cell(f"C{current_row}", value)
            value_type = "text"
        if block_id in source_annotations:
            comment_refs[block_id] = f"A{current_row}"
        rows.append(
            [
                _text_cell(f"A{current_row}", _text(block.get("id"))),
                _text_cell(f"B{current_row}", label),
                value_cell,
                _text_cell(f"D{current_row}", value_type),
            ]
        )
        current_row += 1

    max_column_count = max(max_column_count, _max_column_from_ranges(merge_ranges))
    dimension = f"A1:{_cell_ref(max_column_count, max(current_row - 1, 3))}"
    sheet_rows = "\n".join(_xlsx_row(index + 1, cells) for index, cells in enumerate(rows) if cells)
    merge_xml = _xlsx_merge_cells_xml(merge_ranges)
    vml_content_type = ""
    comment_content_type = ""
    xlsx_comment_parts: list[tuple[str, str]] = []
    legacy_drawing_xml = ""
    sheet_relationships: tuple[str, str] | None = None
    if comment_refs:
        xlsx_comment_entries = [
            (comment_refs[block_id], _joined_annotation_text(texts))
            for block_id, texts in source_annotations.items()
            if block_id in comment_refs
        ]
        vml_content_type = (
            '  <Default Extension="vml" '
            'ContentType="application/vnd.openxmlformats-officedocument.vmlDrawing"/>\n'
            '  <Override PartName="/xl/drawings/vmlDrawing1.vml" '
            'ContentType="application/vnd.openxmlformats-officedocument.vmlDrawing"/>\n'
        )
        comment_content_type = (
            '  <Override PartName="/xl/comments1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml"/>\n'
        )
        legacy_drawing_xml = '  <legacyDrawing r:id="rId2"/>\n'
        xlsx_comment_parts.append(
            (
                "xl/comments1.xml",
                _xlsx_comments_xml(xlsx_comment_entries),
            )
        )
        sheet_relationships = (
            "xl/worksheets/_rels/sheet1.xml.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="../comments1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/vmlDrawing" Target="../drawings/vmlDrawing1.vml"/>
</Relationships>
""",
        )
        xlsx_comment_parts.append(
            (
                "xl/drawings/vmlDrawing1.vml",
                _xlsx_vml_drawing_xml([ref for ref, _text in xlsx_comment_entries]),
            )
        )
    parts = [
        (
            "[Content_Types].xml",
            f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
{vml_content_type.rstrip()}
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
{comment_content_type.rstrip()}
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
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="{dimension}"/>
  <sheetData>
{sheet_rows}
  </sheetData>
{merge_xml}
{legacy_drawing_xml.rstrip()}
</worksheet>
""",
        ),
    ]
    if sheet_relationships is not None:
        parts.append(sheet_relationships)
    parts.extend(xlsx_comment_parts)
    _write_zip(output_path, parts)


def _blocks(document_ir: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    blocks = document_ir.get("blocks")
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes)):
        raise ValueError("document_ir.blocks must be a list")
    for block in blocks:
        if not isinstance(block, Mapping):
            raise ValueError("document_ir.blocks entries must be objects")
        block_type = _text(block.get("type"))
        if block_type not in SUPPORTED_BLOCK_TYPES:
            raise ValueError(f"unsupported document_ir.blocks type: {block_type!r}")
    _unique_block_ids(blocks)
    return blocks


def _validate_editable_pdf_ir(document_ir: Mapping[str, Any]) -> None:
    if _text(document_ir.get("schema_version")) != "document-ir/v1":
        raise ValueError("document_ir.schema_version must be document-ir/v1")
    document = _mapping(document_ir.get("document"), "document")
    if _text(document.get("source_type")) != "pdf":
        raise ValueError("document_ir.document.source_type must be pdf")
    _editable_pdf_blocks(document_ir)


def _editable_pdf_blocks(document_ir: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    blocks = document_ir.get("blocks")
    if not isinstance(blocks, Sequence) or isinstance(blocks, (str, bytes)):
        raise ValueError("document_ir.blocks must be a list")
    for block in blocks:
        if not isinstance(block, Mapping):
            raise ValueError("document_ir.blocks entries must be objects")
        block_type = _text(block.get("type"))
        if block_type not in EDITABLE_PDF_BLOCK_TYPES:
            raise ValueError(f"unsupported document_ir.blocks type: {block_type!r}")
    _unique_block_ids(blocks)
    return blocks


def _block_ids(blocks: Sequence[Mapping[str, Any]]) -> list[str]:
    return [_text(block.get("id")) for block in blocks]


def _unique_block_ids(blocks: Sequence[Mapping[str, Any]]) -> set[str]:
    block_ids = _block_ids(blocks)
    unique_ids = set(block_ids)
    if len(block_ids) != len(unique_ids):
        raise ValueError("document_ir.blocks ids must be unique")
    return unique_ids


def _render_directives(
    conversion_plan: Mapping[str, Any] | None,
    render_plan: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    if conversion_plan is not None and "render" in conversion_plan:
        raise ValueError("conversion_plan.render is not supported; pass render_plan instead")
    if render_plan is None:
        return {}
    if not isinstance(render_plan, Mapping):
        raise ValueError("render_plan must be an object")
    allowed_keys = {"source_annotations", "table_merges"}
    unsupported_keys = sorted(str(key) for key in render_plan if key not in allowed_keys)
    if unsupported_keys:
        raise ValueError(f"render_plan contains unsupported directives: {unsupported_keys!r}")
    return render_plan


def _source_annotations_by_block(
    render: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
) -> dict[str, list[str]]:
    annotations = render.get("source_annotations", [])
    if not isinstance(annotations, Sequence) or isinstance(annotations, (str, bytes)):
        raise ValueError("render_plan.source_annotations must be a list")
    valid_block_ids = _unique_block_ids(blocks)
    by_block: dict[str, list[str]] = {}
    for index, annotation in enumerate(annotations):
        if not isinstance(annotation, Mapping):
            raise ValueError(f"render_plan.source_annotations[{index}] must be an object")
        block_id = annotation.get("block_id")
        text = annotation.get("text")
        if not isinstance(block_id, str) or block_id not in valid_block_ids:
            raise ValueError(f"render_plan.source_annotations[{index}].block_id must reference an IR block")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"render_plan.source_annotations[{index}].text is required")
        by_block.setdefault(block_id, []).append(text)
    return by_block


def _table_merges_by_block(
    render: Mapping[str, Any],
    blocks: Sequence[Mapping[str, Any]],
) -> dict[str, list[str]]:
    merges = render.get("table_merges", [])
    if not isinstance(merges, Sequence) or isinstance(merges, (str, bytes)):
        raise ValueError("render_plan.table_merges must be a list")
    _unique_block_ids(blocks)
    table_blocks = _table_blocks_by_id(blocks)
    by_block: dict[str, list[str]] = {}
    for index, merge in enumerate(merges):
        if not isinstance(merge, Mapping):
            raise ValueError(f"render_plan.table_merges[{index}] must be an object")
        block_id = merge.get("block_id")
        merge_range = merge.get("range")
        if not isinstance(block_id, str) or block_id not in table_blocks:
            raise ValueError(f"render_plan.table_merges[{index}].block_id must reference a table block")
        if not isinstance(merge_range, str) or not XLSX_RANGE_RE.fullmatch(merge_range):
            raise ValueError(f"render_plan.table_merges[{index}].range is invalid")
        block_merges = by_block.setdefault(block_id, [])
        _validate_xlsx_table_merge(merge_range, table_blocks[block_id], block_merges, index)
        block_merges.append(merge_range)
    return by_block


def _table_blocks_by_id(blocks: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {
        _text(block.get("id")): block
        for block in blocks
        if _text(block.get("type")) == "table"
    }


def _should_render_xlsx_table_grid(
    block: Mapping[str, Any],
    table_merges: Mapping[str, Sequence[str]],
) -> bool:
    block_id = _text(block.get("id"))
    return bool(table_merges.get(block_id))


def _joined_annotation_text(texts: Sequence[str]) -> str:
    return "\n".join(texts)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"document_ir.{label} must be an object")
    return value


def _docx_block(block: Mapping[str, Any], *, comment_id: int | None = None) -> str:
    kind = _text(block.get("type"))
    if kind == "table":
        return _docx_table(_docx_table_rows(block), comment_id=comment_id)
    if kind == "list_item":
        return _docx_paragraph(_text(block.get("text")), numbering=True, comment_id=comment_id)
    if kind in {"field", "paragraph"}:
        return _docx_paragraph(_text(block.get("text")), comment_id=comment_id)
    if kind == "heading":
        return _docx_paragraph(_text(block.get("text")), style="Heading1", comment_id=comment_id)
    raise ValueError(f"unsupported document_ir.blocks type: {kind!r}")


def _docx_footnote_reference_paragraph(
    footnote_id: int,
    *,
    comment_id: int | None = None,
) -> str:
    comment_start = "" if comment_id is None else f'<w:commentRangeStart w:id="{comment_id}"/>'
    comment_end = (
        ""
        if comment_id is None
        else (
            f'<w:commentRangeEnd w:id="{comment_id}"/>'
            f'<w:r><w:commentReference w:id="{comment_id}"/></w:r>'
        )
    )
    return (
        "<w:p>"
        f"{comment_start}"
        f'<w:r><w:footnoteReference w:id="{footnote_id}"/></w:r>'
        f"{comment_end}"
        "</w:p>"
    )


def _docx_paragraph(
    text: str,
    *,
    style: str | None = None,
    numbering: bool = False,
    comment_id: int | None = None,
) -> str:
    paragraph_properties = []
    if style is not None:
        paragraph_properties.append(f'<w:pStyle w:val="{_xml_escape(style)}"/>')
    if numbering:
        paragraph_properties.append('<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>')
    properties_xml = (
        ""
        if not paragraph_properties
        else f"<w:pPr>{''.join(paragraph_properties)}</w:pPr>"
    )
    comment_start = "" if comment_id is None else f'<w:commentRangeStart w:id="{comment_id}"/>'
    comment_end = (
        ""
        if comment_id is None
        else (
            f'<w:commentRangeEnd w:id="{comment_id}"/>'
            f'<w:r><w:commentReference w:id="{comment_id}"/></w:r>'
        )
    )
    return f"<w:p>{properties_xml}{comment_start}{_docx_runs(text)}{comment_end}</w:p>"


def _docx_runs(text: str) -> str:
    runs: list[str] = []
    index = 0
    text_start = 0
    while index < len(text):
        character = text[index]
        if character in {"\t", "\n", "\r"}:
            if text_start < index:
                runs.append(_docx_text_run(text[text_start:index]))
            if character == "\t":
                runs.append("<w:r><w:tab/></w:r>")
            else:
                runs.append("<w:r><w:br/></w:r>")
                if character == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                    index += 1
            text_start = index + 1
        index += 1
    if text_start < len(text):
        runs.append(_docx_text_run(text[text_start:]))
    return "".join(runs)


def _docx_text_run(text: str) -> str:
    space_attr = ' xml:space="preserve"' if _needs_xml_space_preserve(text) else ""
    return f"<w:r><w:t{space_attr}>{_xml_escape(text)}</w:t></w:r>"


def _docx_table(rows: Sequence[Sequence[str]], *, comment_id: int | None = None) -> str:
    row_parts = []
    for row_index, row in enumerate(rows):
        cell_parts = []
        for cell_index, cell in enumerate(row):
            cell_comment_id = comment_id if row_index == 0 and cell_index == 0 else None
            cell_parts.append(f"<w:tc>{_docx_paragraph(cell, comment_id=cell_comment_id)}</w:tc>")
        row_parts.append(f"<w:tr>{''.join(cell_parts)}</w:tr>")
    row_xml = "".join(row_parts)
    return f"<w:tbl>{row_xml}</w:tbl>"


def _docx_table_rows(block: Mapping[str, Any]) -> Sequence[Sequence[str]]:
    rows = block.get("rows")
    if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
        normalized_rows = [
            [_text(cell) for cell in row]
            for row in rows
            if isinstance(row, Sequence) and not isinstance(row, (str, bytes))
        ]
        if normalized_rows:
            return normalized_rows
    sanitized_text = _normalize_table_row_delimiters(_sanitize_xml_text(_text(block.get("text"))))
    if not sanitized_text:
        return [[""]]
    return [line.split("\t") for line in sanitized_text.split("\n")]


def _editable_pdf_block_comment(block: Mapping[str, Any]) -> str:
    block_id = _text(block.get("id"))
    block_type = _text(block.get("type"))
    review = _mapping_or_empty(block.get("review"))
    warnings_value = review.get("warnings", [])
    warnings = (
        [_text(warning) for warning in warnings_value]
        if isinstance(warnings_value, Sequence) and not isinstance(warnings_value, (str, bytes))
        else []
    )
    parts = [
        f"block_id={block_id}",
        f"type={block_type}",
        f"source_page={_text(block.get('source_page'))}",
        f"bbox={_editable_pdf_bbox_text(block.get('bbox'))}",
        f"extractor={_editable_pdf_extractor_text(block.get('extractor'))}",
        f"confidence={_text(block.get('confidence'))}",
        f"requires_review={str(review.get('requires_review') is True).lower()}",
    ]
    if warnings:
        parts.append(f"warnings={'; '.join(warnings)}")
    return "\n".join(parts)


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _editable_pdf_bbox_text(value: Any) -> str:
    bbox = _mapping_or_empty(value)
    unit = _text(bbox.get("unit") or "pt")
    coordinates = [
        _text(bbox.get("x")),
        _text(bbox.get("y")),
        _text(bbox.get("width")),
        _text(bbox.get("height")),
    ]
    return f"{','.join(coordinates)} {unit}"


def _editable_pdf_extractor_text(value: Any) -> str:
    extractor = _mapping_or_empty(value)
    name = _text(extractor.get("name") or value)
    version = _text(extractor.get("version") or "unknown")
    return f"{name}@{version}"


def _split_field(text: str) -> tuple[str, str]:
    label, separator, value = text.partition(":")
    if not separator:
        return "", text
    return label.strip(), value


def _typed_xlsx_value(value: str) -> tuple[Any, str]:
    numeric_candidate = value.strip()
    if _is_plain_number(numeric_candidate):
        return (lambda ref: _number_cell(ref, numeric_candidate)), "number"
    return (lambda ref: _text_cell(ref, value)), "text"


def _typed_xlsx_cell(ref: str, value: str) -> str:
    rendered_value, _value_type = _typed_xlsx_value(value)
    return rendered_value(ref)


def _is_plain_number(value: str) -> bool:
    if not ASCII_NUMBER_RE.fullmatch(value):
        return False
    if _exceeds_spreadsheet_numeric_precision(value):
        return False
    try:
        numeric_value = Decimal(value)
    except InvalidOperation:
        return False
    return numeric_value.is_finite()


def _exceeds_spreadsheet_numeric_precision(value: str) -> bool:
    return _spreadsheet_significant_digit_count(value) > MAX_EXACT_SPREADSHEET_DIGITS


def _spreadsheet_significant_digit_count(value: str) -> int:
    integer_part, _, fractional_part = value.removeprefix("-").partition(".")
    significant_digits = f"{integer_part}{fractional_part}".lstrip("0")
    return len(significant_digits) if significant_digits else 1


def _xlsx_row(row_index: int, cells: Sequence[str]) -> str:
    return f'    <row r="{row_index}">{"".join(cells)}</row>'


def _text_cell(ref: str, value: str) -> str:
    space_attr = ' xml:space="preserve"' if _needs_xml_space_preserve(value) else ""
    return f'<c r="{ref}" t="inlineStr"><is><t{space_attr}>{_xml_escape(value)}</t></is></c>'


def _number_cell(ref: str, value: str) -> str:
    return f'<c r="{ref}"><v>{_xml_escape(value)}</v></c>'


def _cell_ref(column_index: int, row_index: int) -> str:
    if column_index < 1 or row_index < 1:
        raise ValueError("spreadsheet cell indexes are 1-based")
    letters = []
    current = column_index
    while current:
        current, remainder = divmod(current - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return f"{''.join(reversed(letters))}{row_index}"


def _max_column_from_ranges(ranges: Sequence[str]) -> int:
    max_column = 1
    for merge_range in ranges:
        _start, _separator, end_ref = merge_range.partition(":")
        match = XLSX_CELL_RE.fullmatch(end_ref)
        if match is None:
            continue
        max_column = max(max_column, _column_index(match.group(1)))
    return max_column


def _column_index(column_name: str) -> int:
    column_index = 0
    for character in column_name:
        column_index = column_index * 26 + (ord(character) - ord("A") + 1)
    return column_index


def _validate_xlsx_table_merge(
    merge_range: str,
    block: Mapping[str, Any],
    existing_ranges: Sequence[str],
    directive_index: int,
) -> None:
    _validate_xlsx_table_merge_range(merge_range, block, directive_index)
    _validate_xlsx_table_merge_does_not_overlap(merge_range, existing_ranges, directive_index)
    _validate_xlsx_table_merge_preserves_values(merge_range, block, directive_index)


def _validate_xlsx_table_merge_range(
    merge_range: str,
    block: Mapping[str, Any],
    directive_index: int,
) -> None:
    start_column, start_row, end_column, end_row = _xlsx_range_coordinates(merge_range)
    table_rows = _docx_table_rows(block)
    table_row_count = len(table_rows)
    table_column_count = max((len(row) for row in table_rows), default=1)
    first_table_row = 4
    last_table_row = first_table_row + table_row_count - 1
    if (
        start_column < 1
        or end_column > table_column_count
        or start_column > end_column
        or start_row < first_table_row
        or end_row > last_table_row
        or start_row > end_row
    ):
        raise ValueError(
            f"render_plan.table_merges[{directive_index}].range must stay within the table grid"
        )


def _validate_xlsx_table_merge_does_not_overlap(
    merge_range: str,
    existing_ranges: Sequence[str],
    directive_index: int,
) -> None:
    start_column, start_row, end_column, end_row = _xlsx_range_coordinates(merge_range)
    for existing_range in existing_ranges:
        (
            existing_start_column,
            existing_start_row,
            existing_end_column,
            existing_end_row,
        ) = _xlsx_range_coordinates(existing_range)
        if (
            max(start_column, existing_start_column) <= min(end_column, existing_end_column)
            and max(start_row, existing_start_row) <= min(end_row, existing_end_row)
        ):
            raise ValueError(
                f"render_plan.table_merges[{directive_index}].range must not overlap another table merge"
            )


def _validate_xlsx_table_merge_preserves_values(
    merge_range: str,
    block: Mapping[str, Any],
    directive_index: int,
) -> None:
    start_column, start_row, end_column, end_row = _xlsx_range_coordinates(merge_range)
    table_rows = _docx_table_rows(block)
    for row_index in range(start_row, end_row + 1):
        table_row_index = row_index - 4
        row = table_rows[table_row_index]
        for column_index in range(start_column, end_column + 1):
            if row_index == start_row and column_index == start_column:
                continue
            table_column_index = column_index - 1
            value = row[table_column_index] if table_column_index < len(row) else ""
            if _text(value):
                raise ValueError(
                    f"render_plan.table_merges[{directive_index}].range must not cover populated non-anchor cells"
                )


def _xlsx_range_coordinates(merge_range: str) -> tuple[int, int, int, int]:
    start_ref, _separator, end_ref = merge_range.partition(":")
    start_column, start_row = _xlsx_cell_coordinates(start_ref)
    end_column, end_row = _xlsx_cell_coordinates(end_ref)
    return start_column, start_row, end_column, end_row


def _xlsx_cell_coordinates(ref: str) -> tuple[int, int]:
    match = XLSX_CELL_RE.fullmatch(ref)
    if match is None:
        raise ValueError(f"render_plan.table_merges range is invalid: {ref!r}")
    return _column_index(match.group(1)), int(match.group(2))


def _offset_xlsx_range(merge_range: str, row_delta: int) -> str:
    start_ref, _separator, end_ref = merge_range.partition(":")
    return f"{_offset_xlsx_cell(start_ref, row_delta)}:{_offset_xlsx_cell(end_ref, row_delta)}"


def _offset_xlsx_cell(ref: str, row_delta: int) -> str:
    match = XLSX_CELL_RE.fullmatch(ref)
    if match is None:
        raise ValueError(f"render_plan.table_merges range is invalid: {ref!r}")
    row_index = int(match.group(2)) + row_delta
    if row_index < 1:
        raise ValueError("render_plan.table_merges range resolves outside the worksheet")
    return f"{match.group(1)}{row_index}"


def _xlsx_merge_cells_xml(ranges: Sequence[str]) -> str:
    if not ranges:
        return ""
    merge_cells = "".join(f'    <mergeCell ref="{_xml_escape(merge_range)}"/>' for merge_range in ranges)
    return f'  <mergeCells count="{len(ranges)}">{merge_cells}</mergeCells>'


def _docx_styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal">
    <w:name w:val="Normal"/>
  </w:style>
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
    <w:basedOn w:val="Normal"/>
    <w:next w:val="Normal"/>
    <w:qFormat/>
  </w:style>
</w:styles>
"""


def _docx_numbering_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0">
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/>
      <w:numFmt w:val="bullet"/>
      <w:lvlText w:val="&#8226;"/>
    </w:lvl>
  </w:abstractNum>
  <w:num w:numId="1">
    <w:abstractNumId w:val="0"/>
  </w:num>
</w:numbering>
"""


def _docx_comments_xml(comments: Sequence[tuple[int, str]]) -> str:
    comment_xml = "".join(
        f'<w:comment w:id="{comment_id}" w:author="VeriDoc" w:initials="VD">'
        f'<w:p>{_docx_runs(text)}</w:p>'
        f"</w:comment>"
        for comment_id, text in comments
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  {comment_xml}
</w:comments>
"""


def _docx_footnotes_xml(footnotes: Sequence[tuple[int, str]]) -> str:
    footnote_xml = "".join(
        f'<w:footnote w:id="{footnote_id}"><w:p>{_docx_runs(text)}</w:p></w:footnote>'
        for footnote_id, text in footnotes
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>
  <w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>
  {footnote_xml}
</w:footnotes>
"""


def _xlsx_comments_xml(comments: Sequence[tuple[str, str]]) -> str:
    comment_xml = "".join(
        f'<comment ref="{_xml_escape(ref)}" authorId="0">'
        f"<text><r><t>{_xml_escape(text)}</t></r></text>"
        f"</comment>"
        for ref, text in comments
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<comments xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <authors><author>VeriDoc</author></authors>
  <commentList>{comment_xml}</commentList>
</comments>
"""


def _xlsx_vml_drawing_xml(comment_refs: Sequence[str]) -> str:
    shapes = "".join(
        _xlsx_vml_comment_shape(ref, shape_index)
        for shape_index, ref in enumerate(comment_refs, start=1025)
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<xml xmlns:v="urn:schemas-microsoft-com:vml"
  xmlns:o="urn:schemas-microsoft-com:office:office"
  xmlns:x="urn:schemas-microsoft-com:office:excel">
  <o:shapelayout v:ext="edit"><o:idmap v:ext="edit" data="1"/></o:shapelayout>
  <v:shapetype id="_x0000_t202" coordsize="21600,21600" o:spt="202" path="m,l,21600r21600,l21600,xe">
    <v:stroke joinstyle="miter"/>
    <v:path gradientshapeok="t" o:connecttype="rect"/>
  </v:shapetype>
  {shapes}
</xml>
"""


def _xlsx_vml_comment_shape(ref: str, shape_index: int) -> str:
    column_index, row_index = _xlsx_cell_coordinates(ref)
    anchor_start_column = column_index
    anchor_start_row = row_index - 1
    anchor_end_column = anchor_start_column + 2
    anchor_end_row = anchor_start_row + 4
    anchor = (
        f"{anchor_start_column}, 15, {anchor_start_row}, 2, "
        f"{anchor_end_column}, 15, {anchor_end_row}, 4"
    )
    return (
        f'<v:shape id="_x0000_s{shape_index}" type="#_x0000_t202" '
        'style="position:absolute;margin-left:80pt;margin-top:5pt;width:108pt;'
        'height:59.25pt;z-index:1;visibility:hidden" fillcolor="#ffffe1" '
        'o:insetmode="auto">'
        '<v:fill color2="#ffffe1"/>'
        '<v:shadow on="t" color="black" obscured="t"/>'
        '<v:path o:connecttype="none"/>'
        '<v:textbox style="mso-direction-alt:auto"/>'
        '<x:ClientData ObjectType="Note">'
        '<x:MoveWithCells/>'
        '<x:SizeWithCells/>'
        f"<x:Anchor>{anchor}</x:Anchor>"
        '<x:AutoFill>False</x:AutoFill>'
        f"<x:Row>{row_index - 1}</x:Row>"
        f"<x:Column>{column_index - 1}</x:Column>"
        "</x:ClientData>"
        "</v:shape>"
    )


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _xml_escape(value: str) -> str:
    return "".join(
        "&#13;" if character == "\r" else escape(character)
        for character in _sanitize_xml_text(value)
    )


def _sanitize_xml_text(value: str) -> str:
    return "".join(character if _is_xml_char(character) else " " for character in value)


def _needs_xml_space_preserve(value: str) -> bool:
    sanitized_value = _sanitize_xml_text(value)
    return bool(sanitized_value) and (
        sanitized_value[0].isspace() or sanitized_value[-1].isspace()
    )


def _normalize_table_row_delimiters(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _is_xml_char(character: str) -> bool:
    codepoint = ord(character)
    return (
        codepoint in {0x09, 0x0A, 0x0D}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def _write_zip(output_path: str | Path, parts: Iterable[tuple[str, str]]) -> None:
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", ZIP_STORED) as archive:
        for name, content in parts:
            info = ZipInfo(filename=name, date_time=FIXED_ZIP_TIMESTAMP)
            info.create_system = 0
            info.compress_type = ZIP_STORED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content.encode("utf-8"))
