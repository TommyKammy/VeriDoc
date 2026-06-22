from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath, Path
import re
from typing import Any, Dict, List, Optional, Union
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OFFICE_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
OFFICE_REL_TYPE_BASE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
STYLES_REL_TYPE = f"{OFFICE_REL_TYPE_BASE}/styles"


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


@dataclass(frozen=True)
class _WorkbookRelationship:
    target: str
    rel_type: Optional[str]


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
    styles_part = _read_styles_part_name(archive)
    if styles_part is None:
        return []
    root = _read_xml(archive, styles_part, "styles")
    custom_formats = {
        num_format.attrib["numFmtId"]: num_format.attrib["formatCode"]
        for num_format in root.findall(f"{SHEET_NS}numFmts/{SHEET_NS}numFmt")
        if "numFmtId" in num_format.attrib and "formatCode" in num_format.attrib
    }
    base_style_formats = [
        _xf_format_code(style_format, custom_formats)
        for style_format in root.findall(f"{SHEET_NS}cellStyleXfs/{SHEET_NS}xf")
    ]
    style_formats: List[Optional[str]] = []
    for cell_format in root.findall(f"{SHEET_NS}cellXfs/{SHEET_NS}xf"):
        style_formats.append(
            _xf_format_code(
                cell_format,
                custom_formats,
                base_style_formats=base_style_formats,
            )
        )
    return style_formats


def _read_styles_part_name(archive: ZipFile) -> Optional[str]:
    if "xl/_rels/workbook.xml.rels" in archive.namelist():
        relationships = _read_workbook_relationships(archive)
        for relationship in relationships.values():
            if relationship.rel_type == STYLES_REL_TYPE:
                return _resolve_workbook_target(relationship.target)
    if "xl/styles.xml" in archive.namelist():
        return "xl/styles.xml"
    return None


def _xf_format_code(
    cell_format: ElementTree.Element,
    custom_formats: Dict[str, str],
    *,
    base_style_formats: Optional[List[Optional[str]]] = None,
) -> Optional[str]:
    if _is_false(cell_format.attrib.get("applyNumberFormat")):
        return None
    if "numFmtId" in cell_format.attrib:
        return custom_formats.get(cell_format.attrib["numFmtId"])
    if base_style_formats is None:
        return None
    base_style_index = _style_index(cell_format.attrib.get("xfId"))
    if base_style_index is None:
        return None
    if base_style_index < 0 or base_style_index >= len(base_style_formats):
        return None
    return base_style_formats[base_style_index]


def _read_workbook_sheet_parts(archive: ZipFile) -> List[tuple[str, str]]:
    workbook = _read_xml(archive, "xl/workbook.xml", "workbook")
    relationships = _read_workbook_relationships(archive)
    sheet_parts: List[tuple[str, str]] = []

    for sheet in workbook.findall(f"{SHEET_NS}sheets/{SHEET_NS}sheet"):
        name = sheet.attrib.get("name", "")
        rel_id = sheet.attrib.get(f"{OFFICE_REL_NS}id")
        if rel_id is None or rel_id not in relationships:
            raise ValueError(f"worksheet relationship is missing for sheet: {name}")
        sheet_parts.append((name, _resolve_workbook_target(relationships[rel_id].target)))

    return sheet_parts


def _read_workbook_relationships(archive: ZipFile) -> Dict[str, _WorkbookRelationship]:
    root = _read_xml(archive, "xl/_rels/workbook.xml.rels", "workbook relationships")
    return {
        rel.attrib["Id"]: _WorkbookRelationship(
            target=rel.attrib["Target"],
            rel_type=rel.attrib.get("Type"),
        )
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
    cells: List[XlsxCell] = []
    for row in root.findall(f"{SHEET_NS}sheetData/{SHEET_NS}row"):
        row_style_index = (
            _style_index(row.attrib.get("s"))
            if _is_true(row.attrib.get("customFormat"))
            else None
        )
        cells.extend(
            _cell_from_xml(
                cell,
                shared_strings,
                style_formats,
                column_styles,
                row_style_index,
            )
            for cell in row.findall(f"{SHEET_NS}c")
        )
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
    row_style_index: Optional[int],
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
            row_style_index,
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
    row_style_index: Optional[int],
) -> Optional[str]:
    format_code = _cell_format_code(cell, style_formats, column_styles, row_style_index)
    if format_code is None:
        return None
    if not _has_zero_padding_candidate(format_code):
        return None
    integer_value = _integral_numeric_value(value_text)
    if integer_value is None:
        return None
    zero_format = _zero_padding_format(format_code, integer_value)
    if zero_format is None:
        return None
    width, show_negative_sign = zero_format
    digits = str(abs(integer_value))
    padded = digits.zfill(width)
    if integer_value < 0 and show_negative_sign:
        return "-" + padded
    return padded


def _cell_format_code(
    cell: ElementTree.Element,
    style_formats: List[Optional[str]],
    column_styles: List[_ColumnStyle],
    row_style_index: Optional[int],
) -> Optional[str]:
    style_index_text = cell.attrib.get("s")
    style_index = _style_index(style_index_text)
    if style_index is None:
        style_index = row_style_index
    if style_index is None:
        style_index = _column_style_index(cell.attrib.get("r", ""), column_styles)
    if style_index is None:
        style_index = 0
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
    section_info = _format_section(format_code, value)
    if section_info is None:
        return None
    section, uses_negative_section = section_info
    normalized = _strip_leading_format_directives(section)
    match = re.fullmatch(r"(-?)(0+)", normalized)
    if match is None:
        return None
    width = len(match.group(2))
    if width <= 1:
        return None
    show_negative_sign = value < 0 and (not uses_negative_section or bool(match.group(1)))
    return width, show_negative_sign


def _has_zero_padding_candidate(format_code: str) -> bool:
    for section in _format_sections(format_code)[:3]:
        normalized = _strip_leading_format_directives(section)
        if re.fullmatch(r"-?0{2,}", normalized) is not None:
            return True
    return False


def _format_section(format_code: str, value: int) -> Optional[tuple[str, bool]]:
    sections = _format_sections(format_code)
    if not sections:
        return None
    conditional_sections = [
        (section, _section_condition(section)) for section in sections[:3]
    ]
    if any(condition is not None for _, condition in conditional_sections):
        for index, (section, condition) in enumerate(conditional_sections):
            if condition is None or _condition_matches(condition, value):
                return section, value < 0 and index == 1
        return None

    if value > 0:
        return sections[0], False
    if value < 0 and len(sections) > 1:
        return sections[1], True
    if value == 0 and len(sections) > 2:
        return sections[2], False
    return sections[0], False


def _section_condition(section: str) -> Optional[tuple[str, Decimal]]:
    for directive in _leading_format_directives(section):
        match = re.fullmatch(
            r"\s*(<=|>=|<>|=|<|>)\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)\s*",
            directive,
        )
        if match is None:
            continue
        try:
            return match.group(1), Decimal(match.group(2))
        except InvalidOperation:
            return None
    return None


def _leading_format_directives(section: str) -> List[str]:
    directives: List[str] = []
    remainder = section
    while True:
        match = re.match(r"\s*\[([^\]]+)\]", remainder)
        if match is None:
            return directives
        directives.append(match.group(1))
        remainder = remainder[match.end() :]


def _strip_leading_format_directives(section: str) -> str:
    remainder = section
    while True:
        match = re.match(r"\s*\[[^\]]+\]", remainder)
        if match is None:
            return remainder.strip()
        remainder = remainder[match.end() :]


def _condition_matches(condition: tuple[str, Decimal], value: int) -> bool:
    operator, operand = condition
    left = Decimal(value)
    if operator == "<":
        return left < operand
    if operator == "<=":
        return left <= operand
    if operator == ">":
        return left > operand
    if operator == ">=":
        return left >= operand
    if operator == "=":
        return left == operand
    if operator == "<>":
        return left != operand
    return False


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


def _is_true(value: Optional[str]) -> bool:
    return value is not None and value.lower() in {"1", "true"}


def _integral_numeric_value(value_text: str) -> Optional[int]:
    if value_text == "":
        return None
    try:
        value = Decimal(value_text)
    except InvalidOperation:
        return None
    if not value.is_finite() or value != value.to_integral_value():
        return None
    return int(value)


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
