from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

from core.parsers.pdf_table_extraction import (
    ExpectedTableShape,
    ExtractedTable,
    TableBBox,
    TableExtractionCandidate,
    build_table_extraction_report,
    compare_pdf_table_extractors,
)


def _candidate(
    name: str,
    flavor: str,
    rows: list[list[str]],
    *,
    has_bboxes: bool = True,
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
                page_number=1,
                rows=rows,
                cell_bboxes=bboxes,
            )
        ],
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
