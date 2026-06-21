from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List, Optional, Union
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass(frozen=True)
class DocxBlock:
    kind: str
    text: str
    style: Optional[str] = None
    rows: Optional[List[List[str]]] = None


@dataclass(frozen=True)
class DocxStructure:
    source_path: str
    blocks: List[DocxBlock]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_docx_structure(docx_path: Union[str, Path]) -> DocxStructure:
    """Extract paragraph, heading, and table structure from a DOCX package."""
    source = Path(docx_path)
    if not source.is_file():
        raise FileNotFoundError(f"DOCX file not found: {source}")

    document = _read_xml_part(source, "word/document.xml", "DOCX document body")
    body = document.find(f"{WORD_NS}body")
    if body is None:
        return DocxStructure(source_path=str(source), blocks=[])

    blocks: List[DocxBlock] = []
    for element in list(body):
        if element.tag == f"{WORD_NS}p":
            block = _paragraph_block(element)
            if block is not None:
                blocks.append(block)
        elif element.tag == f"{WORD_NS}tbl":
            rows = _table_rows(element)
            if rows:
                text = "\n".join("\t".join(cell for cell in row) for row in rows)
                blocks.append(DocxBlock(kind="table", text=text, rows=rows))

    return DocxStructure(source_path=str(source), blocks=blocks)


def _read_xml_part(package_path: Path, part_name: str, label: str) -> ElementTree.Element:
    try:
        with ZipFile(package_path) as archive:
            with archive.open(part_name) as part:
                return ElementTree.parse(part).getroot()
    except KeyError as exc:
        raise ValueError(f"missing {label}: {part_name}") from exc
    except (BadZipFile, ElementTree.ParseError) as exc:
        raise ValueError(f"failed to read {label}: {package_path}") from exc


def _paragraph_block(paragraph: ElementTree.Element) -> Optional[DocxBlock]:
    text = _text_content(paragraph)
    if not text:
        return None
    style = _paragraph_style(paragraph)
    kind = "heading" if _is_heading_style(style) else "paragraph"
    return DocxBlock(kind=kind, text=text, style=style)


def _paragraph_style(paragraph: ElementTree.Element) -> Optional[str]:
    style = paragraph.find(f"{WORD_NS}pPr/{WORD_NS}pStyle")
    if style is None:
        return None
    return style.attrib.get(f"{WORD_NS}val")


def _is_heading_style(style: Optional[str]) -> bool:
    return style is not None and style.lower().startswith("heading")


def _table_rows(table: ElementTree.Element) -> List[List[str]]:
    rows: List[List[str]] = []
    for row in table.findall(f"{WORD_NS}tr"):
        cells = [_table_cell_text(cell) for cell in row.findall(f"{WORD_NS}tc")]
        if cells:
            rows.append(cells)
    return rows


def _table_cell_text(cell: ElementTree.Element) -> str:
    paragraphs = cell.findall(f"{WORD_NS}p")
    if not paragraphs:
        return _text_content(cell)
    return "\n".join(_text_content(paragraph) for paragraph in paragraphs)


def _text_content(element: ElementTree.Element) -> str:
    chunks: List[str] = []
    for node in element.iter():
        if node.tag == f"{WORD_NS}t":
            chunks.append(node.text or "")
        elif node.tag == f"{WORD_NS}tab":
            chunks.append("\t")
        elif node.tag in {f"{WORD_NS}br", f"{WORD_NS}cr"}:
            chunks.append("\n")
    return "".join(chunks)
