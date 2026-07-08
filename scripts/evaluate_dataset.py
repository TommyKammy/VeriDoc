#!/usr/bin/env python3
"""Compute Phase 0 evaluation metrics for public VeriDoc fixtures."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
import math
import os
import platform
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterable, Mapping
from xml.etree import ElementTree
from zipfile import ZipFile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.llm.conversion_plan import ConversionPlanValidationError, validate_conversion_plan


DEFAULT_EVALUATION_CASES = Path("datasets/gold/evaluation_cases_v0.json")
DEFAULT_LLM_STABILITY_RUNS = Path("datasets/gold/llm_stability_runs_v0.json")
DEFAULT_POC_COMPARISON = Path("datasets/gold/poc_mode_comparison_v1.json")
DEFAULT_GMP_ACCEPTANCE = Path("datasets/gold/gmp_acceptance_v1.json")
DEFAULT_P9_HARNESS_MANIFEST = Path("datasets/poc_evaluation_manifest_v1.json")
EVALUATION_CASES_SCHEMA_VERSION = "veridoc-evaluation-cases/v0"
LLM_STABILITY_RUNS_SCHEMA_VERSION = "veridoc-llm-stability-runs/v0"
POC_MODE_COMPARISON_SCHEMA_VERSION = "veridoc-poc-mode-comparison/v1"
GMP_ACCEPTANCE_SCHEMA_VERSION = "veridoc-gmp-acceptance/v1"
P9_HARNESS_SCHEMA_VERSION = "veridoc-p9-poc-evaluation-harness/v0"
POC_ACCEPTANCE_REPORT_SCHEMA_VERSION = "veridoc-poc-acceptance-report/v0"
P9_EVALUATION_MANIFEST_SCHEMA_VERSION = "veridoc-poc-evaluation-dataset/v1"
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
POC_AUTH_SESSION_README_REF = "README.md Local PoC API authentication"
POC_AUTH_SESSION_ENV_VAR = "VERIDOC_LOCAL_AUTH_TOKENS"
POC_AUTH_SESSION_ENV_SUCCESS_COVERAGE_REFS = (
    "tests/test_poc_web_api.py::test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
)
POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS = (
    *POC_AUTH_SESSION_ENV_SUCCESS_COVERAGE_REFS,
    "tests/test_poc_web_api.py::test_poc_http_api_filters_review_action_audit_events_by_action",
    "tests/test_poc_web_api.py::test_poc_http_api_allows_approval_with_revised_text_target",
    "tests/test_poc_web_api.py::test_poc_http_api_requires_admin_role_for_retry_job_event",
)
POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS = (
    "tests/test_poc_web_api.py::test_poc_http_api_authenticates_review_events_before_parsing_payload",
    "tests/test_poc_web_api.py::test_poc_http_api_rejects_read_only_review_role_before_parsing_payload",
    "tests/test_poc_web_api.py::test_poc_http_api_requires_configured_local_auth_token_for_review_events",
    "tests/test_poc_web_api.py::test_poc_http_api_authenticates_job_events_before_parsing_payload",
)
POC_AUTH_SESSION_COVERAGE_REFS = (
    *POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS,
    *POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS,
)
POC_AUTH_SESSION_FAIL_CLOSED_EXPECTED_STATUS_BY_REF = {
    "tests/test_poc_web_api.py::test_poc_http_api_authenticates_review_events_before_parsing_payload": 401,
    "tests/test_poc_web_api.py::test_poc_http_api_rejects_read_only_review_role_before_parsing_payload": 403,
    "tests/test_poc_web_api.py::test_poc_http_api_requires_configured_local_auth_token_for_review_events": 401,
    "tests/test_poc_web_api.py::test_poc_http_api_authenticates_job_events_before_parsing_payload": 401,
}
POC_AUTH_SESSION_FAIL_CLOSED_REF_EXPECTATIONS = {
    "tests/test_poc_web_api.py::test_poc_http_api_authenticates_review_events_before_parsing_payload": {
        "status_codes": frozenset((401,)),
        "method": "POST",
        "path": "/api/review-events",
        "auth_source": "direct",
        "forbid_auth_tokens": True,
        "required_literals": frozenset(("{not valid json", "auth_required")),
        "asserted_literals": frozenset(("auth_required",)),
    },
    "tests/test_poc_web_api.py::test_poc_http_api_rejects_read_only_review_role_before_parsing_payload": {
        "status_codes": frozenset((403,)),
        "method": "POST",
        "path": "/api/review-events",
        "auth_source": "direct",
        "auth_tokens": frozenset(("viewer-token",)),
        "required_literals": frozenset(
            ("{not valid json", "Bearer viewer-token", "forbidden")
        ),
        "asserted_literals": frozenset(("forbidden",)),
    },
    "tests/test_poc_web_api.py::test_poc_http_api_requires_configured_local_auth_token_for_review_events": {
        "status_codes": frozenset((401,)),
        "method": "POST",
        "path": "/api/review-events",
        "auth_source": "direct",
        "forbid_auth_tokens": True,
        "required_literals": frozenset(
            ("Authorization bearer token is required", "auth_required")
        ),
        "asserted_literals": frozenset(
            ("Authorization bearer token is required", "auth_required")
        ),
    },
    "tests/test_poc_web_api.py::test_poc_http_api_authenticates_job_events_before_parsing_payload": {
        "status_codes": frozenset((401,)),
        "method": "POST",
        "path": "/api/job-events",
        "auth_source": "direct",
        "forbid_auth_tokens": True,
        "required_literals": frozenset(("{not valid json", "auth_required")),
        "asserted_literals": frozenset(("auth_required",)),
    },
}
POC_AUTH_SESSION_COVERAGE_INPUT_PATHS = (
    Path("README.md"),
    Path("tests/test_poc_web_api.py"),
)
POC_AUTH_SESSION_SUCCESS_TOKEN_LITERALS = frozenset(
    ("env-reviewer-token", "reviewer-token", "approver-token", "admin-token")
)
POC_AUTH_SESSION_AUTH_TOKEN_LITERALS = frozenset(
    (*POC_AUTH_SESSION_SUCCESS_TOKEN_LITERALS, "viewer-token")
)
POC_AUTH_SESSION_EXPECTED_ROLE_BY_TOKEN = {
    "env-reviewer-token": "reviewer",
    "viewer-token": "viewer",
    "reviewer-token": "reviewer",
    "approver-token": "approver",
    "admin-token": "admin",
}
POC_AUTH_SESSION_SUCCESS_STATUS_CODES = frozenset((200, 202))
POC_AUTH_SESSION_ENV_ROLES = frozenset(("viewer", "reviewer", "approver", "admin"))
POC_AUTH_SESSION_TRUSTED_STATUS_HELPERS = {
    "_post_review_event_on_connection": ("POST", "/api/review-events"),
}
POC_AUTH_SESSION_SUCCESS_REF_EXPECTATIONS = {
    "tests/test_poc_web_api.py::test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": {
        "tokens": frozenset(("env-reviewer-token",)),
        "auth_source": "env",
        "status_codes": frozenset((202,)),
        "required_literals": frozenset(("conversion-env-auth",)),
    },
    "tests/test_poc_web_api.py::test_poc_http_api_filters_review_action_audit_events_by_action": {
        "tokens": frozenset(("admin-token",)),
        "auth_source": "direct",
        "status_codes": frozenset((202,)),
        "required_literals": frozenset(("approve", "conversion-current")),
    },
    "tests/test_poc_web_api.py::test_poc_http_api_allows_approval_with_revised_text_target": {
        "tokens": frozenset(("admin-token",)),
        "auth_source": "direct",
        "status_codes": frozenset((202,)),
        "required_literals": frozenset(("approve", "Lot: SAMPLE-001 corrected")),
    },
    "tests/test_poc_web_api.py::test_poc_http_api_requires_admin_role_for_retry_job_event": {
        "tokens": frozenset(("admin-token",)),
        "auth_source": "direct",
        "status_codes": frozenset((202,)),
        "method": "POST",
        "path": "/api/job-events",
        "required_literals": frozenset(("retry_conversion",)),
    },
}
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
P9_PRIMARY_ARTIFACT_FORMAT_BY_CONVERSION_MODE = {
    "word_to_excel": "xlsx",
    "pdf_to_excel": "xlsx",
    "excel_to_word": "docx",
    "pdf_to_word": "docx",
}
P9_FIXTURE_SOURCE_TYPES_BY_CATEGORY = {
    "word": frozenset(("word",)),
    "excel": frozenset(("excel",)),
    "text_pdf": frozenset(("text_pdf",)),
    "record_pdf": frozenset(("record_excerpt",)),
    "scanned_pdf": frozenset(("scanned_pdf",)),
}
P9_REQUIRED_SOURCE_CATEGORIES = frozenset(P9_FIXTURE_SOURCE_TYPES_BY_CATEGORY)
P9_REPRESENTATIVE_FLAG_BY_CATEGORY = {
    "record_pdf": "record_pdf_representative",
}
P9_LLM_SCENARIOS = ("no_llm", "llm_requested")
P9_MVP_BEFORE_GATE_REVISION_OPTIONAL_PDF_DEPS = (
    "p9-mvp-before-pdf-eval-dependency-gate"
)
P9_MVP_BEFORE_GATE_REVISION_PLACEHOLDER_FIXTURE = (
    "p9-mvp-before-representative-fixture-gate"
)
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


def poc_auth_session_coverage_evidence_refs() -> tuple[str, ...]:
    return (POC_AUTH_SESSION_README_REF, *POC_AUTH_SESSION_COVERAGE_REFS)


def poc_auth_session_coverage_input_paths(repo_root: Path) -> tuple[Path, ...]:
    resolved_root = repo_root.resolve()
    return tuple(resolved_root / path for path in POC_AUTH_SESSION_COVERAGE_INPUT_PATHS)


def poc_auth_session_coverage_inputs_tracked_in_repo(repo_root: Path) -> bool:
    resolved_root = repo_root.resolve()
    return all(
        poc_acceptance_tracked_repo_path(path, resolved_root)
        for path in poc_auth_session_coverage_input_paths(resolved_root)
    )


def _top_level_function_nodes(
    test_source: str,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    try:
        tree = ast.parse(test_source)
    except SyntaxError:
        return {}
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    duplicate_names: set[str] = set()
    rebound_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in functions:
                duplicate_names.add(node.name)
            functions[node.name] = node
            continue
        rebound_names.update(_top_level_rebound_function_names(node, functions))
    return {
        name: node
        for name, node in functions.items()
        if name not in duplicate_names and name not in rebound_names
    }


def _top_level_rebound_function_names(
    node: ast.stmt,
    functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef],
) -> frozenset[str]:
    rebound: set[str] = set()
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        for target in _assignment_targets(node):
            rebound.update(_target_name_bindings(target) & functions.keys())
    if isinstance(node, ast.Delete):
        for target in node.targets:
            rebound.update(_target_name_bindings(target) & functions.keys())
    return frozenset(rebound)


def _module_shadowed_runtime_names(
    tree: ast.Module,
    names: frozenset[str],
) -> frozenset[str]:
    shadowed: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if statement.name in names:
                shadowed.add(statement.name)
            continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for target in _assignment_targets(statement):
                shadowed.update(_target_name_bindings(target) & names)
        if isinstance(statement, ast.Delete):
            for target in statement.targets:
                shadowed.update(_target_name_bindings(target) & names)
    return frozenset(shadowed)


def _pytestmark_value_is_skipped_or_xfailed(node: ast.AST) -> bool:
    mark_name = _dotted_name(node)
    if mark_name in {
        "pytest.mark.skip",
        "pytest.mark.skipif",
        "pytest.mark.xfail",
        "skip",
        "skipif",
        "xfail",
    }:
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return any(
            _pytestmark_value_is_skipped_or_xfailed(element)
            for element in node.elts
        )
    return False


def _module_is_skipped_or_xfailed(tree: ast.Module) -> bool:
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else (statement.target,)
        )
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in targets
        ):
            continue
        value = statement.value
        if value is not None and _pytestmark_value_is_skipped_or_xfailed(value):
            return True
    return False


def _module_has_pytestmark(tree: ast.Module) -> bool:
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else (statement.target,)
        )
        if any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in targets
        ):
            return True
    return False


def _module_has_top_level_skip_call(tree: ast.Module) -> bool:
    for statement in tree.body:
        if _statement_has_pytest_module_skip_call(statement):
            return True
    return False


def _statement_has_pytest_module_skip_call(statement: ast.stmt) -> bool:
    for child in _walk_statement_without_nested_scopes(statement):
        if not isinstance(child, ast.Call):
            continue
        if _dotted_name(child.func) in {
            "pytest.skip",
            "pytest.importorskip",
            "skip",
            "importorskip",
        }:
            return True
    return False


def _test_function_nodes(
    test_source: str,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    try:
        tree = ast.parse(test_source)
    except SyntaxError:
        return {}
    if (
        _module_is_skipped_or_xfailed(tree)
        or _module_has_pytestmark(tree)
        or _module_has_top_level_skip_call(tree)
        or _module_disables_test_collection(tree)
    ):
        return {}
    disabled_test_names = _function_test_opt_out_names(tree)
    empty_parametrize_names = _empty_collection_literal_names(tree)
    return {
        name: node
        for name, node in _top_level_function_nodes(test_source).items()
        if name not in disabled_test_names
        and not _test_function_is_skipped_or_xfailed(
            node,
            empty_parametrize_names=empty_parametrize_names,
        )
        and not _test_function_has_unresolved_fixture_args(node)
    }


def _module_disables_test_collection(tree: ast.Module) -> bool:
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else (statement.target,)
        )
        if not any(
            isinstance(target, ast.Name) and target.id == "__test__"
            for target in targets
        ):
            continue
        if isinstance(statement.value, ast.Constant) and not bool(
            statement.value.value
        ):
            return True
    return False


def _function_test_opt_out_names(tree: ast.Module) -> frozenset[str]:
    disabled: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.Expr):
            disabled_name = _setattr_disabled_test_name(statement.value)
            if disabled_name is not None:
                disabled.add(disabled_name)
            continue
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        if not (
            isinstance(statement.value, ast.Constant)
            and not bool(statement.value.value)
        ):
            continue
        targets = (
            statement.targets
            if isinstance(statement, ast.Assign)
            else (statement.target,)
        )
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "__test__"
                and isinstance(target.value, ast.Name)
            ):
                disabled.add(target.value.id)
    return frozenset(disabled)


def _setattr_sets_falsey_test_marker(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        and len(node.args) >= 3
        and _constant_string_value(node.args[1]) == "__test__"
        and isinstance(node.args[2], ast.Constant)
        and not bool(node.args[2].value)
    )


def _setattr_disabled_test_name(node: ast.AST) -> str | None:
    if not _setattr_sets_falsey_test_marker(node):
        return None
    assert isinstance(node, ast.Call)
    if isinstance(node.args[0], ast.Name):
        return node.args[0].id
    return None


def _empty_collection_literal_names(tree: ast.Module) -> frozenset[str]:
    names: set[str] = set()
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        value = statement.value
        if not isinstance(value, (ast.List, ast.Tuple, ast.Set)) or value.elts:
            continue
        for target in _assignment_targets(statement):
            names.update(_target_name_bindings(target))
    return frozenset(names)


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent is not None else node.attr
    if isinstance(node, ast.Call):
        return _dotted_name(node.func)
    return None


def _test_function_is_skipped_or_xfailed(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    empty_parametrize_names: frozenset[str] = frozenset(),
) -> bool:
    if isinstance(node, ast.AsyncFunctionDef):
        return True
    for decorator in node.decorator_list:
        name = _dotted_name(decorator)
        if name in {
            "pytest.mark.skip",
            "pytest.mark.skipif",
            "pytest.mark.xfail",
            "pytest.fixture",
            "unittest.skip",
            "unittest.skipIf",
            "unittest.skipUnless",
            "skip",
            "skipif",
            "xfail",
            "fixture",
        }:
            return True
        if _decorator_is_empty_parametrize(
            decorator,
            empty_parametrize_names=empty_parametrize_names,
        ):
            return True
        if _decorator_parametrizes_names(decorator, frozenset(("monkeypatch",))):
            return True
        if _dotted_name(decorator.func) not in {
            "pytest.mark.parametrize",
            "parametrize",
        } if isinstance(decorator, ast.Call) else True:
            return True
    return False


def _decorator_is_empty_parametrize(
    node: ast.AST,
    *,
    empty_parametrize_names: frozenset[str] = frozenset(),
) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if _dotted_name(node.func) not in {"pytest.mark.parametrize", "parametrize"}:
        return False
    if len(node.args) < 2:
        return False
    values = node.args[1]
    if isinstance(values, ast.Name):
        return True
    if not isinstance(values, (ast.List, ast.Tuple)):
        return False
    if not values.elts:
        return True
    return all(_parametrize_value_is_skipped_or_xfailed(value) for value in values.elts)


def _parametrize_value_is_skipped_or_xfailed(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if _dotted_name(node.func) not in {"pytest.param", "param"}:
        return False
    for keyword in node.keywords:
        if keyword.arg == "marks" and _pytestmark_value_is_skipped_or_xfailed(
            keyword.value
        ):
            return True
    return False


def _decorator_parametrizes_names(
    node: ast.AST,
    names: frozenset[str],
) -> bool:
    return bool(_decorator_parametrized_names(node) & names)


def _decorator_parametrized_names(node: ast.AST) -> frozenset[str]:
    if not isinstance(node, ast.Call):
        return frozenset()
    if _dotted_name(node.func) not in {"pytest.mark.parametrize", "parametrize"}:
        return frozenset()
    if not node.args:
        return frozenset()
    arg_names: set[str] = set()
    first_arg = node.args[0]
    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
        arg_names.update(
            name.strip()
            for name in first_arg.value.replace(",", " ").split()
            if name.strip()
        )
    if isinstance(first_arg, (ast.List, ast.Tuple)):
        for element in first_arg.elts:
            value = _constant_string_value(element)
            if value is not None:
                arg_names.add(value)
    return frozenset(arg_names)


def _function_arg_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    args = node.args
    return frozenset(
        arg.arg
        for arg in (
            *args.posonlyargs,
            *args.args,
            *args.kwonlyargs,
        )
    )


def _function_required_positional_arg_count(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> int:
    args = (*node.args.posonlyargs, *node.args.args)
    return max(0, len(args) - len(node.args.defaults))


def _function_required_keyword_only_arg_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> frozenset[str]:
    return frozenset(
        arg.arg
        for arg, default in zip(
            node.args.kwonlyargs,
            node.args.kw_defaults,
            strict=False,
        )
        if default is None
    )


def _function_accepts_no_arguments(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    return (
        _function_required_positional_arg_count(node) == 0
        and not _function_required_keyword_only_arg_names(node)
    )


def _function_parametrized_arg_names(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> frozenset[str]:
    names: set[str] = set()
    for decorator in node.decorator_list:
        names.update(_decorator_parametrized_names(decorator))
    return frozenset(names)


def _test_function_has_unresolved_fixture_args(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    allowed_args = frozenset(("monkeypatch",)) | _function_parametrized_arg_names(node)
    return bool(_function_arg_names(node) - allowed_args)


def _call_satisfies_function_signature(
    call: ast.Call,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    if any(keyword.arg is None for keyword in call.keywords):
        return False
    required_positional = _function_required_positional_arg_count(node)
    positional_args = (*node.args.posonlyargs, *node.args.args)
    positional_capacity = len(positional_args)
    if len(call.args) < required_positional:
        return False
    if node.args.vararg is None and len(call.args) > positional_capacity:
        return False
    keyword_names = {keyword.arg for keyword in call.keywords if keyword.arg}
    already_bound_positional_names = {
        arg.arg for arg in positional_args[: len(call.args)]
    }
    if keyword_names & already_bound_positional_names:
        return False
    accepted_keyword_names = {
        arg.arg
        for arg in (
            *node.args.args,
            *node.args.kwonlyargs,
        )
    }
    if not keyword_names.issubset(accepted_keyword_names):
        return False
    return _function_required_keyword_only_arg_names(node).issubset(keyword_names)


def _constant_int_value(node: ast.AST) -> int | None:
    if not isinstance(node, ast.Constant):
        return None
    if isinstance(node.value, bool) or not isinstance(node.value, int):
        return None
    return node.value


def _compare_checks_success_status_equality(node: ast.Compare) -> bool:
    return _compare_checks_status_equality(
        node, POC_AUTH_SESSION_SUCCESS_STATUS_CODES
    )


def _compare_checks_status_equality(
    node: ast.Compare,
    status_codes: frozenset[int],
) -> bool:
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
        return False
    parts = (node.left, *node.comparators)
    values = tuple(_constant_int_value(part) for part in parts)
    has_success_status = any(value in status_codes for value in values)
    has_observed_status_side = any(value is None for value in values)
    return has_success_status and has_observed_status_side


def _literal_string_values(
    node: ast.AST,
    name_literal_bindings: Mapping[str, frozenset[str]] | None = None,
) -> frozenset[str]:
    if name_literal_bindings is None:
        name_literal_bindings = {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return frozenset((node.value,))
    if isinstance(node, ast.Name):
        return name_literal_bindings.get(node.id, frozenset())
    return frozenset()


def _privileged_auth_tokens_in_node(
    node: ast.AST,
    name_literal_bindings: Mapping[str, frozenset[str]] | None = None,
) -> frozenset[str]:
    return frozenset(
        value.strip()
        for value in _literal_string_values(node, name_literal_bindings)
        if value.strip() in POC_AUTH_SESSION_SUCCESS_TOKEN_LITERALS
    )


def _local_auth_token_mapping_value_is_valid(
    node: ast.AST,
    *,
    expected_role: str | None = None,
) -> bool:
    if not isinstance(node, ast.Dict):
        return False
    role: str | None = None
    principal_id: str | None = None
    for key, value in zip(node.keys, node.values, strict=False):
        if _constant_string_value(key) == "role":
            role = _constant_string_value(value)
        if _constant_string_value(key) == "principal_id":
            principal_id = _constant_string_value(value)
    if role not in POC_AUTH_SESSION_ENV_ROLES:
        return False
    if expected_role is not None and role != expected_role:
        return False
    return bool(principal_id and principal_id.strip())


def _local_auth_token_mapping_tokens(node: ast.AST) -> frozenset[str]:
    if not isinstance(node, ast.Dict):
        return frozenset()
    tokens: set[str] = set()
    for key, value in zip(node.keys, node.values, strict=False):
        token = _constant_string_value(key)
        if (
            token in POC_AUTH_SESSION_AUTH_TOKEN_LITERALS
            and _local_auth_token_mapping_value_is_valid(
                value,
                expected_role=POC_AUTH_SESSION_EXPECTED_ROLE_BY_TOKEN.get(token),
            )
        ):
            tokens.add(token)
    return frozenset(tokens)


def _local_auth_token_helper_tokens(
    test_source: str,
) -> dict[str, frozenset[str]]:
    helpers: dict[str, frozenset[str]] = {}
    for name, node in _top_level_function_nodes(test_source).items():
        if _test_function_is_skipped_or_xfailed(
            node
        ) or not _function_accepts_no_arguments(node):
            continue
        tokens: set[str] = set()
        for ordered_statement in _ordered_function_statements(node.body):
            statement = ordered_statement.statement
            if isinstance(statement, ast.Return):
                tokens.update(_local_auth_token_mapping_tokens(statement.value))
                break
        if tokens:
            helpers[name] = frozenset(tokens)
    return helpers


def _visible_local_auth_token_helpers(
    local_auth_token_helpers: Mapping[str, frozenset[str]],
    shadowed_names: Iterable[str],
) -> dict[str, frozenset[str]]:
    shadowed = set(shadowed_names)
    return {
        name: tokens
        for name, tokens in local_auth_token_helpers.items()
        if name not in shadowed
    }


def _authorization_header_token(value: str) -> str | None:
    scheme, separator, token = value.strip().partition(" ")
    if separator != " " or scheme != "Bearer" or not token.strip():
        return None
    return token.strip()


def _auth_header_tokens_in_node(
    node: ast.AST,
    name_literal_bindings: Mapping[str, frozenset[str]] | None = None,
) -> frozenset[str]:
    if not isinstance(node, ast.Dict):
        return frozenset()
    tokens: set[str] = set()
    for key, value in zip(node.keys, node.values, strict=False):
        if _constant_string_value(key) != "Authorization":
            continue
        for header_value in _literal_string_values(value, name_literal_bindings):
            header_token = _authorization_header_token(header_value)
            if header_token in POC_AUTH_SESSION_AUTH_TOKEN_LITERALS:
                tokens.add(header_token)
    return frozenset(tokens)


def _call_passes_privileged_auth_token(node: ast.Call) -> bool:
    return bool(_call_privileged_auth_tokens(node))


def _call_privileged_auth_tokens(
    node: ast.Call,
    name_literal_bindings: Mapping[str, frozenset[str]] | None = None,
) -> frozenset[str]:
    tokens: set[str] = set()
    for keyword in node.keywords:
        if keyword.arg == "role_token":
            tokens.update(
                _privileged_auth_tokens_in_node(
                    keyword.value,
                    name_literal_bindings,
                )
            )
        if keyword.arg == "headers":
            tokens.update(
                _auth_header_tokens_in_node(
                    keyword.value,
                    name_literal_bindings,
                )
            )
    return frozenset(tokens)


def _call_bound_auth_header_tokens(
    node: ast.Call,
    auth_header_tokens_by_name: Mapping[str, frozenset[str]],
) -> frozenset[str]:
    tokens: set[str] = set()
    for keyword in node.keywords:
        if (
            keyword.arg == "headers"
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id in auth_header_tokens_by_name
        ):
            tokens.update(auth_header_tokens_by_name[keyword.value.id])
    return frozenset(tokens)


def _assert_checks_success_status(node: ast.Assert) -> bool:
    return isinstance(
        node.test, ast.Compare
    ) and _compare_checks_success_status_equality(node.test)


def _status_expr_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return f"name:{node.id}"
    if isinstance(node, ast.Attribute):
        parent = _status_expr_key(node.value)
        if parent is not None:
            return f"attr:{parent}.{node.attr}"
    return None


def _success_status_asserted_expr_keys(node: ast.Assert) -> frozenset[str]:
    return _status_asserted_expr_keys(node, POC_AUTH_SESSION_SUCCESS_STATUS_CODES)


def _status_asserted_expr_keys(
    node: ast.Assert,
    status_codes: frozenset[int],
) -> frozenset[str]:
    if not isinstance(node.test, ast.Compare):
        return frozenset()
    compare = node.test
    if len(compare.ops) != 1 or not isinstance(compare.ops[0], ast.Eq):
        return frozenset()
    parts = (compare.left, *compare.comparators)
    values = tuple(_constant_int_value(part) for part in parts)
    if not any(value in status_codes for value in values):
        return frozenset()
    keys = {
        key
        for part, value in zip(parts, values, strict=False)
        if value is None
        for key in (_status_expr_key(part),)
        if key is not None
    }
    return frozenset(keys)


def _asserted_status_code(node: ast.Assert) -> int | None:
    if not isinstance(node.test, ast.Compare):
        return None
    parts = (node.test.left, *node.test.comparators)
    for part in parts:
        value = _constant_int_value(part)
        if value is not None:
            return value
    return None


def _assigned_status_expr_keys(target: ast.AST) -> frozenset[str]:
    if isinstance(target, (ast.Tuple, ast.List)) and target.elts:
        key = _status_expr_key(target.elts[0])
        return frozenset((key,)) if key is not None else frozenset()
    return frozenset()


def _assigned_name_targets(target: ast.AST) -> frozenset[str]:
    if isinstance(target, ast.Name):
        return frozenset((target.id,))
    if isinstance(target, (ast.Tuple, ast.List)):
        return frozenset(
            element.id for element in target.elts if isinstance(element, ast.Name)
        )
    return frozenset()


def _assigned_observation_keys(target: ast.AST) -> frozenset[str]:
    keys: set[str] = set()
    key = _status_expr_key(target)
    if key is not None:
        keys.add(key)
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            keys.update(_assigned_observation_keys(element))
    return frozenset(keys)


def _assignment_targets(
    node: ast.Assign | ast.AnnAssign | ast.AugAssign,
) -> tuple[ast.AST, ...]:
    return node.targets if isinstance(node, ast.Assign) else (node.target,)


def _walk_statement_without_nested_scopes(node: ast.AST) -> tuple[ast.AST, ...]:
    nodes: list[ast.AST] = []

    class Visitor(ast.NodeVisitor):
        def generic_visit(self, child: ast.AST) -> None:
            nodes.append(child)
            super().generic_visit(child)

        def visit_FunctionDef(self, child: ast.FunctionDef) -> None:
            nodes.append(child)

        def visit_AsyncFunctionDef(self, child: ast.AsyncFunctionDef) -> None:
            nodes.append(child)

        def visit_ClassDef(self, child: ast.ClassDef) -> None:
            nodes.append(child)

        def visit_Lambda(self, child: ast.Lambda) -> None:
            nodes.append(child)

        def visit_BoolOp(self, child: ast.BoolOp) -> None:
            nodes.append(child)

        def visit_IfExp(self, child: ast.IfExp) -> None:
            nodes.append(child)

        def visit_ListComp(self, child: ast.ListComp) -> None:
            nodes.append(child)

        def visit_SetComp(self, child: ast.SetComp) -> None:
            nodes.append(child)

        def visit_DictComp(self, child: ast.DictComp) -> None:
            nodes.append(child)

        def visit_GeneratorExp(self, child: ast.GeneratorExp) -> None:
            nodes.append(child)

    Visitor().visit(node)
    return tuple(nodes)


def _target_name_bindings(target: ast.AST) -> frozenset[str]:
    if isinstance(target, ast.Name):
        return frozenset((target.id,))
    if isinstance(target, (ast.Tuple, ast.List)):
        return frozenset(
            name
            for element in target.elts
            for name in _target_name_bindings(element)
        )
    return frozenset()


def _statement_bound_names(node: ast.stmt) -> frozenset[str]:
    names: set[str] = set()
    for child in _walk_statement_without_nested_scopes(node):
        if isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for target in _assignment_targets(child):
                names.update(_target_name_bindings(target))
        if isinstance(child, ast.Delete):
            for target in child.targets:
                names.update(_target_name_bindings(target))
    return frozenset(names)


def _clear_status_observations_for_targets(
    status_observations: dict[str, "_AuthenticatedStatusObservation"],
    targets: Iterable[ast.AST],
) -> None:
    target_keys = {
        key for target in targets for key in _assigned_observation_keys(target)
    }
    for key in target_keys:
        status_observations.pop(key, None)
        prefix = f"attr:{key}."
        for observed_key in tuple(status_observations):
            if observed_key.startswith(prefix):
                status_observations.pop(observed_key, None)


def _setattr_status_targets(node: ast.AST) -> tuple[ast.Attribute, ...]:
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        and len(node.args) >= 2
        and _constant_string_value(node.args[1]) == "status"
    ):
        return ()
    return (ast.Attribute(value=node.args[0], attr="status", ctx=ast.Store()),)


def _statement_unconditionally_exits(statement: ast.stmt) -> bool:
    if isinstance(statement, (ast.Return, ast.Raise)):
        return True
    if (
        isinstance(statement, ast.Assert)
        and isinstance(statement.test, ast.Constant)
        and not bool(statement.test.value)
    ):
        return True
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
        return _dotted_name(statement.value.func) in {
            "pytest.fail",
            "pytest.skip",
            "pytest.xfail",
            "fail",
            "skip",
            "xfail",
        }
    return False


def _compound_statement_contains_exit(statement: ast.stmt) -> bool:
    return any(
        isinstance(child, ast.stmt) and _statement_unconditionally_exits(child)
        for child in _walk_statement_without_nested_scopes(statement)
    )


@dataclass(frozen=True)
class _OrderedFunctionStatement:
    statement: ast.stmt
    bound_names_before: frozenset[str] = frozenset()


def _with_optional_var_bindings(node: ast.With | ast.AsyncWith) -> frozenset[str]:
    names: set[str] = set()
    for item in node.items:
        if item.optional_vars is not None:
            names.update(_target_name_bindings(item.optional_vars))
    return frozenset(names)


def _with_statement_uses_pytest_raises(node: ast.With | ast.AsyncWith) -> bool:
    return any(
        isinstance(item.context_expr, ast.Call)
        and _dotted_name(item.context_expr.func) in {"pytest.raises", "raises"}
        for item in node.items
    )


def _ordered_function_statements(
    statements: Iterable[ast.stmt],
    *,
    bound_names_before: frozenset[str] = frozenset(),
) -> Iterable[_OrderedFunctionStatement]:
    pending_bound_names = set(bound_names_before)
    for statement in statements:
        if isinstance(statement, (ast.Try, ast.TryStar)):
            body_exited = False
            for child in _ordered_function_statements(
                statement.body,
                bound_names_before=frozenset(pending_bound_names),
            ):
                yield child
                body_exited = _statement_unconditionally_exits(child.statement)
            if body_exited:
                break
            yield from _ordered_function_statements(
                statement.orelse,
                bound_names_before=frozenset(pending_bound_names),
            )
            yield from _ordered_function_statements(
                statement.finalbody,
                bound_names_before=frozenset(pending_bound_names),
            )
            continue
        if isinstance(
            statement,
            (ast.With, ast.AsyncWith),
        ):
            if _with_statement_uses_pytest_raises(statement):
                continue
            pending_bound_names.update(_with_optional_var_bindings(statement))
            body_exited = False
            for child in _ordered_function_statements(
                statement.body,
                bound_names_before=frozenset(pending_bound_names),
            ):
                yield child
                body_exited = _statement_unconditionally_exits(child.statement)
            if body_exited:
                break
            continue
        if isinstance(statement, (ast.For, ast.AsyncFor, ast.While, ast.If)):
            if _compound_statement_contains_exit(statement):
                break
            continue
        yield _OrderedFunctionStatement(
            statement,
            bound_names_before=frozenset(pending_bound_names),
        )
        if _statement_unconditionally_exits(statement):
            break


def _method_call_receiver_name(node: ast.Call, method_name: str) -> str | None:
    if not isinstance(node.func, ast.Attribute) or node.func.attr != method_name:
        return None
    if isinstance(node.func.value, ast.Name):
        return node.func.value.id
    return None


@dataclass(frozen=True)
class _AuthenticatedStatusObservation:
    tokens: frozenset[str]
    env_tokens_before_request: frozenset[str]
    direct_tokens_before_request: frozenset[str] = frozenset()
    status_code: int | None = None
    response_name: str | None = None
    request_method: str | None = None
    request_path: str | None = None
    string_literals: frozenset[str] = frozenset()
    asserted_response_literals: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _TrustedPocStatusHelper:
    method: str
    path: str
    payload_parameter: str
    payload_arg_position: int
    function_node: ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class _PocAuthSessionEvidenceContext:
    test_functions: Mapping[str, ast.FunctionDef | ast.AsyncFunctionDef]
    local_auth_token_helpers: Mapping[str, frozenset[str]]
    trusted_status_helpers: Mapping[str, _TrustedPocStatusHelper]
    module_shadowed_http_constructor_names: frozenset[str]


@dataclass(frozen=True)
class _PocAuthSessionValidationStep:
    refs: tuple[str, ...]
    ref_is_present: Callable[[_PocAuthSessionEvidenceContext, str], bool]


def _string_literals_in_node(node: ast.AST) -> frozenset[str]:
    literals: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Constant):
            continue
        if isinstance(child.value, str):
            literals.add(child.value)
        if isinstance(child.value, bytes):
            try:
                literals.add(child.value.decode("utf-8"))
            except UnicodeDecodeError:
                continue
    return frozenset(literals)


def _asserted_string_literals_in_node(node: ast.AST) -> frozenset[str]:
    literals: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            literals.update(_string_literals_in_node(child))
    return frozenset(literals)


def _loaded_name_references(node: ast.AST) -> frozenset[str]:
    return frozenset(
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    )


def _string_literals_with_bound_names(
    node: ast.AST,
    name_literal_bindings: Mapping[str, frozenset[str]],
) -> frozenset[str]:
    literals = set(_string_literals_in_node(node))
    for name in _loaded_name_references(node):
        literals.update(name_literal_bindings.get(name, frozenset()))
    return frozenset(literals)


def _update_name_literal_bindings_after_statement(
    node: ast.stmt,
    name_literal_bindings: dict[str, frozenset[str]],
) -> None:
    for child in _walk_statement_without_nested_scopes(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr
            in {
                "append",
                "clear",
                "extend",
                "insert",
                "pop",
                "popitem",
                "remove",
                "setdefault",
                "sort",
                "update",
                "write",
            }
            and isinstance(child.func.value, ast.Name)
        ):
            name_literal_bindings.pop(child.func.value.id, None)
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        value = node.value
        if value is None:
            return
        if any(isinstance(child, ast.IfExp) for child in ast.walk(value)):
            for target in _assignment_targets(node):
                for name in _target_name_bindings(target):
                    name_literal_bindings.pop(name, None)
            return
        value_literals = _string_literals_with_bound_names(
            value, name_literal_bindings
        )
        for target in _assignment_targets(node):
            for name in _target_name_bindings(target):
                name_literal_bindings[name] = value_literals
    if isinstance(node, ast.Delete):
        for target in node.targets:
            for name in _target_name_bindings(target):
                name_literal_bindings.pop(name, None)


def _mutated_name_bindings(node: ast.stmt) -> frozenset[str]:
    names: set[str] = set()
    for child in _walk_statement_without_nested_scopes(node):
        if isinstance(child, (ast.Assign, ast.AnnAssign)):
            for target in _assignment_targets(child):
                if isinstance(target, ast.Subscript) and isinstance(
                    target.value, ast.Name
                ):
                    names.add(target.value.id)
        if isinstance(child, ast.Delete):
            for target in child.targets:
                if isinstance(target, ast.Subscript) and isinstance(
                    target.value, ast.Name
                ):
                    names.add(target.value.id)
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr
            in {
                "append",
                "clear",
                "extend",
                "insert",
                "pop",
                "popitem",
                "remove",
                "setdefault",
                "sort",
                "update",
                "write",
            }
            and isinstance(child.func.value, ast.Name)
        ):
            names.add(child.func.value.id)
    return frozenset(names)


def _mutates_server_local_auth_tokens(node: ast.stmt) -> bool:
    for child in _walk_statement_without_nested_scopes(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr
            in {
                "clear",
                "pop",
                "popitem",
                "setdefault",
                "update",
            }
            and _targets_server_local_auth_tokens(child.func.value)
        ):
            return True
        if isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for target in _assignment_targets(child):
                if (
                    isinstance(target, ast.Subscript)
                    and _targets_server_local_auth_tokens(target.value)
                ):
                    return True
        if isinstance(child, ast.Delete):
            for target in child.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and _targets_server_local_auth_tokens(target.value)
                ):
                    return True
    return False


def _request_method_and_path(node: ast.Call) -> tuple[str | None, str | None]:
    if _method_call_receiver_name(node, "request") is None:
        return None, None
    method = _constant_string_value(node.args[0]) if len(node.args) >= 1 else None
    path = _constant_string_value(node.args[1]) if len(node.args) >= 2 else None
    return method, path


def _node_references_response_status(
    node: ast.AST,
    response_names: frozenset[str],
) -> bool:
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and child.attr == "status"
            and isinstance(child.value, ast.Name)
            and child.value.id in response_names
        ):
            return True
    return False


def _condition_checks_name_not_none(node: ast.AST, name: str) -> bool:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return False
    parts = (node.left, *node.comparators)
    return (
        any(isinstance(part, ast.Name) and part.id == name for part in parts)
        and any(isinstance(part, ast.Constant) and part.value is None for part in parts)
        and isinstance(node.ops[0], (ast.IsNot, ast.NotEq))
    )


def _trusted_helper_ordered_statements(
    statements: Iterable[ast.stmt],
    *,
    required_non_none_name: str,
) -> Iterable[_OrderedFunctionStatement]:
    for statement in statements:
        if (
            isinstance(statement, ast.If)
            and _condition_checks_name_not_none(
                statement.test,
                required_non_none_name,
            )
            and not statement.orelse
        ):
            yield from _ordered_function_statements(statement.body)
            continue
        yield from _ordered_function_statements((statement,))


def _trusted_poc_status_helpers(test_source: str) -> dict[str, _TrustedPocStatusHelper]:
    helpers: dict[str, _TrustedPocStatusHelper] = {}
    functions = _top_level_function_nodes(test_source)
    for name, expected_request in POC_AUTH_SESSION_TRUSTED_STATUS_HELPERS.items():
        node = functions.get(name)
        if node is None or _test_function_is_skipped_or_xfailed(node):
            continue
        if len(node.args.args) < 2:
            continue
        connection_arg = node.args.args[0].arg
        payload_arg = node.args.args[1].arg
        if not any(arg.arg == "role_token" for arg in node.args.kwonlyargs):
            continue
        saw_expected_request = False
        saw_role_token_header = False
        saw_payload_body = False
        saw_returned_response_status = False
        header_names_with_role_token: set[str] = set()
        payload_bound_names = {payload_arg}
        response_names: set[str] = set()
        helper_is_trusted = True
        for ordered_statement in _trusted_helper_ordered_statements(
            node.body,
            required_non_none_name="role_token",
        ):
            statement = ordered_statement.statement
            if connection_arg in ordered_statement.bound_names_before:
                helper_is_trusted = False
                break
            header_names_with_role_token.update(
                _header_names_assigned_authorization_role_token(statement)
            )
            for child in _walk_statement_without_nested_scopes(statement):
                if isinstance(child, (ast.Assign, ast.AnnAssign)):
                    value = child.value
                    if value is not None and (
                        _loaded_name_references(value) & payload_bound_names
                    ):
                        for target in _assignment_targets(child):
                            payload_bound_names.update(_target_name_bindings(target))
                    if isinstance(value, ast.Call) and (
                        _method_call_receiver_name(value, "getresponse")
                        == connection_arg
                    ):
                        if (
                            saw_expected_request
                            and saw_role_token_header
                            and saw_payload_body
                        ):
                            for target in _assignment_targets(child):
                                response_names.update(_assigned_name_targets(target))
                if not isinstance(child, ast.Call):
                    continue
                if _method_call_receiver_name(child, "request") == connection_arg:
                    saw_expected_request = (
                        _request_method_and_path(child) == expected_request
                    )
                    if saw_expected_request:
                        saw_role_token_header = (
                            _request_uses_authorization_role_token(child)
                            or _request_uses_authorization_role_token_header_name(
                                child,
                                frozenset(header_names_with_role_token),
                            )
                        )
                        saw_payload_body = _request_body_references_names(
                            child, frozenset(payload_bound_names)
                        )
            if isinstance(statement, ast.Return) and statement.value is not None:
                saw_returned_response_status = _node_references_response_status(
                    statement.value,
                    frozenset(response_names),
                )
            if connection_arg in _statement_bound_names(statement):
                helper_is_trusted = False
                break
        if (
            helper_is_trusted
            and saw_expected_request
            and saw_role_token_header
            and saw_payload_body
            and saw_returned_response_status
        ):
            helpers[name] = _TrustedPocStatusHelper(
                method=expected_request[0],
                path=expected_request[1],
                payload_parameter=payload_arg,
                payload_arg_position=1,
                function_node=node,
            )
    return helpers


def _trusted_poc_status_helper_payload_literals(
    node: ast.Call,
    helper: _TrustedPocStatusHelper,
    name_literal_bindings: Mapping[str, frozenset[str]],
) -> frozenset[str]:
    literals: set[str] = set()
    if len(node.args) > helper.payload_arg_position:
        literals.update(
            _string_literals_with_bound_names(
                node.args[helper.payload_arg_position],
                name_literal_bindings,
            )
        )
    for keyword in node.keywords:
        if keyword.arg == helper.payload_parameter:
            literals.update(
                _string_literals_with_bound_names(
                    keyword.value,
                    name_literal_bindings,
                )
            )
    return frozenset(literals)


def _trusted_poc_status_helper_for_call(
    node: ast.Call,
    poc_connection_names: frozenset[str],
    trusted_status_helpers: Mapping[str, _TrustedPocStatusHelper],
) -> _TrustedPocStatusHelper | None:
    helper_name = _dotted_name(node.func)
    if helper_name not in trusted_status_helpers:
        return None
    helper = trusted_status_helpers[helper_name]
    if not _call_satisfies_function_signature(node, helper.function_node):
        return None
    if not node.args or not isinstance(node.args[0], ast.Name):
        return None
    if node.args[0].id not in poc_connection_names:
        return None
    return helper


def _request_uses_authorization_role_token(node: ast.Call) -> bool:
    for keyword in node.keywords:
        if keyword.arg != "headers" or not isinstance(keyword.value, ast.Dict):
            continue
        for key, value in zip(keyword.value.keys, keyword.value.values, strict=False):
            if _constant_string_value(key) != "Authorization":
                continue
            if _value_formats_bearer_role_token(value):
                return True
    return False


def _request_uses_authorization_role_token_header_name(
    node: ast.Call,
    header_names: frozenset[str],
) -> bool:
    for keyword in node.keywords:
        if keyword.arg != "headers" or not isinstance(keyword.value, ast.Name):
            continue
        if keyword.value.id in header_names:
            return True
    return False


def _request_body_references_names(
    node: ast.Call,
    names: frozenset[str],
) -> bool:
    body_node: ast.AST | None = None
    if len(node.args) >= 3:
        body_node = node.args[2]
    for keyword in node.keywords:
        if keyword.arg == "body":
            body_node = keyword.value
            break
    if body_node is not None and any(
        isinstance(child, (ast.BoolOp, ast.IfExp, ast.Lambda))
        for child in ast.walk(body_node)
    ):
        return False
    return bool(body_node is not None and _loaded_name_references(body_node) & names)


def _header_names_assigned_authorization_role_token(node: ast.AST) -> frozenset[str]:
    names: set[str] = set()
    for child in _walk_statement_without_nested_scopes(node):
        if not isinstance(child, (ast.Assign, ast.AnnAssign)):
            continue
        for target in _assignment_targets(child):
            header_name = _authorization_header_assignment_name(target)
            if header_name is None:
                continue
            if _value_formats_bearer_role_token(child.value):
                names.add(header_name)
        if isinstance(child.value, ast.Dict):
            for target in _assignment_targets(child):
                for name in _assigned_name_targets(target):
                    if _headers_dict_uses_authorization_role_token(child.value):
                        names.add(name)
    return frozenset(names)


def _authorization_header_assignment_name(target: ast.AST) -> str | None:
    if not isinstance(target, ast.Subscript):
        return None
    if _constant_string_value(target.slice) != "Authorization":
        return None
    if isinstance(target.value, ast.Name):
        return target.value.id
    return None


def _headers_dict_uses_authorization_role_token(node: ast.Dict) -> bool:
    for key, value in zip(node.keys, node.values, strict=False):
        if _constant_string_value(key) != "Authorization":
            continue
        if _value_formats_bearer_role_token(value):
            return True
    return False


def _value_formats_bearer_role_token(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr):
        return (
            len(node.values) == 2
            and isinstance(node.values[0], ast.Constant)
            and node.values[0].value == "Bearer "
            and isinstance(node.values[1], ast.FormattedValue)
            and isinstance(node.values[1].value, ast.Name)
            and node.values[1].value.id == "role_token"
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        parts = _string_concatenation_parts(node)
        return (
            len(parts) == 2
            and isinstance(parts[0], ast.Constant)
            and parts[0].value == "Bearer "
            and isinstance(parts[1], ast.Name)
            and parts[1].id == "role_token"
        )
    return False


def _string_concatenation_parts(node: ast.AST) -> tuple[ast.AST, ...]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return (
            *_string_concatenation_parts(node.left),
            *_string_concatenation_parts(node.right),
        )
    return (node,)


def _node_references_poc_server_address(
    node: ast.AST,
    poc_server_names: frozenset[str],
) -> bool:
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and child.attr in {"server_address", "server_port"}
            and isinstance(child.value, ast.Name)
            and child.value.id in poc_server_names
        ):
            return True
    return False


def _node_references_poc_server_port(
    node: ast.AST,
    poc_server_names: frozenset[str],
) -> bool:
    for child in ast.walk(node):
        if (
            _server_address_subscript_index(child) == 1
            and isinstance(child.value, ast.Attribute)
            and isinstance(child.value.value, ast.Name)
            and child.value.value.id in poc_server_names
        ):
            return True
        if (
            isinstance(child, ast.Attribute)
            and child.attr == "server_port"
            and isinstance(child.value, ast.Name)
            and child.value.id in poc_server_names
        ):
            return True
    return False


def _server_address_subscript_index(node: ast.AST) -> int | None:
    if not isinstance(node, ast.Subscript):
        return None
    if not (
        isinstance(node.value, ast.Attribute)
        and node.value.attr == "server_address"
        and isinstance(node.value.value, ast.Name)
    ):
        return None
    index = _constant_int_value(node.slice)
    return index if index in {0, 1} else None


def _node_references_poc_server_host(
    node: ast.AST,
    poc_server_names: frozenset[str],
) -> bool:
    host = _constant_string_value(node)
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    for child in ast.walk(node):
        if (
            _server_address_subscript_index(child) == 0
            and isinstance(child.value, ast.Attribute)
            and isinstance(child.value.value, ast.Name)
            and child.value.value.id in poc_server_names
        ):
            return True
        if (
            isinstance(child, ast.Attribute)
            and child.attr == "server_port"
            and isinstance(child.value, ast.Name)
            and child.value.id in poc_server_names
        ):
            return False
    return False


def _call_creates_poc_server(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if _dotted_name(node.func) not in {
        "ThreadingHTTPServer",
        "http.server.ThreadingHTTPServer",
    }:
        return False
    if len(node.args) < 2:
        return False
    handler_name = _dotted_name(node.args[1])
    return handler_name == "PocWebRequestHandler"


def _call_creates_poc_http_connection(
    node: ast.AST,
    poc_server_names: frozenset[str],
    *,
    shadowed_constructor_names: frozenset[str] = frozenset(),
) -> bool:
    constructor_name = _dotted_name(node.func) if isinstance(node, ast.Call) else None
    if (
        constructor_name == "HTTPConnection"
        and "HTTPConnection" in shadowed_constructor_names
    ):
        return False
    if constructor_name == "http.client.HTTPConnection" and (
        "http" in shadowed_constructor_names
    ):
        return False
    if not (
        isinstance(node, ast.Call)
        and constructor_name in {"HTTPConnection", "http.client.HTTPConnection"}
        and len(node.args) >= 2
    ):
        return False
    return _node_references_poc_server_host(
        node.args[0],
        poc_server_names,
    ) and _node_references_poc_server_port(node.args[1], poc_server_names)


def _authenticated_success_status_observations(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    local_auth_token_helpers: dict[str, frozenset[str]] | None = None,
    trusted_status_helpers: Mapping[str, _TrustedPocStatusHelper] | None = None,
    module_shadowed_http_constructor_names: frozenset[str] = frozenset(),
) -> tuple[_AuthenticatedStatusObservation, ...]:
    if local_auth_token_helpers is None:
        local_auth_token_helpers = {}
    if trusted_status_helpers is None:
        trusted_status_helpers = {}
    env_tokens_seen: set[str] = set()
    direct_tokens_seen: set[str] = set()
    active_monkeypatch_fixture_names = set(
        name for name in _function_arg_names(node) if name == "monkeypatch"
    )
    pending_request_by_connection: dict[
        str, _AuthenticatedStatusObservation
    ] = {}
    poc_server_names: set[str] = set()
    poc_connection_names: set[str] = set()
    status_observations: dict[str, _AuthenticatedStatusObservation] = {}
    success_observations: list[_AuthenticatedStatusObservation] = []
    name_literal_bindings: dict[str, frozenset[str]] = {}
    shadowed_local_auth_helper_names = set(
        _function_arg_names(node) & frozenset(local_auth_token_helpers)
    )
    shadowed_http_constructor_names = set(
        module_shadowed_http_constructor_names
        | (_function_arg_names(node) & frozenset(("HTTPConnection", "http")))
    )
    shadowed_trusted_status_helper_names = set(
        _function_arg_names(node) & frozenset(trusted_status_helpers)
    )
    os_module_available = "os" not in _function_arg_names(node)

    for ordered_statement in _ordered_function_statements(node.body):
        statement = ordered_statement.statement
        if "os" in ordered_statement.bound_names_before:
            os_module_available = False
        shadowed_http_constructor_names.update(
            ordered_statement.bound_names_before
            & frozenset(("HTTPConnection", "http"))
        )
        active_monkeypatch_fixture_names.difference_update(
            ordered_statement.bound_names_before
        )
        shadowed_local_auth_helper_names.update(
            ordered_statement.bound_names_before & frozenset(local_auth_token_helpers)
        )
        shadowed_trusted_status_helper_names.update(
            ordered_statement.bound_names_before & frozenset(trusted_status_helpers)
        )
        visible_local_auth_token_helpers = _visible_local_auth_token_helpers(
            local_auth_token_helpers,
            shadowed_local_auth_helper_names,
        )
        response_connections_seen = {
            connection_name
            for child in _walk_statement_without_nested_scopes(statement)
            if isinstance(child, ast.Call)
            for connection_name in (_method_call_receiver_name(child, "getresponse"),)
            if connection_name is not None
        }
        response_connections_recorded: set[str] = set()

        for child in _walk_statement_without_nested_scopes(statement):
            if not isinstance(child, ast.Call):
                continue
            configured_tokens = _setattr_local_auth_token_value_tokens(
                child,
                local_auth_token_helpers=visible_local_auth_token_helpers,
            )
            if configured_tokens is not None:
                direct_tokens_seen = set(configured_tokens)
                continue
            if _calls_delattr_local_auth_tokens(child):
                direct_tokens_seen.clear()
                continue
            connection_name = _method_call_receiver_name(child, "request")
            if connection_name is None:
                continue
            if connection_name not in poc_connection_names:
                continue
            tokens = _call_privileged_auth_tokens(child, name_literal_bindings)
            if tokens:
                method, path = _request_method_and_path(child)
                pending_request_by_connection[connection_name] = (
                    _AuthenticatedStatusObservation(
                        tokens=tokens,
                        env_tokens_before_request=frozenset(env_tokens_seen),
                        direct_tokens_before_request=frozenset(direct_tokens_seen),
                        request_method=method,
                        request_path=path,
                        string_literals=_string_literals_with_bound_names(
                            child, name_literal_bindings
                        ),
                    )
                )

        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            targets = _assignment_targets(statement)
            _clear_status_observations_for_targets(status_observations, targets)
            for target in targets:
                for name in _assigned_name_targets(target):
                    pending_request_by_connection.pop(name, None)
                    if name == "server":
                        direct_tokens_seen.clear()
                    if _call_creates_poc_server(value):
                        poc_server_names.add(name)
                    else:
                        poc_server_names.discard(name)
                    if _call_creates_poc_http_connection(
                        value,
                        frozenset(poc_server_names),
                        shadowed_constructor_names=frozenset(
                            shadowed_http_constructor_names
                        ),
                    ):
                        poc_connection_names.add(name)
                    else:
                        poc_connection_names.discard(name)
                if _targets_server_local_auth_tokens(target):
                    direct_tokens_seen = set(
                        _direct_auth_token_value_tokens(
                            value,
                            local_auth_token_helpers=visible_local_auth_token_helpers,
                        )
                    )
            if isinstance(value, ast.Call):
                tokens = _call_privileged_auth_tokens(value, name_literal_bindings)
                if tokens:
                    helper_name = _dotted_name(value.func)
                    trusted_helper = (
                        None
                        if helper_name in shadowed_trusted_status_helper_names
                        else _trusted_poc_status_helper_for_call(
                            value,
                            frozenset(poc_connection_names),
                            trusted_status_helpers,
                        )
                    )
                    if trusted_helper is None:
                        tokens = frozenset()
                if tokens:
                    method = trusted_helper.method if trusted_helper is not None else None
                    path = trusted_helper.path if trusted_helper is not None else None
                    observation = _AuthenticatedStatusObservation(
                        tokens=tokens,
                        env_tokens_before_request=frozenset(env_tokens_seen),
                        direct_tokens_before_request=frozenset(direct_tokens_seen),
                        request_method=method,
                        request_path=path,
                        string_literals=_string_literals_with_bound_names(
                            value, name_literal_bindings
                        )
                        if trusted_helper is None
                        else _trusted_poc_status_helper_payload_literals(
                            value,
                            trusted_helper,
                            name_literal_bindings,
                        ),
                    )
                    for target in targets:
                        for key in _assigned_status_expr_keys(target):
                            status_observations[key] = observation

                connection_name = _method_call_receiver_name(value, "getresponse")
                if connection_name is not None:
                    pending = pending_request_by_connection.pop(
                        connection_name, None
                    )
                    response_connections_recorded.add(connection_name)
                    if pending is not None:
                        observation = _AuthenticatedStatusObservation(
                            tokens=pending.tokens,
                            env_tokens_before_request=(
                                pending.env_tokens_before_request
                            ),
                            direct_tokens_before_request=(
                                pending.direct_tokens_before_request
                            ),
                            request_method=pending.request_method,
                            request_path=pending.request_path,
                            string_literals=pending.string_literals,
                        )
                        for target in targets:
                            for name in _assigned_name_targets(target):
                                status_observations[
                                    f"attr:name:{name}.status"
                                ] = observation
        for connection_name in response_connections_seen - response_connections_recorded:
            pending_request_by_connection.pop(connection_name, None)

        for child in _walk_statement_without_nested_scopes(statement):
            _clear_status_observations_for_targets(
                status_observations,
                _setattr_status_targets(child),
            )

        if isinstance(statement, ast.Delete):
            _clear_status_observations_for_targets(
                status_observations, statement.targets
            )
            for target in statement.targets:
                if _targets_server_local_auth_tokens(target):
                    direct_tokens_seen.clear()
                for name in _target_name_bindings(target):
                    if name == "server":
                        direct_tokens_seen.clear()
                    poc_server_names.discard(name)
                    poc_connection_names.discard(name)
                    pending_request_by_connection.pop(name, None)

        if _mutates_server_local_auth_tokens(statement):
            direct_tokens_seen.clear()

        if isinstance(statement, ast.Assert):
            for key in _success_status_asserted_expr_keys(statement):
                observation = status_observations.get(key)
                if observation is not None:
                    success_observations.append(
                        _AuthenticatedStatusObservation(
                            tokens=observation.tokens,
                            env_tokens_before_request=(
                                observation.env_tokens_before_request
                            ),
                            direct_tokens_before_request=(
                                observation.direct_tokens_before_request
                            ),
                            status_code=_asserted_status_code(statement),
                            request_method=observation.request_method,
                            request_path=observation.request_path,
                            string_literals=observation.string_literals,
                        )
                    )

        active_monkeypatch_fixture_names.difference_update(
            _statement_bound_names(statement)
        )
        shadowed_local_auth_helper_names.update(
            _statement_bound_names(statement) & frozenset(local_auth_token_helpers)
        )
        shadowed_trusted_status_helper_names.update(
            _statement_bound_names(statement) & frozenset(trusted_status_helpers)
        )
        _update_name_literal_bindings_after_statement(
            statement, name_literal_bindings
        )
        env_tokens_seen = set(
            _env_auth_tokens_after_statement(
                statement,
                env_tokens_seen,
                monkeypatch_fixture_names=frozenset(
                    active_monkeypatch_fixture_names
                ),
                os_module_available=os_module_available,
            )
        )
        if "os" in _statement_bound_names(statement):
            os_module_available = False
        shadowed_http_constructor_names.update(
            _statement_bound_names(statement) & frozenset(("HTTPConnection", "http"))
        )

    return tuple(success_observations)


def _test_function_has_authenticated_success_markers(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    local_auth_token_helpers: dict[str, frozenset[str]] | None = None,
    trusted_status_helpers: Mapping[str, _TrustedPocStatusHelper] | None = None,
    module_shadowed_http_constructor_names: frozenset[str] = frozenset(),
) -> bool:
    return bool(
        _authenticated_success_status_observations(
            node,
            local_auth_token_helpers=local_auth_token_helpers,
            trusted_status_helpers=trusted_status_helpers,
            module_shadowed_http_constructor_names=(
                module_shadowed_http_constructor_names
            ),
        )
    )


def _test_function_matches_success_ref_expectation(
    ref: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    local_auth_token_helpers: dict[str, frozenset[str]] | None = None,
    trusted_status_helpers: Mapping[str, _TrustedPocStatusHelper] | None = None,
    module_shadowed_http_constructor_names: frozenset[str] = frozenset(),
) -> bool:
    expectation = POC_AUTH_SESSION_SUCCESS_REF_EXPECTATIONS.get(ref)
    if expectation is None:
        return _test_function_has_authenticated_success_markers(
            node,
            local_auth_token_helpers=local_auth_token_helpers,
            trusted_status_helpers=trusted_status_helpers,
            module_shadowed_http_constructor_names=(
                module_shadowed_http_constructor_names
            ),
        )
    expected_tokens = expectation.get("tokens", frozenset())
    expected_status_codes = expectation.get("status_codes", frozenset())
    expected_method = expectation.get("method")
    expected_path = expectation.get("path")
    auth_source = expectation.get("auth_source")
    required_literals = expectation.get("required_literals", frozenset())
    for observation in _authenticated_success_status_observations(
        node,
        local_auth_token_helpers=local_auth_token_helpers,
        trusted_status_helpers=trusted_status_helpers,
        module_shadowed_http_constructor_names=module_shadowed_http_constructor_names,
    ):
        if not required_literals.issubset(observation.string_literals):
            continue
        if expected_tokens and not observation.tokens & expected_tokens:
            continue
        if (
            expected_status_codes
            and observation.status_code not in expected_status_codes
        ):
            continue
        if auth_source == "env":
            if not observation.tokens & observation.env_tokens_before_request:
                continue
            if observation.direct_tokens_before_request:
                continue
        if auth_source == "direct" and not (
            observation.tokens & observation.direct_tokens_before_request
        ):
            continue
        if expected_method is not None and observation.request_method != expected_method:
            continue
        if expected_path is not None and observation.request_path != expected_path:
            continue
        return True
    return False


def _assigns_server_local_auth_tokens(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for statement in node.body:
        for child in _walk_statement_without_nested_scopes(statement):
            if _calls_setattr_local_auth_tokens(child):
                return True
            if not isinstance(child, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            targets = (
                child.targets if isinstance(child, ast.Assign) else (child.target,)
            )
            if any(_targets_server_local_auth_tokens(target) for target in targets):
                return True
    return False


def _calls_setattr_local_auth_tokens(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "server"
        and _constant_string_value(node.args[1]) == "local_auth_tokens"
    )


def _env_auth_tokens_after_statement(
    node: ast.stmt,
    current_tokens: Iterable[str],
    *,
    monkeypatch_fixture_names: frozenset[str] = frozenset(),
    os_module_available: bool = True,
) -> frozenset[str]:
    tokens = set(current_tokens)
    for child in _walk_statement_without_nested_scopes(node):
        if isinstance(child, ast.Call):
            func = child.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "setenv"
                and isinstance(func.value, ast.Name)
                and func.value.id in monkeypatch_fixture_names
                and len(child.args) >= 2
                and _constant_string_value(child.args[0]) == POC_AUTH_SESSION_ENV_VAR
            ):
                tokens = set(
                    _env_auth_token_value_tokens(
                        _constant_string_value(child.args[1]) or ""
                    )
                )
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "delenv"
                and child.args
                and _constant_string_value(child.args[0]) == POC_AUTH_SESSION_ENV_VAR
            ):
                tokens.clear()
            if (
                os_module_available
                and isinstance(func, ast.Attribute)
                and func.attr == "pop"
                and _is_os_environ_node(func.value)
                and child.args
                and _constant_string_value(child.args[0]) == POC_AUTH_SESSION_ENV_VAR
            ):
                tokens.clear()
            if (
                os_module_available
                and isinstance(func, ast.Attribute)
                and func.attr == "clear"
                and _is_os_environ_node(func.value)
            ):
                tokens.clear()
        if not isinstance(child, (ast.Assign, ast.AnnAssign)):
            continue
        targets = child.targets if isinstance(child, ast.Assign) else (child.target,)
        if os_module_available and any(
            _targets_env_auth_var(target) for target in targets
        ):
            tokens = set(
                _env_auth_token_value_tokens(
                    _constant_string_value(child.value) or ""
                )
            )
    if os_module_available and isinstance(node, ast.Delete) and any(
        _targets_env_auth_var(target) for target in node.targets
    ):
        tokens.clear()
    return frozenset(tokens)


def _env_auth_token_value_has_valid_mapping(value: str) -> bool:
    return bool(_env_auth_token_value_tokens(value))


def _env_auth_token_value_tokens(value: str) -> frozenset[str]:
    tokens: set[str] = set()
    for entry in value.split(","):
        identity, separator, token = entry.partition("=")
        token = token.strip()
        if separator != "=" or not token:
            continue
        role, principal_separator, principal = identity.strip().partition(":")
        if (
            principal_separator == ":"
            and role.strip() in POC_AUTH_SESSION_ENV_ROLES
            and role.strip() == POC_AUTH_SESSION_EXPECTED_ROLE_BY_TOKEN.get(token)
            and principal.strip()
        ):
            tokens.add(token)
    return frozenset(tokens)


def _privileged_auth_tokens_passed_by_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> frozenset[str]:
    tokens: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            tokens.update(_call_privileged_auth_tokens(child))
    return frozenset(tokens)


def _test_function_uses_env_auth_boundary(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    local_auth_token_helpers: dict[str, frozenset[str]] | None = None,
    trusted_status_helpers: Mapping[str, _TrustedPocStatusHelper] | None = None,
    module_shadowed_http_constructor_names: frozenset[str] = frozenset(),
) -> bool:
    if _assigns_server_local_auth_tokens(node):
        return False
    return any(
        observation.tokens & observation.env_tokens_before_request
        for observation in _authenticated_success_status_observations(
            node,
            local_auth_token_helpers=local_auth_token_helpers,
            trusted_status_helpers=trusted_status_helpers,
            module_shadowed_http_constructor_names=(
                module_shadowed_http_constructor_names
            ),
        )
    )


def _test_function_uses_direct_auth_boundary(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    local_auth_token_helpers: dict[str, frozenset[str]] | None = None,
    trusted_status_helpers: Mapping[str, _TrustedPocStatusHelper] | None = None,
    module_shadowed_http_constructor_names: frozenset[str] = frozenset(),
) -> bool:
    return any(
        observation.tokens & observation.direct_tokens_before_request
        for observation in _authenticated_success_status_observations(
            node,
            local_auth_token_helpers=local_auth_token_helpers,
            trusted_status_helpers=trusted_status_helpers,
            module_shadowed_http_constructor_names=(
                module_shadowed_http_constructor_names
            ),
        )
    )


def _direct_auth_tokens_after_statement(
    node: ast.stmt,
    current_tokens: Iterable[str],
    *,
    local_auth_token_helpers: dict[str, frozenset[str]] | None = None,
) -> frozenset[str]:
    if local_auth_token_helpers is None:
        local_auth_token_helpers = {}
    tokens = set(current_tokens)
    for child in _walk_statement_without_nested_scopes(node):
        if isinstance(child, ast.Call):
            configured_tokens = _setattr_local_auth_token_value_tokens(
                child,
                local_auth_token_helpers=local_auth_token_helpers,
            )
            if configured_tokens is not None:
                tokens = set(configured_tokens)
            if _calls_delattr_local_auth_tokens(child):
                tokens.clear()
        if isinstance(child, ast.Delete) and any(
            _targets_server_local_auth_tokens(target) for target in child.targets
        ):
            tokens.clear()
        if not isinstance(child, (ast.Assign, ast.AnnAssign)):
            continue
        for target in _assignment_targets(child):
            if _targets_server_local_auth_tokens(target):
                tokens = set(
                    _direct_auth_token_value_tokens(
                        child.value,
                        local_auth_token_helpers=local_auth_token_helpers,
                    )
                )
    return frozenset(tokens)


def _targets_server_local_auth_tokens(target: ast.AST) -> bool:
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "local_auth_tokens"
        and isinstance(target.value, ast.Name)
        and target.value.id == "server"
    )


def _setattr_local_auth_token_value_tokens(
    node: ast.Call,
    *,
    local_auth_token_helpers: dict[str, frozenset[str]] | None = None,
) -> frozenset[str] | None:
    if local_auth_token_helpers is None:
        local_auth_token_helpers = {}
    if (
        isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        and len(node.args) >= 3
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "server"
        and _constant_string_value(node.args[1]) == "local_auth_tokens"
    ):
        return _direct_auth_token_value_tokens(
            node.args[2],
            local_auth_token_helpers=local_auth_token_helpers,
        )
    return None


def _direct_auth_token_value_tokens(
    node: ast.AST,
    *,
    local_auth_token_helpers: dict[str, frozenset[str]] | None = None,
) -> frozenset[str]:
    if local_auth_token_helpers is None:
        local_auth_token_helpers = {}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.args or node.keywords:
            return frozenset()
        return local_auth_token_helpers.get(node.func.id, frozenset())
    return _local_auth_token_mapping_tokens(node)


def _calls_delattr_local_auth_tokens(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "delattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "server"
        and _constant_string_value(node.args[1]) == "local_auth_tokens"
    )


def _test_function_asserts_status_code(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    expected_status: int,
) -> bool:
    return bool(_asserted_status_observations(node, frozenset((expected_status,))))


def _direct_response_read_receiver_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        receiver = _method_call_receiver_name(node, "read")
        if receiver is not None:
            return receiver
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "decode"
            and isinstance(node.func.value, ast.Call)
        ):
            return _direct_response_read_receiver_name(node.func.value)
        if _dotted_name(node.func) == "json.loads" and node.args:
            return _direct_response_read_receiver_name(node.args[0])
    return None


def _response_read_receiver_names(node: ast.AST) -> frozenset[str]:
    receiver = _direct_response_read_receiver_name(node)
    return frozenset((receiver,)) if receiver is not None else frozenset()


def _asserted_status_observations(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    status_codes: frozenset[int],
    *,
    local_auth_token_helpers: dict[str, frozenset[str]] | None = None,
    module_shadowed_http_constructor_names: frozenset[str] = frozenset(),
) -> tuple[_AuthenticatedStatusObservation, ...]:
    if local_auth_token_helpers is None:
        local_auth_token_helpers = {}
    env_tokens_seen: set[str] = set()
    direct_tokens_seen: set[str] = set()
    active_monkeypatch_fixture_names = set(
        name for name in _function_arg_names(node) if name == "monkeypatch"
    )
    status_observations: dict[str, _AuthenticatedStatusObservation] = {}
    pending_request_by_connection: dict[str, _AuthenticatedStatusObservation] = {}
    response_names_by_body_name: dict[str, str] = {}
    asserted_literals_by_response_name: dict[str, set[str]] = {}
    asserted_observations: list[_AuthenticatedStatusObservation] = []
    name_literal_bindings: dict[str, frozenset[str]] = {}
    auth_header_tokens_by_name: dict[str, frozenset[str]] = {}
    poc_server_names: set[str] = set()
    poc_connection_names: set[str] = set()
    shadowed_local_auth_helper_names = set(
        _function_arg_names(node) & frozenset(local_auth_token_helpers)
    )
    shadowed_http_constructor_names = set(
        module_shadowed_http_constructor_names
        | (_function_arg_names(node) & frozenset(("HTTPConnection", "http")))
    )
    os_module_available = "os" not in _function_arg_names(node)
    for ordered_statement in _ordered_function_statements(node.body):
        statement = ordered_statement.statement
        if "os" in ordered_statement.bound_names_before:
            os_module_available = False
        shadowed_http_constructor_names.update(
            ordered_statement.bound_names_before
            & frozenset(("HTTPConnection", "http"))
        )
        active_monkeypatch_fixture_names.difference_update(
            ordered_statement.bound_names_before
        )
        shadowed_local_auth_helper_names.update(
            ordered_statement.bound_names_before & frozenset(local_auth_token_helpers)
        )
        visible_local_auth_token_helpers = _visible_local_auth_token_helpers(
            local_auth_token_helpers,
            shadowed_local_auth_helper_names,
        )
        response_connections_seen = {
            connection_name
            for child in _walk_statement_without_nested_scopes(statement)
            if isinstance(child, ast.Call)
            for connection_name in (_method_call_receiver_name(child, "getresponse"),)
            if connection_name is not None
        }
        response_connections_recorded: set[str] = set()
        for child in _walk_statement_without_nested_scopes(statement):
            if not isinstance(child, ast.Call):
                continue
            connection_name = _method_call_receiver_name(child, "request")
            if connection_name is None:
                continue
            if connection_name not in poc_connection_names:
                continue
            method, path = _request_method_and_path(child)
            tokens = _call_privileged_auth_tokens(
                child,
                name_literal_bindings,
            ) | _call_bound_auth_header_tokens(
                child,
                auth_header_tokens_by_name,
            )
            pending_request_by_connection[connection_name] = (
                _AuthenticatedStatusObservation(
                    tokens=tokens,
                    env_tokens_before_request=frozenset(env_tokens_seen),
                    direct_tokens_before_request=frozenset(direct_tokens_seen),
                    request_method=method,
                    request_path=path,
                    string_literals=_string_literals_with_bound_names(
                        child, name_literal_bindings
                    ),
                )
            )

        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = _assignment_targets(statement)
            assigned_names = {
                name for target in targets for name in _target_name_bindings(target)
            }
            _clear_status_observations_for_targets(status_observations, targets)
            for name in assigned_names:
                response_names_by_body_name.pop(name, None)
                asserted_literals_by_response_name.pop(name, None)
            value = statement.value
            if value is not None:
                header_tokens = _auth_header_tokens_in_node(
                    value,
                    name_literal_bindings,
                )
                for target in targets:
                    for name in _target_name_bindings(target):
                        if header_tokens:
                            auth_header_tokens_by_name[name] = header_tokens
                        else:
                            auth_header_tokens_by_name.pop(name, None)
            for target in targets:
                for name in _assigned_name_targets(target):
                    pending_request_by_connection.pop(name, None)
                    if name == "server":
                        direct_tokens_seen.clear()
                    if _call_creates_poc_server(value):
                        poc_server_names.add(name)
                    else:
                        poc_server_names.discard(name)
                    if _call_creates_poc_http_connection(
                        value,
                        frozenset(poc_server_names),
                        shadowed_constructor_names=frozenset(
                            shadowed_http_constructor_names
                        ),
                    ):
                        poc_connection_names.add(name)
                    else:
                        poc_connection_names.discard(name)
            if isinstance(value, ast.Call):
                for target in targets:
                    for key in _assigned_status_expr_keys(target):
                        status_observations[key] = _AuthenticatedStatusObservation(
                            tokens=frozenset(),
                            env_tokens_before_request=frozenset(env_tokens_seen),
                            direct_tokens_before_request=frozenset(
                                direct_tokens_seen
                            ),
                            string_literals=_string_literals_with_bound_names(
                                value, name_literal_bindings
                            ),
                        )
                connection_name = _method_call_receiver_name(value, "getresponse")
                pending = pending_request_by_connection.pop(connection_name, None)
                if pending is not None:
                    response_connections_recorded.add(connection_name)
                    for target in targets:
                        for name in _assigned_name_targets(target):
                            asserted_literals_by_response_name.setdefault(name, set())
                            status_observations[
                                f"attr:name:{name}.status"
                            ] = _AuthenticatedStatusObservation(
                                tokens=pending.tokens,
                                env_tokens_before_request=(
                                    pending.env_tokens_before_request
                                ),
                                direct_tokens_before_request=(
                                    pending.direct_tokens_before_request
                                ),
                                response_name=name,
                                request_method=pending.request_method,
                                request_path=pending.request_path,
                                string_literals=pending.string_literals,
                            )
                response_read_names = _response_read_receiver_names(value)
                for response_name in response_read_names:
                    if response_name not in asserted_literals_by_response_name:
                        continue
                    for target in targets:
                        for body_name in _assigned_name_targets(target):
                            response_names_by_body_name[body_name] = response_name
        for connection_name in response_connections_seen - response_connections_recorded:
            pending_request_by_connection.pop(connection_name, None)

        for child in _walk_statement_without_nested_scopes(statement):
            _clear_status_observations_for_targets(
                status_observations,
                _setattr_status_targets(child),
            )

        if isinstance(statement, ast.Delete):
            _clear_status_observations_for_targets(
                status_observations, statement.targets
            )
            deleted_names: set[str] = set()
            for target in statement.targets:
                if _targets_server_local_auth_tokens(target):
                    direct_tokens_seen.clear()
                deleted_names.update(_target_name_bindings(target))
            for name in deleted_names:
                if name == "server":
                    direct_tokens_seen.clear()
                poc_server_names.discard(name)
                poc_connection_names.discard(name)
                pending_request_by_connection.pop(name, None)
                asserted_literals_by_response_name.pop(name, None)
                auth_header_tokens_by_name.pop(name, None)
            if deleted_names:
                response_names_by_body_name = {
                    body_name: response_name
                    for body_name, response_name in response_names_by_body_name.items()
                    if body_name not in deleted_names
                    and response_name not in deleted_names
                }

        for name in _mutated_name_bindings(statement):
            auth_header_tokens_by_name.pop(name, None)
            response_names_by_body_name.pop(name, None)
            asserted_literals_by_response_name.pop(name, None)
        if _mutates_server_local_auth_tokens(statement):
            direct_tokens_seen.clear()

        if isinstance(statement, ast.Assert):
            assert_literals = _string_literals_in_node(statement)
            loaded_names = _loaded_name_references(statement)
            for body_name, response_name in response_names_by_body_name.items():
                if body_name in loaded_names:
                    asserted_literals_by_response_name.setdefault(
                        response_name, set()
                    ).update(assert_literals)
            asserted_keys = _status_asserted_expr_keys(
                statement, status_codes
            )
            status_code = _asserted_status_code(statement)
            for key in asserted_keys:
                observation = status_observations.get(key)
                if observation is None:
                    continue
                asserted_observations.append(
                    _AuthenticatedStatusObservation(
                        tokens=observation.tokens,
                        env_tokens_before_request=(
                            observation.env_tokens_before_request
                        ),
                        direct_tokens_before_request=(
                            observation.direct_tokens_before_request
                        ),
                        status_code=status_code,
                        response_name=observation.response_name,
                        request_method=observation.request_method,
                        request_path=observation.request_path,
                        string_literals=(
                            observation.string_literals
                            | _string_literals_in_node(statement)
                        ),
                    )
                )
        active_monkeypatch_fixture_names.difference_update(
            _statement_bound_names(statement)
        )
        _update_name_literal_bindings_after_statement(
            statement, name_literal_bindings
        )
        env_tokens_seen = set(
            _env_auth_tokens_after_statement(
                statement,
                env_tokens_seen,
                monkeypatch_fixture_names=frozenset(
                    active_monkeypatch_fixture_names
                ),
                os_module_available=os_module_available,
            )
        )
        if "os" in _statement_bound_names(statement):
            os_module_available = False
        shadowed_http_constructor_names.update(
            _statement_bound_names(statement) & frozenset(("HTTPConnection", "http"))
        )
        direct_tokens_seen = set(
            _direct_auth_tokens_after_statement(
                statement,
                direct_tokens_seen,
                local_auth_token_helpers=visible_local_auth_token_helpers,
            )
        )
        shadowed_local_auth_helper_names.update(
            _statement_bound_names(statement) & frozenset(local_auth_token_helpers)
        )
    return tuple(
        _AuthenticatedStatusObservation(
            tokens=observation.tokens,
            env_tokens_before_request=observation.env_tokens_before_request,
            direct_tokens_before_request=observation.direct_tokens_before_request,
            status_code=observation.status_code,
            response_name=observation.response_name,
            request_method=observation.request_method,
            request_path=observation.request_path,
            string_literals=observation.string_literals,
            asserted_response_literals=frozenset(
                asserted_literals_by_response_name.get(
                    observation.response_name or "", set()
                )
            ),
        )
        for observation in asserted_observations
    )


def _test_function_matches_fail_closed_ref_expectation(
    ref: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    local_auth_token_helpers: dict[str, frozenset[str]] | None = None,
    module_shadowed_http_constructor_names: frozenset[str] = frozenset(),
) -> bool:
    expectation = POC_AUTH_SESSION_FAIL_CLOSED_REF_EXPECTATIONS[ref]
    expected_status_codes = expectation.get("status_codes", frozenset())
    expected_method = expectation.get("method")
    expected_path = expectation.get("path")
    expected_auth_tokens = expectation.get("auth_tokens", frozenset())
    auth_source = expectation.get("auth_source")
    forbid_auth_tokens = bool(expectation.get("forbid_auth_tokens", False))
    required_literals = expectation.get("required_literals", frozenset())
    asserted_literals = expectation.get("asserted_literals", frozenset())
    request_literals = required_literals - asserted_literals
    for observation in _asserted_status_observations(
        node,
        expected_status_codes,
        local_auth_token_helpers=local_auth_token_helpers,
        module_shadowed_http_constructor_names=module_shadowed_http_constructor_names,
    ):
        if not (
            observation.env_tokens_before_request
            or observation.direct_tokens_before_request
        ):
            continue
        if auth_source == "env":
            configured_tokens = observation.env_tokens_before_request
        elif auth_source == "direct":
            configured_tokens = observation.direct_tokens_before_request
        else:
            configured_tokens = (
                observation.env_tokens_before_request
                | observation.direct_tokens_before_request
            )
        if not configured_tokens:
            continue
        if forbid_auth_tokens and observation.tokens:
            continue
        if expected_auth_tokens:
            if not observation.tokens & expected_auth_tokens:
                continue
            if not observation.tokens & configured_tokens:
                continue
        if not request_literals.issubset(observation.string_literals):
            continue
        if not asserted_literals.issubset(observation.asserted_response_literals):
            continue
        if expected_method is not None and observation.request_method != expected_method:
            continue
        if expected_path is not None and observation.request_path != expected_path:
            continue
        return True
    return False


def _constant_string_value(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return None
    return node.value


def _targets_env_auth_var(target: ast.AST) -> bool:
    if not isinstance(target, ast.Subscript):
        return False
    if _constant_string_value(target.slice) != POC_AUTH_SESSION_ENV_VAR:
        return False
    return _is_os_environ_node(target.value)


def _is_os_environ_node(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _poc_auth_session_test_name_from_ref(ref: str) -> str:
    _path, test_name = ref.split("::", 1)
    return test_name


def _poc_auth_session_direct_success_refs() -> tuple[str, ...]:
    env_success_refs = frozenset(POC_AUTH_SESSION_ENV_SUCCESS_COVERAGE_REFS)
    return tuple(
        ref
        for ref in POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
        if ref not in env_success_refs
    )


def _poc_auth_session_evidence_context(
    test_source: str,
) -> _PocAuthSessionEvidenceContext | None:
    try:
        test_tree = ast.parse(test_source)
    except SyntaxError:
        return None
    return _PocAuthSessionEvidenceContext(
        test_functions=_test_function_nodes(test_source),
        local_auth_token_helpers=_local_auth_token_helper_tokens(test_source),
        trusted_status_helpers=_trusted_poc_status_helpers(test_source),
        module_shadowed_http_constructor_names=_module_shadowed_runtime_names(
            test_tree,
            frozenset(("HTTPConnection", "http")),
        ),
    )


def _poc_auth_session_test_node(
    context: _PocAuthSessionEvidenceContext,
    ref: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    return context.test_functions.get(_poc_auth_session_test_name_from_ref(ref))


def _poc_auth_session_required_refs_exist(
    context: _PocAuthSessionEvidenceContext,
) -> bool:
    return all(
        _poc_auth_session_test_node(context, ref) is not None
        for ref in POC_AUTH_SESSION_COVERAGE_REFS
    )


def _poc_auth_session_success_ref_is_present(
    context: _PocAuthSessionEvidenceContext,
    ref: str,
) -> bool:
    test_node = _poc_auth_session_test_node(context, ref)
    if test_node is None:
        return False
    return _test_function_matches_success_ref_expectation(
        ref,
        test_node,
        local_auth_token_helpers=context.local_auth_token_helpers,
        trusted_status_helpers=context.trusted_status_helpers,
        module_shadowed_http_constructor_names=(
            context.module_shadowed_http_constructor_names
        ),
    )


def _poc_auth_session_env_success_ref_is_present(
    context: _PocAuthSessionEvidenceContext,
    ref: str,
) -> bool:
    test_node = _poc_auth_session_test_node(context, ref)
    if test_node is None:
        return False
    return _test_function_uses_env_auth_boundary(
        test_node,
        local_auth_token_helpers=context.local_auth_token_helpers,
        trusted_status_helpers=context.trusted_status_helpers,
        module_shadowed_http_constructor_names=(
            context.module_shadowed_http_constructor_names
        ),
    )


def _poc_auth_session_direct_success_ref_is_present(
    context: _PocAuthSessionEvidenceContext,
    ref: str,
) -> bool:
    test_node = _poc_auth_session_test_node(context, ref)
    if test_node is None:
        return False
    return _test_function_uses_direct_auth_boundary(
        test_node,
        local_auth_token_helpers=context.local_auth_token_helpers,
        trusted_status_helpers=context.trusted_status_helpers,
        module_shadowed_http_constructor_names=(
            context.module_shadowed_http_constructor_names
        ),
    )


def _poc_auth_session_fail_closed_ref_is_present(
    context: _PocAuthSessionEvidenceContext,
    ref: str,
) -> bool:
    test_node = _poc_auth_session_test_node(context, ref)
    if test_node is None:
        return False
    return _test_function_matches_fail_closed_ref_expectation(
        ref,
        test_node,
        local_auth_token_helpers=context.local_auth_token_helpers,
        module_shadowed_http_constructor_names=(
            context.module_shadowed_http_constructor_names
        ),
    )


def _poc_auth_session_validation_steps() -> tuple[_PocAuthSessionValidationStep, ...]:
    return (
        _PocAuthSessionValidationStep(
            refs=POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS,
            ref_is_present=_poc_auth_session_success_ref_is_present,
        ),
        _PocAuthSessionValidationStep(
            refs=POC_AUTH_SESSION_ENV_SUCCESS_COVERAGE_REFS,
            ref_is_present=_poc_auth_session_env_success_ref_is_present,
        ),
        _PocAuthSessionValidationStep(
            refs=_poc_auth_session_direct_success_refs(),
            ref_is_present=_poc_auth_session_direct_success_ref_is_present,
        ),
        _PocAuthSessionValidationStep(
            refs=POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS,
            ref_is_present=_poc_auth_session_fail_closed_ref_is_present,
        ),
    )


def _poc_auth_session_validation_steps_are_present(
    context: _PocAuthSessionEvidenceContext,
) -> bool:
    return all(
        step.ref_is_present(context, ref)
        for step in _poc_auth_session_validation_steps()
        for ref in step.refs
    )


def poc_auth_session_coverage_is_present(repo_root: Path = REPO_ROOT) -> bool:
    readme_path = repo_root / "README.md"
    test_path = repo_root / "tests" / "test_poc_web_api.py"
    try:
        readme = readme_path.read_text(encoding="utf-8")
        test_source = test_path.read_text(encoding="utf-8")
    except OSError:
        return False

    if "## Local PoC API authentication" not in readme:
        return False
    if POC_AUTH_SESSION_ENV_VAR not in readme:
        return False
    context = _poc_auth_session_evidence_context(test_source)
    if context is None:
        return False
    if not _poc_auth_session_required_refs_exist(context):
        return False
    return _poc_auth_session_validation_steps_are_present(context)


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
class LLMStabilityAcceptanceThreshold:
    min_plan_agreement_rate: float = 1.0
    min_confirmed_value_agreement_rate: float = 1.0
    max_schema_failure_rate: float = 0.0
    max_deterministic_fallback_rate: float = 0.0
    max_external_ai_api_guard_violation_count: int = 0
    max_unstable_example_count: int = 0
    max_harness_llm_scenario_failure_count: int = 0

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "scope": (
                "MVP-before LLM correction/completion acceptance requires "
                "deterministic synthetic outputs and no external AI API "
                "transmission."
            ),
            "min_plan_agreement_rate": self.min_plan_agreement_rate,
            "min_confirmed_value_agreement_rate": (
                self.min_confirmed_value_agreement_rate
            ),
            "max_schema_failure_rate": self.max_schema_failure_rate,
            "max_deterministic_fallback_rate": self.max_deterministic_fallback_rate,
            "max_external_ai_api_guard_violation_count": (
                self.max_external_ai_api_guard_violation_count
            ),
            "max_unstable_example_count": self.max_unstable_example_count,
            "max_harness_llm_scenario_failure_count": (
                self.max_harness_llm_scenario_failure_count
            ),
        }


LLM_STABILITY_ACCEPTANCE_THRESHOLD = LLMStabilityAcceptanceThreshold()


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
    llm_stability_source: Path
    poc_comparison_source: Path
    repo_root: Path | None = None

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
                "llm_stability_source": str(self.llm_stability_source),
                "poc_comparison_source": str(self.poc_comparison_source),
                "llm_stability": self.llm_stability.as_dict(),
                "poc_mode_comparison": self.poc_mode_comparison.as_dict(),
            },
        }


@dataclass(frozen=True)
class PoCAcceptanceReport:
    p9_harness: P9HarnessReport
    generated_at: str
    commit: str
    commit_is_clean: bool = True
    generation_command: str = (
        "python3 scripts/evaluate_dataset.py --poc-acceptance-report"
    )
    evaluator_commit: str | None = None
    evaluator_commit_is_clean: bool | None = None

    def as_dict(self) -> dict[str, object]:
        harness_payload = self.p9_harness.as_dict()
        summary = harness_payload["summary"]
        assert isinstance(summary, dict)
        evaluator_commit = self.evaluator_commit or self.commit
        evaluator_commit_is_clean = (
            self.commit_is_clean
            if self.evaluator_commit_is_clean is None
            else self.evaluator_commit_is_clean
        )
        poc_comparison = self.p9_harness.poc_mode_comparison
        llm_stability = self.p9_harness.llm_stability
        results = list(self.p9_harness.results)
        failed_results = [result for result in results if result.get("ok") is not True]
        fail_closed_gate_results = [
            result for result in results if result.get("fail_closed") is True
        ]
        usable_results = [result for result in results if result.get("ok") is True]
        artifact_failures = [
            result
            for result in results
            if result.get("artifact_expectations_met") is not True
        ]
        structured_output_failures = poc_acceptance_structured_output_failures(results)
        unaudited_results = [
            result for result in results if result.get("audit_present") is not True
        ]
        by_mode = poc_acceptance_conversion_mode_results(results)
        observed_representative_modes = {
            str(result.get("representative_mode"))
            for result in usable_results
            if result.get("representative_mode") is not None
        }
        missing_representative_modes = sorted(
            set(P9_REPRESENTATIVE_FLAGS_BY_MODE) - observed_representative_modes
        )
        observed_source_categories = {
            str(result.get("sample_category"))
            for result in usable_results
            if result.get("sample_category") is not None
        }
        missing_source_categories = sorted(
            P9_REQUIRED_SOURCE_CATEGORIES - observed_source_categories
        )
        external_violation_count = int(summary["external_ai_api_guard_violation_count"])
        llm_scenario_failures = poc_acceptance_llm_scenario_failures(results)
        llm_stability_threshold_failures = llm_stability_acceptance_failures(
            llm_stability,
            llm_scenario_failures=llm_scenario_failures,
            external_ai_api_guard_violation_count=external_violation_count,
        )
        high_quality_mode = poc_acceptance_required_mode(
            poc_comparison,
            "high_quality",
        )
        high_quality_source_linkage_rate = (
            high_quality_mode.source_linkage_rate if high_quality_mode else 0.0
        )
        functionality_status = (
            "fail"
            if (
                failed_results
                or missing_representative_modes
                or missing_source_categories
                or not poc_comparison.manual_correction_time.target_met
            )
            else "pass"
        )
        llm_control_status = (
            "fail" if llm_stability_threshold_failures else "pass"
        )
        manifest_repo_root = poc_acceptance_harness_repo_root(self.p9_harness)
        evidence_inputs_tracked_in_manifest_repo = (
            poc_acceptance_evidence_inputs_tracked_in_manifest_repo(self.p9_harness)
        )
        poc_auth_session_evidence_inputs_tracked = (
            poc_auth_session_coverage_inputs_tracked_in_repo(manifest_repo_root)
        )
        reproducibility_status = (
            "pass"
            if (
                self.commit != "unknown"
                and self.commit_is_clean
                and evaluator_commit != "unknown"
                and evaluator_commit_is_clean
                and evidence_inputs_tracked_in_manifest_repo
            )
            else "fail"
        )
        authenticated_poc_api_session_checked = (
            poc_auth_session_evidence_inputs_tracked
            and poc_auth_session_coverage_is_present(manifest_repo_root)
        )
        security_status = (
            "fail"
            if external_violation_count
            else "pass"
            if authenticated_poc_api_session_checked
            else "unknown"
        )
        acceptance_matrix = [
            poc_acceptance_row(
                "functionality",
                "機能",
                functionality_status,
                (
                    f"{summary['completed_count']} of {summary['case_count']} "
                    "representative conversion runs completed without harness "
                    "failures; missing representative modes: "
                    f"{missing_representative_modes or 'none'}; missing source "
                    f"categories: {missing_source_categories or 'none'}; manual "
                    "correction target met: "
                    f"{poc_comparison.manual_correction_time.target_met}."
                ),
                [
                    "p9_harness.summary.completed_count",
                    "p9_harness.results",
                    "p9_harness.results[].representative_mode",
                    "p9_harness.results[].sample_category",
                    "poc_mode_comparison.manual_correction_time.target_met",
                ],
            ),
            poc_acceptance_row(
                "structured_output",
                "構造化",
                "fail" if structured_output_failures else "pass",
                (
                    f"{len(structured_output_failures)} runs failed primary "
                    "artifact structure or expectations."
                ),
                [
                    "p9_harness.results[].artifact_expectations_met",
                    "p9_harness.results[].failure_reason",
                ],
            ),
            poc_acceptance_row(
                "llm_control",
                "LLM制御",
                llm_control_status,
                (
                    "External AI API guard violations: "
                    f"{external_violation_count}; unstable LLM examples: "
                    f"{llm_stability.unstable_example_count}; harness LLM "
                    f"scenario failures: {len(llm_scenario_failures)}; "
                    "threshold failures: "
                    f"{llm_stability_threshold_failures or 'none'}."
                ),
                [
                    "p9_harness.summary.external_ai_api_guard_violation_count",
                    "p9_harness.results[].llm_scenario",
                    "p9_harness.results[].llm_status",
                    "llm_stability_acceptance_threshold",
                    "llm_stability_comparison.unstable_examples",
                ],
            ),
            poc_acceptance_row(
                "traceability",
                "追跡性",
                "pass" if high_quality_source_linkage_rate >= 1.0 else "fail",
                (
                    "High-quality PoC source linkage rate: "
                    f"{high_quality_source_linkage_rate:.3f}."
                ),
                ["poc_mode_comparison.modes[].source_linkage_rate"],
            ),
            poc_acceptance_row(
                "safety",
                "安全性",
                "pass"
                if (
                    poc_comparison.high_risk_false_auto_confirmed_count
                    <= poc_comparison.high_risk_false_auto_confirmed_target
                    and external_violation_count == 0
                )
                else "fail",
                (
                    "High-risk false auto-confirmed count: "
                    f"{poc_comparison.high_risk_false_auto_confirmed_count}; "
                    f"target: {poc_comparison.high_risk_false_auto_confirmed_target}."
                ),
                [
                    "poc_mode_comparison.high_risk_false_auto_confirmed_count",
                    "p9_harness.summary.external_ai_api_guard_violation_count",
                ],
            ),
            poc_acceptance_row(
                "logs",
                "ログ",
                "fail" if unaudited_results else "pass",
                f"{len(unaudited_results)} harness rows lacked audit evidence.",
                ["p9_harness.results[].audit_present"],
            ),
            poc_acceptance_row(
                "security",
                "セキュリティ",
                security_status,
                (
                    "External AI API guard violations: "
                    f"{external_violation_count}; authenticated PoC API session "
                    "checked: "
                    f"{authenticated_poc_api_session_checked}; auth evidence "
                    "inputs tracked in manifest repo: "
                    f"{poc_auth_session_evidence_inputs_tracked}."
                ),
                [
                    *poc_auth_session_coverage_evidence_refs(),
                    "p9_harness.summary.external_ai_api_guard_violation_count",
                ],
            ),
            poc_acceptance_row(
                "reproducibility",
                "再現性",
                reproducibility_status,
                (
                    "Report records commit, dataset manifest, comparison "
                    "inputs, and the generation command; commit is "
                    f"{self.commit!r}; worktree clean: {self.commit_is_clean}; "
                    "evidence inputs tracked in manifest repo: "
                    f"{evidence_inputs_tracked_in_manifest_repo}."
                ),
                ["tested_environment", "evidence"],
            ),
        ]
        criterion_status_counts = poc_acceptance_criterion_status_counts(
            acceptance_matrix
        )
        matrix_evidence = poc_acceptance_matrix_evidence(
            failed_results=failed_results,
            artifact_failures=artifact_failures,
            structured_output_failures=structured_output_failures,
            unaudited_results=unaudited_results,
            llm_scenario_failures=llm_scenario_failures,
            observed_representative_modes=sorted(observed_representative_modes),
            missing_representative_modes=missing_representative_modes,
            observed_source_categories=sorted(observed_source_categories),
            missing_source_categories=missing_source_categories,
            high_quality_source_linkage_rate=high_quality_source_linkage_rate,
            external_violation_count=external_violation_count,
            poc_comparison=poc_comparison,
            llm_stability=llm_stability,
            llm_stability_threshold_failures=llm_stability_threshold_failures,
            authenticated_poc_api_session_checked=(
                authenticated_poc_api_session_checked
            ),
            poc_auth_session_evidence_inputs_tracked=(
                poc_auth_session_evidence_inputs_tracked
            ),
            commit=self.commit,
            commit_is_clean=self.commit_is_clean,
            evaluator_commit=evaluator_commit,
            evaluator_commit_is_clean=evaluator_commit_is_clean,
            evidence_inputs_tracked_in_manifest_repo=(
                evidence_inputs_tracked_in_manifest_repo
            ),
        )

        return {
            "schema_version": POC_ACCEPTANCE_REPORT_SCHEMA_VERSION,
            "title": "Phase 9 PoC Acceptance Report",
            "criteria_source": "15.2_PoC受入基準",
            "criteria_source_sections": ["§2", "§3", "§4", "§5", "§6"],
            "generated_at": self.generated_at,
            "tested_environment": {
                "commit": self.commit,
                "commit_is_clean": self.commit_is_clean,
                "evaluator_commit": evaluator_commit,
                "evaluator_commit_is_clean": evaluator_commit_is_clean,
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "evidence": {
                "dataset_manifest": str(
                    poc_acceptance_top_level_evidence_path(self.p9_harness.manifest)
                ),
                "llm_stability_runs": str(
                    poc_acceptance_top_level_evidence_path(
                        self.p9_harness.llm_stability_source
                    )
                ),
                "poc_mode_comparison": str(
                    poc_acceptance_top_level_evidence_path(
                        self.p9_harness.poc_comparison_source
                    )
                ),
                "generation_command": self.generation_command,
            },
            "overall_status": poc_acceptance_overall_status(acceptance_matrix),
            "criterion_status_counts": criterion_status_counts,
            "acceptance_matrix": acceptance_matrix,
            "matrix_evidence": matrix_evidence,
            "fail_closed_conditions": poc_acceptance_fail_closed_conditions(
                external_violation_count=external_violation_count,
                poc_comparison=poc_comparison,
                unaudited_results=unaudited_results,
                llm_stability=llm_stability,
                llm_scenario_failures=llm_scenario_failures,
                llm_stability_threshold_failures=(
                    llm_stability_threshold_failures
                ),
                structured_output_failures=structured_output_failures,
            ),
            "conversion_mode_results": by_mode,
            "llm_stability_acceptance_threshold": (
                LLM_STABILITY_ACCEPTANCE_THRESHOLD.as_dict()
            ),
            "llm_stability_comparison": llm_stability.as_dict(),
            "poc_mode_comparison": poc_comparison.as_dict(),
            "p9_harness": harness_payload,
            "p9_harness_results": results,
            "review_ui_observations": {
                "mode_diffs": list(poc_comparison.mode_diffs),
                "manual_correction_time": poc_comparison.manual_correction_time.as_dict(),
                "requires_review_count_by_mode": {
                    mode.mode: mode.requires_review_count
                    for mode in poc_comparison.modes
                },
                "warning_count_by_mode": {
                    mode.mode: mode.warning_count for mode in poc_comparison.modes
                },
            },
            "known_limitations": poc_acceptance_known_limitations(
                failed_results,
                fail_closed_gate_results,
            ),
            "follow_up_issue_candidates": poc_acceptance_follow_up_candidates(
                failed_results,
                fail_closed_gate_results,
                llm_stability_threshold_failures,
                unaudited_results,
                poc_comparison,
            ),
            "p9_harness_summary": summary,
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
    for candidate in manifest_path.parents:
        if (candidate / EXPECTED_DATASET_MANIFEST).is_file():
            return candidate
    if manifest_path.name == "poc_evaluation_manifest_v1.json" and manifest_path.parent.name == "datasets":
        return manifest_path.parent.parent
    return manifest_path.parent


def p9_fixture_manifest_path(manifest: dict[str, Any], repo_root: Path) -> Path:
    if manifest.get("schema_version") != P9_EVALUATION_MANIFEST_SCHEMA_VERSION:
        raise EvaluationCaseError(
            f"unsupported P9 evaluation manifest schema_version {manifest.get('schema_version')!r}"
        )
    fixture_manifest = manifest.get("fixture_manifest")
    if fixture_manifest != str(EXPECTED_DATASET_MANIFEST):
        raise EvaluationCaseError(
            "P9 evaluation manifest fixture_manifest must be datasets/fixtures/manifest.json"
        )
    return repo_root / EXPECTED_DATASET_MANIFEST


def p9_sample_representative_mode(sample: dict[str, Any]) -> str:
    if sample.get("category") == "scanned_pdf":
        return "scanned_pdf_ocr"
    conversion_mode = sample.get("conversion_mode")
    if isinstance(conversion_mode, str) and conversion_mode in P9_CONVERSION_MODE_BY_MODE:
        return conversion_mode
    raise EvaluationCaseError(
        f"P9 sample {sample.get('id')!r} has unsupported conversion_mode {conversion_mode!r}"
    )


def p9_sample_conversion_mode(sample: dict[str, Any], representative_mode: str) -> str:
    conversion_mode = sample.get("conversion_mode")
    if isinstance(conversion_mode, str) and conversion_mode in P9_CONVERSION_MODE_BY_MODE:
        return conversion_mode
    return P9_CONVERSION_MODE_BY_MODE[representative_mode]


def p9_required_representative_flag(
    sample: dict[str, Any],
    representative_mode: str,
) -> str:
    category = sample.get("category")
    if isinstance(category, str) and category in P9_REPRESENTATIVE_FLAG_BY_CATEGORY:
        return P9_REPRESENTATIVE_FLAG_BY_CATEGORY[category]
    return P9_REPRESENTATIVE_FLAGS_BY_MODE[representative_mode]


def p9_validate_representative_fixture_link(
    sample: dict[str, Any],
    fixture: dict[str, Any],
    *,
    representative_mode: str,
) -> None:
    sample_id = sample.get("id")
    fixture_id = sample.get("fixture_id")
    category = sample.get("category")
    allowed_source_types = (
        P9_FIXTURE_SOURCE_TYPES_BY_CATEGORY.get(category)
        if isinstance(category, str)
        else None
    )
    if (
        allowed_source_types is not None
        and fixture.get("source_type") not in allowed_source_types
    ):
        raise EvaluationCaseError(
            f"P9 sample {sample_id!r} fixture {fixture_id!r} source_type "
            f"{fixture.get('source_type')!r} does not match category {category!r}"
        )
    representative_flag = p9_required_representative_flag(sample, representative_mode)
    if fixture.get(representative_flag) is not True:
        raise EvaluationCaseError(
            f"P9 sample {sample_id!r} fixture {fixture_id!r} must declare "
            f"{representative_flag}"
        )


def p9_evaluation_samples(
    p9_manifest: dict[str, Any],
    fixture_manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    samples = p9_manifest.get("samples")
    if not isinstance(samples, list):
        raise EvaluationCaseError("P9 evaluation manifest must define a samples list")
    fixtures = fixture_manifest.get("fixtures")
    if not isinstance(fixtures, list):
        raise EvaluationCaseError("fixture manifest must define a fixtures list")
    fixtures_by_id = {
        fixture["id"]: fixture
        for fixture in fixtures
        if isinstance(fixture, dict) and isinstance(fixture.get("id"), str)
    }

    evaluation_samples: list[dict[str, Any]] = []
    seen_sample_ids: set[str] = set()
    observed_modes: set[str] = set()
    usable_modes: set[str] = set()
    observed_categories: set[str] = set()
    usable_categories: set[str] = set()
    placeholder_by_mode: dict[str, list[dict[str, Any]]] = {}
    placeholder_by_category: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        if not isinstance(sample, dict):
            raise EvaluationCaseError("each P9 evaluation sample needs an object")
        sample_id = sample.get("id")
        if not isinstance(sample_id, str) or not sample_id:
            raise EvaluationCaseError("each P9 evaluation sample needs a string id")
        if sample_id in seen_sample_ids:
            raise EvaluationCaseError(f"duplicate P9 sample id {sample_id!r}")
        seen_sample_ids.add(sample_id)
        category = sample.get("category")
        if category not in P9_REQUIRED_SOURCE_CATEGORIES:
            raise EvaluationCaseError(
                f"P9 sample {sample_id!r} has unsupported source category {category!r}"
            )
        representative_mode = p9_sample_representative_mode(sample)
        conversion_mode = p9_sample_conversion_mode(sample, representative_mode)
        observed_modes.add(representative_mode)
        observed_categories.add(str(category))
        fixture_id = sample.get("fixture_id")
        fixture = fixtures_by_id.get(fixture_id) if isinstance(fixture_id, str) else None
        if fixture is None:
            if fixture_id is not None or sample.get("dataset_status") != "manifest_placeholder":
                raise EvaluationCaseError(
                    f"P9 sample {sample_id!r} must reference a fixture_manifest fixture"
                )
            placeholder = {
                "sample_id": sample_id,
                "sample_category": sample.get("category"),
                "dataset_status": sample.get("dataset_status"),
                "availability_reason": sample.get("availability_reason"),
                "evaluation_focus": sample.get("evaluation_focus"),
                "expected_warning_or_review_focus": sample.get(
                    "expected_warning_or_review_focus"
                ),
                "fixture_id": fixture_id,
                "representative_mode": representative_mode,
                "conversion_mode": conversion_mode,
            }
            placeholder_by_mode.setdefault(representative_mode, []).append(placeholder)
            placeholder_by_category.setdefault(str(category), []).append(placeholder)
            continue
        if fixture is not None:
            p9_validate_representative_fixture_link(
                sample,
                fixture,
                representative_mode=representative_mode,
            )
        merged = dict(fixture or {})
        merged.update(
            {
                "sample_id": sample_id,
                "sample_category": sample.get("category"),
                "dataset_status": sample.get("dataset_status"),
                "availability_reason": sample.get("availability_reason"),
                "evaluation_focus": sample.get("evaluation_focus"),
                "expected_warning_or_review_focus": sample.get(
                    "expected_warning_or_review_focus"
                ),
                "fixture_id": fixture_id,
                "representative_mode": representative_mode,
                "conversion_mode": conversion_mode,
            }
        )
        evaluation_samples.append(merged)
        usable_modes.add(representative_mode)
        usable_categories.add(str(category))

    required_modes = set(P9_REPRESENTATIVE_FLAGS_BY_MODE)
    missing_modes = sorted(required_modes - observed_modes)
    if missing_modes:
        raise EvaluationCaseError(
            f"P9 evaluation manifest has no representative for {missing_modes[0]}"
        )
    required_categories_value = p9_manifest.get("required_categories")
    if not isinstance(required_categories_value, list) or not all(
        isinstance(category, str) for category in required_categories_value
    ):
        raise EvaluationCaseError(
            "P9 evaluation manifest must declare required_categories"
        )
    required_categories = set(required_categories_value)
    if required_categories != set(P9_REQUIRED_SOURCE_CATEGORIES):
        expected_categories = sorted(P9_REQUIRED_SOURCE_CATEGORIES)
        raise EvaluationCaseError(
            "P9 evaluation manifest required_categories must match "
            f"{expected_categories!r}"
        )
    missing_categories = sorted(P9_REQUIRED_SOURCE_CATEGORIES - observed_categories)
    if missing_categories:
        raise EvaluationCaseError(
            "P9 evaluation manifest has no representative for source category "
            f"{missing_categories[0]!r}"
        )
    appended_placeholder_ids: set[str] = set()
    appended_placeholder_categories: set[str] = set()
    missing_usable_modes = required_modes - usable_modes
    for missing_usable_mode in sorted(missing_usable_modes):
        placeholders = placeholder_by_mode.get(missing_usable_mode, [])
        if not placeholders:
            continue
        placeholder = placeholders[0]
        evaluation_samples.append(placeholder)
        if isinstance(placeholder.get("sample_id"), str):
            appended_placeholder_ids.add(str(placeholder["sample_id"]))
        if isinstance(placeholder.get("sample_category"), str):
            appended_placeholder_categories.add(str(placeholder["sample_category"]))
    for missing_usable_category in sorted(P9_REQUIRED_SOURCE_CATEGORIES - usable_categories):
        if missing_usable_category in appended_placeholder_categories:
            continue
        for placeholder in placeholder_by_category.get(missing_usable_category, []):
            placeholder_id = placeholder.get("sample_id")
            if (
                not isinstance(placeholder_id, str)
                or placeholder_id in appended_placeholder_ids
            ):
                continue
            evaluation_samples.append(placeholder)
            appended_placeholder_ids.add(placeholder_id)
    return evaluation_samples


def p9_result_for_unavailable_fixture(
    fixture: dict[str, Any],
    *,
    mode: str,
    llm_scenario: str,
    failure_reason: str,
    fail_closed: bool = False,
    mvp_before_gate_revision: str | None = None,
) -> dict[str, object]:
    conversion_mode = (
        fixture.get("conversion_mode")
        if isinstance(fixture.get("conversion_mode"), str)
        else P9_CONVERSION_MODE_BY_MODE[mode]
    )
    artifact_expectation_failures = (
        [f"fail-closed MVP-before gate revision: {mvp_before_gate_revision}"]
        if fail_closed and mvp_before_gate_revision
        else []
    )
    audit_failure_reason = f"{failure_reason}; conversion audit missing"
    return {
        "sample_id": fixture.get("sample_id"),
        "fixture_id": fixture.get("id"),
        "source_fixture_id": fixture.get("fixture_id"),
        "title": fixture.get("title"),
        "sample_category": fixture.get("sample_category"),
        "source_type": fixture.get("source_type"),
        "format": fixture.get("format"),
        "path": fixture.get("path"),
        "conversion_mode": conversion_mode,
        "representative_mode": mode,
        "llm_scenario": llm_scenario,
        "llm_requested": llm_scenario == "llm_requested",
        "ocr_requested": mode == "scanned_pdf_ocr",
        "ok": False,
        "fail_closed": fail_closed,
        "mvp_before_gate_revision": mvp_before_gate_revision,
        "ir_generated": False,
        "artifact_generated": False,
        "artifact_count": 0,
        "warnings_count": 0,
        "review_items_count": 0,
        "audit_present": False,
        "processing_time_ms": 0.0,
        "failure_reason": audit_failure_reason,
        "llm_status": "not_run",
        "llm_fallback_used": False,
        "use_ocr_status": "not_run",
        "external_ai_api_guard_violation": False,
        "conversion_status": "not_run",
        "artifact_expectations_met": False,
        "artifact_expectation_failures": artifact_expectation_failures,
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


def p9_expectations_for_mode(
    fixture: dict[str, Any], representative_mode: str
) -> dict[str, Any] | None:
    key = P9_EXPECTATION_KEYS_BY_MODE.get(representative_mode)
    if key is None:
        return None
    expectations = fixture.get(key)
    return expectations if isinstance(expectations, dict) else None


def p9_primary_artifact(artifacts: object) -> dict[str, Any] | None:
    if not isinstance(artifacts, list):
        return None
    for artifact in artifacts:
        if isinstance(artifact, dict) and artifact.get("kind") == "primary":
            return artifact
    return None


def p9_xlsx_comments_by_ref(xlsx_path: Path) -> dict[str, str]:
    namespace = {"xlsx": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    comments_by_ref: dict[str, str] = {}
    with ZipFile(xlsx_path) as archive:
        comment_names = [
            name
            for name in archive.namelist()
            if (
                name.startswith("xl/comments/comment")
                or name.startswith("xl/comments")
            )
            and name.endswith(".xml")
        ]
        for comment_name in comment_names:
            root = ElementTree.fromstring(archive.read(comment_name))
            for comment in root.findall(".//xlsx:comment", namespace):
                ref = comment.attrib.get("ref")
                if not isinstance(ref, str):
                    continue
                text = "".join(
                    text_node.text or ""
                    for text_node in comment.findall(".//xlsx:t", namespace)
                )
                comments_by_ref[ref] = text
    return comments_by_ref


def p9_cell_row_index(ref: str) -> int:
    digits = "".join(ch for ch in ref if ch.isdigit())
    return int(digits) if digits else 0


def p9_cell_column_label(ref: str) -> str:
    return "".join(ch for ch in ref if ch.isalpha()).upper()


def p9_validate_xlsx_artifact(
    artifact_path: Path, expectations: dict[str, Any], fixture_id: object
) -> list[str]:
    from core.parsers.xlsx_extraction import extract_xlsx_structure

    failures: list[str] = []
    xlsx = extract_xlsx_structure(artifact_path)
    if not xlsx.sheets:
        return ["xlsx artifact has no sheets"]
    sheet = xlsx.sheets[0]
    cells = {cell.ref: (cell.value, cell.value_type) for cell in sheet.cells}
    expected_dimension = expectations.get("dimension")
    if isinstance(expected_dimension, str) and sheet.dimension != expected_dimension:
        failures.append(
            f"expected xlsx dimension {expected_dimension}, got {sheet.dimension}"
        )
    expected_cells = expectations.get("cells")
    if isinstance(expected_cells, dict):
        for ref, expected in expected_cells.items():
            if not isinstance(ref, str) or not isinstance(expected, dict):
                failures.append(f"fixture {fixture_id!r} has malformed cell expectation")
                continue
            expected_pair = (expected.get("value"), expected.get("value_type"))
            if cells.get(ref) != expected_pair:
                failures.append(
                    f"expected cell {ref} {expected_pair!r}, got {cells.get(ref)!r}"
                )
    expected_row_count = expectations.get("row_count")
    if isinstance(expected_row_count, int):
        actual_row_count = len({p9_cell_row_index(cell.ref) for cell in sheet.cells})
        if actual_row_count != expected_row_count:
            failures.append(
                f"expected {expected_row_count} xlsx rows, got {actual_row_count}"
            )
    min_column_count = expectations.get("min_column_count")
    if isinstance(min_column_count, int):
        actual_column_count = len({p9_cell_column_label(cell.ref) for cell in sheet.cells})
        if actual_column_count < min_column_count:
            failures.append(
                f"expected at least {min_column_count} xlsx columns, got {actual_column_count}"
            )
    source_comment = expectations.get("source_comment")
    table_start_row = None
    if isinstance(source_comment, dict) and isinstance(source_comment.get("cell"), str):
        table_start_row = p9_cell_row_index(source_comment["cell"])
    table_cells = [
        cell
        for cell in sheet.cells
        if table_start_row is None or p9_cell_row_index(cell.ref) >= table_start_row
    ]
    expected_table_row_count = expectations.get("table_row_count")
    if isinstance(expected_table_row_count, int):
        actual_table_row_count = len(
            {p9_cell_row_index(cell.ref) for cell in table_cells}
        )
        if actual_table_row_count != expected_table_row_count:
            failures.append(
                f"expected {expected_table_row_count} table rows, got {actual_table_row_count}"
            )
    expected_table_column_count = expectations.get("table_column_count")
    if isinstance(expected_table_column_count, int):
        actual_table_column_count = len(
            {p9_cell_column_label(cell.ref) for cell in table_cells}
        )
        if actual_table_column_count != expected_table_column_count:
            failures.append(
                "expected "
                f"{expected_table_column_count} table columns, got "
                f"{actual_table_column_count}"
            )
    if isinstance(source_comment, dict):
        comment_ref = source_comment.get("cell")
        contains = source_comment.get("contains")
        comments_by_ref = p9_xlsx_comments_by_ref(artifact_path)
        if not isinstance(comment_ref, str) or comment_ref not in comments_by_ref:
            failures.append(f"expected source comment at {comment_ref!r}")
        elif isinstance(contains, list):
            comment_text = comments_by_ref[comment_ref]
            for expected_text in contains:
                if isinstance(expected_text, str) and expected_text not in comment_text:
                    failures.append(
                        f"expected source comment at {comment_ref} to contain {expected_text!r}"
                    )
    return failures


def p9_validate_docx_artifact(
    artifact_path: Path, expectations: dict[str, Any]
) -> list[str]:
    from core.parsers.docx_extraction import extract_docx_structure

    failures: list[str] = []
    docx = extract_docx_structure(artifact_path)
    table_rows = [block.rows for block in docx.blocks if block.kind == "table"]
    expected_table_rows = expectations.get("table_rows")
    if isinstance(expected_table_rows, list) and table_rows != expected_table_rows:
        failures.append("docx table rows did not match expectations")
    expected_headings = expectations.get("heading_texts")
    if isinstance(expected_headings, list):
        headings = [block.text for block in docx.blocks if block.kind == "heading"]
        if headings != expected_headings:
            failures.append("docx heading texts did not match expectations")
    expected_paragraphs = expectations.get("paragraph_texts")
    if isinstance(expected_paragraphs, list):
        paragraphs = [block.text for block in docx.blocks if block.kind == "paragraph"]
        if paragraphs != expected_paragraphs:
            failures.append("docx paragraph texts did not match expectations")
    return failures


def p9_validate_artifact_expectations(
    *,
    fixture: dict[str, Any],
    conversion_mode: str,
    representative_mode: str,
    primary_artifact: dict[str, Any] | None,
    warnings: object,
    allowed_runtime_warning_prefixes: tuple[str, ...] = (),
) -> list[str]:
    expectations = p9_expectations_for_mode(fixture, representative_mode)
    failures: list[str] = []
    expected_artifact_format = P9_PRIMARY_ARTIFACT_FORMAT_BY_CONVERSION_MODE.get(
        conversion_mode
    )
    if primary_artifact is None:
        if expected_artifact_format is not None:
            return ["primary artifact is missing"]
        return []
    artifact_content = primary_artifact.get("content")
    artifact_format = primary_artifact.get("format")
    if not isinstance(artifact_content, bytes):
        return ["primary artifact content is missing"]
    artifact_format_mismatch = (
        expected_artifact_format is not None
        and artifact_format != expected_artifact_format
    )
    if artifact_format_mismatch:
        failures.append(
            "primary artifact format "
            f"{artifact_format!r} did not match expected "
            f"{expected_artifact_format!r} for {conversion_mode}"
        )
    if expectations is None:
        return failures
    expected_warnings = expectations.get("warnings")
    if isinstance(expected_warnings, list):
        warning_list = warnings if isinstance(warnings, list) else []
        expected_warning_values = [
            expected_warning
            for expected_warning in expected_warnings
            if isinstance(expected_warning, str)
        ]
        for expected_warning in expected_warnings:
            if isinstance(expected_warning, str) and expected_warning not in warning_list:
                failures.append(f"expected warning {expected_warning!r} was not emitted")
        for actual_warning in warning_list:
            if (
                isinstance(actual_warning, str)
                and actual_warning not in expected_warning_values
                and not p9_runtime_warning_is_review_only(
                    actual_warning,
                    conversion_mode=conversion_mode,
                    allowed_prefixes=allowed_runtime_warning_prefixes,
                )
            ):
                failures.append(f"unexpected warning {actual_warning!r} was emitted")
    if artifact_format_mismatch:
        return failures
    suffix = f".{artifact_format}" if isinstance(artifact_format, str) else ""
    artifact_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as artifact_file:
            artifact_file.write(artifact_content)
            artifact_file.flush()
            artifact_path = Path(artifact_file.name)

        if artifact_format == "xlsx":
            failures.extend(
                p9_validate_xlsx_artifact(
                    artifact_path, expectations, fixture.get("id")
                )
            )
        elif artifact_format == "docx":
            failures.extend(p9_validate_docx_artifact(artifact_path, expectations))
    except Exception as exc:
        failures.append(f"artifact validation failed: {type(exc).__name__}: {exc}")
    finally:
        if artifact_path is not None:
            try:
                artifact_path.unlink(missing_ok=True)
            except OSError as exc:
                failures.append(f"artifact cleanup failed: {type(exc).__name__}: {exc}")
    return failures


def p9_runtime_warning_is_review_only(
    warning: str,
    *,
    conversion_mode: str,
    allowed_prefixes: tuple[str, ...],
) -> bool:
    if warning == f"conversion mode {conversion_mode} selected":
        return True
    if warning.startswith(allowed_prefixes):
        return True
    return p9_runtime_warning_is_known_parser_review_gate(warning)


def p9_runtime_warning_is_known_parser_review_gate(warning: str) -> bool:
    if warning in {
        "upload was treated as plain text; parser confidence requires review",
        "JSON upload root is not an object; content requires review",
        "PDF table extraction produced no selected table; xlsx artifact requires review",
        "PDF table extraction candidates disagreed; xlsx artifact requires review",
        (
            "PDF table extraction selected table has incomplete cell boundaries; "
            "xlsx artifact requires review"
        ),
    }:
        return True
    if (
        warning.startswith("PDF table extraction candidate unavailable: ")
        and warning.endswith("; xlsx artifact requires review")
    ):
        return True
    if not warning.startswith("blocks["):
        return False
    block_index, separator, marker = warning.removeprefix("blocks[").partition("]")
    if separator != "]" or not block_index.isdecimal():
        return False
    return marker in (
        ".bbox missing; block marked requires_review",
        ".parser marked block requires_review",
    )


def p9_exception_is_fail_closed_gate(exc: Exception) -> bool:
    return type(exc).__name__ == "PocServerDependencyError"


def p9_conversion_result(
    fixture: dict[str, Any],
    *,
    fixture_path: Path,
    mode: str,
    llm_scenario: str,
) -> dict[str, object]:
    from services.api.poc_web import convert_uploaded_document

    conversion_mode = (
        fixture.get("conversion_mode")
        if isinstance(fixture.get("conversion_mode"), str)
        else P9_CONVERSION_MODE_BY_MODE[mode]
    )
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
        fail_closed = p9_exception_is_fail_closed_gate(exc)
        return {
            **p9_result_for_unavailable_fixture(
                fixture,
                mode=mode,
                llm_scenario=llm_scenario,
                failure_reason=f"{type(exc).__name__}: {exc}",
                fail_closed=fail_closed,
                mvp_before_gate_revision=(
                    P9_MVP_BEFORE_GATE_REVISION_OPTIONAL_PDF_DEPS
                    if fail_closed
                    else None
                ),
            ),
            "processing_time_ms": round(elapsed_ms, 3),
        }

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    audit = converted.get("audit") if isinstance(converted.get("audit"), dict) else None
    conversion_settings = audit.get("conversion_settings") if audit else None
    conversion_settings_malformed = not isinstance(conversion_settings, dict)
    if conversion_settings_malformed:
        conversion_settings = {}
    use_llm = conversion_settings.get("use_llm", {})
    use_ocr = conversion_settings.get("use_ocr", {})
    conversion_plan = audit.get("conversion_plan", {}) if audit else {}
    external_ai_api_guard_violation = p9_external_ai_api_guard_violation(audit)
    allowed_runtime_warning_prefixes = (
        ("LLM conversion plan fallback ",)
        if llm_requested
        and isinstance(use_llm, dict)
        and use_llm.get("status") == "blocked"
        and use_llm.get("enabled") is False
        else ()
    )
    artifacts = converted.get("artifacts")
    artifact_list = artifacts if isinstance(artifacts, list) else []
    primary_artifact = p9_primary_artifact(artifact_list)
    primary_artifact_count = sum(
        1
        for artifact in artifact_list
        if isinstance(artifact, dict) and artifact.get("kind") == "primary"
    )
    warnings = converted.get("warnings", [])
    review_items = converted.get("review_items", [])
    conversion_status = converted.get("status")
    ir_generated = isinstance(converted.get("document_ir"), dict)
    audit_present = audit is not None
    artifact_expectation_failures = p9_validate_artifact_expectations(
        fixture=fixture,
        conversion_mode=conversion_mode,
        representative_mode=mode,
        primary_artifact=primary_artifact,
        warnings=warnings,
        allowed_runtime_warning_prefixes=allowed_runtime_warning_prefixes,
    )
    row_failures: list[str] = []
    if conversion_status == "blocked":
        row_failures.append("conversion status blocked")
    elif conversion_status not in {"converted", "requires_review"}:
        row_failures.append(
            f"conversion status {conversion_status!r} is not a valid terminal status"
        )
    if not ir_generated:
        row_failures.append("document IR missing")
    if not audit_present:
        row_failures.append("conversion audit missing")
    if conversion_settings_malformed:
        row_failures.append("conversion settings missing or malformed")
    if external_ai_api_guard_violation:
        row_failures.append("external AI API guard violation")
    if primary_artifact_count > 1:
        row_failures.append(
            f"expected exactly one primary artifact, got {primary_artifact_count}"
        )
    if mode == "scanned_pdf_ocr":
        use_ocr_status = use_ocr.get("status") if isinstance(use_ocr, dict) else None
        if use_ocr_status != "enabled":
            row_failures.append(f"OCR status {use_ocr_status!r} is not enabled")
    if not isinstance(use_llm, dict):
        row_failures.append(f"{llm_scenario} scenario LLM audit missing")
    elif llm_scenario == "no_llm":
        use_llm_status = use_llm.get("status")
        if use_llm_status != "disabled":
            row_failures.append(
                f"no_llm scenario LLM status {use_llm_status!r} is not disabled"
            )
        elif use_llm.get("requested") is True or use_llm.get("enabled") is True:
            row_failures.append("no_llm scenario used LLM")
    elif llm_scenario == "llm_requested":
        use_llm_status = use_llm.get("status")
        llm_request_tracked = use_llm.get("requested") is True
        llm_enabled = use_llm.get("enabled") is True
        llm_blocked_fallback = (
            use_llm_status == "blocked" and use_llm.get("enabled") is False
        )
        if not llm_request_tracked or not (llm_enabled or llm_blocked_fallback):
            row_failures.append(
                "llm_requested scenario LLM status "
                f"{use_llm_status!r} did not request LLM"
            )
    if artifact_expectation_failures:
        row_failures.append(
            "artifact expectation mismatch: "
            + "; ".join(artifact_expectation_failures[:3])
        )
    ok = not row_failures
    if row_failures:
        failure_reason = "; ".join(row_failures)
    return {
        "sample_id": fixture.get("sample_id"),
        "fixture_id": fixture.get("id"),
        "source_fixture_id": fixture.get("fixture_id"),
        "title": fixture.get("title"),
        "sample_category": fixture.get("sample_category"),
        "source_type": fixture.get("source_type"),
        "format": fixture.get("format"),
        "path": fixture.get("path"),
        "conversion_mode": conversion_mode,
        "representative_mode": mode,
        "llm_scenario": llm_scenario,
        "llm_requested": llm_requested,
        "ocr_requested": ocr_requested,
        "ok": ok,
        "ir_generated": ir_generated,
        "artifact_generated": primary_artifact_count > 0,
        "artifact_count": len(artifact_list),
        "warnings_count": len(warnings) if isinstance(warnings, list) else 0,
        "review_items_count": len(review_items) if isinstance(review_items, list) else 0,
        "audit_present": audit_present,
        "processing_time_ms": round(elapsed_ms, 3),
        "failure_reason": failure_reason,
        "llm_status": use_llm.get("status") if isinstance(use_llm, dict) else None,
        "llm_fallback_used": (
            isinstance(conversion_plan, dict)
            and conversion_plan.get("status") == "fallback"
        ),
        "use_ocr_status": use_ocr.get("status") if isinstance(use_ocr, dict) else None,
        "external_ai_api_guard_violation": external_ai_api_guard_violation,
        "conversion_status": conversion_status,
        "artifact_expectations_met": not artifact_expectation_failures,
        "artifact_expectation_failures": artifact_expectation_failures,
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
    fixture_manifest_path = p9_fixture_manifest_path(manifest, repo_root)
    fixture_manifest = load_json(fixture_manifest_path)
    fixture_paths = fixture_paths_from_manifest(fixture_manifest, repo_root)
    representative_samples = p9_evaluation_samples(manifest, fixture_manifest)

    results: list[dict[str, object]] = []
    for fixture in representative_samples:
        mode = str(fixture["representative_mode"])
        fixture_id = fixture.get("id")
        fixture_path = fixture_paths.get(fixture_id) if isinstance(fixture_id, str) else None
        for llm_scenario in P9_LLM_SCENARIOS:
            if fixture_path is None:
                pathless_real_fixture = isinstance(fixture_id, str)
                if pathless_real_fixture:
                    failure_reason = (
                        f"fixture {fixture_id!r} path is missing or null "
                        "in fixture manifest"
                    )
                else:
                    failure_reason = (
                        str(fixture.get("availability_reason"))
                        if fixture.get("availability_reason")
                        else "representative fixture path is unavailable"
                    )
                results.append(
                    p9_result_for_unavailable_fixture(
                        fixture,
                        mode=mode,
                        llm_scenario=llm_scenario,
                        failure_reason=failure_reason,
                        fail_closed=not pathless_real_fixture,
                        mvp_before_gate_revision=(
                            P9_MVP_BEFORE_GATE_REVISION_PLACEHOLDER_FIXTURE
                            if not pathless_real_fixture
                            else None
                        ),
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
        repo_root=repo_root,
    )
    return P9HarnessReport(
        manifest=manifest_path,
        results=tuple(results),
        llm_stability=llm_report.llm_stability,
        poc_mode_comparison=llm_report.poc_mode_comparison,
        llm_stability_source=llm_stability_runs_path,
        poc_comparison_source=poc_comparison_path,
        repo_root=repo_root,
    )


def current_git_commit(repo_root: Path = REPO_ROOT) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    commit = completed.stdout.strip()
    return commit if commit else "unknown"


def current_stdout_path() -> Path | None:
    for fd_path in (Path("/proc/self/fd/1"), Path("/dev/fd/1")):
        try:
            target = os.readlink(fd_path)
        except OSError:
            continue
        path = Path(target)
        if path.is_absolute():
            return path
    return None


def git_status_exclude_pathspecs(
    repo_root: Path,
    ignored_paths: Iterable[Path],
) -> list[str]:
    pathspecs: list[str] = []
    resolved_root = repo_root.resolve()
    for ignored_path in ignored_paths:
        try:
            resolved_ignored = ignored_path.resolve()
            relative_ignored = resolved_ignored.relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        pathspecs.append(f":(exclude){relative_ignored.as_posix()}")
    return pathspecs


def current_git_worktree_clean(
    repo_root: Path = REPO_ROOT,
    *,
    ignored_paths: Iterable[Path] = (),
    include_untracked: bool = True,
) -> bool:
    command = [
        "git",
        "status",
        "--porcelain",
        "--untracked-files=all" if include_untracked else "--untracked-files=no",
    ]
    exclude_pathspecs = git_status_exclude_pathspecs(repo_root, ignored_paths)
    if exclude_pathspecs:
        command.extend(["--", ".", *exclude_pathspecs])
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return completed.stdout.strip() == ""


def poc_acceptance_manifest_default_path(repo_root: Path, default_path: Path) -> Path:
    if default_path.is_absolute():
        return default_path
    return repo_root / default_path


def poc_acceptance_manifest_input_path(repo_root: Path, input_path: Path) -> Path:
    if input_path.is_absolute():
        return input_path
    return repo_root / input_path


def poc_acceptance_top_level_evidence_path(path: Path) -> Path:
    try:
        repo_root = poc_acceptance_report_repo_root(path)
    except (EvaluationCaseError, OSError):
        repo_root = REPO_ROOT
    if repo_root.resolve() != REPO_ROOT.resolve() or not path.is_absolute():
        return path
    try:
        return path.resolve().relative_to(repo_root.resolve())
    except (OSError, ValueError):
        return path


def poc_acceptance_report_manifest_path(manifest_path: Path) -> Path:
    if not manifest_path.is_absolute() and manifest_path == DEFAULT_P9_HARNESS_MANIFEST:
        return REPO_ROOT / manifest_path
    return manifest_path


def build_poc_acceptance_report(
    manifest_path: Path = DEFAULT_P9_HARNESS_MANIFEST,
    *,
    llm_stability_runs_path: Path | None = None,
    poc_comparison_path: Path | None = None,
    generation_command: str = (
        "python3 scripts/evaluate_dataset.py --poc-acceptance-report"
    ),
) -> PoCAcceptanceReport:
    report_manifest_path = poc_acceptance_report_manifest_path(manifest_path)
    resolved_manifest_path = report_manifest_path.resolve()
    manifest_repo_root = p9_manifest_repo_root(resolved_manifest_path)
    evaluated_manifest_path = (
        manifest_path
        if (
            manifest_repo_root.resolve() == REPO_ROOT.resolve()
            and Path.cwd().resolve() == REPO_ROOT.resolve()
            and not manifest_path.is_absolute()
        )
        else resolved_manifest_path
    )
    resolved_llm_stability_runs_path = (
        poc_acceptance_manifest_input_path(manifest_repo_root, llm_stability_runs_path)
        if llm_stability_runs_path is not None
        else poc_acceptance_manifest_default_path(
            manifest_repo_root,
            DEFAULT_LLM_STABILITY_RUNS,
        )
    )
    resolved_poc_comparison_path = (
        poc_acceptance_manifest_input_path(manifest_repo_root, poc_comparison_path)
        if poc_comparison_path is not None
        else poc_acceptance_manifest_default_path(
            manifest_repo_root,
            DEFAULT_POC_COMPARISON,
        )
    )
    p9_harness = evaluate_p9_harness(
        evaluated_manifest_path,
        llm_stability_runs_path=resolved_llm_stability_runs_path,
        poc_comparison_path=resolved_poc_comparison_path,
    )
    ignored_cleanliness_paths = tuple(
        path for path in (current_stdout_path(),) if path is not None
    )
    return PoCAcceptanceReport(
        p9_harness=p9_harness,
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        commit=current_git_commit(manifest_repo_root),
        commit_is_clean=current_git_worktree_clean(
            manifest_repo_root,
            ignored_paths=ignored_cleanliness_paths,
        ),
        generation_command=generation_command,
        evaluator_commit=current_git_commit(REPO_ROOT),
        evaluator_commit_is_clean=current_git_worktree_clean(
            REPO_ROOT,
            ignored_paths=ignored_cleanliness_paths,
        ),
    )


def poc_acceptance_generation_command(
    *,
    manifest_path: Path,
    llm_stability_runs_path: Path | None,
    poc_comparison_path: Path | None,
) -> str:
    command = ["python3", "scripts/evaluate_dataset.py", "--poc-acceptance-report"]
    if manifest_path != DEFAULT_P9_HARNESS_MANIFEST:
        command.append(str(manifest_path))
    if llm_stability_runs_path is not None:
        command.extend(["--llm-stability-runs", str(llm_stability_runs_path)])
    if poc_comparison_path is not None:
        command.extend(["--poc-comparison", str(poc_comparison_path)])
    return shlex.join(command)


def poc_acceptance_required_mode(
    poc_comparison: PoCComparisonMetrics,
    mode_name: str,
) -> PoCModeMetrics | None:
    for mode in poc_comparison.modes:
        if mode.mode == mode_name:
            return mode
    return None


def poc_acceptance_llm_scenario_failures(
    results: list[dict[str, object]],
) -> list[dict[str, object]]:
    failures: list[dict[str, object]] = []
    for result in results:
        if poc_acceptance_result_violates_llm_scenario(result):
            failures.append(result)
    return failures


def poc_acceptance_result_violates_llm_scenario(result: dict[str, object]) -> bool:
    if result.get("fail_closed") is True:
        return False
    llm_scenario = result.get("llm_scenario")
    if llm_scenario not in P9_LLM_SCENARIOS:
        return False
    reason = str(result.get("failure_reason") or "")
    if f"{llm_scenario} scenario" in reason:
        return True
    llm_status = result.get("llm_status")
    if llm_scenario == "no_llm":
        if result.get("llm_requested") is True:
            return True
        return llm_status not in {None, "not_run", "disabled"}
    if llm_scenario == "llm_requested":
        if llm_status == "disabled":
            return True
        if result.get("ok") is True and llm_status not in {"enabled", "blocked"}:
            return True
    return False


def poc_acceptance_structured_output_failures(
    results: list[dict[str, object]],
) -> list[dict[str, object]]:
    failures: list[dict[str, object]] = []
    for result in results:
        reason = str(result.get("failure_reason") or "")
        if (
            result.get("artifact_expectations_met") is not True
            or "expected exactly one primary artifact" in reason
        ):
            failures.append(result)
    return failures


def poc_acceptance_report_repo_root(manifest_path: Path) -> Path:
    manifest = manifest_path if manifest_path.is_absolute() else REPO_ROOT / manifest_path
    return p9_manifest_repo_root(manifest.resolve())


def poc_acceptance_harness_repo_root(p9_harness: P9HarnessReport) -> Path:
    if p9_harness.repo_root is not None:
        return p9_harness.repo_root.resolve()
    return poc_acceptance_report_repo_root(p9_harness.manifest)


def poc_acceptance_path_in_repo(path: Path, repo_root: Path) -> bool:
    resolved_root = repo_root.resolve()
    candidate = path if path.is_absolute() else resolved_root / path
    try:
        resolved_candidate = candidate.resolve()
    except OSError:
        return False
    return (
        resolved_candidate == resolved_root
        or resolved_candidate.is_relative_to(resolved_root)
    )


def poc_acceptance_tracked_repo_path(path: Path, repo_root: Path) -> bool:
    if not poc_acceptance_path_in_repo(path, repo_root):
        return False
    resolved_root = repo_root.resolve()
    candidate = path if path.is_absolute() else resolved_root / path
    try:
        relative_candidate = candidate.resolve().relative_to(resolved_root)
        completed = subprocess.run(
            [
                "git",
                "ls-files",
                "--error-unmatch",
                "--",
                relative_candidate.as_posix(),
            ],
            cwd=resolved_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return completed.returncode == 0


def poc_acceptance_p9_input_paths(p9_harness: P9HarnessReport) -> tuple[Path, ...]:
    repo_root = poc_acceptance_harness_repo_root(p9_harness)
    paths: list[Path] = [
        p9_harness.manifest,
        p9_harness.llm_stability_source,
        p9_harness.poc_comparison_source,
        *POC_AUTH_SESSION_COVERAGE_INPUT_PATHS,
    ]
    paths.extend(
        poc_acceptance_poc_comparison_input_paths(
            p9_harness.poc_comparison_source,
            repo_root,
        )
    )
    try:
        manifest = load_json(
            p9_harness.manifest
            if p9_harness.manifest.is_absolute()
            else repo_root / p9_harness.manifest
        )
        paths.append(p9_fixture_manifest_path(manifest, repo_root))
    except (EvaluationCaseError, OSError, json.JSONDecodeError):
        paths.append(repo_root / EXPECTED_DATASET_MANIFEST)
    for result in p9_harness.results:
        result_path = result.get("path")
        if isinstance(result_path, str) and result_path:
            paths.append(Path(result_path))

    unique_paths: list[Path] = []
    seen_paths: set[str] = set()
    for path in paths:
        candidate = path if path.is_absolute() else repo_root / path
        key = str(candidate)
        if key not in seen_paths:
            unique_paths.append(path)
            seen_paths.add(key)
    return tuple(unique_paths)


def poc_acceptance_poc_comparison_input_paths(
    poc_comparison_source: Path,
    repo_root: Path,
) -> tuple[Path, ...]:
    comparison_path = (
        poc_comparison_source
        if poc_comparison_source.is_absolute()
        else repo_root / poc_comparison_source
    )
    paths: list[Path] = []
    try:
        comparison_data = load_json(comparison_path)
        paths.extend(
            [
                evaluation_cases_path_from_comparison(comparison_data, repo_root),
                high_risk_labels_path_from_comparison(comparison_data, repo_root),
            ]
        )
        fixture_manifest_path = manifest_path_from_cases(comparison_data, repo_root)
        paths.append(fixture_manifest_path)
        fixture_paths = fixture_paths_from_manifest(
            load_json(fixture_manifest_path),
            repo_root,
        )
        paths.extend(fixture_paths.values())
    except (EvaluationCaseError, OSError, json.JSONDecodeError):
        paths.extend(
            [
                repo_root / EXPECTED_EVALUATION_CASES,
                repo_root / EXPECTED_HIGH_RISK_LABELS,
                repo_root / EXPECTED_DATASET_MANIFEST,
            ]
        )
    return tuple(paths)


def poc_acceptance_evidence_inputs_tracked_in_manifest_repo(
    p9_harness: P9HarnessReport,
) -> bool:
    repo_root = poc_acceptance_harness_repo_root(p9_harness)
    return all(
        poc_acceptance_tracked_repo_path(path, repo_root)
        for path in poc_acceptance_p9_input_paths(p9_harness)
    )


def poc_acceptance_overall_status(
    acceptance_matrix: list[dict[str, object]],
) -> str:
    return (
        "pass"
        if acceptance_matrix
        and all(row.get("status") == "pass" for row in acceptance_matrix)
        else "fail"
    )


def poc_acceptance_criterion_status_counts(
    acceptance_matrix: list[dict[str, object]],
) -> dict[str, int]:
    counts = {"fail": 0, "pass": 0, "unknown": 0}
    for row in acceptance_matrix:
        status = str(row.get("status"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def poc_acceptance_row(
    criterion_id: str,
    criterion_label: str,
    status: str,
    evidence: str,
    evidence_refs: list[str],
) -> dict[str, object]:
    return {
        "criterion_id": criterion_id,
        "criterion_label": criterion_label,
        "status": status,
        "evidence": evidence,
        "evidence_refs": evidence_refs,
    }


def poc_acceptance_result_evidence_rows(
    results: list[dict[str, object]],
) -> list[dict[str, object]]:
    evidence_fields = (
        "sample_id",
        "fixture_id",
        "sample_category",
        "conversion_mode",
        "representative_mode",
        "llm_scenario",
        "llm_status",
        "ok",
        "fail_closed",
        "mvp_before_gate_revision",
        "failure_reason",
        "artifact_expectations_met",
        "artifact_expectation_failures",
        "audit_present",
        "external_ai_api_guard_violation",
    )
    rows: list[dict[str, object]] = []
    for result in results:
        rows.append(
            {
                field: result[field]
                for field in evidence_fields
                if field in result
            }
        )
    return rows


def llm_stability_acceptance_failures(
    llm_stability: LLMStabilityMetrics,
    *,
    llm_scenario_failures: list[dict[str, object]],
    external_ai_api_guard_violation_count: int | None = None,
    threshold: LLMStabilityAcceptanceThreshold = LLM_STABILITY_ACCEPTANCE_THRESHOLD,
) -> list[str]:
    failures: list[str] = []
    effective_external_violation_count = (
        llm_stability.external_ai_api_guard_violation_count
        if external_ai_api_guard_violation_count is None
        else external_ai_api_guard_violation_count
    )
    if llm_stability.plan_agreement_rate < threshold.min_plan_agreement_rate:
        failures.append("plan_agreement_rate")
    if (
        llm_stability.confirmed_value_agreement_rate
        < threshold.min_confirmed_value_agreement_rate
    ):
        failures.append("confirmed_value_agreement_rate")
    if llm_stability.schema_failure_rate > threshold.max_schema_failure_rate:
        failures.append("schema_failure_rate")
    if (
        llm_stability.deterministic_fallback_rate
        > threshold.max_deterministic_fallback_rate
    ):
        failures.append("deterministic_fallback_rate")
    if (
        effective_external_violation_count
        > threshold.max_external_ai_api_guard_violation_count
    ):
        failures.append("external_ai_api_guard_violation_count")
    if llm_stability.unstable_example_count > threshold.max_unstable_example_count:
        failures.append("unstable_example_count")
    if (
        len(llm_scenario_failures)
        > threshold.max_harness_llm_scenario_failure_count
    ):
        failures.append("harness_llm_scenario_failure_count")
    return failures


def poc_acceptance_matrix_evidence(
    *,
    failed_results: list[dict[str, object]],
    artifact_failures: list[dict[str, object]],
    structured_output_failures: list[dict[str, object]],
    unaudited_results: list[dict[str, object]],
    llm_scenario_failures: list[dict[str, object]],
    observed_representative_modes: list[str],
    missing_representative_modes: list[str],
    observed_source_categories: list[str],
    missing_source_categories: list[str],
    high_quality_source_linkage_rate: float,
    external_violation_count: int,
    poc_comparison: PoCComparisonMetrics,
    llm_stability: LLMStabilityMetrics,
    llm_stability_threshold_failures: list[str],
    authenticated_poc_api_session_checked: bool,
    poc_auth_session_evidence_inputs_tracked: bool,
    commit: str,
    commit_is_clean: bool,
    evaluator_commit: str,
    evaluator_commit_is_clean: bool,
    evidence_inputs_tracked_in_manifest_repo: bool,
) -> dict[str, object]:
    return {
        "functionality": {
            "observed_representative_modes": observed_representative_modes,
            "missing_representative_modes": missing_representative_modes,
            "observed_source_categories": observed_source_categories,
            "missing_source_categories": missing_source_categories,
            "manual_correction_time": poc_comparison.manual_correction_time.as_dict(),
            "failed_rows": poc_acceptance_result_evidence_rows(failed_results),
        },
        "structured_output": {
            "artifact_expectation_failures": poc_acceptance_result_evidence_rows(
                artifact_failures
            ),
            "rows": poc_acceptance_result_evidence_rows(
                structured_output_failures
            ),
        },
        "llm_control": {
            "external_ai_api_guard_violation_count": external_violation_count,
            "threshold": LLM_STABILITY_ACCEPTANCE_THRESHOLD.as_dict(),
            "threshold_failures": llm_stability_threshold_failures,
            "unstable_examples": list(llm_stability.unstable_examples),
            "scenario_failures": poc_acceptance_result_evidence_rows(
                llm_scenario_failures
            ),
        },
        "traceability": {
            "high_quality_source_linkage_rate": high_quality_source_linkage_rate,
        },
        "safety": {
            "high_risk_false_auto_confirmed_count": (
                poc_comparison.high_risk_false_auto_confirmed_count
            ),
            "high_risk_false_auto_confirmed_target": (
                poc_comparison.high_risk_false_auto_confirmed_target
            ),
            "external_ai_api_guard_violation_count": external_violation_count,
        },
        "logs": {
            "rows": poc_acceptance_result_evidence_rows(unaudited_results),
        },
        "security": {
            "external_ai_api_guard_violation_count": external_violation_count,
            "authenticated_poc_api_session_checked": (
                authenticated_poc_api_session_checked
            ),
            "authenticated_poc_api_session_evidence_inputs_tracked": (
                poc_auth_session_evidence_inputs_tracked
            ),
            "authenticated_poc_api_session_evidence_refs": list(
                poc_auth_session_coverage_evidence_refs()
            ),
        },
        "reproducibility": {
            "commit": commit,
            "commit_is_clean": commit_is_clean,
            "evaluator_commit": evaluator_commit,
            "evaluator_commit_is_clean": evaluator_commit_is_clean,
            "evidence_inputs_tracked_in_manifest_repo": (
                evidence_inputs_tracked_in_manifest_repo
            ),
        },
    }


def poc_acceptance_condition(
    condition_id: str,
    description: str,
    status: str,
    evidence: str,
) -> dict[str, str]:
    return {
        "condition_id": condition_id,
        "description": description,
        "status": status,
        "evidence": evidence,
    }


def poc_acceptance_conversion_mode_results(
    results: list[dict[str, object]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for conversion_mode in sorted({str(result["conversion_mode"]) for result in results}):
        mode_results = [
            result
            for result in results
            if str(result.get("conversion_mode")) == conversion_mode
        ]
        rows.append(
            {
                "conversion_mode": conversion_mode,
                "case_count": len(mode_results),
                "completed_count": sum(
                    1 for result in mode_results if result.get("ok") is True
                ),
                "failure_count": sum(
                    1 for result in mode_results if result.get("ok") is not True
                ),
                "llm_scenarios": sorted(
                    {
                        str(result.get("llm_scenario"))
                        for result in mode_results
                        if result.get("llm_scenario") is not None
                    }
                ),
                "sample_ids": sorted(
                    {
                        str(result.get("sample_id"))
                        for result in mode_results
                        if result.get("sample_id") is not None
                    }
                ),
            }
        )
    return rows


def poc_acceptance_fail_closed_conditions(
    *,
    external_violation_count: int,
    poc_comparison: PoCComparisonMetrics,
    unaudited_results: list[dict[str, object]],
    llm_stability: LLMStabilityMetrics,
    llm_scenario_failures: list[dict[str, object]],
    llm_stability_threshold_failures: list[str],
    structured_output_failures: list[dict[str, object]],
) -> list[dict[str, str]]:
    return [
        poc_acceptance_condition(
            "original_absent_value_confirmed",
            "原本に無い値の確定",
            "pass"
            if poc_comparison.high_risk_false_auto_confirmed_count
            <= poc_comparison.high_risk_false_auto_confirmed_target
            else "fail",
            (
                "High-risk false auto-confirmed count is "
                f"{poc_comparison.high_risk_false_auto_confirmed_count}."
            ),
        ),
        poc_acceptance_condition(
            "llm_correction_or_completion",
            "LLM補正/補完",
            "fail" if llm_stability_threshold_failures else "pass",
            (
                "LLM stability evidence has "
                f"{llm_stability.unstable_example_count} unstable examples; "
                "harness LLM scenario failures: "
                f"{len(llm_scenario_failures)}; external AI API guard "
                f"violations: {external_violation_count}; threshold failures: "
                f"{llm_stability_threshold_failures or 'none'}."
            ),
        ),
        poc_acceptance_condition(
            "unknown_source_normal_output",
            "出典不明通常出力",
            "fail" if structured_output_failures else "pass",
            (
                f"{len(structured_output_failures)} rows failed artifact/source "
                "structure or expectations."
            ),
        ),
        poc_acceptance_condition(
            "high_risk_auto_confirm",
            "高リスク自動確定",
            "pass"
            if poc_comparison.high_risk_false_auto_confirmed_count == 0
            else "fail",
            (
                "High-risk false auto-confirmed count is "
                f"{poc_comparison.high_risk_false_auto_confirmed_count}."
            ),
        ),
        poc_acceptance_condition(
            "audit_log_missing",
            "ログ欠落",
            "fail" if unaudited_results else "pass",
            f"{len(unaudited_results)} rows lacked audit evidence.",
        ),
        poc_acceptance_condition(
            "external_transmission",
            "外部送信",
            "pass" if external_violation_count == 0 else "fail",
            f"External AI API guard violation count is {external_violation_count}.",
        ),
        poc_acceptance_condition(
            "source_document_replacement",
            "原本代替扱い",
            "pass",
            "The report is marked as PoC evidence and does not claim GMP production use.",
        ),
    ]


def poc_acceptance_known_limitations(
    failed_results: list[dict[str, object]],
    fail_closed_gate_results: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    limitations: list[dict[str, object]] = [
        {
            "id": "not_go_no_go",
            "description": "This report is not the final MVP go/no-go decision.",
        },
        {
            "id": "not_gmp_validation",
            "description": "This report is not a formal GMP validation document.",
        },
    ]
    non_gate_failed_results = [
        result for result in failed_results if result.get("fail_closed") is not True
    ]
    for result in non_gate_failed_results[:5]:
        limitations.append(
            {
                "id": f"p9_harness_failure_{result.get('sample_id')}",
                "description": str(result.get("failure_reason")),
                "conversion_mode": result.get("conversion_mode"),
                "llm_scenario": result.get("llm_scenario"),
            }
        )
    for result in fail_closed_gate_results or []:
        limitations.append(
            {
                "id": f"p9_fail_closed_gate_{result.get('sample_id')}",
                "description": str(result.get("failure_reason")),
                "conversion_mode": result.get("conversion_mode"),
                "llm_scenario": result.get("llm_scenario"),
                "mvp_before_gate_revision": result.get("mvp_before_gate_revision"),
            }
        )
    return limitations


def poc_acceptance_follow_up_candidates(
    failed_results: list[dict[str, object]],
    fail_closed_gate_results: list[dict[str, object]],
    llm_stability_threshold_failures: list[str],
    unaudited_results: list[dict[str, object]],
    poc_comparison: PoCComparisonMetrics,
) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    non_gate_failed_results = [
        result for result in failed_results if result.get("fail_closed") is not True
    ]
    gate_revisions = sorted(
        {
            str(result.get("mvp_before_gate_revision"))
            for result in fail_closed_gate_results
            if result.get("fail_closed") is True
            and isinstance(result.get("mvp_before_gate_revision"), str)
        }
    )
    if gate_revisions:
        candidates.append(
            {
                "title": "Resolve fail-closed P9 MVP-before gate revisions",
                "reason": (
                    f"{len(gate_revisions)} gate revision(s) still require "
                    "follow-up: "
                    + ", ".join(gate_revisions)
                    + "."
                ),
            }
        )
    if non_gate_failed_results:
        candidates.append(
            {
                "title": "Resolve failing P9 representative conversion harness rows",
                "reason": (
                    f"{len(non_gate_failed_results)} harness rows are not "
                    "acceptance-ready."
                ),
            }
        )
    if llm_stability_threshold_failures:
        candidates.append(
            {
                "title": "Resolve LLM stability acceptance threshold failures",
                "reason": (
                    f"{len(llm_stability_threshold_failures)} threshold "
                    "failure(s) remain in the synthetic stability and P9 "
                    "harness LLM-control evidence: "
                    + ", ".join(llm_stability_threshold_failures)
                    + "."
                ),
            }
        )
    if unaudited_results:
        candidates.append(
            {
                "title": "Require audit evidence for all P9 harness outcomes",
                "reason": f"{len(unaudited_results)} rows lacked audit evidence.",
            }
        )
    if not poc_comparison.manual_correction_time.target_met:
        candidates.append(
            {
                "title": "Close the PoC manual-correction-time acceptance gap",
                "reason": (
                    "Assisted review timing missed the configured "
                    f"{poc_comparison.manual_correction_time.target_reduction_rate:.3f} "
                    "reduction target."
                ),
            }
        )
    return candidates


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
    for parent in gold_path.parents:
        if parent.name == "datasets" and (parent / "fixtures" / "manifest.json").is_file():
            return parent.parent
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
    *,
    repo_root: Path | None = None,
) -> LLMStabilityEvaluationReport:
    resolved_stability_path = llm_stability_runs_path.resolve()
    resolved_comparison_path = poc_comparison_path.resolve()
    poc_repo_root = (
        repo_root.resolve()
        if repo_root is not None
        else repository_root_for_gold_path(resolved_comparison_path)
    )
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
    parser.add_argument(
        "--poc-acceptance-report",
        type=Path,
        nargs="?",
        const=DEFAULT_P9_HARNESS_MANIFEST,
        help=(
            "Emit the Phase9 PoC acceptance report that maps 15.2 criteria "
            "to pass/fail/unknown evidence."
        ),
    )
    args = parser.parse_args()
    selected_report_modes = [
        args.p9_harness is not None,
        args.poc_acceptance_report is not None,
        args.gmp_acceptance is not None,
        args.llm_stability_report,
    ]
    if args.p9_harness is not None and args.gmp_acceptance is not None:
        parser.error("--p9-harness cannot be combined with --gmp-acceptance")
    if sum(1 for selected in selected_report_modes if selected) > 1:
        parser.error(
            "--p9-harness, --poc-acceptance-report, --gmp-acceptance, and "
            "--llm-stability-report cannot be combined"
        )

    try:
        if args.poc_acceptance_report is not None:
            metrics = build_poc_acceptance_report(
                args.poc_acceptance_report,
                llm_stability_runs_path=args.llm_stability_runs,
                poc_comparison_path=args.poc_comparison,
                generation_command=poc_acceptance_generation_command(
                    manifest_path=args.poc_acceptance_report,
                    llm_stability_runs_path=args.llm_stability_runs,
                    poc_comparison_path=args.poc_comparison,
                ),
            )
        elif args.p9_harness is not None:
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
    if (
        args.p9_harness is None
        and args.gmp_acceptance is not None
        and not metrics.target_met
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
