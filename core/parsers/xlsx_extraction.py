from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import PurePosixPath, Path
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
        return asdict(self)


def extract_xlsx_structure(xlsx_path: Union[str, Path]) -> XlsxStructure:
    """Extract sheet, cell type, cell value, and merged-cell structure from XLSX."""
    source = Path(xlsx_path)
    if not source.is_file():
        raise FileNotFoundError(f"XLSX file not found: {source}")

    with _open_package(source) as archive:
        shared_strings = _read_shared_strings(archive)
        sheet_parts = _read_workbook_sheet_parts(archive)
        sheets = [
            _read_sheet(archive, name=name, part_name=part_name, shared_strings=shared_strings)
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
) -> XlsxSheet:
    root = _read_xml(archive, part_name, f"worksheet {name}")
    dimension = None
    dimension_element = root.find(f"{SHEET_NS}dimension")
    if dimension_element is not None:
        dimension = dimension_element.attrib.get("ref")

    cells = [
        _cell_from_xml(cell, shared_strings)
        for row in root.findall(f"{SHEET_NS}sheetData/{SHEET_NS}row")
        for cell in row.findall(f"{SHEET_NS}c")
    ]
    merged_ranges = [
        merge.attrib["ref"]
        for merge in root.findall(f"{SHEET_NS}mergeCells/{SHEET_NS}mergeCell")
        if "ref" in merge.attrib
    ]
    return XlsxSheet(name=name, dimension=dimension, cells=cells, merged_ranges=merged_ranges)


def _cell_from_xml(cell: ElementTree.Element, shared_strings: List[str]) -> XlsxCell:
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
        value = value_text == "1"
        value_type = "boolean"
    elif raw_type == "str":
        value = value_text
        value_type = "string"
    else:
        value = _number_value(value_text)
        value_type = "number" if value_text else "blank"

    return XlsxCell(ref=ref, value=value, value_type=value_type)


def _cell_value_text(cell: ElementTree.Element) -> str:
    value = cell.find(f"{SHEET_NS}v")
    return "" if value is None or value.text is None else value.text


def _shared_string_value(value_text: str, shared_strings: List[str]) -> str:
    try:
        return shared_strings[int(value_text)]
    except (IndexError, ValueError) as exc:
        raise ValueError(f"shared string index is invalid: {value_text}") from exc


def _number_value(value_text: str) -> Any:
    if value_text == "":
        return None
    try:
        number = float(value_text)
    except ValueError:
        return value_text
    return int(number) if number.is_integer() else number


def _joined_text(element: Optional[ElementTree.Element]) -> str:
    if element is None:
        return ""
    return "".join(text.text or "" for text in element.iter(f"{SHEET_NS}t"))
