from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

from core.parsers.pdf_table_extraction import (
    ExpectedTableShape,
    ExtractedTable,
    TableBBox,
    TableExtractionCandidate,
    _pdfplumber_bbox,
    build_table_extraction_report,
    compare_pdf_table_extractors,
)


def _candidate(
    name: str,
    flavor: str,
    rows: list[list[str]],
    *,
    has_bboxes: bool = True,
    page_number: int = 1,
) -> TableExtractionCandidate:
    bboxes = [
        [
            TableBBox(x=float(column * 50), y=float(row * 20), width=50.0, height=20.0)
            if has_bboxes
            else None
            for column, _cell in enumerate(values)
        ]
        for row, values in enumerate(rows)
    ]
    return TableExtractionCandidate(
        extractor=name,
        flavor=flavor,
        version="test",
        status="ok",
        tables=[
            ExtractedTable(
                extractor=name,
                flavor=flavor,
                page_number=page_number,
                rows=rows,
                cell_bboxes=bboxes,
            )
        ],
        notes="synthetic candidate",
    )


def _multi_table_candidate(
    name: str,
    flavor: str,
    tables: list[list[list[str]]],
) -> TableExtractionCandidate:
    return TableExtractionCandidate(
        extractor=name,
        flavor=flavor,
        version="test",
        status="ok",
        tables=[
            ExtractedTable(
                extractor=name,
                flavor=flavor,
                page_number=index,
                rows=rows,
                cell_bboxes=[
                    [
                        TableBBox(
                            x=float(column * 50),
                            y=float(row * 20),
                            width=50.0,
                            height=20.0,
                        )
                        for column, _cell in enumerate(values)
                    ]
                    for row, values in enumerate(rows)
                ],
            )
            for index, rows in enumerate(tables, start=1)
        ],
        notes="synthetic candidate",
    )


def _empty_candidate(name: str, flavor: str) -> TableExtractionCandidate:
    return TableExtractionCandidate(
        extractor=name,
        flavor=flavor,
        version="test",
        status="ok",
        tables=[],
        notes="synthetic candidate",
    )


def test_build_table_extraction_report_detects_shape_and_boundary_differences(
    tmp_path: Path,
) -> None:
    report = build_table_extraction_report(
        source_path=tmp_path / "ruled-table.pdf",
        expected_shape=ExpectedTableShape(rows=3, columns=2),
        candidates=[
            _candidate("camelot", "lattice", [["A", "B"], ["C", "D"], ["E", "F"]]),
            _candidate("camelot", "stream", [["A", "B", ""], ["C", "D", ""], ["E", "F", ""]]),
            _candidate(
                "pdfplumber",
                "table",
                [["A", "B"], ["C", "D"], ["E", "F"]],
                has_bboxes=False,
            ),
        ],
    )

    assert report.selected_candidate == "camelot:lattice"
    assert {mismatch.kind for mismatch in report.mismatches} == {
        "cell-boundary",
        "candidate-shape",
        "column-count",
    }
    assert any(
        mismatch.candidate == "camelot:stream" and mismatch.expected == "2" and mismatch.actual == "3"
        for mismatch in report.mismatches
    )
    assert any(
        mismatch.candidate == "pdfplumber:table" and mismatch.kind == "cell-boundary"
        for mismatch in report.mismatches
    )
    assert any(
        mismatch.candidate == "camelot:lattice vs camelot:stream"
        and mismatch.expected == "3x2"
        and mismatch.actual == "3x3"
        for mismatch in report.mismatches
    )


def test_build_table_extraction_report_rejects_ragged_expected_shape(
    tmp_path: Path,
) -> None:
    ragged_table = _candidate("camelot", "lattice", [["A", "B"], ["C"], ["E", "F"]])

    report = build_table_extraction_report(
        source_path=tmp_path / "ruled-table.pdf",
        expected_shape=ExpectedTableShape(rows=3, columns=2),
        candidates=[ragged_table],
    )

    assert ragged_table.tables[0].row_widths == [2, 1, 2]
    assert ragged_table.tables[0].is_rectangular is False
    assert ragged_table.tables[0].column_count == 0
    assert report.selected_candidate is None
    assert any(
        mismatch.kind == "column-count"
        and mismatch.candidate == "camelot:lattice"
        and mismatch.expected == "2"
        and mismatch.actual == "row widths [2, 1, 2]"
        for mismatch in report.mismatches
    )


def test_build_table_extraction_report_rejects_empty_candidates_without_expected_shape(
    tmp_path: Path,
) -> None:
    report = build_table_extraction_report(
        source_path=tmp_path / "no-table.pdf",
        candidates=[
            _empty_candidate("camelot", "lattice"),
            _empty_candidate("pdfplumber", "table"),
        ],
    )

    assert report.selected_candidate is None
    assert {
        (mismatch.kind, mismatch.candidate, mismatch.expected, mismatch.actual)
        for mismatch in report.mismatches
    } == {
        ("missing-table", "camelot:lattice", "at least one table", "0x0"),
        ("missing-table", "pdfplumber:table", "at least one table", "0x0"),
    }


def test_build_table_extraction_report_rejects_missing_bboxes_without_expected_shape(
    tmp_path: Path,
) -> None:
    report = build_table_extraction_report(
        source_path=tmp_path / "ruled-table.pdf",
        candidates=[
            _candidate("camelot", "lattice", [["A", "B"], ["C", "D"]], has_bboxes=False),
        ],
    )

    assert report.selected_candidate is None
    assert [
        (mismatch.kind, mismatch.candidate, mismatch.expected, mismatch.actual)
        for mismatch in report.mismatches
    ] == [
        (
            "cell-boundary",
            "camelot:lattice",
            "all cells have bboxes",
            "missing one or more cell bboxes",
        )
    ]


def test_build_table_extraction_report_preserves_ragged_widths_without_expected_shape(
    tmp_path: Path,
) -> None:
    report = build_table_extraction_report(
        source_path=tmp_path / "ruled-table.pdf",
        candidates=[
            _candidate("camelot", "lattice", [["A"], ["B", "C"]]),
            _candidate("camelot", "stream", [["A", "B", "C"], ["D"]]),
        ],
    )

    assert report.selected_candidate is None
    assert any(
        mismatch.kind == "candidate-shape"
        and mismatch.candidate == "camelot:lattice vs camelot:stream"
        and mismatch.expected == "row widths [1, 2]"
        and mismatch.actual == "row widths [3, 1]"
        for mismatch in report.mismatches
    )


def test_build_table_extraction_report_compares_every_table_without_expected_shape(
    tmp_path: Path,
) -> None:
    report = build_table_extraction_report(
        source_path=tmp_path / "multi-table.pdf",
        candidates=[
            _multi_table_candidate(
                "camelot",
                "lattice",
                [[["A", "B"], ["C", "D"]], [["Lot", "Assay"], ["A-001", "12.5"]]],
            ),
            _multi_table_candidate(
                "pdfplumber",
                "table",
                [[["A", "B"], ["C", "D"]], [["Lot"], ["A-001", "12.5"]]],
            ),
        ],
    )

    assert report.selected_candidate is None
    assert any(
        mismatch.kind == "candidate-shape"
        and mismatch.candidate == "camelot:lattice vs pdfplumber:table"
        and mismatch.expected == "2x2"
        and mismatch.actual == "row widths [1, 2]"
        and "table 2 shape" in mismatch.notes
        for mismatch in report.mismatches
    )


def test_build_table_extraction_report_blocks_selection_when_cell_text_disagrees(
    tmp_path: Path,
) -> None:
    report = build_table_extraction_report(
        source_path=tmp_path / "text-mismatch.pdf",
        candidates=[
            _candidate("camelot", "lattice", [["Lot", "Assay"], ["A-001", "12.5"]]),
            _candidate("pdfplumber", "table", [["Lot", "Assay"], ["A-001", "13.0"]]),
        ],
    )

    assert report.selected_candidate is None
    assert any(
        mismatch.kind == "candidate-text"
        and mismatch.candidate == "camelot:lattice vs pdfplumber:table"
        and mismatch.expected == "Lot\tAssay\nA-001\t12.5"
        and mismatch.actual == "Lot\tAssay\nA-001\t13.0"
        and "table 1 cell text" in mismatch.notes
        for mismatch in report.mismatches
    )


def test_build_table_extraction_report_blocks_selection_when_table_pages_disagree(
    tmp_path: Path,
) -> None:
    report = build_table_extraction_report(
        source_path=tmp_path / "repeated-form.pdf",
        candidates=[
            _candidate(
                "camelot",
                "lattice",
                [["Lot", "Assay"], ["A-001", "12.5"]],
                page_number=1,
            ),
            _candidate(
                "pdfplumber",
                "table",
                [["Lot", "Assay"], ["A-001", "12.5"]],
                page_number=2,
            ),
        ],
    )

    assert report.selected_candidate is None
    assert any(
        mismatch.kind == "candidate-page"
        and mismatch.candidate == "camelot:lattice vs pdfplumber:table"
        and mismatch.expected == "page 1"
        and mismatch.actual == "page 2"
        and "table 1 page" in mismatch.notes
        for mismatch in report.mismatches
    )


def test_build_table_extraction_report_blocks_selection_when_table_counts_disagree(
    tmp_path: Path,
) -> None:
    report = build_table_extraction_report(
        source_path=tmp_path / "count-mismatch.pdf",
        candidates=[
            _multi_table_candidate(
                "camelot",
                "lattice",
                [[["A", "B"], ["C", "D"]], [["Lot", "Assay"], ["A-001", "12.5"]]],
            ),
            _multi_table_candidate(
                "pdfplumber",
                "table",
                [[["A", "B"], ["C", "D"]]],
            ),
        ],
    )

    assert report.selected_candidate is None
    assert any(
        mismatch.kind == "candidate-table-count"
        and mismatch.candidate == "camelot:lattice vs pdfplumber:table"
        for mismatch in report.mismatches
    )


def test_pdfplumber_bbox_preserves_top_left_origin() -> None:
    class Row:
        cells = [(10.0, 20.0, 40.0, 50.0)]

    bboxes = _pdfplumber_bbox(Row())

    assert bboxes == [
        TableBBox(
            x=10.0,
            y=20.0,
            width=30.0,
            height=30.0,
            origin="top-left",
        )
    ]


def test_compare_pdf_table_extractors_reports_missing_optional_dependencies(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    original_import = builtins.__import__

    def import_without_table_extractors(name: str, *args: Any, **kwargs: Any) -> Any:
        if name in {"camelot", "pdfplumber"}:
            raise ImportError(f"{name} unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_table_extractors)

    report = compare_pdf_table_extractors(
        pdf_path,
        expected_shape=ExpectedTableShape(rows=3, columns=2),
    )

    assert [(candidate.name, candidate.status) for candidate in report.candidates] == [
        ("camelot:lattice", "not-installed"),
        ("camelot:stream", "not-installed"),
        ("pdfplumber:table", "not-installed"),
    ]
    assert report.selected_candidate is None
    assert all("requirements-pdf-eval.txt" in candidate.notes for candidate in report.candidates)
