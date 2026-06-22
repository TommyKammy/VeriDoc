from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import PurePosixPath, Path
import re
from typing import Any, Dict, List, Optional, Union
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OFFICE_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


@dataclass(frozen=True)
class XlsxCell:
    ref: str
    value: Any
    value_type: str


@dataclass(frozen=True)
class XlsxSheet:
    name: str
    dimension: Optional[str]
    cells: List[XlsxCell]
    merged_ranges: List[str]


@dataclass(frozen=True)
class XlsxStructure:
    source_path: str
    sheets: List[XlsxSheet]

    def to_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))


@dataclass(frozen=True)
class _ColumnStyle:
    min_column: int
    max_column: int
    style_index: int


def extract_xlsx_structure(xlsx_path: Union[str, Path]) -> XlsxStructure:
    """Extract sheet, cell type, cell value, and merged-cell structure from XLSX."""
    source = Path(xlsx_path)
    if not source.is_file():
        raise FileNotFoundError(f"XLSX file not found: {source}")

    with _open_package(source) as archive:
        shared_strings = _read_shared_strings(archive)
        style_formats = _read_style_formats(archive)
        sheet_parts = _read_workbook_sheet_parts(archive)
        sheets = [
            _read_sheet(
                archive,
                name=name,
                part_name=part_name,
                shared_strings=shared_strings,
                style_formats=style_formats,
            )
            for name, part_name in sheet_parts
        ]
    return XlsxStructure(source_path=str(source), sheets=sheets)


def _open_package(source: Path) -> ZipFile:
    try:
        return ZipFile(source)
    except BadZipFile as exc:
        raise ValueError(f"failed to read XLSX package: {source}") from exc


def _read_xml(archive: ZipFile, part_name: str, label: str) -> ElementTree.Element:
    try:
        with archive.open(part_name) as part:
            return ElementTree.parse(part).getroot()
    except KeyError as exc:
        raise ValueError(f"missing {label}: {part_name}") from exc
    except ElementTree.ParseError as exc:
        raise ValueError(f"failed to parse {label}: {part_name}") from exc


def _read_shared_strings(archive: ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _read_xml(archive, "xl/sharedStrings.xml", "shared strings")
    return [_joined_text(item) for item in root.findall(f"{SHEET_NS}si")]


def _read_style_formats(archive: ZipFile) -> List[Optional[str]]:
    if "xl/styles.xml" not in archive.namelist():
        return []
    root = _read_xml(archive, "xl/styles.xml", "styles")
    custom_formats = {
        num_format.attrib["numFmtId"]: num_format.attrib["formatCode"]
        for num_format in root.findall(f"{SHEET_NS}numFmts/{SHEET_NS}numFmt")
        if "numFmtId" in num_format.attrib and "formatCode" in num_format.attrib
    }
    style_formats: List[Optional[str]] = []
    for cell_format in root.findall(f"{SHEET_NS}cellXfs/{SHEET_NS}xf"):
        if _is_false(cell_format.attrib.get("applyNumberFormat")):
            style_formats.append(None)
            continue
        style_formats.append(custom_formats.get(cell_format.attrib.get("numFmtId", "")))
    return style_formats


def _read_workbook_sheet_parts(archive: ZipFile) -> List[tuple[str, str]]:
    workbook = _read_xml(archive, "xl/workbook.xml", "workbook")
    relationships = _read_relationships(archive)
    sheet_parts: List[tuple[str, str]] = []

    for sheet in workbook.findall(f"{SHEET_NS}sheets/{SHEET_NS}sheet"):
        name = sheet.attrib.get("name", "")
        rel_id = sheet.attrib.get(f"{OFFICE_REL_NS}id")
        if rel_id is None or rel_id not in relationships:
            raise ValueError(f"worksheet relationship is missing for sheet: {name}")
        sheet_parts.append((name, _resolve_workbook_target(relationships[rel_id])))

    return sheet_parts


def _read_relationships(archive: ZipFile) -> Dict[str, str]:
    root = _read_xml(archive, "xl/_rels/workbook.xml.rels", "workbook relationships")
    return {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in root.findall(f"{REL_NS}Relationship")
        if "Id" in rel.attrib and "Target" in rel.attrib
    }


def _resolve_workbook_target(target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return str(PurePosixPath("xl") / target)


def _read_sheet(
    archive: ZipFile,
    *,
    name: str,
    part_name: str,
    shared_strings: List[str],
    style_formats: List[Optional[str]],
) -> XlsxSheet:
    root = _read_xml(archive, part_name, f"worksheet {name}")
    dimension = None
    dimension_element = root.find(f"{SHEET_NS}dimension")
    if dimension_element is not None:
        dimension = dimension_element.attrib.get("ref")

    column_styles = _read_column_styles(root)
    cells = [
        _cell_from_xml(cell, shared_strings, style_formats, column_styles)
        for row in root.findall(f"{SHEET_NS}sheetData/{SHEET_NS}row")
        for cell in row.findall(f"{SHEET_NS}c")
    ]
    merged_ranges = [
        merge.attrib["ref"]
        for merge in root.findall(f"{SHEET_NS}mergeCells/{SHEET_NS}mergeCell")
        if "ref" in merge.attrib
    ]
    return XlsxSheet(name=name, dimension=dimension, cells=cells, merged_ranges=merged_ranges)


def _cell_from_xml(
    cell: ElementTree.Element,
    shared_strings: List[str],
    style_formats: List[Optional[str]],
    column_styles: List[_ColumnStyle],
) -> XlsxCell:
    ref = cell.attrib.get("r", "")
    raw_type = cell.attrib.get("t")
    value_text = _cell_value_text(cell)

    if raw_type == "s":
        value = _shared_string_value(value_text, shared_strings)
        value_type = "shared_string"
    elif raw_type == "inlineStr":
        value = _joined_text(cell.find(f"{SHEET_NS}is"))
        value_type = "inline_string"
    elif raw_type == "b":
        value = None if value_text == "" else value_text == "1"
        value_type = "boolean"
    elif raw_type == "str":
        value = value_text
        value_type = "string"
    elif raw_type is None or raw_type == "n":
        formatted_identifier = _formatted_identifier_value(
            cell,
            value_text,
            style_formats,
            column_styles,
        )
        if formatted_identifier is not None:
            value = formatted_identifier
            value_type = "string"
        else:
            value = _number_value(value_text)
            value_type = "number" if value_text else "blank"
    elif raw_type == "d":
        value = value_text
        value_type = "date"
    elif raw_type == "e":
        value = value_text
        value_type = "error"
    else:
        value = value_text
        value_type = f"unknown:{raw_type}"

    return XlsxCell(ref=ref, value=value, value_type=value_type)


def _formatted_identifier_value(
    cell: ElementTree.Element,
    value_text: str,
    style_formats: List[Optional[str]],
    column_styles: List[_ColumnStyle],
) -> Optional[str]:
    if value_text == "" or not re.fullmatch(r"-?\d+", value_text):
        return None
    format_code = _cell_format_code(cell, style_formats, column_styles)
    if format_code is None:
        return None
    zero_format = _zero_padding_format(format_code, int(value_text))
    if zero_format is None:
        return None
    width, show_negative_sign = zero_format
    digits = value_text[1:] if value_text.startswith("-") else value_text
    padded = digits.zfill(width)
    if value_text.startswith("-") and show_negative_sign:
        return "-" + padded
    return padded


def _cell_format_code(
    cell: ElementTree.Element,
    style_formats: List[Optional[str]],
    column_styles: List[_ColumnStyle],
) -> Optional[str]:
    style_index_text = cell.attrib.get("s")
    style_index = _style_index(style_index_text)
    if style_index is None:
        style_index = _column_style_index(cell.attrib.get("r", ""), column_styles)
    if style_index is None:
        return None
    if style_index < 0 or style_index >= len(style_formats):
        return None
    return style_formats[style_index]


def _read_column_styles(root: ElementTree.Element) -> List[_ColumnStyle]:
    column_styles: List[_ColumnStyle] = []
    for column in root.findall(f"{SHEET_NS}cols/{SHEET_NS}col"):
        min_column = _positive_int(column.attrib.get("min"))
        max_column = _positive_int(column.attrib.get("max"))
        style_index = _style_index(column.attrib.get("style"))
        if min_column is None or max_column is None or style_index is None:
            continue
        if min_column > max_column:
            continue
        column_styles.append(
            _ColumnStyle(
                min_column=min_column,
                max_column=max_column,
                style_index=style_index,
            )
        )
    return column_styles


def _column_style_index(ref: str, column_styles: List[_ColumnStyle]) -> Optional[int]:
    column_number = _column_number(ref)
    if column_number is None:
        return None
    for column_style in column_styles:
        if column_style.min_column <= column_number <= column_style.max_column:
            return column_style.style_index
    return None


def _column_number(ref: str) -> Optional[int]:
    match = re.match(r"\$?([A-Za-z]+)", ref)
    if match is None:
        return None
    column_number = 0
    for char in match.group(1).upper():
        column_number = column_number * 26 + ord(char) - ord("A") + 1
    return column_number


def _style_index(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _positive_int(value: Optional[str]) -> Optional[int]:
    parsed = _style_index(value)
    if parsed is None or parsed < 1:
        return None
    return parsed


def _zero_padding_format(format_code: str, value: int) -> Optional[tuple[int, bool]]:
    sections = _format_sections(format_code)
    if not sections:
        return None
    uses_negative_section = False
    if value > 0:
        section = sections[0]
    elif value < 0 and len(sections) > 1:
        uses_negative_section = True
        section = sections[1]
    elif value == 0 and len(sections) > 2:
        section = sections[2]
    else:
        section = sections[0]
    normalized = re.sub(r"\[[^\]]+\]", "", section)
    match = re.fullmatch(r"(-?)(0+)", normalized)
    if match is None:
        return None
    show_negative_sign = value < 0 and (not uses_negative_section or bool(match.group(1)))
    return len(match.group(2)), show_negative_sign


def _format_sections(format_code: str) -> List[str]:
    sections: List[str] = []
    current: List[str] = []
    in_quote = False
    for char in format_code:
        if char == '"':
            in_quote = not in_quote
            current.append(char)
        elif char == ";" and not in_quote:
            sections.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    sections.append("".join(current).strip())
    return sections


def _is_false(value: Optional[str]) -> bool:
    return value is not None and value.lower() in {"0", "false"}


def _cell_value_text(cell: ElementTree.Element) -> str:
    value = cell.find(f"{SHEET_NS}v")
    return "" if value is None or value.text is None else value.text


def _shared_string_value(value_text: str, shared_strings: List[str]) -> str:
    try:
        index = int(value_text)
    except (IndexError, ValueError) as exc:
        raise ValueError(f"shared string index is invalid: {value_text}") from exc
    if index < 0 or index >= len(shared_strings):
        raise ValueError(f"shared string index is invalid: {value_text}")
    return shared_strings[index]


def _number_value(value_text: str) -> Any:
    if value_text == "":
        return None
    try:
        return int(value_text)
    except ValueError:
        return value_text


def _joined_text(element: Optional[ElementTree.Element]) -> str:
    if element is None:
        return ""
    chunks: List[str] = []

    def collect(node: ElementTree.Element) -> None:
        if node.tag == f"{SHEET_NS}rPh":
            return
        if node.tag == f"{SHEET_NS}t":
            chunks.append(node.text or "")
        for child in list(node):
            collect(child)

    collect(element)
    return "".join(chunks)


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value
