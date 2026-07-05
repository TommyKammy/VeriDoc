#!/usr/bin/env python3
"""Compute Phase 0 evaluation metrics for public VeriDoc fixtures."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.llm.conversion_plan import ConversionPlanValidationError, validate_conversion_plan


DEFAULT_EVALUATION_CASES = Path("datasets/gold/evaluation_cases_v0.json")
DEFAULT_LLM_STABILITY_RUNS = Path("datasets/gold/llm_stability_runs_v0.json")
DEFAULT_POC_COMPARISON = Path("datasets/gold/poc_mode_comparison_v1.json")
DEFAULT_GMP_ACCEPTANCE = Path("datasets/gold/gmp_acceptance_v1.json")
DEFAULT_P9_HARNESS_MANIFEST = Path("datasets/fixtures/manifest.json")
EVALUATION_CASES_SCHEMA_VERSION = "veridoc-evaluation-cases/v0"
LLM_STABILITY_RUNS_SCHEMA_VERSION = "veridoc-llm-stability-runs/v0"
POC_MODE_COMPARISON_SCHEMA_VERSION = "veridoc-poc-mode-comparison/v1"
GMP_ACCEPTANCE_SCHEMA_VERSION = "veridoc-gmp-acceptance/v1"
P9_HARNESS_SCHEMA_VERSION = "veridoc-p9-poc-evaluation-harness/v0"
HIGH_RISK_LABELS_SCHEMA_VERSION = "veridoc-high-risk-labels/v0"
FIXTURE_MANIFEST_SCHEMA_VERSION = "veridoc-eval-fixtures/v0"
FIXTURE_SCHEMA_VERSION = "veridoc-evaluation-fixture/v0"
EXPECTED_ALLOWED_FIXTURE_ROOT = Path("datasets/fixtures")
EXPECTED_DATASET_MANIFEST = EXPECTED_ALLOWED_FIXTURE_ROOT / "manifest.json"
EXPECTED_EVALUATION_CASES = Path("datasets/gold/evaluation_cases_v0.json")
EXPECTED_HIGH_RISK_LABELS = Path("datasets/gold/high_risk_labels_v0.json")
EXPECTED_POC_COMPARISON = Path("datasets/gold/poc_mode_comparison_v1.json")
EXPECTED_GMP_ACCEPTANCE_COMMAND = (
    "python3 scripts/evaluate_dataset.py --gmp-acceptance "
    "datasets/gold/gmp_acceptance_v1.json"
)
GMP_ACCEPTANCE_PUBLIC_EVIDENCE_ROOTS = (
    Path("datasets/fixtures"),
    Path("datasets/gold"),
    Path("docs"),
    Path("scripts"),
    Path("tests"),
)
GMP_ACCEPTANCE_VERIFICATION_SHELL_CONTROL_CHARS = frozenset(";&|<>()")
GMP_ACCEPTANCE_VERIFICATION_SHELL_EXPANSION_MARKERS = (
    "$",
    "`",
    "*",
    "?",
    "[",
    "]",
    "{",
    "}",
    "~",
)
GMP_ACCEPTANCE_VERIFICATION_PATH_OPTION_NAMES = frozenset(
    (
        "--basetemp",
        "--cache-dir",
        "--confcutdir",
        "--junitxml",
        "--rootdir",
    )
)
GMP_ACCEPTANCE_VERIFICATION_PATH_ENV_NAMES = frozenset(("PYTHONHOME",))
GMP_ACCEPTANCE_VERIFICATION_ALLOWED_PYTHON_MODULES = frozenset(("pytest",))
EXPECTED_GMP_ACCEPTANCE_SOD_SCOPE = (
    "review approval flows with authenticated actor identity"
)
EXPECTED_GMP_ACCEPTANCE_SOD_NO_AUTH_NOTE = "no-auth approval attempts are forbidden"
EXPECTED_SCOPE_PHASE = "phase0"
PUBLIC_FIXTURE_ANONYMIZATION_VALUES = {"anonymized", "synthetic"}
PUBLIC_LLM_STABILITY_SOURCE_KINDS = {"anonymized_text", "synthetic_text"}
REQUIRED_POC_MODES = ("no_llm", "standard", "high_quality")
P9_REPRESENTATIVE_FLAGS_BY_MODE = {
    "word_to_excel": "word_to_excel_representative",
    "excel_to_word": "excel_to_word_representative",
    "pdf_to_excel": "pdf_to_excel_representative",
    "pdf_to_word": "pdf_to_word_representative",
    "scanned_pdf_ocr": "scanned_pdf_ocr_representative",
}
P9_EXPECTATION_KEYS_BY_MODE = {
    "word_to_excel": "word_to_excel_expectations",
    "excel_to_word": "excel_to_word_expectations",
    "pdf_to_excel": "pdf_to_excel_expectations",
    "pdf_to_word": "pdf_to_word_expectations",
    "scanned_pdf_ocr": "scanned_pdf_ocr_expectations",
}
P9_CONVERSION_MODE_BY_MODE = {
    "word_to_excel": "word_to_excel",
    "excel_to_word": "excel_to_word",
    "pdf_to_excel": "pdf_to_excel",
    "pdf_to_word": "pdf_to_word",
    "scanned_pdf_ocr": "pdf_to_word",
}
P9_LLM_SCENARIOS = ("no_llm", "llm_requested")
REQUIRED_GMP_ACCEPTANCE_CRITERIA = (
    "high_risk_review",
    "missed_detection_zero",
    "source_traceability",
    "originality",
    "audit_trail",
    "completeness",
    "reproducibility",
    "segregation_of_duties",
)
HighRiskLabelKey = tuple[str, str, str]


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
    schema_failure_rate: float
    repair_success_rate: float
    deterministic_fallback_rate: float
    external_ai_api_guard_violation_count: int
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
            "schema_failure_rate": self.schema_failure_rate,
            "repair_success_rate": self.repair_success_rate,
            "deterministic_fallback_rate": self.deterministic_fallback_rate,
            "external_ai_api_guard_violation_count": (
                self.external_ai_api_guard_violation_count
            ),
            "distinct_plan_count": self.distinct_plan_count,
            "distinct_confirmed_value_count": self.distinct_confirmed_value_count,
            "unstable_example_count": self.unstable_example_count,
            "unstable_examples": list(self.unstable_examples),
        }


@dataclass(frozen=True)
class PoCModeMetrics:
    mode: str
    table_extraction_rate: float
    cell_match_rate: float
    source_linkage_rate: float
    high_risk_false_auto_confirmed_count: int
    requires_review_count: int
    warning_count: int

    def as_dict(self) -> dict[str, int | float | str]:
        return {
            "mode": self.mode,
            "table_extraction_rate": self.table_extraction_rate,
            "cell_match_rate": self.cell_match_rate,
            "source_linkage_rate": self.source_linkage_rate,
            "high_risk_false_auto_confirmed_count": self.high_risk_false_auto_confirmed_count,
            "requires_review_count": self.requires_review_count,
            "warning_count": self.warning_count,
        }


@dataclass(frozen=True)
class ManualCorrectionTimeMetrics:
    measurement_method: str
    baseline_minutes: float
    assisted_minutes: float
    reduction_minutes: float
    reduction_rate: float
    target_reduction_rate: float
    target_met: bool

    def as_dict(self) -> dict[str, bool | float | str]:
        return {
            "measurement_method": self.measurement_method,
            "baseline_minutes": self.baseline_minutes,
            "assisted_minutes": self.assisted_minutes,
            "reduction_minutes": self.reduction_minutes,
            "reduction_rate": self.reduction_rate,
            "target_reduction_rate": self.target_reduction_rate,
            "target_met": self.target_met,
        }


@dataclass(frozen=True)
class PoCComparisonMetrics:
    mode_count: int
    high_risk_false_auto_confirmed_count: int
    high_risk_false_auto_confirmed_target: int
    target_met: bool
    manual_correction_time: ManualCorrectionTimeMetrics
    modes: tuple[PoCModeMetrics, ...]
    mode_diffs: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "mode_count": self.mode_count,
            "required_modes": list(REQUIRED_POC_MODES),
            "high_risk_false_auto_confirmed_count": self.high_risk_false_auto_confirmed_count,
            "high_risk_false_auto_confirmed_target": self.high_risk_false_auto_confirmed_target,
            "target_met": self.target_met,
            "manual_correction_time": self.manual_correction_time.as_dict(),
            "modes": [mode.as_dict() for mode in self.modes],
            "mode_diffs": list(self.mode_diffs),
        }


@dataclass(frozen=True)
class LLMStabilityEvaluationReport:
    llm_stability: LLMStabilityMetrics
    poc_mode_comparison: PoCComparisonMetrics
    stability_source: Path
    poc_comparison_source: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "veridoc-llm-stability-evaluation/v0",
            "phase9_handoff": {
                "stability_source": str(self.stability_source),
                "poc_comparison_source": str(self.poc_comparison_source),
                "notes": (
                    "Public synthetic minimal harness for plan drift, schema/repair/"
                    "fallback rates, review/warning diffs, and external AI API guard "
                    "violations."
                ),
            },
            "llm_stability": self.llm_stability.as_dict(),
            "poc_mode_comparison": self.poc_mode_comparison.as_dict(),
        }


@dataclass(frozen=True)
class P9HarnessReport:
    manifest: Path
    results: tuple[dict[str, object], ...]
    llm_stability: LLMStabilityMetrics
    poc_mode_comparison: PoCComparisonMetrics

    @property
    def failure_count(self) -> int:
        return sum(1 for result in self.results if not result["ok"])

    @property
    def completed_count(self) -> int:
        return sum(1 for result in self.results if result["ok"])

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": P9_HARNESS_SCHEMA_VERSION,
            "dataset_manifest": str(self.manifest),
            "summary": {
                "case_count": len(self.results),
                "completed_count": self.completed_count,
                "failure_count": self.failure_count,
                "conversion_modes": sorted(
                    {str(result["conversion_mode"]) for result in self.results}
                ),
                "llm_scenarios": list(P9_LLM_SCENARIOS),
                "external_ai_api_guard_violation_count": sum(
                    1
                    for result in self.results
                    if result["external_ai_api_guard_violation"]
                )
                + self.llm_stability.external_ai_api_guard_violation_count,
            },
            "results": list(self.results),
            "phase8_comparison": {
                "llm_stability": self.llm_stability.as_dict(),
                "poc_mode_comparison": self.poc_mode_comparison.as_dict(),
            },
        }


@dataclass(frozen=True)
class GmpAcceptanceMetrics:
    poc_comparison: str
    criterion_count: int
    failed_criterion_count: int
    high_risk_false_auto_confirmed_count: int
    high_risk_false_auto_confirmed_target: int
    target_met: bool
    criteria: tuple[dict[str, object], ...]
    failed_criteria: tuple[dict[str, object], ...]
    verification_commands: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "poc_comparison": self.poc_comparison,
            "criterion_count": self.criterion_count,
            "failed_criterion_count": self.failed_criterion_count,
            "high_risk_false_auto_confirmed_count": self.high_risk_false_auto_confirmed_count,
            "high_risk_false_auto_confirmed_target": (
                self.high_risk_false_auto_confirmed_target
            ),
            "target_met": self.target_met,
            "criteria": list(self.criteria),
            "failed_criteria": list(self.failed_criteria),
            "verification_commands": list(self.verification_commands),
        }


@dataclass(frozen=True)
class PoCCapturedHighRiskEvidence:
    actual_values_by_label: dict[HighRiskLabelKey, set[str]]
    auto_confirmed_labels: frozenset[HighRiskLabelKey]


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


def normalized_text_contains(container: object, candidate: object) -> bool:
    normalized_container = normalized_text(container)
    normalized_candidate = normalized_text(candidate)
    return bool(normalized_candidate) and normalized_candidate in normalized_container


def values_match_authoritative(expected: object, actual: object) -> bool:
    if is_number(expected) and is_number(actual):
        return expected == actual
    if type(actual) is not type(expected):
        return False
    return actual == expected


def source_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    expected_source = expected.get("source")
    actual_source = actual.get("source")
    return (
        is_valid_source_anchor(expected_source)
        and is_valid_source_anchor(actual_source)
        and expected_source == actual_source
    )


def source_contains(container: object, candidate: object) -> bool:
    if not is_valid_source_anchor(container) or not is_valid_source_anchor(candidate):
        return False
    assert isinstance(container, dict)
    assert isinstance(candidate, dict)
    if container["source_page"] != candidate["source_page"]:
        return False
    container_bbox = container["bbox"]
    candidate_bbox = candidate["bbox"]
    return (
        candidate_bbox["x"] >= container_bbox["x"]
        and candidate_bbox["y"] >= container_bbox["y"]
        and candidate_bbox["x"] + candidate_bbox["width"]
        <= container_bbox["x"] + container_bbox["width"]
        and candidate_bbox["y"] + candidate_bbox["height"]
        <= container_bbox["y"] + container_bbox["height"]
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


def p9_manifest_repo_root(manifest_path: Path) -> Path:
    if (
        manifest_path.name == "manifest.json"
        and manifest_path.parent.name == "fixtures"
        and manifest_path.parent.parent.name == "datasets"
    ):
        return manifest_path.parent.parent.parent
    return manifest_path.parent


def p9_representative_fixtures(manifest: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    fixtures = manifest.get("fixtures")
    if not isinstance(fixtures, list):
        raise EvaluationCaseError("fixture manifest must define a fixtures list")

    representatives: dict[str, list[dict[str, Any]]] = {
        mode: [] for mode in P9_REPRESENTATIVE_FLAGS_BY_MODE
    }
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            raise EvaluationCaseError("each fixture manifest entry needs an object")
        for mode, flag in P9_REPRESENTATIVE_FLAGS_BY_MODE.items():
            if fixture.get(flag) is True:
                representatives[mode].append(fixture)

    for mode, mode_fixtures in representatives.items():
        if mode_fixtures:
            continue
        if mode == "scanned_pdf_ocr":
            representatives[mode].append(
                {
                    "id": "scanned-pdf-ocr-representative-missing",
                    "title": "Scanned PDF/OCR representative fixture unavailable",
                    "path": None,
                    "source_type": "scanned_pdf",
                    "format": "pdf",
                    "public_review_safe": True,
                    "confidentiality": "public",
                    "anonymization": "pending_synthetic_fixture",
                }
            )
            continue
        raise EvaluationCaseError(f"P9 manifest has no representative for {mode}")
    return representatives


def p9_result_for_unavailable_fixture(
    fixture: dict[str, Any],
    *,
    mode: str,
    llm_scenario: str,
    failure_reason: str,
) -> dict[str, object]:
    conversion_mode = P9_CONVERSION_MODE_BY_MODE[mode]
    return {
        "fixture_id": fixture.get("id"),
        "title": fixture.get("title"),
        "source_type": fixture.get("source_type"),
        "format": fixture.get("format"),
        "path": fixture.get("path"),
        "conversion_mode": conversion_mode,
        "representative_mode": mode,
        "llm_scenario": llm_scenario,
        "llm_requested": llm_scenario == "llm_requested",
        "ocr_requested": mode == "scanned_pdf_ocr",
        "ok": False,
        "ir_generated": False,
        "artifact_generated": False,
        "artifact_count": 0,
        "warnings_count": 0,
        "review_items_count": 0,
        "audit_present": False,
        "processing_time_ms": 0.0,
        "failure_reason": failure_reason,
        "llm_status": "not_run",
        "llm_fallback_used": False,
        "use_ocr_status": "not_run",
        "external_ai_api_guard_violation": False,
    }


def p9_external_ai_api_guard_violation(audit: dict[str, Any] | None) -> bool:
    if not isinstance(audit, dict):
        return False
    llm_audit = audit.get("llm")
    if not isinstance(llm_audit, dict):
        return False
    if llm_audit.get("enabled") is not True:
        return False
    return llm_audit.get("base_url_type") != "local"


def p9_conversion_result(
    fixture: dict[str, Any],
    *,
    fixture_path: Path,
    mode: str,
    llm_scenario: str,
) -> dict[str, object]:
    from services.api.poc_web import convert_uploaded_document

    conversion_mode = P9_CONVERSION_MODE_BY_MODE[mode]
    llm_requested = llm_scenario == "llm_requested"
    ocr_requested = mode == "scanned_pdf_ocr"
    started_at = time.perf_counter()
    try:
        converted = convert_uploaded_document(
            filename=fixture_path.name,
            content=fixture_path.read_bytes(),
            conversion_mode=conversion_mode,
            use_llm=llm_requested,
            use_ocr=ocr_requested,
        )
        failure_reason = None
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        return {
            **p9_result_for_unavailable_fixture(
                fixture,
                mode=mode,
                llm_scenario=llm_scenario,
                failure_reason=f"{type(exc).__name__}: {exc}",
            ),
            "processing_time_ms": round(elapsed_ms, 3),
        }

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    audit = converted.get("audit") if isinstance(converted.get("audit"), dict) else None
    conversion_settings = audit.get("conversion_settings", {}) if audit else {}
    use_llm = conversion_settings.get("use_llm", {})
    use_ocr = conversion_settings.get("use_ocr", {})
    conversion_plan = audit.get("conversion_plan", {}) if audit else {}
    artifacts = converted.get("artifacts")
    artifact_list = artifacts if isinstance(artifacts, list) else []
    primary_artifact_count = sum(
        1
        for artifact in artifact_list
        if isinstance(artifact, dict) and artifact.get("kind") == "primary"
    )
    warnings = converted.get("warnings", [])
    review_items = converted.get("review_items", [])
    return {
        "fixture_id": fixture.get("id"),
        "title": fixture.get("title"),
        "source_type": fixture.get("source_type"),
        "format": fixture.get("format"),
        "path": fixture.get("path"),
        "conversion_mode": conversion_mode,
        "representative_mode": mode,
        "llm_scenario": llm_scenario,
        "llm_requested": llm_requested,
        "ocr_requested": ocr_requested,
        "ok": True,
        "ir_generated": isinstance(converted.get("document_ir"), dict),
        "artifact_generated": primary_artifact_count > 0,
        "artifact_count": len(artifact_list),
        "warnings_count": len(warnings) if isinstance(warnings, list) else 0,
        "review_items_count": len(review_items) if isinstance(review_items, list) else 0,
        "audit_present": audit is not None,
        "processing_time_ms": round(elapsed_ms, 3),
        "failure_reason": failure_reason,
        "llm_status": use_llm.get("status") if isinstance(use_llm, dict) else None,
        "llm_fallback_used": (
            isinstance(conversion_plan, dict)
            and conversion_plan.get("status") == "fallback"
        ),
        "use_ocr_status": use_ocr.get("status") if isinstance(use_ocr, dict) else None,
        "external_ai_api_guard_violation": p9_external_ai_api_guard_violation(audit),
    }


def evaluate_p9_harness(
    manifest_path: Path = DEFAULT_P9_HARNESS_MANIFEST,
    *,
    llm_stability_runs_path: Path = DEFAULT_LLM_STABILITY_RUNS,
    poc_comparison_path: Path = DEFAULT_POC_COMPARISON,
) -> P9HarnessReport:
    resolved_manifest = manifest_path.resolve()
    repo_root = p9_manifest_repo_root(resolved_manifest)
    manifest = load_json(resolved_manifest)
    fixture_paths = fixture_paths_from_manifest(manifest, repo_root)
    representatives = p9_representative_fixtures(manifest)

    results: list[dict[str, object]] = []
    for mode, mode_fixtures in representatives.items():
        for fixture in mode_fixtures:
            fixture_id = fixture.get("id")
            fixture_path = fixture_paths.get(fixture_id) if isinstance(fixture_id, str) else None
            for llm_scenario in P9_LLM_SCENARIOS:
                if fixture_path is None:
                    results.append(
                        p9_result_for_unavailable_fixture(
                            fixture,
                            mode=mode,
                            llm_scenario=llm_scenario,
                            failure_reason="representative fixture path is unavailable",
                        )
                    )
                    continue
                results.append(
                    p9_conversion_result(
                        fixture,
                        fixture_path=fixture_path,
                        mode=mode,
                        llm_scenario=llm_scenario,
                    )
                )

    llm_report = evaluate_llm_stability_report(
        llm_stability_runs_path,
        poc_comparison_path,
    )
    return P9HarnessReport(
        manifest=manifest_path,
        results=tuple(results),
        llm_stability=llm_report.llm_stability,
        poc_mode_comparison=llm_report.poc_mode_comparison,
    )


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


def repository_root_for_gold_path(gold_path: Path) -> Path:
    if gold_path.parent.name == "gold" and gold_path.parent.parent.name == "datasets":
        return gold_path.parent.parent.parent
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
    if len(values) == 0:
        raise EvaluationCaseError(
            f"{run_context}: confirmed_values must contain at least one public confirmed value"
        )

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


def reference_run_metadata(
    runs: list[object],
    plan_fingerprints: list[str],
    value_fingerprints: list[str],
    reference_plan: str,
    reference_values: str,
) -> dict[str, str]:
    joint_reference_run_ids = [
        str(run["run_id"])
        for run, plan_fingerprint, value_fingerprint in zip(
            runs, plan_fingerprints, value_fingerprints
        )
        if (
            isinstance(run, dict)
            and plan_fingerprint == reference_plan
            and value_fingerprint == reference_values
        )
    ]
    if joint_reference_run_ids:
        return {"reference_run_id": min(joint_reference_run_ids)}

    plan_reference_run_ids = [
        str(run["run_id"])
        for run, plan_fingerprint in zip(runs, plan_fingerprints)
        if isinstance(run, dict) and plan_fingerprint == reference_plan
    ]
    value_reference_run_ids = [
        str(run["run_id"])
        for run, value_fingerprint in zip(runs, value_fingerprints)
        if isinstance(run, dict) and value_fingerprint == reference_values
    ]
    return {
        "reference_plan_run_id": min(plan_reference_run_ids),
        "reference_confirmed_values_run_id": min(value_reference_run_ids),
    }


def validate_llm_stability_source_kind(conversion_plan: dict[str, Any], run_context: str) -> None:
    source_kind = conversion_plan.get("source_kind")
    if (
        not isinstance(source_kind, str)
        or source_kind not in PUBLIC_LLM_STABILITY_SOURCE_KINDS
    ):
        raise EvaluationCaseError(
            f"{run_context}.conversion_plan.source_kind must be public-only synthetic or anonymized text"
        )


def validate_llm_run_outcome(run: dict[str, Any], run_context: str) -> dict[str, bool]:
    outcome = run.get("outcome")
    if outcome is None:
        outcome = {
            "schema_validation_passed": True,
            "repair_attempted": False,
            "repair_succeeded": False,
            "deterministic_fallback_used": False,
            "external_ai_api_transmission_attempted": False,
        }
    if not isinstance(outcome, dict):
        raise EvaluationCaseError(f"{run_context}.outcome must be an object")

    normalized: dict[str, bool] = {}
    for field in (
        "schema_validation_passed",
        "repair_attempted",
        "repair_succeeded",
        "deterministic_fallback_used",
        "external_ai_api_transmission_attempted",
    ):
        value = outcome.get(field)
        if not isinstance(value, bool):
            raise EvaluationCaseError(f"{run_context}.outcome.{field} must be a boolean")
        normalized[field] = value

    schema_passed = normalized["schema_validation_passed"]
    repair_attempted = normalized["repair_attempted"]
    repair_succeeded = normalized["repair_succeeded"]
    fallback_used = normalized["deterministic_fallback_used"]

    if repair_succeeded and not repair_attempted:
        raise EvaluationCaseError(
            f"{run_context}.outcome.repair_succeeded requires repair_attempted"
        )
    if schema_passed and (repair_attempted or repair_succeeded):
        raise EvaluationCaseError(
            f"{run_context}.outcome repair fields require a schema validation failure"
        )
    if schema_passed and fallback_used:
        raise EvaluationCaseError(
            f"{run_context}.outcome deterministic fallback requires a schema validation failure"
        )
    if not schema_passed and repair_succeeded and fallback_used:
        raise EvaluationCaseError(
            f"{run_context}.outcome cannot both repair successfully and use "
            "deterministic fallback"
        )
    if not schema_passed and not repair_succeeded and not fallback_used:
        raise EvaluationCaseError(
            f"{run_context}.outcome schema failures must be repaired or use deterministic fallback"
        )
    return normalized


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
    schema_failure_count = 0
    repair_attempt_count = 0
    repair_success_count = 0
    deterministic_fallback_count = 0
    external_ai_api_guard_violation_count = 0
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
        outcome = validate_llm_run_outcome(run, run_context)
        if not outcome["schema_validation_passed"]:
            schema_failure_count += 1
        if outcome["repair_attempted"]:
            repair_attempt_count += 1
        if outcome["repair_succeeded"]:
            repair_success_count += 1
        if outcome["deterministic_fallback_used"]:
            deterministic_fallback_count += 1
        if outcome["external_ai_api_transmission_attempted"]:
            external_ai_api_guard_violation_count += 1

    reference_plan = most_common_fingerprint(plan_fingerprints)
    reference_values = most_common_fingerprint(value_fingerprints)
    plan_matches = sum(fingerprint == reference_plan for fingerprint in plan_fingerprints)
    value_matches = sum(fingerprint == reference_values for fingerprint in value_fingerprints)
    reference_metadata = reference_run_metadata(
        runs,
        plan_fingerprints,
        value_fingerprints,
        reference_plan,
        reference_values,
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
                    **reference_metadata,
                    "run_id": str(run["run_id"]),
                    "changed": ",".join(changes),
                }
            )

    return LLMStabilityMetrics(
        input_id=input_id,
        run_count=expected_run_count,
        plan_agreement_rate=ratio(plan_matches, expected_run_count),
        confirmed_value_agreement_rate=ratio(value_matches, expected_run_count),
        schema_failure_rate=ratio(schema_failure_count, expected_run_count),
        repair_success_rate=ratio(repair_success_count, repair_attempt_count),
        deterministic_fallback_rate=ratio(
            deterministic_fallback_count, expected_run_count
        ),
        external_ai_api_guard_violation_count=external_ai_api_guard_violation_count,
        distinct_plan_count=len(set(plan_fingerprints)),
        distinct_confirmed_value_count=len(set(value_fingerprints)),
        unstable_example_count=len(unstable_examples),
        unstable_examples=tuple(unstable_examples[:3]),
    )


def validate_ratio_metric(value: object, context: str) -> float:
    if not is_number(value):
        raise EvaluationCaseError(f"{context} must be a finite number")
    metric = float(value)
    if metric < 0.0 or metric > 1.0:
        raise EvaluationCaseError(f"{context} must be between 0.0 and 1.0")
    return metric


def validate_non_negative_int(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EvaluationCaseError(f"{context} must be a non-negative integer")
    return value


def validate_positive_minutes(value: object, context: str) -> float:
    if not is_number(value):
        raise EvaluationCaseError(f"{context} must be a finite number")
    minutes = float(value)
    if minutes <= 0.0:
        raise EvaluationCaseError(f"{context} must be greater than 0")
    return minutes


def validate_non_negative_minutes(value: object, context: str) -> float:
    if not is_number(value):
        raise EvaluationCaseError(f"{context} must be a finite number")
    minutes = float(value)
    if minutes < 0.0:
        raise EvaluationCaseError(f"{context} must be non-negative")
    return minutes


def evaluate_manual_correction_time(data: dict[str, Any]) -> ManualCorrectionTimeMetrics:
    record = data.get("manual_correction_time")
    if not isinstance(record, dict):
        raise EvaluationCaseError("PoC comparison must define manual_correction_time")

    method = record.get("measurement_method")
    if not isinstance(method, str) or not normalized_text(method):
        raise EvaluationCaseError("manual_correction_time.measurement_method must be non-empty")

    baseline_minutes = validate_positive_minutes(
        record.get("baseline_minutes"),
        "manual_correction_time.baseline_minutes",
    )
    assisted_minutes = validate_non_negative_minutes(
        record.get("assisted_minutes"),
        "manual_correction_time.assisted_minutes",
    )
    target_reduction_rate = validate_ratio_metric(
        record.get("target_reduction_rate"),
        "manual_correction_time.target_reduction_rate",
    )
    reduction_minutes = baseline_minutes - assisted_minutes
    reduction_rate = ratio(reduction_minutes, baseline_minutes)
    return ManualCorrectionTimeMetrics(
        measurement_method=normalized_text(method),
        baseline_minutes=baseline_minutes,
        assisted_minutes=assisted_minutes,
        reduction_minutes=reduction_minutes,
        reduction_rate=reduction_rate,
        target_reduction_rate=target_reduction_rate,
        target_met=reduction_rate >= target_reduction_rate,
    )


def high_risk_labels_path_from_comparison(data: dict[str, Any], repo_root: Path) -> Path:
    labels_path = data.get("high_risk_labels")
    if not isinstance(labels_path, str) or not labels_path:
        raise EvaluationCaseError("high_risk_labels must be a non-empty string")
    path = Path(labels_path)
    if path.is_absolute() or path != EXPECTED_HIGH_RISK_LABELS:
        raise EvaluationCaseError("high_risk_labels must be datasets/gold/high_risk_labels_v0.json")
    return repo_root / path


def evaluation_cases_path_from_comparison(data: dict[str, Any], repo_root: Path) -> Path:
    cases_path = data.get("evaluation_cases")
    if not isinstance(cases_path, str) or not cases_path:
        raise EvaluationCaseError("evaluation_cases must be a non-empty string")
    path = Path(cases_path)
    if path.is_absolute() or path != EXPECTED_EVALUATION_CASES:
        raise EvaluationCaseError("evaluation_cases must be datasets/gold/evaluation_cases_v0.json")
    return repo_root / path


def poc_comparison_path_from_gmp_acceptance(data: dict[str, Any], repo_root: Path) -> Path:
    comparison_path = data.get("poc_comparison")
    if not isinstance(comparison_path, str) or not comparison_path:
        raise EvaluationCaseError("poc_comparison must be a non-empty string")
    path = Path(comparison_path)
    if path.is_absolute() or path != EXPECTED_POC_COMPARISON:
        raise EvaluationCaseError(
            "poc_comparison must be datasets/gold/poc_mode_comparison_v1.json"
        )
    return repo_root / path


def high_risk_label_index(labels_data: dict[str, Any]) -> dict[HighRiskLabelKey, dict[str, Any]]:
    if labels_data.get("schema_version") != HIGH_RISK_LABELS_SCHEMA_VERSION:
        raise EvaluationCaseError(
            f"unsupported high-risk labels schema_version {labels_data.get('schema_version')!r}"
        )
    validate_scope(labels_data)
    if labels_data.get("dataset_manifest") != str(EXPECTED_DATASET_MANIFEST):
        raise EvaluationCaseError("high-risk labels dataset_manifest must match evaluation fixtures")
    items = labels_data.get("items")
    if not isinstance(items, list) or not items:
        raise EvaluationCaseError("high-risk labels must contain at least one item")
    taxonomy = labels_data.get("label_taxonomy")
    if not isinstance(taxonomy, list) or not taxonomy:
        raise EvaluationCaseError("high-risk labels must define label_taxonomy")
    taxonomy_ids: set[str] = set()
    for index, taxonomy_item in enumerate(taxonomy):
        context = f"high_risk_labels.label_taxonomy[{index}]"
        if not isinstance(taxonomy_item, dict):
            raise EvaluationCaseError(f"{context} must be an object")
        taxonomy_id = taxonomy_item.get("id")
        if not isinstance(taxonomy_id, str) or not taxonomy_id:
            raise EvaluationCaseError(f"{context}.id must be a non-empty string")
        if taxonomy_id in taxonomy_ids:
            raise EvaluationCaseError(f"duplicate high-risk label taxonomy id {taxonomy_id!r}")
        if taxonomy_item.get("risk_level") != "high":
            raise EvaluationCaseError(f"{context}.risk_level must be high")
        taxonomy_ids.add(taxonomy_id)

    indexed: dict[HighRiskLabelKey, dict[str, Any]] = {}
    for index, item in enumerate(items):
        context = f"high_risk_labels.items[{index}]"
        if not isinstance(item, dict):
            raise EvaluationCaseError(f"{context} must be an object")
        fixture_id = item.get("fixture_id")
        block_id = item.get("block_id")
        label_id = item.get("label_id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise EvaluationCaseError(f"{context}.fixture_id must be a non-empty string")
        if not isinstance(block_id, str) or not block_id:
            raise EvaluationCaseError(f"{context}.block_id must be a non-empty string")
        if not isinstance(label_id, str) or not label_id:
            raise EvaluationCaseError(f"{context}.label_id must be a non-empty string")
        if label_id not in taxonomy_ids:
            raise EvaluationCaseError(f"{context}.label_id must be declared in label_taxonomy")
        if "expected_value" not in item or item.get("expected_value") is None:
            raise EvaluationCaseError(f"{context}.expected_value must be defined")
        if item.get("risk_level") != "high":
            raise EvaluationCaseError(f"{context}.risk_level must be high")
        if item.get("requires_review") is not True:
            raise EvaluationCaseError(f"{context}.requires_review must be true")
        key = (fixture_id, block_id, label_id)
        if key in indexed:
            raise EvaluationCaseError(f"duplicate high-risk label {key!r}")
        indexed[key] = item
    return indexed


def fixture_blocks_by_id(fixture: dict[str, Any], fixture_id: str) -> dict[str, dict[str, Any]]:
    if fixture.get("schema_version") != FIXTURE_SCHEMA_VERSION:
        raise EvaluationCaseError(
            f"unsupported fixture schema_version {fixture.get('schema_version')!r}"
        )
    pages = pages_by_number(fixture, fixture_id)
    blocks = fixture.get("blocks")
    if not isinstance(blocks, list):
        raise EvaluationCaseError(f"fixture {fixture_id!r}: blocks must be a list")

    indexed: dict[str, dict[str, Any]] = {}
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("id"), str):
            raise EvaluationCaseError(f"fixture {fixture_id!r}: each block needs a string id")
        block_id = block["id"]
        if block_id in indexed:
            raise EvaluationCaseError(f"fixture {fixture_id!r}: duplicate block id {block_id!r}")
        validated_cell_text(block, f"fixture {fixture_id!r} block {block_id!r}")
        validate_source_anchor_on_page(
            block.get("source"), pages, f"fixture {fixture_id!r} block {block_id!r}"
        )
        indexed[block_id] = block
    return indexed


def validate_high_risk_labels_against_fixtures(
    labels: dict[HighRiskLabelKey, dict[str, Any]], fixture_paths: dict[str, Path]
) -> dict[HighRiskLabelKey, dict[str, Any]]:
    fixture_cache: dict[str, dict[str, Any]] = {}
    block_cache: dict[str, dict[str, dict[str, Any]]] = {}
    blocks_by_label: dict[HighRiskLabelKey, dict[str, Any]] = {}
    for label_key, label in labels.items():
        fixture_id, block_id, _label_id = label_key
        fixture_path = fixture_paths.get(fixture_id)
        if fixture_path is None:
            raise EvaluationCaseError(
                f"high-risk label {label_key!r} references fixture missing from dataset_manifest"
            )
        fixture = fixture_cache.get(fixture_id)
        if fixture is None:
            fixture = load_json(fixture_path)
            fixture_cache[fixture_id] = fixture

        document = fixture.get("document")
        if not isinstance(document, dict) or not isinstance(document.get("id"), str):
            raise EvaluationCaseError(f"fixture {fixture_id!r}: document.id must be a string")
        if label.get("document_id") != document["id"]:
            raise EvaluationCaseError(
                f"high-risk label {label_key!r} document_id must match fixture"
            )

        blocks = block_cache.get(fixture_id)
        if blocks is None:
            blocks = fixture_blocks_by_id(fixture, fixture_id)
            block_cache[fixture_id] = blocks
        block = blocks.get(block_id)
        if block is None:
            raise EvaluationCaseError(
                f"high-risk label {label_key!r} block_id is not present in fixture"
            )
        expected_text = label.get("expected_text")
        if not isinstance(expected_text, str) or not normalized_text(expected_text):
            raise EvaluationCaseError(f"high-risk label {label_key!r} expected_text is required")
        if normalized_text(expected_text) != normalized_text(block.get("text", "")):
            raise EvaluationCaseError(
                f"high-risk label {label_key!r} expected_text must match fixture block"
            )
        pages = pages_by_number(fixture, fixture_id)
        validate_source_anchor_on_page(
            label.get("evidence"), pages, f"high-risk label {label_key!r} evidence"
        )
        if label.get("evidence") != block.get("source"):
            raise EvaluationCaseError(
                f"high-risk label {label_key!r} evidence must match fixture block source"
            )
        blocks_by_label[label_key] = block
    return blocks_by_label


def validate_poc_high_risk_item_against_label(
    item: dict[str, Any],
    labels: dict[HighRiskLabelKey, dict[str, Any]],
    context: str,
) -> bool:
    fixture_id = item.get("fixture_id")
    block_id = item.get("block_id")
    label_id = item.get("label_id")
    if not isinstance(fixture_id, str) or not fixture_id:
        raise EvaluationCaseError(f"{context}.fixture_id must be a non-empty string")
    if not isinstance(block_id, str) or not block_id:
        raise EvaluationCaseError(f"{context}.block_id must be a non-empty string")
    if not isinstance(label_id, str) or not label_id:
        raise EvaluationCaseError(f"{context}.label_id must be a non-empty string")
    label = labels.get((fixture_id, block_id, label_id))
    if label is None:
        raise EvaluationCaseError(f"{context} must reference an authoritative high-risk label")
    if "expected_value" not in item or item.get("expected_value") is None:
        raise EvaluationCaseError(f"{context}.expected_value must be defined")
    if item.get("risk_level") != label.get("risk_level"):
        raise EvaluationCaseError(f"{context}.risk_level must match high-risk labels")
    if item.get("requires_review") != label.get("requires_review"):
        raise EvaluationCaseError(f"{context}.requires_review must match high-risk labels")
    if not values_match_authoritative(label.get("expected_value"), item.get("expected_value")):
        raise EvaluationCaseError(f"{context}.expected_value must match high-risk labels")
    if "actual_value" not in item:
        raise EvaluationCaseError(f"{context}.actual_value must be present")
    expected_value = label.get("expected_value")
    actual_value = item.get("actual_value")
    if not isinstance(expected_value, str) and isinstance(actual_value, str):
        raise EvaluationCaseError(
            f"{context}.actual_value must not be a string for non-string high-risk labels"
        )
    status = item.get("status")
    if not isinstance(status, str) or not status:
        raise EvaluationCaseError(f"{context}.status must be a non-empty string")
    if status != "requires_review":
        raise EvaluationCaseError(f"{context}.status must be requires_review")
    return values_match_authoritative(expected_value, actual_value)


def evaluate_poc_mode_cases(
    mode_record: dict[str, Any],
    evaluation_cases_data: dict[str, Any],
    repo_root: Path,
    context: str,
) -> EvaluationMetrics:
    expected_cases = evaluation_cases_data.get("cases")
    if not isinstance(expected_cases, list):
        raise EvaluationCaseError("evaluation_cases must define cases")
    expected_cases_by_id = cases_by_id(expected_cases)

    mode_cases = mode_record.get("cases")
    if not isinstance(mode_cases, list) or not mode_cases:
        raise EvaluationCaseError(f"{context}.cases must list captured evaluation cases")
    mode_cases_by_id = cases_by_id(mode_cases)
    if set(mode_cases_by_id) != set(expected_cases_by_id):
        raise EvaluationCaseError(f"{context}.cases must cover all evaluation cases")

    scored_cases: list[dict[str, Any]] = []
    for case_id, expected_case in expected_cases_by_id.items():
        mode_case = mode_cases_by_id[case_id]
        actual = mode_case.get("actual")
        if not isinstance(actual, dict):
            raise EvaluationCaseError(f"{context}.cases[{case_id!r}].actual must be an object")
        scored_case = dict(expected_case)
        scored_case["actual"] = actual
        scored_cases.append(scored_case)

    scoring_data = dict(evaluation_cases_data)
    scoring_data["cases"] = scored_cases
    return evaluate_cases(scoring_data, manifest_root=repo_root)


def collect_poc_high_risk_label_evidence(
    mode_record: dict[str, Any],
    evaluation_cases_data: dict[str, Any],
    labels: dict[HighRiskLabelKey, dict[str, Any]],
    label_blocks: dict[HighRiskLabelKey, dict[str, Any]],
    context: str,
) -> PoCCapturedHighRiskEvidence:
    expected_cases = evaluation_cases_data.get("cases")
    if not isinstance(expected_cases, list):
        raise EvaluationCaseError("evaluation_cases must define cases")
    expected_cases_by_id = cases_by_id(expected_cases)

    mode_cases = mode_record.get("cases")
    if not isinstance(mode_cases, list) or not mode_cases:
        raise EvaluationCaseError(f"{context}.cases must list captured evaluation cases")
    mode_cases_by_id = cases_by_id(mode_cases)
    if set(mode_cases_by_id) != set(expected_cases_by_id):
        raise EvaluationCaseError(f"{context}.cases must cover all evaluation cases")

    actual_values_by_label = {key: set() for key in labels}
    auto_confirmed_labels: set[HighRiskLabelKey] = set()
    string_expected_values_by_fixture = {
        fixture_id: tuple(
            label["expected_value"]
            for key, label in labels.items()
            if key[0] == fixture_id and isinstance(label.get("expected_value"), str)
        )
        for fixture_id in {key[0] for key in labels}
    }
    for case_id, expected_case in expected_cases_by_id.items():
        fixture_id = expected_case.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise EvaluationCaseError(f"evaluation case {case_id!r} fixture_id must be a string")
        relevant_labels = {
            key: label
            for key, label in labels.items()
            if key[0] == fixture_id
        }
        if not relevant_labels:
            continue
        mode_case = mode_cases_by_id[case_id]
        expected_tables = required_tables_by_id(
            expected_case.get("expected", {}), f"case {case_id!r}: expected"
        )
        actual_tables = tables_by_id(mode_case.get("actual"))
        for table_id, expected_table in expected_tables.items():
            expected_cells = required_cells_by_id(
                expected_table, f"case {case_id!r}: expected table {table_id!r}"
            )
            actual_cells = cells_by_id(actual_tables.get(table_id, {"cells": []}))
            for cell_id, expected_cell in expected_cells.items():
                for label_key, label in relevant_labels.items():
                    label_block = label_blocks[label_key]
                    if not source_contains(label_block.get("source"), expected_cell.get("source")):
                        continue
                    expected_value = label.get("expected_value")
                    expected_text = actual_cell_text(
                        expected_cell,
                        f"case {case_id!r}: expected cell {cell_id!r}",
                    )
                    if isinstance(expected_value, str):
                        if not normalized_text_contains(expected_text, expected_value):
                            continue
                    elif expected_cell.get("requires_review") is not True or any(
                        normalized_text_contains(expected_text, string_expected_value)
                        for string_expected_value in string_expected_values_by_fixture[fixture_id]
                    ):
                        continue
                    actual_cell = actual_cells.get(cell_id)
                    if actual_cell is None:
                        continue
                    actual_text = actual_cell_text(
                        actual_cell,
                        f"{context}.cases[{case_id!r}].actual cell {cell_id!r}",
                    )
                    actual_values_by_label[label_key].add(normalized_text(actual_text))
                    if isinstance(expected_value, str) and normalized_text(
                        actual_text
                    ) == normalized_text(expected_text):
                        actual_values_by_label[label_key].add(normalized_text(expected_value))
                    if actual_auto_confirmed(
                        actual_cell,
                        f"{context}.cases[{case_id!r}].actual cell {cell_id!r}",
                    ):
                        auto_confirmed_labels.add(label_key)

    for label_key, values in actual_values_by_label.items():
        if not values:
            raise EvaluationCaseError(
                f"{context}.cases must include captured actual value for high-risk label "
                f"{label_key!r}"
            )
    return PoCCapturedHighRiskEvidence(
        actual_values_by_label=actual_values_by_label,
        auto_confirmed_labels=frozenset(auto_confirmed_labels),
    )


def poc_mode_actual_values_by_high_risk_label(
    mode_record: dict[str, Any],
    evaluation_cases_data: dict[str, Any],
    labels: dict[HighRiskLabelKey, dict[str, Any]],
    label_blocks: dict[HighRiskLabelKey, dict[str, Any]],
    context: str,
) -> dict[HighRiskLabelKey, set[str]]:
    return collect_poc_high_risk_label_evidence(
        mode_record, evaluation_cases_data, labels, label_blocks, context
    ).actual_values_by_label


def poc_mode_warning_ids(mode_record: dict[str, Any], context: str) -> set[str]:
    warning_ids = validate_text_list_allow_empty(
        mode_record.get("warnings", []), f"{context}.warnings"
    )
    return set(warning_ids)


def validate_reported_metric_matches_computed(
    reported: float, computed: float, context: str
) -> None:
    if not math.isclose(reported, computed):
        raise EvaluationCaseError(f"{context} must match recomputed evaluation cases")


def evaluate_poc_mode_comparison(
    data: dict[str, Any], repo_root: Path | None = None
) -> PoCComparisonMetrics:
    if data.get("schema_version") != POC_MODE_COMPARISON_SCHEMA_VERSION:
        raise EvaluationCaseError(
            f"unsupported PoC comparison schema_version {data.get('schema_version')!r}"
    )
    validate_scope(data)
    root = repo_root or Path.cwd()
    manifest_path = manifest_path_from_cases(data, root)
    fixture_paths = fixture_paths_from_manifest(load_json(manifest_path), root)
    labels = high_risk_label_index(load_json(high_risk_labels_path_from_comparison(data, root)))
    label_blocks = validate_high_risk_labels_against_fixtures(labels, fixture_paths)
    authoritative_label_keys = set(labels)
    evaluation_cases_data = load_json(evaluation_cases_path_from_comparison(data, root))
    missing_fixture_ids = sorted({label_key[0] for label_key in labels} - set(fixture_paths))
    if missing_fixture_ids:
        raise EvaluationCaseError(
            f"high-risk labels reference fixtures missing from dataset_manifest: {missing_fixture_ids!r}"
        )

    acceptance_targets = data.get("acceptance_targets")
    if not isinstance(acceptance_targets, dict):
        raise EvaluationCaseError("PoC comparison must define acceptance_targets")
    target = validate_non_negative_int(
        acceptance_targets.get("high_risk_false_auto_confirmed_count"),
        "acceptance_targets.high_risk_false_auto_confirmed_count",
    )
    if target != 0:
        raise EvaluationCaseError("high-risk false auto-confirmation target must be 0")
    manual_correction_time = evaluate_manual_correction_time(data)

    modes = data.get("modes")
    if not isinstance(modes, list):
        raise EvaluationCaseError("PoC comparison modes must be a list")

    seen_modes: set[str] = set()
    mode_metrics: list[PoCModeMetrics] = []
    mode_review_keys: dict[str, set[str]] = {}
    mode_warnings: dict[str, set[str]] = {}
    total_high_risk_false_auto_confirmed = 0
    for index, mode_record in enumerate(modes):
        context = f"modes[{index}]"
        if not isinstance(mode_record, dict):
            raise EvaluationCaseError(f"{context} must be an object")
        mode = mode_record.get("mode")
        if not isinstance(mode, str) or mode not in REQUIRED_POC_MODES:
            raise EvaluationCaseError(f"{context}.mode must be one of {REQUIRED_POC_MODES!r}")
        if mode in seen_modes:
            raise EvaluationCaseError(f"duplicate PoC comparison mode {mode!r}")
        seen_modes.add(mode)

        reported_metrics = mode_record.get("metrics")
        if not isinstance(reported_metrics, dict):
            raise EvaluationCaseError(f"{context}.metrics must be an object")
        computed_metrics = evaluate_poc_mode_cases(
            mode_record, evaluation_cases_data, root, context
        )
        table_extraction_rate = validate_ratio_metric(
            reported_metrics.get("table_extraction_rate"),
            f"{context}.metrics.table_extraction_rate",
        )
        validate_reported_metric_matches_computed(
            table_extraction_rate,
            computed_metrics.table_extraction_rate,
            f"{context}.metrics.table_extraction_rate",
        )
        high_risk_evidence = collect_poc_high_risk_label_evidence(
            mode_record, evaluation_cases_data, labels, label_blocks, context
        )
        actual_values_by_label = high_risk_evidence.actual_values_by_label
        high_risk_items = mode_record.get("high_risk_items")
        if not isinstance(high_risk_items, list) or not high_risk_items:
            raise EvaluationCaseError(f"{context}.high_risk_items must list high-risk checks")
        unique_warning_ids = poc_mode_warning_ids(mode_record, context)

        mode_label_keys: set[tuple[str, str]] = set()
        diff_review_keys: set[str] = set()
        requires_review_count = 0
        reported_auto_confirmed_labels: set[HighRiskLabelKey] = set()
        for item_index, item in enumerate(high_risk_items):
            item_context = f"{context}.high_risk_items[{item_index}]"
            if not isinstance(item, dict):
                raise EvaluationCaseError(f"{item_context} must be an object")
            matches_authoritative_value = validate_poc_high_risk_item_against_label(
                item, labels, item_context
            )
            label_key = (item["fixture_id"], item["block_id"], item["label_id"])
            if label_key in mode_label_keys:
                raise EvaluationCaseError(
                    f"{context}.high_risk_items has duplicate label {label_key!r}"
                )
            mode_label_keys.add(label_key)
            actual_value = item.get("actual_value")
            if not isinstance(actual_value, str) and not matches_authoritative_value:
                raise EvaluationCaseError(
                    f"{item_context}.actual_value must match high-risk labels"
                )
            if isinstance(actual_value, str) and normalized_text(actual_value) not in (
                actual_values_by_label.get(label_key) or set()
            ):
                raise EvaluationCaseError(
                    f"{item_context}.actual_value must match captured mode case value "
                    "for the high-risk label"
                )
            if item.get("risk_level") != "high":
                raise EvaluationCaseError(f"{item_context}.risk_level must be high")
            if item.get("requires_review") is not True:
                raise EvaluationCaseError(f"{item_context}.requires_review must be true")
            auto_confirmed = item.get("auto_confirmed")
            if not isinstance(auto_confirmed, bool):
                raise EvaluationCaseError(f"{item_context}.auto_confirmed must be a boolean")
            if item.get("status") == "requires_review":
                requires_review_count += 1
                diff_review_keys.add(review_key_for_diff(label_key))
            if auto_confirmed:
                reported_auto_confirmed_labels.add(label_key)

        if mode_label_keys != authoritative_label_keys:
            raise EvaluationCaseError(
                f"{context}.high_risk_items must cover all authoritative high-risk labels"
            )

        cell_match_rate = validate_ratio_metric(
            reported_metrics.get("cell_match_rate"),
            f"{context}.metrics.cell_match_rate",
        )
        validate_reported_metric_matches_computed(
            cell_match_rate,
            computed_metrics.cell_match_rate,
            f"{context}.metrics.cell_match_rate",
        )

        source_linkage_rate = validate_ratio_metric(
            reported_metrics.get("source_linkage_rate"),
            f"{context}.metrics.source_linkage_rate",
        )
        validate_reported_metric_matches_computed(
            source_linkage_rate,
            computed_metrics.source_linkage_rate,
            f"{context}.metrics.source_linkage_rate",
        )

        high_risk_false_auto_confirmed_count = len(
            reported_auto_confirmed_labels | set(high_risk_evidence.auto_confirmed_labels)
        )
        total_high_risk_false_auto_confirmed += high_risk_false_auto_confirmed_count
        mode_metrics.append(
            PoCModeMetrics(
                mode=mode,
                table_extraction_rate=table_extraction_rate,
                cell_match_rate=cell_match_rate,
                source_linkage_rate=source_linkage_rate,
                high_risk_false_auto_confirmed_count=high_risk_false_auto_confirmed_count,
                requires_review_count=requires_review_count,
                warning_count=len(unique_warning_ids),
            )
        )
        mode_review_keys[mode] = diff_review_keys
        mode_warnings[mode] = unique_warning_ids

    if tuple(sorted(seen_modes)) != tuple(sorted(REQUIRED_POC_MODES)):
        raise EvaluationCaseError(
            "PoC comparison must include exactly no_llm, standard, and high_quality modes"
        )

    mode_order = {mode: index for index, mode in enumerate(REQUIRED_POC_MODES)}
    baseline_mode = "no_llm"
    mode_diffs = tuple(
        mode_diff_summary(
            baseline_mode,
            candidate_mode,
            mode_review_keys[baseline_mode],
            mode_review_keys[candidate_mode],
            mode_warnings[baseline_mode],
            mode_warnings[candidate_mode],
        )
        for candidate_mode in REQUIRED_POC_MODES
        if candidate_mode != baseline_mode
    )
    return PoCComparisonMetrics(
        mode_count=len(mode_metrics),
        high_risk_false_auto_confirmed_count=total_high_risk_false_auto_confirmed,
        high_risk_false_auto_confirmed_target=target,
        target_met=(
            total_high_risk_false_auto_confirmed <= target
            and manual_correction_time.target_met
        ),
        manual_correction_time=manual_correction_time,
        modes=tuple(sorted(mode_metrics, key=lambda item: mode_order[item.mode])),
        mode_diffs=mode_diffs,
    )


def validate_text_list(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise EvaluationCaseError(f"{context} must be a non-empty list")
    workstation_home_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not normalized_text(item):
            raise EvaluationCaseError(f"{context}[{index}] must be a non-empty string")
        if Path(item).is_absolute() or any(
            fragment in item for fragment in workstation_home_fragments
        ):
            raise EvaluationCaseError(f"{context}[{index}] must be repo-relative or generic")
        items.append(normalized_text(item))
    return tuple(items)


def validate_text_list_allow_empty(value: object, context: str) -> tuple[str, ...]:
    if value is None:
        raise EvaluationCaseError(f"{context} must be a list")
    if not isinstance(value, list):
        raise EvaluationCaseError(f"{context} must be a list")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not normalized_text(item):
            raise EvaluationCaseError(f"{context}[{index}] must be a non-empty string")
        items.append(normalized_text(item))
    return tuple(items)


def review_key_for_diff(label_key: HighRiskLabelKey) -> str:
    fixture_id, block_id, label_id = label_key
    return f"{fixture_id}:{block_id}:{label_id}"


def mode_diff_summary(
    baseline_mode: str,
    candidate_mode: str,
    baseline_review_keys: set[str],
    candidate_review_keys: set[str],
    baseline_warnings: set[str],
    candidate_warnings: set[str],
) -> dict[str, object]:
    added_review_items = sorted(candidate_review_keys - baseline_review_keys)
    removed_review_items = sorted(baseline_review_keys - candidate_review_keys)
    added_warnings = sorted(candidate_warnings - baseline_warnings)
    removed_warnings = sorted(baseline_warnings - candidate_warnings)
    return {
        "baseline_mode": baseline_mode,
        "candidate_mode": candidate_mode,
        "review_item_added_count": len(added_review_items),
        "review_item_removed_count": len(removed_review_items),
        "warning_added_count": len(added_warnings),
        "warning_removed_count": len(removed_warnings),
        "added_review_items": added_review_items,
        "removed_review_items": removed_review_items,
        "added_warnings": added_warnings,
        "removed_warnings": removed_warnings,
    }


def validate_repo_relative_file_refs(
    refs: tuple[str, ...], context: str, repo_root: Path
) -> tuple[Path, ...]:
    resolved_root = repo_root.resolve()
    resolved_paths: list[Path] = []
    for index, ref in enumerate(refs):
        path = Path(ref)
        if path.is_absolute() or ".." in path.parts:
            raise EvaluationCaseError(f"{context}[{index}] must be a repo-relative file")
        resolved_path = (repo_root / path).resolve()
        if not resolved_path.is_relative_to(resolved_root):
            raise EvaluationCaseError(f"{context}[{index}] must stay inside the repository")
        if not resolved_path.is_file():
            raise EvaluationCaseError(f"{context}[{index}] must reference an existing file")
        resolved_paths.append(resolved_path)
    return tuple(resolved_paths)


def validate_public_gmp_acceptance_evidence_refs(
    refs: tuple[str, ...],
    context: str,
    repo_root: Path,
    declared_fixture_paths: frozenset[Path],
) -> None:
    resolved_refs = validate_repo_relative_file_refs(refs, context, repo_root)
    resolved_allowed_roots = tuple(
        (repo_root / allowed_root).resolve()
        for allowed_root in GMP_ACCEPTANCE_PUBLIC_EVIDENCE_ROOTS
    )
    for index, (ref, resolved_ref) in enumerate(zip(refs, resolved_refs)):
        path = Path(ref)
        lexical_public = any(
            path == allowed_root or path.is_relative_to(allowed_root)
            for allowed_root in GMP_ACCEPTANCE_PUBLIC_EVIDENCE_ROOTS
        )
        resolved_public = any(
            resolved_ref == allowed_root or resolved_ref.is_relative_to(allowed_root)
            for allowed_root in resolved_allowed_roots
        )
        if not lexical_public or not resolved_public:
            raise EvaluationCaseError(
                f"{context}[{index}] must reference public synthetic GMP evidence"
            )
        resolved_fixture_root = (repo_root / EXPECTED_ALLOWED_FIXTURE_ROOT).resolve()
        resolved_fixture_manifest = (repo_root / EXPECTED_DATASET_MANIFEST).resolve()
        lexical_fixture_ref = path.is_relative_to(EXPECTED_ALLOWED_FIXTURE_ROOT)
        resolved_fixture_ref = resolved_ref.is_relative_to(resolved_fixture_root)
        if (
            lexical_fixture_ref
            or resolved_fixture_ref
        ) and resolved_ref != resolved_fixture_manifest:
            if resolved_ref not in declared_fixture_paths:
                raise EvaluationCaseError(
                    f"{context}[{index}] must reference manifest-declared "
                    "public synthetic GMP fixture evidence"
                )


def require_gmp_acceptance_rerun_command(verification_commands: tuple[str, ...]) -> None:
    if EXPECTED_GMP_ACCEPTANCE_COMMAND not in verification_commands:
        raise EvaluationCaseError(
            "verification_commands must include "
            f"{EXPECTED_GMP_ACCEPTANCE_COMMAND!r}"
        )


def gmp_acceptance_assignment_path_values(name: str, value: str) -> tuple[str, ...]:
    if not name:
        return ()
    upper_name = name.upper()
    if upper_name.endswith("PATH") or upper_name in GMP_ACCEPTANCE_VERIFICATION_PATH_ENV_NAMES:
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
            return (value,)
        separators = tuple(separator for separator in (os.pathsep, ";", ":") if separator)
        values = [value]
        for separator in separators:
            if separator in value:
                values = [part for item in values for part in item.split(separator)]
        return tuple(part for part in values if part)
    if name in GMP_ACCEPTANCE_VERIFICATION_PATH_OPTION_NAMES:
        return (value,)
    return ()


def gmp_acceptance_python_module_path_candidates(module_name: str) -> tuple[str, ...]:
    if module_name in GMP_ACCEPTANCE_VERIFICATION_ALLOWED_PYTHON_MODULES:
        return ()
    module_parts = tuple(part for part in module_name.split(".") if part)
    if not module_parts or tuple(module_name.split(".")) != module_parts:
        return (module_name,)
    module_path = Path(*module_parts)
    return (module_path.as_posix(), module_path.with_suffix(".py").as_posix())


def validate_gmp_acceptance_verification_command_paths(
    verification_commands: tuple[str, ...],
    repo_root: Path,
) -> None:
    resolved_root = repo_root.resolve()
    resolved_allowed_roots = tuple(
        (repo_root / allowed_root).resolve()
        for allowed_root in GMP_ACCEPTANCE_PUBLIC_EVIDENCE_ROOTS
    )
    lexical_allowed_roots = tuple(str(path) for path in GMP_ACCEPTANCE_PUBLIC_EVIDENCE_ROOTS)
    for index, command in enumerate(verification_commands):
        try:
            lexer = shlex.shlex(command, posix=False, punctuation_chars=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError as exc:
            raise EvaluationCaseError(
                f"verification_commands[{index}] must be shell-tokenizable"
            ) from exc

        candidates: list[tuple[str, bool]] = []
        executable_seen = False
        validate_next_module_name = False
        for token in tokens:
            normalized_token = token.strip("\"'")
            if any(
                marker in normalized_token
                for marker in GMP_ACCEPTANCE_VERIFICATION_SHELL_EXPANSION_MARKERS
            ):
                raise EvaluationCaseError(
                    f"verification_commands[{index}] must not contain shell expansion tokens"
                )
            if (
                normalized_token
                and set(normalized_token) <= GMP_ACCEPTANCE_VERIFICATION_SHELL_CONTROL_CHARS
            ):
                raise EvaluationCaseError(
                    f"verification_commands[{index}] must not contain shell control operators"
                )
            if validate_next_module_name:
                validate_next_module_name = False
                for module_candidate in gmp_acceptance_python_module_path_candidates(
                    normalized_token
                ):
                    candidates.append((module_candidate, True))
                continue
            candidates.append((normalized_token, False))
            if "=" in normalized_token:
                assignment_name, assignment_value = normalized_token.split("=", 1)
                for assignment_candidate in gmp_acceptance_assignment_path_values(
                    assignment_name, assignment_value
                ):
                    candidates.append((assignment_candidate, True))
            if not normalized_token or set(
                normalized_token
            ) <= GMP_ACCEPTANCE_VERIFICATION_SHELL_CONTROL_CHARS:
                continue
            if normalized_token == "-m":
                validate_next_module_name = True
                continue
            if normalized_token.startswith("-"):
                continue
            if "=" in normalized_token:
                continue
            if not executable_seen:
                executable_seen = True
                continue
            candidates.append((normalized_token, True))
        for candidate, _ in candidates:
            if not candidate:
                continue
            candidate_path = Path(candidate)
            if candidate_path.is_absolute() or PureWindowsPath(candidate).is_absolute():
                raise EvaluationCaseError(
                    f"verification_commands[{index}] must not contain absolute paths"
                )
        for candidate, force_path_validation in candidates:
            if not candidate:
                continue
            candidate_path = Path(candidate)

            path_like = (
                force_path_validation
                or "/" in candidate
                or "\\" in candidate
                or candidate.startswith(".")
                or candidate_path.suffix
                or candidate in lexical_allowed_roots
            )
            if not path_like:
                continue
            if ".." in candidate_path.parts:
                raise EvaluationCaseError(
                    f"verification_commands[{index}] must not contain parent paths"
                )
            resolved_candidate = (repo_root / candidate_path).resolve()
            if not resolved_candidate.is_relative_to(resolved_root):
                raise EvaluationCaseError(
                    f"verification_commands[{index}] must stay inside the repository"
                )
            public_path = any(
                resolved_candidate == allowed_root
                or resolved_candidate.is_relative_to(allowed_root)
                for allowed_root in resolved_allowed_roots
            )
            if not public_path:
                raise EvaluationCaseError(
                    f"verification_commands[{index}] must reference public repository files"
                )
            if not resolved_candidate.exists():
                raise EvaluationCaseError(
                    f"verification_commands[{index}] must reference existing public repository paths"
                )


def validate_gmp_acceptance_dataset_manifest(
    data: dict[str, Any], repo_root: Path
) -> Path:
    manifest_path = manifest_path_from_cases(data, repo_root)
    if not manifest_path.is_file():
        raise EvaluationCaseError("dataset_manifest must reference an existing file")
    return manifest_path


def validate_gmp_acceptance_verification_commands(
    data: dict[str, Any],
    repo_root: Path,
) -> tuple[str, ...]:
    verification_commands = validate_text_list(
        data.get("verification_commands"), "verification_commands"
    )
    validate_gmp_acceptance_verification_command_paths(verification_commands, repo_root)
    require_gmp_acceptance_rerun_command(verification_commands)
    return verification_commands


def validate_gmp_acceptance_evidence_refs(
    criterion: dict[str, Any],
    context: str,
    repo_root: Path,
    declared_fixture_paths: frozenset[Path],
) -> tuple[str, ...]:
    evidence_refs = validate_text_list(
        criterion.get("evidence_refs"), f"{context}.evidence_refs"
    )
    validate_public_gmp_acceptance_evidence_refs(
        evidence_refs,
        f"{context}.evidence_refs",
        repo_root,
        declared_fixture_paths,
    )
    return evidence_refs


def validate_gmp_acceptance_source_traceability(
    status: object, high_quality_metrics: PoCModeMetrics
) -> None:
    if high_quality_metrics.source_linkage_rate < 1.0 and status == "pass":
        raise EvaluationCaseError(
            "source_traceability cannot pass when high_quality source linkage is incomplete"
        )


def validate_gmp_acceptance_segregation_of_duties(
    criterion: dict[str, Any], context: str, status: str
) -> None:
    if status != "pass":
        return
    if criterion.get("scope") != EXPECTED_GMP_ACCEPTANCE_SOD_SCOPE:
        raise EvaluationCaseError(
            f"{context}.scope must qualify segregation_of_duties pass status to "
            "authenticated actor identity"
        )
    notes = criterion.get("notes")
    if not isinstance(notes, str) or EXPECTED_GMP_ACCEPTANCE_SOD_NO_AUTH_NOTE not in notes:
        raise EvaluationCaseError(
            f"{context}.notes must document fail-closed no-auth approval handling"
        )


def gmp_acceptance_target_met(
    failed_criteria: list[dict[str, object]],
    poc_metrics: PoCComparisonMetrics,
    high_quality_metrics: PoCModeMetrics,
) -> bool:
    high_risk_target_met = (
        poc_metrics.high_risk_false_auto_confirmed_count
        <= poc_metrics.high_risk_false_auto_confirmed_target
    )
    source_traceability_target_met = high_quality_metrics.source_linkage_rate >= 1.0
    return not failed_criteria and high_risk_target_met and source_traceability_target_met


def high_quality_poc_mode_metrics(poc_metrics: PoCComparisonMetrics) -> PoCModeMetrics:
    for mode_metrics in poc_metrics.modes:
        if mode_metrics.mode == "high_quality":
            return mode_metrics
    raise EvaluationCaseError("PoC comparison must include high_quality mode")


def evaluate_gmp_acceptance(
    data: dict[str, Any], repo_root: Path | None = None
) -> GmpAcceptanceMetrics:
    if data.get("schema_version") != GMP_ACCEPTANCE_SCHEMA_VERSION:
        raise EvaluationCaseError(
            f"unsupported GMP acceptance schema_version {data.get('schema_version')!r}"
        )
    root = repo_root or Path.cwd()
    manifest_path = validate_gmp_acceptance_dataset_manifest(data, root)
    declared_fixture_paths = frozenset(
        path.resolve()
        for path in fixture_paths_from_manifest(load_json(manifest_path), root).values()
    )
    validate_scope(data)
    poc_path = poc_comparison_path_from_gmp_acceptance(data, root)
    poc_metrics = evaluate_poc_mode_comparison(load_json(poc_path), repo_root=root)
    high_quality_metrics = high_quality_poc_mode_metrics(poc_metrics)

    verification_commands = validate_gmp_acceptance_verification_commands(data, root)
    criteria = data.get("criteria")
    if not isinstance(criteria, list):
        raise EvaluationCaseError("GMP acceptance criteria must be a list")
    if len(criteria) != len(REQUIRED_GMP_ACCEPTANCE_CRITERIA):
        raise EvaluationCaseError("GMP acceptance criteria must cover all 15.7 criteria")

    seen_ids: set[str] = set()
    normalized_criteria: list[dict[str, object]] = []
    failed_criteria: list[dict[str, object]] = []
    for index, criterion in enumerate(criteria):
        context = f"criteria[{index}]"
        if not isinstance(criterion, dict):
            raise EvaluationCaseError(f"{context} must be an object")
        criterion_id = criterion.get("id")
        if criterion_id != REQUIRED_GMP_ACCEPTANCE_CRITERIA[index]:
            raise EvaluationCaseError(
                f"{context}.id must be {REQUIRED_GMP_ACCEPTANCE_CRITERIA[index]!r}"
            )
        if criterion_id in seen_ids:
            raise EvaluationCaseError(f"duplicate GMP acceptance criterion {criterion_id!r}")
        seen_ids.add(criterion_id)

        title = criterion.get("title")
        if not isinstance(title, str) or not normalized_text(title):
            raise EvaluationCaseError(f"{context}.title must be a non-empty string")
        status = criterion.get("status")
        if status not in {"pass", "fail"}:
            raise EvaluationCaseError(f"{context}.status must be pass or fail")
        evidence_refs = validate_gmp_acceptance_evidence_refs(
            criterion,
            context,
            root,
            declared_fixture_paths,
        )
        notes = criterion.get("notes")
        if not isinstance(notes, str) or not normalized_text(notes):
            raise EvaluationCaseError(f"{context}.notes must be a non-empty string")

        normalized = {
            "id": criterion_id,
            "title": normalized_text(title),
            "status": status,
            "evidence_refs": list(evidence_refs),
            "notes": normalized_text(notes),
        }
        scope = criterion.get("scope")
        if scope is not None:
            if not isinstance(scope, str) or not normalized_text(scope):
                raise EvaluationCaseError(f"{context}.scope must be a non-empty string")
            normalized["scope"] = normalized_text(scope)
        excluded_contexts = criterion.get("excluded_contexts")
        if excluded_contexts is not None:
            normalized["excluded_contexts"] = list(
                validate_text_list(excluded_contexts, f"{context}.excluded_contexts")
            )
        if criterion_id == "missed_detection_zero":
            if (
                poc_metrics.high_risk_false_auto_confirmed_count
                > poc_metrics.high_risk_false_auto_confirmed_target
                and status == "pass"
            ):
                raise EvaluationCaseError(
                    "missed_detection_zero cannot pass when high-risk false "
                    "auto-confirmation count exceeds target"
                )
            normalized["observed_count"] = poc_metrics.high_risk_false_auto_confirmed_count
            normalized["target"] = poc_metrics.high_risk_false_auto_confirmed_target

        if criterion_id == "source_traceability":
            validate_gmp_acceptance_source_traceability(status, high_quality_metrics)
            normalized["high_quality_source_linkage_rate"] = (
                high_quality_metrics.source_linkage_rate
            )
            normalized["target"] = 1.0
        if criterion_id == "segregation_of_duties":
            validate_gmp_acceptance_segregation_of_duties(criterion, context, status)

        normalized_criteria.append(normalized)
        if status == "fail":
            failed_criteria.append(normalized)

    target_met = gmp_acceptance_target_met(
        failed_criteria, poc_metrics, high_quality_metrics
    )
    return GmpAcceptanceMetrics(
        poc_comparison=str(EXPECTED_POC_COMPARISON),
        criterion_count=len(normalized_criteria),
        failed_criterion_count=len(failed_criteria),
        high_risk_false_auto_confirmed_count=(
            poc_metrics.high_risk_false_auto_confirmed_count
        ),
        high_risk_false_auto_confirmed_target=(
            poc_metrics.high_risk_false_auto_confirmed_target
        ),
        target_met=target_met,
        criteria=tuple(normalized_criteria),
        failed_criteria=tuple(failed_criteria),
        verification_commands=verification_commands,
    )


def evaluate_llm_stability_report(
    llm_stability_runs_path: Path,
    poc_comparison_path: Path,
) -> LLMStabilityEvaluationReport:
    resolved_stability_path = llm_stability_runs_path.resolve()
    resolved_comparison_path = poc_comparison_path.resolve()
    poc_repo_root = repository_root_for_gold_path(resolved_comparison_path)
    return LLMStabilityEvaluationReport(
        llm_stability=evaluate_llm_stability(load_json(resolved_stability_path)),
        poc_mode_comparison=evaluate_poc_mode_comparison(
            load_json(resolved_comparison_path),
            repo_root=poc_repo_root,
        ),
        stability_source=llm_stability_runs_path,
        poc_comparison_source=poc_comparison_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_EVALUATION_CASES)
    parser.add_argument(
        "--llm-stability-report",
        action="store_true",
        help=(
            "Emit the minimal Phase8 LLM stability evaluation handoff report from "
            "public synthetic gold records."
        ),
    )
    parser.add_argument(
        "--llm-stability-runs",
        type=Path,
        help="Measure N-run LLM output stability from a public synthetic run record.",
    )
    parser.add_argument(
        "--poc-comparison",
        type=Path,
        help="Compare no-LLM, standard, and high-quality PoC outputs from public records.",
    )
    parser.add_argument(
        "--gmp-acceptance",
        type=Path,
        help="Report GMP-08 15.7 acceptance criteria from public synthetic records.",
    )
    parser.add_argument(
        "--p9-harness",
        type=Path,
        nargs="?",
        const=DEFAULT_P9_HARNESS_MANIFEST,
        help=(
            "Run the Phase9 PoC conversion harness against representative "
            "P9-01 fixture manifest entries."
        ),
    )
    args = parser.parse_args()

    try:
        if args.p9_harness is not None:
            metrics = evaluate_p9_harness(
                args.p9_harness,
                llm_stability_runs_path=args.llm_stability_runs
                or DEFAULT_LLM_STABILITY_RUNS,
                poc_comparison_path=args.poc_comparison or DEFAULT_POC_COMPARISON,
            )
        elif args.gmp_acceptance is not None:
            gmp_acceptance_path = args.gmp_acceptance.resolve()
            metrics = evaluate_gmp_acceptance(
                load_json(gmp_acceptance_path),
                repo_root=repository_root_for_gold_path(gmp_acceptance_path),
            )
        elif args.llm_stability_report:
            metrics = evaluate_llm_stability_report(
                args.llm_stability_runs or DEFAULT_LLM_STABILITY_RUNS,
                args.poc_comparison or DEFAULT_POC_COMPARISON,
            )
        elif args.poc_comparison is not None:
            poc_comparison_path = args.poc_comparison.resolve()
            metrics = evaluate_poc_mode_comparison(
                load_json(poc_comparison_path),
                repo_root=repository_root_for_gold_path(poc_comparison_path),
            )
        elif args.llm_stability_runs is not None:
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
    if args.gmp_acceptance is not None and not metrics.target_met:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
