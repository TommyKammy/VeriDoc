from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Iterable, Sequence


@dataclass(frozen=True)
class TableBBox:
    x: float
    y: float
    width: float
    height: float
    unit: str = "pt"
    origin: str = "bottom-left"


@dataclass(frozen=True)
class ExtractedTable:
    extractor: str
    flavor: str
    page_number: int
    rows: list[list[str]]
    cell_bboxes: list[list[TableBBox | None]]

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def column_count(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    @property
    def has_cell_bboxes(self) -> bool:
        expected_cells = sum(len(row) for row in self.rows)
        actual_cells = sum(1 for row in self.cell_bboxes for bbox in row if bbox is not None)
        return expected_cells > 0 and actual_cells == expected_cells


@dataclass(frozen=True)
class TableExtractionCandidate:
    extractor: str
    flavor: str
    version: str | None
    status: str
    tables: list[ExtractedTable]
    notes: str

    @property
    def name(self) -> str:
        return f"{self.extractor}:{self.flavor}"


@dataclass(frozen=True)
class TableExtractionMismatch:
    kind: str
    candidate: str
    expected: str
    actual: str
    notes: str


@dataclass(frozen=True)
class TableExtractionReport:
    source_path: str
    candidates: list[TableExtractionCandidate]
    mismatches: list[TableExtractionMismatch]
    selected_candidate: str | None
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExpectedTableShape:
    rows: int
    columns: int
    require_cell_bboxes: bool = True


class MissingPdfTableExtractorDependency(RuntimeError):
    """Raised when an optional PDF table extraction dependency is unavailable."""


def compare_pdf_table_extractors(
    pdf_path: str | Path,
    *,
    expected_shape: ExpectedTableShape | None = None,
) -> TableExtractionReport:
    """Compare Camelot lattice/stream and pdfplumber table extraction results."""
    source = Path(pdf_path)
    if not source.is_file():
        raise FileNotFoundError(f"PDF file not found: {source}")

    candidates = [
        _compare_camelot(source, flavor="lattice"),
        _compare_camelot(source, flavor="stream"),
        _compare_pdfplumber(source),
    ]
    return build_table_extraction_report(
        source_path=source,
        candidates=candidates,
        expected_shape=expected_shape,
    )


def build_table_extraction_report(
    *,
    source_path: str | Path,
    candidates: Sequence[TableExtractionCandidate],
    expected_shape: ExpectedTableShape | None = None,
) -> TableExtractionReport:
    """Build a minimal shape and cell-boundary diff report from extractor outputs."""
    mismatches: list[TableExtractionMismatch] = []
    ok_candidates = [candidate for candidate in candidates if candidate.status == "ok"]
    first_tables = {candidate.name: _first_table(candidate) for candidate in ok_candidates}

    for candidate in ok_candidates:
        table = first_tables[candidate.name]
        if table is None:
            mismatches.append(
                TableExtractionMismatch(
                    kind="missing-table",
                    candidate=candidate.name,
                    expected=(
                        f"{expected_shape.rows}x{expected_shape.columns}"
                        if expected_shape is not None
                        else "at least one table"
                    ),
                    actual="0x0",
                    notes="No table was extracted from the candidate.",
                )
            )

    if expected_shape is not None:
        for candidate in ok_candidates:
            table = first_tables[candidate.name]
            if table is None:
                continue
            if table.row_count != expected_shape.rows:
                mismatches.append(
                    TableExtractionMismatch(
                        kind="row-count",
                        candidate=candidate.name,
                        expected=str(expected_shape.rows),
                        actual=str(table.row_count),
                        notes="Extracted row count does not match the ruled-table sample.",
                    )
                )
            row_widths = [len(row) for row in table.rows]
            if any(row_width != expected_shape.columns for row_width in row_widths):
                mismatches.append(
                    TableExtractionMismatch(
                        kind="column-count",
                        candidate=candidate.name,
                        expected=str(expected_shape.columns),
                        actual=(
                            str(table.column_count)
                            if table.column_count != expected_shape.columns
                            else f"row widths {row_widths}"
                        ),
                        notes="Extracted column count does not match the ruled-table sample.",
                    )
                )
            if expected_shape.require_cell_bboxes and not table.has_cell_bboxes:
                mismatches.append(
                    TableExtractionMismatch(
                        kind="cell-boundary",
                        candidate=candidate.name,
                        expected="all cells have bboxes",
                        actual="missing one or more cell bboxes",
                        notes="The extraction cannot prove cell boundaries for the sample.",
                    )
                )

    for left_index, left_candidate in enumerate(ok_candidates):
        left_table = first_tables[left_candidate.name]
        if left_table is None:
            continue
        for right_candidate in ok_candidates[left_index + 1 :]:
            right_table = first_tables[right_candidate.name]
            if right_table is None:
                continue
            if (left_table.row_count, left_table.column_count) != (
                right_table.row_count,
                right_table.column_count,
            ):
                mismatches.append(
                    TableExtractionMismatch(
                        kind="candidate-shape",
                        candidate=f"{left_candidate.name} vs {right_candidate.name}",
                        expected=f"{left_table.row_count}x{left_table.column_count}",
                        actual=f"{right_table.row_count}x{right_table.column_count}",
                        notes="Candidate extractors disagree on table shape.",
                    )
                )

    selected_candidate = _select_candidate(ok_candidates, mismatches)
    notes = (
        "Camelot lattice is the provisional candidate when it matches the expected "
        "ruled-table shape and provides cell boundaries; unresolved risks remain "
        "around optional native dependencies and non-ruled tables."
    )
    return TableExtractionReport(
        source_path=str(source_path),
        candidates=list(candidates),
        mismatches=mismatches,
        selected_candidate=selected_candidate,
        notes=notes,
    )


def _compare_camelot(source: Path, *, flavor: str) -> TableExtractionCandidate:
    try:
        import camelot
    except ImportError:
        return TableExtractionCandidate(
            extractor="camelot",
            flavor=flavor,
            version=None,
            status="not-installed",
            tables=[],
            notes=(
                "Camelot is required for PDF table extraction comparison; install "
                "evaluation dependencies with `python3 -m pip install -r "
                "requirements-pdf-eval.txt`."
            ),
        )

    try:
        tables = camelot.read_pdf(str(source), pages="all", flavor=flavor)
        return TableExtractionCandidate(
            extractor="camelot",
            flavor=flavor,
            version=_package_version("camelot-py"),
            status="ok",
            tables=[_table_from_camelot(table, flavor=flavor) for table in tables],
            notes="Camelot table extraction completed.",
        )
    except Exception as exc:
        return TableExtractionCandidate(
            extractor="camelot",
            flavor=flavor,
            version=_package_version("camelot-py"),
            status="failed",
            tables=[],
            notes=str(exc),
        )


def _compare_pdfplumber(source: Path) -> TableExtractionCandidate:
    try:
        import pdfplumber
    except ImportError:
        return TableExtractionCandidate(
            extractor="pdfplumber",
            flavor="table",
            version=None,
            status="not-installed",
            tables=[],
            notes=(
                "pdfplumber is required for PDF table extraction comparison; install "
                "evaluation dependencies with `python3 -m pip install -r "
                "requirements-pdf-eval.txt`."
            ),
        )

    try:
        extracted: list[ExtractedTable] = []
        with pdfplumber.open(source) as pdf:
            for page_index, page in enumerate(pdf.pages, start=1):
                for table in page.find_tables():
                    rows = [[_cell_text(cell) for cell in row] for row in table.extract()]
                    bboxes = [_pdfplumber_bbox(row) for row in table.rows]
                    extracted.append(
                        ExtractedTable(
                            extractor="pdfplumber",
                            flavor="table",
                            page_number=page_index,
                            rows=rows,
                            cell_bboxes=bboxes,
                        )
                    )
        return TableExtractionCandidate(
            extractor="pdfplumber",
            flavor="table",
            version=_package_version("pdfplumber"),
            status="ok",
            tables=extracted,
            notes="pdfplumber table extraction completed.",
        )
    except Exception as exc:
        return TableExtractionCandidate(
            extractor="pdfplumber",
            flavor="table",
            version=_package_version("pdfplumber"),
            status="failed",
            tables=[],
            notes=str(exc),
        )


def _table_from_camelot(table: Any, *, flavor: str) -> ExtractedTable:
    rows = [[_cell_text(value) for value in row] for row in table.df.values.tolist()]
    bboxes: list[list[TableBBox | None]] = []
    for row in getattr(table, "cells", []):
        bboxes.append([_camelot_bbox(cell) for cell in row])
    return ExtractedTable(
        extractor="camelot",
        flavor=flavor,
        page_number=int(getattr(table, "page", 1)),
        rows=rows,
        cell_bboxes=bboxes,
    )


def _camelot_bbox(cell: Any) -> TableBBox | None:
    try:
        x0 = float(cell.x1)
        y0 = float(cell.y1)
        x1 = float(cell.x2)
        y1 = float(cell.y2)
    except (AttributeError, TypeError, ValueError):
        return None
    return _bbox_from_coords(x0, y0, x1, y1)


def _pdfplumber_bbox(row: Any) -> list[TableBBox | None]:
    return [
        _bbox_from_coords(*cell, origin="top-left") if cell is not None else None
        for cell in row.cells
    ]


def _bbox_from_coords(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    origin: str = "bottom-left",
) -> TableBBox | None:
    width = x1 - x0
    height = y1 - y0
    if width <= 0 or height <= 0:
        return None
    return TableBBox(x=x0, y=y0, width=width, height=height, origin=origin)


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first_table(candidate: TableExtractionCandidate) -> ExtractedTable | None:
    return candidate.tables[0] if candidate.tables else None


def _select_candidate(
    candidates: Iterable[TableExtractionCandidate],
    mismatches: Sequence[TableExtractionMismatch],
) -> str | None:
    blocked = {mismatch.candidate for mismatch in mismatches if " vs " not in mismatch.candidate}
    for preferred in ("camelot:lattice", "pdfplumber:table", "camelot:stream"):
        for candidate in candidates:
            if (
                candidate.name == preferred
                and candidate.name not in blocked
                and _first_table(candidate) is not None
            ):
                return candidate.name
    return None


def _package_version(package_name: str) -> str | None:
    try:
        return metadata.version(package_name)
    except metadata.PackageNotFoundError:
        return None
