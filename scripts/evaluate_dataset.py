#!/usr/bin/env python3
"""Compute Phase 0 evaluation metrics for public VeriDoc fixtures."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_EVALUATION_CASES = Path("datasets/gold/evaluation_cases_v0.json")


@dataclass(frozen=True)
class EvaluationMetrics:
    table_extraction_rate: float
    cell_match_rate: float
    source_linkage_rate: float
    false_auto_confirmed_count: int
    expected_table_count: int
    matched_table_count: int
    expected_cell_count: int
    matched_cell_count: int
    expected_source_link_count: int
    matched_source_link_count: int

    def as_dict(self) -> dict[str, int | float]:
        return {
            "table_extraction_rate": self.table_extraction_rate,
            "cell_match_rate": self.cell_match_rate,
            "source_linkage_rate": self.source_linkage_rate,
            "false_auto_confirmed_count": self.false_auto_confirmed_count,
            "expected_table_count": self.expected_table_count,
            "matched_table_count": self.matched_table_count,
            "expected_cell_count": self.expected_cell_count,
            "matched_cell_count": self.matched_cell_count,
            "expected_source_link_count": self.expected_source_link_count,
            "matched_source_link_count": self.matched_source_link_count,
        }


class EvaluationCaseError(ValueError):
    """Raised when evaluation cases are malformed or unsafe to score."""


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise EvaluationCaseError(f"{path}: expected top-level JSON object")
    return data


def ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def normalized_text(value: object) -> str:
    return " ".join(str(value).split())


def source_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    expected_source = expected.get("source")
    actual_source = actual.get("source")
    return isinstance(expected_source, dict) and expected_source == actual_source


def cells_by_id(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cells = table.get("cells")
    if not isinstance(cells, list):
        raise EvaluationCaseError(f"table {table.get('id')!r}: cells must be a list")

    indexed: dict[str, dict[str, Any]] = {}
    for cell in cells:
        if not isinstance(cell, dict) or not isinstance(cell.get("id"), str):
            raise EvaluationCaseError(f"table {table.get('id')!r}: each cell needs a string id")
        indexed[cell["id"]] = cell
    return indexed


def tables_by_id(section: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tables = section.get("tables")
    if not isinstance(tables, list):
        raise EvaluationCaseError("expected and actual sections must define tables lists")

    indexed: dict[str, dict[str, Any]] = {}
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("id"), str):
            raise EvaluationCaseError("each table needs a string id")
        indexed[table["id"]] = table
    return indexed


def validate_scope(data: dict[str, Any]) -> None:
    scope = data.get("scope")
    if not isinstance(scope, dict):
        raise EvaluationCaseError("missing scope")
    if scope.get("public_only") is not True:
        raise EvaluationCaseError("evaluation cases must be public-only")
    if scope.get("confidential_source_documents_allowed") is not False:
        raise EvaluationCaseError("confidential source documents are not allowed")
    if scope.get("production_or_gmp_claim") is not False:
        raise EvaluationCaseError("evaluation cases must not claim production or GMP readiness")


def evaluate_cases(data: dict[str, Any]) -> EvaluationMetrics:
    validate_scope(data)
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise EvaluationCaseError("cases must be a list")

    expected_table_count = 0
    matched_table_count = 0
    expected_cell_count = 0
    matched_cell_count = 0
    expected_source_link_count = 0
    matched_source_link_count = 0
    false_auto_confirmed_count = 0

    for case in cases:
        if not isinstance(case, dict):
            raise EvaluationCaseError("each case must be an object")
        expected_tables = tables_by_id(case.get("expected", {}))
        actual_tables = tables_by_id(case.get("actual", {}))

        expected_table_count += len(expected_tables)
        matched_table_count += len(set(expected_tables) & set(actual_tables))

        for table_id, expected_table in expected_tables.items():
            actual_table = actual_tables.get(table_id, {"cells": []})
            expected_cells = cells_by_id(expected_table)
            actual_cells = cells_by_id(actual_table)
            expected_cell_count += len(expected_cells)

            for cell_id, expected_cell in expected_cells.items():
                actual_cell = actual_cells.get(cell_id)
                if actual_cell is None:
                    continue

                expected_text = normalized_text(expected_cell.get("text", ""))
                actual_text = normalized_text(actual_cell.get("text", ""))
                if expected_text == actual_text:
                    matched_cell_count += 1

                if isinstance(expected_cell.get("source"), dict):
                    expected_source_link_count += 1
                    if source_matches(expected_cell, actual_cell):
                        matched_source_link_count += 1

                if expected_cell.get("requires_review") is True and actual_cell.get("auto_confirmed") is True:
                    false_auto_confirmed_count += 1

    return EvaluationMetrics(
        table_extraction_rate=ratio(matched_table_count, expected_table_count),
        cell_match_rate=ratio(matched_cell_count, expected_cell_count),
        source_linkage_rate=ratio(matched_source_link_count, expected_source_link_count),
        false_auto_confirmed_count=false_auto_confirmed_count,
        expected_table_count=expected_table_count,
        matched_table_count=matched_table_count,
        expected_cell_count=expected_cell_count,
        matched_cell_count=matched_cell_count,
        expected_source_link_count=expected_source_link_count,
        matched_source_link_count=matched_source_link_count,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_EVALUATION_CASES)
    args = parser.parse_args()

    try:
        metrics = evaluate_cases(load_json(args.cases))
    except (OSError, json.JSONDecodeError, EvaluationCaseError) as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(metrics.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
