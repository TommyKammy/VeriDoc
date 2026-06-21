#!/usr/bin/env python3
"""Compute Phase 0 evaluation metrics for public VeriDoc fixtures."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.llm.conversion_plan import ConversionPlanValidationError, validate_conversion_plan


DEFAULT_EVALUATION_CASES = Path("datasets/gold/evaluation_cases_v0.json")
DEFAULT_LLM_STABILITY_RUNS = Path("datasets/gold/llm_stability_runs_v0.json")
EVALUATION_CASES_SCHEMA_VERSION = "veridoc-evaluation-cases/v0"
LLM_STABILITY_RUNS_SCHEMA_VERSION = "veridoc-llm-stability-runs/v0"
FIXTURE_MANIFEST_SCHEMA_VERSION = "veridoc-eval-fixtures/v0"
FIXTURE_SCHEMA_VERSION = "veridoc-evaluation-fixture/v0"
EXPECTED_ALLOWED_FIXTURE_ROOT = Path("datasets/fixtures")
EXPECTED_DATASET_MANIFEST = EXPECTED_ALLOWED_FIXTURE_ROOT / "manifest.json"
EXPECTED_SCOPE_PHASE = "phase0"
PUBLIC_FIXTURE_ANONYMIZATION_VALUES = {"anonymized", "synthetic"}
PUBLIC_LLM_STABILITY_SOURCE_KINDS = {"anonymized_text", "synthetic_text"}


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


@dataclass(frozen=True)
class LLMStabilityMetrics:
    input_id: str
    run_count: int
    plan_agreement_rate: float
    confirmed_value_agreement_rate: float
    distinct_plan_count: int
    distinct_confirmed_value_count: int
    unstable_example_count: int
    unstable_examples: tuple[dict[str, str], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "input_id": self.input_id,
            "run_count": self.run_count,
            "plan_agreement_rate": self.plan_agreement_rate,
            "confirmed_value_agreement_rate": self.confirmed_value_agreement_rate,
            "distinct_plan_count": self.distinct_plan_count,
            "distinct_confirmed_value_count": self.distinct_confirmed_value_count,
            "unstable_example_count": self.unstable_example_count,
            "unstable_examples": list(self.unstable_examples),
        }


class EvaluationCaseError(ValueError):
    """Raised when evaluation cases are malformed or unsafe to score."""


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        data = json.load(
            file,
            parse_constant=lambda constant: (_ for _ in ()).throw(
                EvaluationCaseError(f"{path}: non-finite JSON number is not allowed: {constant}")
            ),
        )
    if not isinstance(data, dict):
        raise EvaluationCaseError(f"{path}: expected top-level JSON object")
    return data


def ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return numerator / denominator


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def normalized_text(value: object) -> str:
    return " ".join(str(value).split())


def source_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    expected_source = expected.get("source")
    actual_source = actual.get("source")
    return (
        is_valid_source_anchor(expected_source)
        and is_valid_source_anchor(actual_source)
        and expected_source == actual_source
    )


def is_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return abs(value) <= sys.float_info.max
    return isinstance(value, float) and math.isfinite(value)


def is_valid_source_anchor(source: object) -> bool:
    if not isinstance(source, dict):
        return False
    if not isinstance(source.get("source_page"), int) or isinstance(
        source.get("source_page"), bool
    ):
        return False
    bbox = source.get("bbox")
    if not isinstance(bbox, dict):
        return False
    required_bbox_keys = ("x", "y", "width", "height")
    if any(not is_number(bbox.get(key)) for key in required_bbox_keys):
        return False
    return source["source_page"] > 0 and bbox["width"] > 0 and bbox["height"] > 0


def validate_source_anchor(source: object, context: str) -> None:
    if not is_valid_source_anchor(source):
        raise EvaluationCaseError(
            f"{context}: source must define source_page and bbox x/y/width/height"
        )


def pages_by_number(fixture: dict[str, Any], fixture_id: str) -> dict[int, dict[str, Any]]:
    pages = fixture.get("pages")
    if not isinstance(pages, list):
        raise EvaluationCaseError(f"fixture {fixture_id!r}: pages must be a list")

    indexed: dict[int, dict[str, Any]] = {}
    for page in pages:
        if not isinstance(page, dict):
            raise EvaluationCaseError(f"fixture {fixture_id!r}: each page must be an object")
        page_number = page.get("page_number")
        if not isinstance(page_number, int) or isinstance(page_number, bool):
            raise EvaluationCaseError(f"fixture {fixture_id!r}: page_number must be an integer")
        if page_number in indexed:
            raise EvaluationCaseError(f"fixture {fixture_id!r}: duplicate page {page_number}")
        if not is_number(page.get("width")) or not is_number(page.get("height")):
            raise EvaluationCaseError(f"fixture {fixture_id!r}: page width and height are required")
        if page["width"] <= 0 or page["height"] <= 0:
            raise EvaluationCaseError(f"fixture {fixture_id!r}: page dimensions must be positive")
        indexed[page_number] = page
    return indexed


def validate_source_anchor_on_page(
    source: object, pages: dict[int, dict[str, Any]], context: str
) -> None:
    validate_source_anchor(source, context)
    assert isinstance(source, dict)
    page = pages.get(source["source_page"])
    if page is None:
        raise EvaluationCaseError(f"{context}: source_page is not declared in fixture pages")

    bbox = source["bbox"]
    if (
        bbox["x"] < 0
        or bbox["y"] < 0
        or bbox["x"] + bbox["width"] > page["width"]
        or bbox["y"] + bbox["height"] > page["height"]
    ):
        raise EvaluationCaseError(f"{context}: source bbox must fit within declared page geometry")


def cells_by_id(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cells = table.get("cells")
    if not isinstance(cells, list):
        raise EvaluationCaseError(f"table {table.get('id')!r}: cells must be a list")

    indexed: dict[str, dict[str, Any]] = {}
    for cell in cells:
        if not isinstance(cell, dict) or not isinstance(cell.get("id"), str):
            raise EvaluationCaseError(f"table {table.get('id')!r}: each cell needs a string id")
        if cell["id"] in indexed:
            raise EvaluationCaseError(
                f"table {table.get('id')!r}: duplicate cell id {cell['id']!r}"
            )
        indexed[cell["id"]] = cell
    return indexed


def required_cells_by_id(table: dict[str, Any], context: str) -> dict[str, dict[str, Any]]:
    indexed = cells_by_id(table)
    if not indexed:
        raise EvaluationCaseError(f"{context}: cells must contain at least one cell")
    return indexed


def tables_by_id(section: object) -> dict[str, dict[str, Any]]:
    if not isinstance(section, dict):
        raise EvaluationCaseError("expected and actual sections must be objects")
    tables = section["tables"] if "tables" in section else None
    if not isinstance(tables, list):
        raise EvaluationCaseError("expected and actual sections must define tables lists")

    indexed: dict[str, dict[str, Any]] = {}
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("id"), str):
            raise EvaluationCaseError("each table needs a string id")
        if table["id"] in indexed:
            raise EvaluationCaseError(f"duplicate table id {table['id']!r}")
        indexed[table["id"]] = table
    return indexed


def required_tables_by_id(section: object, context: str) -> dict[str, dict[str, Any]]:
    indexed = tables_by_id(section)
    if not indexed:
        raise EvaluationCaseError(f"{context}: tables must contain at least one table")
    return indexed


def cases_by_id(cases: list[Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for case in cases:
        if not isinstance(case, dict):
            raise EvaluationCaseError("each case must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            raise EvaluationCaseError("each case needs a non-empty string id")
        if case_id in indexed:
            raise EvaluationCaseError(f"duplicate case id {case_id!r}")
        indexed[case_id] = case
    return indexed


def validate_schema_version(data: dict[str, Any]) -> None:
    if data.get("schema_version") != EVALUATION_CASES_SCHEMA_VERSION:
        raise EvaluationCaseError(
            f"unsupported evaluation schema_version {data.get('schema_version')!r}"
        )


def validate_scope(data: dict[str, Any]) -> None:
    scope = data.get("scope")
    if not isinstance(scope, dict):
        raise EvaluationCaseError("missing scope")
    if scope.get("phase") != EXPECTED_SCOPE_PHASE:
        raise EvaluationCaseError("evaluation cases must target phase0")
    if scope.get("public_only") is not True:
        raise EvaluationCaseError("evaluation cases must be public-only")
    if scope.get("confidential_source_documents_allowed") is not False:
        raise EvaluationCaseError("confidential source documents are not allowed")
    if scope.get("production_or_gmp_claim") is not False:
        raise EvaluationCaseError("evaluation cases must not claim production or GMP readiness")


def validate_llm_stability_scope(data: dict[str, Any]) -> None:
    validate_scope(data)
    source_policy = data.get("source_policy")
    if not isinstance(source_policy, dict):
        raise EvaluationCaseError("LLM stability runs must define source_policy")
    if source_policy.get("synthetic_or_anonymized_only") is not True:
        raise EvaluationCaseError("LLM stability runs must use synthetic or anonymized input")
    if source_policy.get("real_confidential_records_included") is not False:
        raise EvaluationCaseError("LLM stability runs must not include real confidential records")


def manifest_path_from_cases(data: dict[str, Any], manifest_root: Path | None = None) -> Path:
    manifest_path = data.get("dataset_manifest")
    if not isinstance(manifest_path, str) or not manifest_path:
        raise EvaluationCaseError("dataset_manifest must be a non-empty string")
    path = Path(manifest_path)
    if path.is_absolute() or path != EXPECTED_DATASET_MANIFEST:
        raise EvaluationCaseError("dataset_manifest must be datasets/fixtures/manifest.json")
    if manifest_root is not None:
        return manifest_root / path
    return path


def fixture_paths_from_manifest(
    manifest: dict[str, Any], manifest_root: Path
) -> dict[str, Path]:
    if manifest.get("schema_version") != FIXTURE_MANIFEST_SCHEMA_VERSION:
        raise EvaluationCaseError(
            f"unsupported fixture manifest schema_version {manifest.get('schema_version')!r}"
        )

    policy = manifest.get("policy")
    if not isinstance(policy, dict):
        raise EvaluationCaseError("fixture manifest must define a policy")
    allowed_root_value = policy.get("allowed_fixture_root")
    if not isinstance(allowed_root_value, str) or not allowed_root_value:
        raise EvaluationCaseError("fixture manifest policy must define allowed_fixture_root")
    if policy.get("public_only") is not True:
        raise EvaluationCaseError("fixture manifest policy must be public-only")
    if policy.get("confidential_source_documents_allowed") is not False:
        raise EvaluationCaseError("fixture manifest must disallow confidential source documents")
    allowed_root_path = Path(allowed_root_value)
    if allowed_root_path.is_absolute():
        raise EvaluationCaseError("allowed_fixture_root must be repo-relative")
    if ".." in allowed_root_path.parts:
        raise EvaluationCaseError("allowed_fixture_root must be datasets/fixtures")
    allowed_root = (manifest_root / allowed_root_path).resolve()
    expected_allowed_root = (manifest_root / EXPECTED_ALLOWED_FIXTURE_ROOT).resolve()
    if allowed_root != expected_allowed_root:
        raise EvaluationCaseError("allowed_fixture_root must be datasets/fixtures")

    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list):
        raise EvaluationCaseError("fixture manifest must define a fixtures list")

    fixture_paths: dict[str, Path] = {}
    seen_fixture_ids: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict) or not isinstance(fixture.get("id"), str):
            raise EvaluationCaseError("each fixture manifest entry needs a string id")
        fixture_id = fixture["id"]
        if fixture_id in seen_fixture_ids:
            raise EvaluationCaseError(f"duplicate fixture id {fixture_id!r}")
        seen_fixture_ids.add(fixture_id)
        if fixture.get("public_review_safe") is not True:
            raise EvaluationCaseError(f"fixture {fixture_id!r} is not public-review safe")
        if fixture.get("confidentiality") != "public":
            raise EvaluationCaseError(f"fixture {fixture_id!r} must declare public confidentiality")

        fixture_path_value = fixture.get("path")
        if fixture_path_value is None:
            continue
        if fixture.get("anonymization") not in PUBLIC_FIXTURE_ANONYMIZATION_VALUES:
            raise EvaluationCaseError(
                f"fixture {fixture_id!r} must be synthetic or anonymized before scoring"
            )
        if not isinstance(fixture_path_value, str) or not fixture_path_value:
            raise EvaluationCaseError(f"fixture {fixture_id!r} path must be a non-empty string")
        fixture_path = Path(fixture_path_value)
        if fixture_path.is_absolute():
            raise EvaluationCaseError(f"fixture {fixture_id!r} path must be repo-relative")
        resolved_fixture_path = (manifest_root / fixture_path).resolve()
        if not resolved_fixture_path.is_relative_to(allowed_root):
            raise EvaluationCaseError(
                f"fixture {fixture_id!r} path must stay under {allowed_root_value!r}"
            )
        if not resolved_fixture_path.is_file():
            raise EvaluationCaseError(f"fixture {fixture_id!r} path does not exist")
        fixture_paths[fixture_id] = resolved_fixture_path
    return fixture_paths


def validated_cell_text(cell: dict[str, Any], context: str) -> str:
    text = cell.get("text")
    if not isinstance(text, str) or not normalized_text(text):
        raise EvaluationCaseError(f"{context}: text must be a non-empty string")
    return text


def actual_cell_text(cell: dict[str, Any], context: str) -> str:
    text = cell.get("text")
    if not isinstance(text, str):
        raise EvaluationCaseError(f"{context}: text must be a string")
    return text


def actual_auto_confirmed(cell: dict[str, Any], context: str) -> bool:
    value = cell.get("auto_confirmed", False)
    if not isinstance(value, bool):
        raise EvaluationCaseError(f"{context}: auto_confirmed must be a boolean")
    return value


def validate_actual_cells(cells: dict[str, dict[str, Any]], case_id: object) -> None:
    for actual_cell_id, actual_cell in cells.items():
        actual_context = f"case {case_id!r}: actual cell {actual_cell_id!r}"
        actual_cell_text(actual_cell, actual_context)
        actual_auto_confirmed(actual_cell, actual_context)


def validate_expected_tables_against_fixture(
    case: dict[str, Any], fixture: dict[str, Any], fixture_id: str
) -> None:
    if fixture.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise EvaluationCaseError(
            f"unsupported fixture schema_version {fixture.get('schema_version')!r}"
        )

    fixture_document = fixture.get("document")
    if not isinstance(fixture_document, dict) or not isinstance(fixture_document.get("id"), str):
        raise EvaluationCaseError(f"fixture {fixture_id!r}: document.id must be a string")
    if case.get("document_id") != fixture_document["id"]:
        raise EvaluationCaseError(
            f"case {case.get('id')!r}: document_id must match fixture {fixture_id!r}"
        )

    fixture_tables = tables_by_id(fixture)
    expected_tables = required_tables_by_id(
        case.get("expected", {}), f"case {case.get('id')!r}: expected"
    )
    fixture_pages = pages_by_number(fixture, fixture_id)

    for table_id, expected_table in expected_tables.items():
        fixture_table_id = expected_table.get("fixture_table_id")
        if fixture_table_id != table_id:
            raise EvaluationCaseError(
                f"case {case.get('id')!r}: expected table {table_id!r} "
                "must declare a matching fixture_table_id"
            )

        fixture_table = fixture_tables.get(fixture_table_id)
        if fixture_table is None:
            raise EvaluationCaseError(
                f"case {case.get('id')!r}: expected table {table_id!r} "
                f"is not present in fixture {fixture_id!r}"
            )

        fixture_cells = cells_by_id(fixture_table)
        expected_cells = required_cells_by_id(
            expected_table, f"case {case.get('id')!r}: expected table {table_id!r}"
        )
        for cell_id, expected_cell in expected_cells.items():
            fixture_cell = fixture_cells.get(cell_id)
            if fixture_cell is None:
                raise EvaluationCaseError(
                    f"case {case.get('id')!r}: expected cell {cell_id!r} "
                    f"is not present in fixture {fixture_id!r} table {table_id!r}"
                )
            expected_text = validated_cell_text(
                expected_cell, f"case {case.get('id')!r}: expected cell {cell_id!r}"
            )
            fixture_text = validated_cell_text(
                fixture_cell, f"fixture {fixture_id!r} table {table_id!r} cell {cell_id!r}"
            )
            if normalized_text(expected_text) != normalized_text(fixture_text):
                raise EvaluationCaseError(
                    f"case {case.get('id')!r}: expected cell {cell_id!r} "
                    f"text does not match fixture {fixture_id!r}"
                )
            if expected_cell.get("source") != fixture_cell.get("source"):
                raise EvaluationCaseError(
                    f"case {case.get('id')!r}: expected cell {cell_id!r} "
                    f"source does not match fixture {fixture_id!r}"
                )
            validate_source_anchor_on_page(
                fixture_cell.get("source"),
                fixture_pages,
                f"fixture {fixture_id!r} table {table_id!r} cell {cell_id!r}",
            )
            validate_source_anchor_on_page(
                expected_cell.get("source"),
                fixture_pages,
                f"case {case.get('id')!r}: expected cell {cell_id!r}",
            )
            if not isinstance(fixture_cell.get("requires_review"), bool):
                raise EvaluationCaseError(
                    f"fixture {fixture_id!r} table {table_id!r} cell {cell_id!r}: "
                    "requires_review must be a boolean"
                )
            if not isinstance(expected_cell.get("requires_review"), bool):
                raise EvaluationCaseError(
                    f"case {case.get('id')!r}: expected cell {cell_id!r} "
                    "requires_review must be a boolean"
                )
            if expected_cell.get("requires_review") != fixture_cell.get("requires_review"):
                raise EvaluationCaseError(
                    f"case {case.get('id')!r}: expected cell {cell_id!r} "
                    f"requires_review does not match fixture {fixture_id!r}"
                )


def validate_case_fixtures(
    data: dict[str, Any], cases: list[Any], manifest_root: Path | None = None
) -> None:
    indexed_cases = cases_by_id(cases)

    root = manifest_root or Path.cwd()
    manifest = load_json(manifest_path_from_cases(data, root))
    fixture_paths = fixture_paths_from_manifest(manifest, root)

    for case in indexed_cases.values():
        fixture_id = case.get("fixture_id")
        if not isinstance(fixture_id, str) or fixture_id not in fixture_paths:
            raise EvaluationCaseError(f"unknown fixture_id {fixture_id!r}")
        validate_expected_tables_against_fixture(
            case, load_json(fixture_paths[fixture_id]), fixture_id
        )


def manifest_root_for_cases_path(cases_path: Path) -> Path:
    if cases_path.parent.name == "gold" and cases_path.parent.parent.name == "datasets":
        return cases_path.parent.parent.parent
    return Path.cwd()


def evaluate_cases(data: dict[str, Any], manifest_root: Path | None = None) -> EvaluationMetrics:
    validate_schema_version(data)
    validate_scope(data)
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise EvaluationCaseError("cases must be a list")
    if not cases:
        raise EvaluationCaseError("cases must contain at least one evaluation case")
    validate_case_fixtures(data, cases, manifest_root)

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
        expected_tables = required_tables_by_id(
            case.get("expected", {}), f"case {case.get('id')!r}: expected"
        )
        actual_tables = tables_by_id(case.get("actual", {}))

        expected_table_count += len(expected_tables)
        matched_table_count += len(set(expected_tables) & set(actual_tables))

        for table_id, expected_table in expected_tables.items():
            actual_table = actual_tables.get(table_id, {"cells": []})
            expected_cells = required_cells_by_id(
                expected_table, f"case {case.get('id')!r}: expected table {table_id!r}"
            )
            actual_cells = cells_by_id(actual_table)
            validate_actual_cells(actual_cells, case.get("id"))
            expected_cell_count += len(expected_cells)

            for cell_id in expected_cells:
                expected_cell = expected_cells[cell_id]
                expected_has_source_anchor = is_valid_source_anchor(expected_cell.get("source"))
                if expected_has_source_anchor:
                    expected_source_link_count += 1

                actual_cell = actual_cells.get(cell_id)
                if actual_cell is None:
                    continue

                expected_text = normalized_text(expected_cell.get("text", ""))
                actual_text = normalized_text(
                    actual_cell_text(
                        actual_cell,
                        f"case {case.get('id')!r}: actual cell {cell_id!r}",
                    )
                )
                if expected_text == actual_text:
                    matched_cell_count += 1

                if expected_has_source_anchor and source_matches(expected_cell, actual_cell):
                    matched_source_link_count += 1

                if (
                    expected_cell.get("requires_review") is True
                    and actual_auto_confirmed(
                        actual_cell, f"case {case.get('id')!r}: actual cell {cell_id!r}"
                    )
                ):
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


def confirmed_values_fingerprint(values: object, run_context: str) -> str:
    if not isinstance(values, list):
        raise EvaluationCaseError(f"{run_context}: confirmed_values must be a list")
    if not values:
        raise EvaluationCaseError(f"{run_context}: confirmed_values must contain at least one value")

    indexed: dict[str, str] = {}
    for index, value in enumerate(values):
        context = f"{run_context}: confirmed_values[{index}]"
        if not isinstance(value, dict):
            raise EvaluationCaseError(f"{context} must be an object")
        value_id = value.get("id")
        if not isinstance(value_id, str) or not value_id.strip():
            raise EvaluationCaseError(f"{context}.id must be a non-empty string")
        if value_id in indexed:
            raise EvaluationCaseError(f"{run_context}: duplicate confirmed value id {value_id!r}")
        confirmed_value = value.get("value")
        if not isinstance(confirmed_value, str) or not normalized_text(confirmed_value):
            raise EvaluationCaseError(f"{context}.value must be a non-empty string")
        if value.get("auto_confirmed") is not True:
            raise EvaluationCaseError(f"{context}.auto_confirmed must be true")
        indexed[value_id] = normalized_text(confirmed_value)
    return canonical_json(indexed)


def most_common_fingerprint(fingerprints: list[str]) -> str:
    counts = Counter(fingerprints)
    return sorted(counts, key=lambda fingerprint: (-counts[fingerprint], fingerprint))[0]


def validate_llm_stability_source_kind(conversion_plan: dict[str, Any], run_context: str) -> None:
    source_kind = conversion_plan.get("source_kind")
    if source_kind not in PUBLIC_LLM_STABILITY_SOURCE_KINDS:
        raise EvaluationCaseError(
            f"{run_context}.conversion_plan.source_kind must be public synthetic or anonymized text"
        )


def evaluate_llm_stability(data: dict[str, Any]) -> LLMStabilityMetrics:
    if data.get("schema_version") != LLM_STABILITY_RUNS_SCHEMA_VERSION:
        raise EvaluationCaseError(
            f"unsupported LLM stability schema_version {data.get('schema_version')!r}"
        )
    validate_llm_stability_scope(data)

    input_id = data.get("input_id")
    if not isinstance(input_id, str) or not input_id.strip():
        raise EvaluationCaseError("input_id must be a non-empty string")
    expected_run_count = data.get("n")
    if not isinstance(expected_run_count, int) or isinstance(expected_run_count, bool):
        raise EvaluationCaseError("n must be an integer")
    if expected_run_count < 2:
        raise EvaluationCaseError("n must be at least 2 to measure stability")
    runs = data.get("runs")
    if not isinstance(runs, list) or len(runs) != expected_run_count:
        raise EvaluationCaseError("runs length must match n")

    seen_run_ids: set[str] = set()
    plan_fingerprints: list[str] = []
    value_fingerprints: list[str] = []
    for index, run in enumerate(runs):
        run_context = f"run[{index}]"
        if not isinstance(run, dict):
            raise EvaluationCaseError(f"{run_context} must be an object")
        run_id = run.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise EvaluationCaseError(f"{run_context}.run_id must be a non-empty string")
        if run_id in seen_run_ids:
            raise EvaluationCaseError(f"duplicate run_id {run_id!r}")
        seen_run_ids.add(run_id)

        conversion_plan = run.get("conversion_plan")
        try:
            validate_conversion_plan(conversion_plan)
        except ConversionPlanValidationError as exc:
            raise EvaluationCaseError(f"{run_context}.conversion_plan is invalid: {exc}") from exc
        assert isinstance(conversion_plan, dict)
        validate_llm_stability_source_kind(conversion_plan, run_context)
        plan_fingerprints.append(canonical_json(conversion_plan))
        value_fingerprints.append(
            confirmed_values_fingerprint(run.get("confirmed_values"), run_context)
        )

    reference_plan = most_common_fingerprint(plan_fingerprints)
    reference_values = most_common_fingerprint(value_fingerprints)
    plan_matches = sum(fingerprint == reference_plan for fingerprint in plan_fingerprints)
    value_matches = sum(fingerprint == reference_values for fingerprint in value_fingerprints)
    reference_run_id = min(
        str(run["run_id"])
        for run, plan_fingerprint in zip(runs, plan_fingerprints)
        if plan_fingerprint == reference_plan
    )

    unstable_examples: list[dict[str, str]] = []
    for run, plan_fingerprint, value_fingerprint in sorted(
        zip(runs, plan_fingerprints, value_fingerprints),
        key=lambda item: str(item[0]["run_id"]),
    ):
        assert isinstance(run, dict)
        changes: list[str] = []
        if plan_fingerprint != reference_plan:
            changes.append("conversion_plan")
        if value_fingerprint != reference_values:
            changes.append("confirmed_values")
        if changes:
            unstable_examples.append(
                {
                    "reference_run_id": reference_run_id,
                    "run_id": str(run["run_id"]),
                    "changed": ",".join(changes),
                }
            )

    return LLMStabilityMetrics(
        input_id=input_id,
        run_count=expected_run_count,
        plan_agreement_rate=ratio(plan_matches, expected_run_count),
        confirmed_value_agreement_rate=ratio(value_matches, expected_run_count),
        distinct_plan_count=len(set(plan_fingerprints)),
        distinct_confirmed_value_count=len(set(value_fingerprints)),
        unstable_example_count=len(unstable_examples),
        unstable_examples=tuple(unstable_examples[:3]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_EVALUATION_CASES)
    parser.add_argument(
        "--llm-stability-runs",
        type=Path,
        help="Measure N-run LLM output stability from a public synthetic run record.",
    )
    args = parser.parse_args()

    try:
        if args.llm_stability_runs is not None:
            metrics = evaluate_llm_stability(load_json(args.llm_stability_runs.resolve()))
        else:
            cases_path = args.cases.resolve()
            metrics = evaluate_cases(
                load_json(cases_path),
                manifest_root=manifest_root_for_cases_path(cases_path),
            )
    except (OSError, json.JSONDecodeError, EvaluationCaseError) as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(metrics.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
