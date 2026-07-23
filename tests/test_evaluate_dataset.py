from __future__ import annotations

import copy
import hashlib
import importlib.util
from io import BytesIO
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from zipfile import ZIP_DEFLATED, ZipFile


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "evaluate_dataset.py"
CASES_PATH = REPO_ROOT / "datasets" / "gold" / "evaluation_cases_v0.json"
HIGH_RISK_LABELS_PATH = REPO_ROOT / "datasets" / "gold" / "high_risk_labels_v0.json"
LLM_STABILITY_RUNS_PATH = REPO_ROOT / "datasets" / "gold" / "llm_stability_runs_v0.json"
POC_COMPARISON_PATH = REPO_ROOT / "datasets" / "gold" / "poc_mode_comparison_v1.json"
GMP_ACCEPTANCE_PATH = REPO_ROOT / "datasets" / "gold" / "gmp_acceptance_v1.json"
FIXTURE_MANIFEST_PATH = REPO_ROOT / "datasets" / "fixtures" / "manifest.json"
POC_EVALUATION_MANIFEST_PATH = REPO_ROOT / "datasets" / "poc_evaluation_manifest_v1.json"
MVP_EVALUATION_MANIFEST_PATH = REPO_ROOT / "datasets" / "mvp_evaluation_manifest_v1.json"
MVP_ACCEPTANCE_TRACEABILITY_PATH = (
    REPO_ROOT / "docs" / "mvp-acceptance-traceability.md"
)
MVP_SCOPE_DECISIONS_PATH = REPO_ROOT / "docs" / "mvp-scope-decisions.md"
PYMUPDF_AVAILABLE = importlib.util.find_spec("pymupdf") is not None


spec = importlib.util.spec_from_file_location("evaluate_dataset", SCRIPT_PATH)
assert spec is not None
evaluate_dataset = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = evaluate_dataset
spec.loader.exec_module(evaluate_dataset)


def rewritten_docx(
    content: bytes,
    *,
    replacements: dict[str, tuple[tuple[str, str], ...]] | None = None,
    omitted_parts: frozenset[str] = frozenset(),
) -> bytes:
    output = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(
        output,
        "w",
        compression=ZIP_DEFLATED,
    ) as target:
        for member in source.infolist():
            if member.filename in omitted_parts:
                continue
            member_content = source.read(member.filename)
            if replacements is not None and member.filename in replacements:
                member_text = member_content.decode("utf-8")
                for old, new in replacements[member.filename]:
                    member_text = member_text.replace(old, new)
                member_content = member_text.encode("utf-8")
            target.writestr(member, member_content)
    return output.getvalue()


def valid_poc_auth_success_ref_source(
    overrides: dict[str, str] | None = None,
    *,
    include_direct_auth_setup: bool = True,
    local_auth_tokens_source: str | None = None,
    trusted_helper_source: str | None = None,
) -> str:
    snippets: dict[str, str] = {
        "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
            "    monkeypatch.setenv(\n"
            "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
            "        'reviewer:env-reviewer=env-reviewer-token',\n"
            "    )\n"
            "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
            "    status, body = _post_review_event_on_connection(\n"
            "        connection,\n"
            "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
            "        role_token='env-reviewer-token',\n"
            "    )\n"
            "    assert status == 202\n"
        ),
        "test_poc_http_api_filters_review_action_audit_events_by_action": (
            "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
            "    status, body = _post_review_event_on_connection(\n"
            "        connection,\n"
            "        _review_audit_event(\n"
            "            action='approve', conversion_id='conversion-current'\n"
            "        ),\n"
            "        role_token='admin-token',\n"
            "    )\n"
            "    assert status == 202\n"
        ),
        "test_poc_http_api_allows_approval_with_revised_text_target": (
            "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
            "    status, body = _post_review_event_on_connection(\n"
            "        connection,\n"
            "        _review_audit_event(\n"
            "            action='approve',\n"
            "            revised_text='Lot: SAMPLE-001 corrected',\n"
            "        ),\n"
            "        role_token='admin-token',\n"
            "    )\n"
            "    assert status == 202\n"
        ),
        "test_poc_http_api_requires_admin_role_for_retry_job_event": (
            "    action = 'retry_conversion'\n"
            "    body = json.dumps({'action': action}).encode('utf-8')\n"
            "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
            "    connection.request(\n"
            "        'POST',\n"
            "        '/api/job-events',\n"
            "        body=body,\n"
            "        headers={'Authorization': 'Bearer admin-token'},\n"
            "    )\n"
            "    response = connection.getresponse()\n"
            "    assert response.status == 202\n"
        ),
    }
    if overrides is not None:
        snippets.update(overrides)
    functions = []
    direct_auth_setup = (
        "    server.local_auth_tokens = _local_auth_tokens()\n"
        if include_direct_auth_setup
        else ""
    )
    for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS:
        test_name = ref.split("::", 1)[1]
        if ref in evaluate_dataset.POC_AUTH_SESSION_ENV_SUCCESS_COVERAGE_REFS:
            functions.append(f"def {test_name}(monkeypatch):\n{snippets[test_name]}")
        else:
            functions.append(f"def {test_name}():\n{direct_auth_setup}{snippets[test_name]}")
    if include_direct_auth_setup:
        if local_auth_tokens_source is None:
            local_auth_tokens_source = (
                "def _local_auth_tokens():\n"
                "    return {\n"
                "        'viewer-token': {'role': 'viewer', 'principal_id': 'viewer'},\n"
                "        'reviewer-token': {'role': 'reviewer', 'principal_id': 'reviewer'},\n"
                "        'approver-token': {'role': 'approver', 'principal_id': 'approver'},\n"
                "        'admin-token': {'role': 'admin', 'principal_id': 'admin'},\n"
                "    }\n"
            )
        functions.append(local_auth_tokens_source.rstrip())
    server_setup = (
        "    server = ThreadingHTTPServer(('127.0.0.1', 0), PocWebRequestHandler)\n"
    )
    if trusted_helper_source is None:
        trusted_helper_source = (
            "def _post_review_event_on_connection(connection, audit_event, *, role_token):\n"
            "    payload = json.dumps({'audit_event': audit_event}).encode('utf-8')\n"
            "    connection.request(\n"
            "        'POST',\n"
            "        '/api/review-events',\n"
            "        body=payload,\n"
            "        headers={'Authorization': f'Bearer {role_token}'},\n"
            "    )\n"
            "    response = connection.getresponse()\n"
            "    return response.status, {}\n"
        )
    functions.append(trusted_helper_source.rstrip())
    return "\n".join(
        function.replace("\n    ", f"\n{server_setup}    ", 1)
        if function.startswith("def test_")
        else function
        for function in functions
    )


def valid_poc_auth_fail_closed_ref_source(
    overrides: dict[str, str] | None = None,
    *,
    include_auth_setup: bool = True,
) -> str:
    snippets: dict[str, str] = {
        "test_poc_http_api_authenticates_review_events_before_parsing_payload": (
            "    payload = b'{not valid json'\n"
            "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
            "    connection.request(\n"
            "        'POST',\n"
            "        '/api/review-events',\n"
            "        body=payload,\n"
            "        headers={'Content-Type': 'application/json'},\n"
            "    )\n"
            "    response = connection.getresponse()\n"
            "    body = json.loads(response.read().decode('utf-8'))\n"
            "    assert response.status == 401\n"
            "    assert body == {\n"
            "        'error': 'auth_required',\n"
            "        'message': 'Authorization bearer token is required',\n"
            "    }\n"
        ),
        "test_poc_http_api_rejects_read_only_review_role_before_parsing_payload": (
            "    payload = b'{not valid json'\n"
            "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
            "    connection.request(\n"
            "        'POST',\n"
            "        '/api/review-events',\n"
            "        body=payload,\n"
            "        headers={\n"
            "            'Authorization': 'Bearer viewer-token',\n"
            "            'Content-Type': 'application/json',\n"
            "        },\n"
            "    )\n"
            "    response = connection.getresponse()\n"
            "    body = json.loads(response.read().decode('utf-8'))\n"
            "    assert response.status == 403\n"
            "    assert body == {'error': 'forbidden'}\n"
        ),
        "test_poc_http_api_requires_configured_local_auth_token_for_review_events": (
            "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
            "    connection.request(\n"
            "        'POST',\n"
            "        '/api/review-events',\n"
            "        body=b'{\"event_type\":\"conversion_review.edit_submitted\"}',\n"
            "        headers={'Content-Type': 'application/json'},\n"
            "    )\n"
            "    response = connection.getresponse()\n"
            "    body = json.loads(response.read().decode('utf-8'))\n"
            "    assert response.status == 401\n"
            "    assert body == {\n"
            "        'error': 'auth_required',\n"
            "        'message': 'Authorization bearer token is required',\n"
            "    }\n"
        ),
        "test_poc_http_api_authenticates_job_events_before_parsing_payload": (
            "    payload = b'{not valid json'\n"
            "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
            "    connection.request(\n"
            "        'POST',\n"
            "        '/api/job-events',\n"
            "        body=payload,\n"
            "        headers={'Content-Type': 'application/json'},\n"
            "    )\n"
            "    response = connection.getresponse()\n"
            "    body = json.loads(response.read().decode('utf-8'))\n"
            "    assert response.status == 401\n"
            "    assert body == {\n"
            "        'error': 'auth_required',\n"
            "        'message': 'Authorization bearer token is required',\n"
            "    }\n"
        ),
    }
    if overrides is not None:
        snippets.update(overrides)
    server_setup = (
        "    server = ThreadingHTTPServer(('127.0.0.1', 0), PocWebRequestHandler)\n"
    )
    auth_setup = "    server.local_auth_tokens = _local_auth_tokens()\n"
    return "\n".join(
        f"def {ref.split('::', 1)[1]}():\n"
        f"{server_setup}"
        f"{auth_setup if include_auth_setup else ''}"
        f"{snippets[ref.split('::', 1)[1]]}"
        for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
    )


class EvaluateDatasetTest(unittest.TestCase):
    def valid_cases_data(self) -> dict[str, object]:
        return copy.deepcopy(evaluate_dataset.load_json(CASES_PATH))

    def valid_llm_stability_data(self) -> dict[str, object]:
        return copy.deepcopy(evaluate_dataset.load_json(LLM_STABILITY_RUNS_PATH))

    def valid_poc_comparison_data(self) -> dict[str, object]:
        return copy.deepcopy(evaluate_dataset.load_json(POC_COMPARISON_PATH))

    def valid_gmp_acceptance_data(self) -> dict[str, object]:
        return copy.deepcopy(evaluate_dataset.load_json(GMP_ACCEPTANCE_PATH))

    def valid_mvp_manifest_data(self) -> dict[str, object]:
        return copy.deepcopy(evaluate_dataset.load_json(MVP_EVALUATION_MANIFEST_PATH))

    def valid_mvp_acceptance_limits(self) -> dict[str, int]:
        return evaluate_dataset.mvp_acceptance_limits(self.valid_mvp_manifest_data())

    def traceability_with_cell(
        self,
        traceability: str,
        item_id: str,
        cell_index: int,
        value: str,
    ) -> str:
        lines = traceability.splitlines()
        for index, line in enumerate(lines):
            if not line.startswith(f"| {item_id} |"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            cells[cell_index] = value
            lines[index] = "| " + " | ".join(cells) + " |"
            return "\n".join(lines)
        self.fail(f"traceability row {item_id!r} not found")

    def valid_mvp_case(self, index: int = 0) -> dict[str, object]:
        return evaluate_dataset.mvp_evaluation_cases(
            self.valid_mvp_manifest_data(),
            evaluate_dataset.load_json(FIXTURE_MANIFEST_PATH),
        )[index]

    def valid_mvp_conversion_payload(
        self,
        case: dict[str, object],
        *,
        fixture_path: Path,
        fixture_content: bytes,
    ) -> dict[str, object]:
        from services.api.poc_web import (
            CONVERSION_AUDIT_SCHEMA_VERSION,
            CONVERSION_PLAN_PROMPT_ID,
            CONVERSION_PLAN_PROMPT_VERSION,
            CONVERSION_PLAN_SCHEMA_VERSION,
        )
        from core.ir.document_ir_v1 import SCHEMA_VERSION as DOCUMENT_IR_SCHEMA_VERSION

        source_sha256 = hashlib.sha256(fixture_content).hexdigest()
        conversion_id = "conversion-mvp-review-test"
        source_type = str(case["source_type"])
        conversion_mode = str(case["conversion_mode"])
        warnings = copy.deepcopy(case["expected_warnings"])
        review_items = (
            [{"id": "review-item-mvp-test"}]
            if case["expected_status"] == "requires_review"
            else []
        )
        document_ir_schema_version = DOCUMENT_IR_SCHEMA_VERSION
        return {
            "status": case["expected_status"],
            "conversion_id": conversion_id,
            "hashes": {"source_sha256": source_sha256},
            "artifacts": [
                {
                    "kind": "primary",
                    "id": "primary-review-test",
                    "format": case["expected_artifacts"][0]["type"],
                    "content": b"artifact-review-test",
                }
            ],
            "warnings": warnings,
            "review_items": review_items,
            "document_ir": {"schema_version": document_ir_schema_version},
            "audit": {
                "schema_version": CONVERSION_AUDIT_SCHEMA_VERSION,
                "conversion_id": conversion_id,
                "input": {
                    "filename": fixture_path.name,
                    "source_type": source_type,
                    "sha256": source_sha256,
                    "conversion_mode": conversion_mode,
                },
                "source_filename": fixture_path.name,
                "source_type": source_type,
                "source_sha256": source_sha256,
                "conversion_mode": conversion_mode,
                "versions": {
                    "prompt": {
                        "id": CONVERSION_PLAN_PROMPT_ID,
                        "version": CONVERSION_PLAN_PROMPT_VERSION,
                    },
                    "schemas": {
                        "conversion_audit": CONVERSION_AUDIT_SCHEMA_VERSION,
                        "conversion_plan": CONVERSION_PLAN_SCHEMA_VERSION,
                        "document_ir": document_ir_schema_version,
                    }
                },
                "warnings": {"count": len(warnings)},
                "review_items": {"count": len(review_items)},
            },
        }

    def valid_high_risk_labels_data(self) -> dict[str, object]:
        return copy.deepcopy(evaluate_dataset.load_json(HIGH_RISK_LABELS_PATH))

    def poc_auth_session_coverage_is_present_for_source(
        self,
        test_source: str,
        *,
        module_prefix: str = "",
        module_suffix: str = "",
    ) -> bool:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{module_prefix}\n{test_source}\n{module_suffix}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                return evaluate_dataset.poc_auth_session_coverage_is_present(
                    temp_root
                )

    def poc_acceptance_payload(
        self,
        *,
        results: list[dict[str, object]] | None = None,
        llm_external_violation_count: int = 0,
        unstable_example_count: int = 0,
        llm_plan_agreement_rate: float = 1.0,
        llm_confirmed_value_agreement_rate: float = 1.0,
        llm_schema_failure_rate: float = 0.0,
        llm_deterministic_fallback_rate: float = 0.0,
        manual_correction_target_met: bool = True,
        source_linkage_rates: dict[str, float] | None = None,
        manifest: Path = POC_EVALUATION_MANIFEST_PATH,
        llm_stability_source: Path = LLM_STABILITY_RUNS_PATH,
        poc_comparison_source: Path = POC_COMPARISON_PATH,
        harness_repo_root: Path | None = None,
        commit: str = "test-commit",
        commit_is_clean: bool = True,
        evaluator_commit: str | None = None,
        evaluator_commit_is_clean: bool | None = None,
    ) -> dict[str, object]:
        if results is None:
            representative_rows = (
                ("word_to_excel", "word"),
                ("excel_to_word", "excel"),
                ("pdf_to_excel", "text_pdf"),
                ("pdf_to_word", "record_pdf"),
                ("scanned_pdf_ocr", "scanned_pdf"),
            )
            results = [
                {
                    "sample_id": f"sample-{representative_mode}",
                    "sample_category": sample_category,
                    "conversion_mode": evaluate_dataset.P9_CONVERSION_MODE_BY_MODE[
                        representative_mode
                    ],
                    "representative_mode": representative_mode,
                    "llm_scenario": "no_llm",
                    "ok": True,
                    "artifact_expectations_met": True,
                    "audit_present": True,
                    "external_ai_api_guard_violation": False,
                }
                for representative_mode, sample_category in representative_rows
            ]
        llm_stability = evaluate_dataset.LLMStabilityMetrics(
            input_id="synthetic-report-test",
            run_count=1,
            plan_agreement_rate=llm_plan_agreement_rate,
            confirmed_value_agreement_rate=llm_confirmed_value_agreement_rate,
            schema_failure_rate=llm_schema_failure_rate,
            repair_success_rate=1.0,
            deterministic_fallback_rate=llm_deterministic_fallback_rate,
            external_ai_api_guard_violation_count=llm_external_violation_count,
            distinct_plan_count=1,
            distinct_confirmed_value_count=1,
            unstable_example_count=unstable_example_count,
            unstable_examples=(
                {"run_id": "run-002", "changed": "conversion_plan"},
            )
            if unstable_example_count
            else (),
        )
        poc_comparison = evaluate_dataset.PoCComparisonMetrics(
            mode_count=len(evaluate_dataset.REQUIRED_POC_MODES),
            high_risk_false_auto_confirmed_count=0,
            high_risk_false_auto_confirmed_target=0,
            target_met=manual_correction_target_met,
            manual_correction_time=evaluate_dataset.ManualCorrectionTimeMetrics(
                measurement_method="synthetic",
                baseline_minutes=10.0,
                assisted_minutes=4.0 if manual_correction_target_met else 8.0,
                reduction_minutes=6.0 if manual_correction_target_met else 2.0,
                reduction_rate=0.6 if manual_correction_target_met else 0.2,
                target_reduction_rate=0.5,
                target_met=manual_correction_target_met,
            ),
            modes=tuple(
                evaluate_dataset.PoCModeMetrics(
                    mode=mode,
                    table_extraction_rate=1.0,
                    cell_match_rate=1.0,
                    source_linkage_rate=(
                        source_linkage_rates.get(mode, 1.0)
                        if source_linkage_rates is not None
                        else 1.0
                    ),
                    high_risk_false_auto_confirmed_count=0,
                    requires_review_count=0,
                    warning_count=0,
                )
                for mode in evaluate_dataset.REQUIRED_POC_MODES
            ),
            mode_diffs=(),
        )
        harness = evaluate_dataset.P9HarnessReport(
            manifest=manifest,
            results=tuple(results),
            llm_stability=llm_stability,
            poc_mode_comparison=poc_comparison,
            llm_stability_source=llm_stability_source,
            poc_comparison_source=poc_comparison_source,
            repo_root=harness_repo_root,
        )
        report = evaluate_dataset.PoCAcceptanceReport(
            p9_harness=harness,
            generated_at="2026-01-01T00:00:00Z",
            commit=commit,
            commit_is_clean=commit_is_clean,
            evaluator_commit=evaluator_commit,
            evaluator_commit_is_clean=evaluator_commit_is_clean,
        )
        return report.as_dict()

    def prepare_gmp_acceptance_repo(self, temp_root: Path) -> None:
        shutil.copytree(REPO_ROOT / "datasets", temp_root / "datasets")
        (temp_root / "docs").mkdir()
        for doc_name in (
            "change-management-reevaluation.md",
            "gmp04-electronic-records-signatures.md",
            "gmp07-validation-draft.md",
            "gmp08-acceptance-evaluation.md",
        ):
            shutil.copy2(REPO_ROOT / "docs" / doc_name, temp_root / "docs" / doc_name)
        (temp_root / "scripts").mkdir()
        shutil.copy2(
            REPO_ROOT / "scripts" / "evaluate_dataset.py",
            temp_root / "scripts" / "evaluate_dataset.py",
        )
        (temp_root / "scripts" / "ci").mkdir()
        shutil.copy2(
            REPO_ROOT / "scripts" / "ci" / "repo_hygiene.py",
            temp_root / "scripts" / "ci" / "repo_hygiene.py",
        )
        (temp_root / "tests").mkdir()
        shutil.copy2(
            REPO_ROOT / "tests" / "test_poc_web_api.py",
            temp_root / "tests" / "test_poc_web_api.py",
        )

    def evaluate_valid_cases(self, data: dict[str, object]) -> object:
        return evaluate_dataset.evaluate_cases(data, manifest_root=REPO_ROOT)

    def evaluate_with_fixture(
        self,
        data: dict[str, object],
        fixture: dict[str, object],
        fixture_metadata: dict[str, object] | None = None,
        manifest_policy: dict[str, object] | None = None,
        fixture_relpath: str = "datasets/fixtures/fixture.json",
    ) -> object:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            fixture_dir = temp_root / "datasets" / "fixtures"
            fixture_dir.mkdir(parents=True)
            fixture_path = temp_root / fixture_relpath
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path = fixture_dir / "manifest.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            manifest_fixture = {
                "id": data["cases"][0]["fixture_id"],
                "anonymization": "synthetic",
                "public_review_safe": True,
                "confidentiality": "public",
                "path": fixture_relpath,
            }
            if fixture_metadata is not None:
                manifest_fixture.update(fixture_metadata)
            policy = {
                "allowed_fixture_root": "datasets/fixtures",
                "public_only": True,
                "confidential_source_documents_allowed": False,
            }
            if manifest_policy is not None:
                policy.update(manifest_policy)
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": evaluate_dataset.FIXTURE_MANIFEST_SCHEMA_VERSION,
                        "policy": policy,
                        "fixtures": [manifest_fixture],
                    }
                ),
                encoding="utf-8",
            )
            data["dataset_manifest"] = "datasets/fixtures/manifest.json"

            return evaluate_dataset.evaluate_cases(data, manifest_root=temp_root)

    def test_public_fixture_metrics_cover_phase0_acceptance_criteria(self) -> None:
        data = evaluate_dataset.load_json(CASES_PATH)

        metrics = self.evaluate_valid_cases(data)

        self.assertEqual(1.0, metrics.table_extraction_rate)
        self.assertEqual(0.5, metrics.cell_match_rate)
        self.assertEqual(0.5, metrics.source_linkage_rate)
        self.assertEqual(1, metrics.false_auto_confirmed_count)
        self.assertEqual(1, metrics.expected_table_count)
        self.assertEqual(2, metrics.expected_cell_count)
        self.assertEqual(2, metrics.expected_source_link_count)

    def test_llm_stability_metrics_quantify_repeated_output_drift(self) -> None:
        metrics = evaluate_dataset.evaluate_llm_stability(self.valid_llm_stability_data())

        self.assertEqual("synthetic-batch-record-001", metrics.input_id)
        self.assertEqual(3, metrics.run_count)
        self.assertEqual(2 / 3, metrics.plan_agreement_rate)
        self.assertEqual(2 / 3, metrics.confirmed_value_agreement_rate)
        self.assertEqual(2 / 3, metrics.schema_failure_rate)
        self.assertEqual(1 / 2, metrics.repair_success_rate)
        self.assertEqual(1 / 3, metrics.deterministic_fallback_rate)
        self.assertEqual(0, metrics.external_ai_api_guard_violation_count)
        self.assertEqual(2, metrics.distinct_plan_count)
        self.assertEqual(2, metrics.distinct_confirmed_value_count)
        self.assertEqual(2, metrics.unstable_example_count)
        self.assertEqual(
            (
                {
                    "reference_run_id": "run-001",
                    "run_id": "run-002",
                    "changed": "confirmed_values",
                },
                {
                    "reference_run_id": "run-001",
                    "run_id": "run-003",
                    "changed": "conversion_plan",
                },
            ),
            metrics.unstable_examples,
        )

    def test_poc_mode_comparison_measures_required_phase1_modes(self) -> None:
        metrics = evaluate_dataset.evaluate_poc_mode_comparison(
            self.valid_poc_comparison_data(), repo_root=REPO_ROOT
        )

        self.assertEqual(3, metrics.mode_count)
        self.assertEqual(0, metrics.high_risk_false_auto_confirmed_count)
        self.assertTrue(metrics.target_met)
        self.assertEqual(12.0, metrics.manual_correction_time.baseline_minutes)
        self.assertEqual(5.0, metrics.manual_correction_time.assisted_minutes)
        self.assertEqual(7.0, metrics.manual_correction_time.reduction_minutes)
        self.assertEqual(7 / 12, metrics.manual_correction_time.reduction_rate)
        self.assertTrue(metrics.manual_correction_time.target_met)
        self.assertEqual(
            ["no_llm", "standard", "high_quality"],
            [mode["mode"] for mode in metrics.as_dict()["modes"]],
        )
        self.assertEqual([2, 1, 0], [mode["warning_count"] for mode in metrics.as_dict()["modes"]])
        self.assertEqual(
            [
                {
                    "baseline_mode": "no_llm",
                    "candidate_mode": "standard",
                    "review_item_added_count": 0,
                    "review_item_removed_count": 0,
                    "warning_added_count": 0,
                    "warning_removed_count": 1,
                    "added_review_items": [],
                    "removed_review_items": [],
                    "added_warnings": [],
                    "removed_warnings": ["lot-number-mismatch"],
                },
                {
                    "baseline_mode": "no_llm",
                    "candidate_mode": "high_quality",
                    "review_item_added_count": 0,
                    "review_item_removed_count": 0,
                    "warning_added_count": 0,
                    "warning_removed_count": 2,
                    "added_review_items": [],
                    "removed_review_items": [],
                    "added_warnings": [],
                    "removed_warnings": [
                        "lot-number-mismatch",
                        "missing-source-anchor",
                    ],
                },
            ],
            metrics.as_dict()["mode_diffs"],
        )
        high_quality = metrics.as_dict()["modes"][2]
        self.assertEqual(1.0, high_quality["cell_match_rate"])
        self.assertEqual(1.0, high_quality["source_linkage_rate"])
        self.assertEqual(2, high_quality["requires_review_count"])

    def test_p9_harness_runs_representative_manifest_entries_and_tracks_gates(
        self,
    ) -> None:
        report = evaluate_dataset.evaluate_p9_harness(POC_EVALUATION_MANIFEST_PATH)
        payload = report.as_dict()

        self.assertEqual(
            "veridoc-p9-poc-evaluation-harness/v0", payload["schema_version"]
        )
        self.assertEqual(str(POC_EVALUATION_MANIFEST_PATH), payload["dataset_manifest"])
        self.assertEqual(
            ["excel_to_word", "pdf_to_excel", "pdf_to_word", "word_to_excel"],
            payload["summary"]["conversion_modes"],
        )
        self.assertEqual(["no_llm", "llm_requested"], payload["summary"]["llm_scenarios"])
        self.assertEqual(16, payload["summary"]["case_count"])
        self.assertEqual(
            payload["summary"]["case_count"],
            payload["summary"]["completed_count"] + payload["summary"]["failure_count"],
        )
        self.assertEqual(0, payload["summary"]["external_ai_api_guard_violation_count"])
        self.assertIn("phase8_comparison", payload)

        results = payload["results"]
        unaudited_results = [
            result for result in results if result.get("audit_present") is not True
        ]
        self.assertTrue(unaudited_results)
        self.assertTrue(
            all(
                result["ok"] is False
                and "conversion audit missing" in str(result["failure_reason"])
                for result in unaudited_results
            )
        )
        self.assertTrue(
            any(
                result["sample_id"] == "p9-word-001"
                and result["conversion_mode"] == "word_to_excel"
                and result["llm_scenario"] == "no_llm"
                and result["ir_generated"]
                and result["artifact_generated"]
                and result["audit_present"]
                and result["artifact_expectations_met"]
                and result["warnings_count"] >= 0
                and result["review_items_count"] >= 0
                and result["failure_reason"] is None
                for result in results
            )
        )
        self.assertTrue(
            any(
                result["llm_scenario"] == "llm_requested"
                and result["llm_fallback_used"]
                for result in results
            )
        )
        self.assertTrue(
            any(
                result["sample_id"] == "p9-scanned-pdf-001"
                and result["representative_mode"] == "scanned_pdf_ocr"
                and not result["ok"]
                and result["fail_closed"]
                and not result["audit_present"]
                and "conversion audit missing" in str(result["failure_reason"])
                and result["mvp_before_gate_revision"]
                == "p9-mvp-before-representative-fixture-gate"
                for result in results
            )
        )

    def test_p9_harness_fails_pathless_real_representative_fixtures(
        self,
    ) -> None:
        fixture_specs = [
            (
                "pathless-word",
                "word",
                "json",
                "word_to_excel_representative",
                None,
            ),
            (
                "excel-fixture",
                "excel",
                "json",
                "excel_to_word_representative",
                "datasets/fixtures/excel.json",
            ),
            (
                "text-pdf-fixture",
                "text_pdf",
                "pdf",
                "pdf_to_excel_representative",
                "datasets/fixtures/text-pdf.json",
            ),
            (
                "record-pdf-fixture",
                "record_excerpt",
                "pdf",
                "record_pdf_representative",
                "datasets/fixtures/record-pdf.json",
            ),
            (
                "scanned-pdf-fixture",
                "scanned_pdf",
                "pdf",
                "scanned_pdf_ocr_representative",
                "datasets/fixtures/scanned-pdf.json",
            ),
        ]
        samples = [
            ("p9-word-pathless", "word", "pathless-word", "word_to_excel"),
            ("p9-excel", "excel", "excel-fixture", "excel_to_word"),
            ("p9-text-pdf", "text_pdf", "text-pdf-fixture", "pdf_to_excel"),
            ("p9-record-pdf", "record_pdf", "record-pdf-fixture", "pdf_to_word"),
            ("p9-scanned-pdf", "scanned_pdf", "scanned-pdf-fixture", "pdf_to_word"),
        ]

        for path_case in ("omitted", "null"):
            with self.subTest(path_case=path_case):
                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_root = Path(temp_dir)
                    fixture_dir = temp_root / "datasets" / "fixtures"
                    fixture_dir.mkdir(parents=True)
                    fixtures: list[dict[str, object]] = []
                    for (
                        fixture_id,
                        source_type,
                        fixture_format,
                        representative_flag,
                        fixture_relpath,
                    ) in fixture_specs:
                        fixture = {
                            "id": fixture_id,
                            "title": fixture_id,
                            "source_type": source_type,
                            "format": fixture_format,
                            "anonymization": "synthetic",
                            "confidentiality": "public",
                            "public_review_safe": True,
                            representative_flag: True,
                        }
                        if fixture_relpath is None:
                            if path_case == "null":
                                fixture["path"] = None
                        else:
                            fixture["path"] = fixture_relpath
                            fixture_path = temp_root / fixture_relpath
                            fixture_path.parent.mkdir(parents=True, exist_ok=True)
                            fixture_path.write_text("{}", encoding="utf-8")
                        fixtures.append(fixture)

                    (fixture_dir / "manifest.json").write_text(
                        json.dumps(
                            {
                                "schema_version": (
                                    evaluate_dataset.FIXTURE_MANIFEST_SCHEMA_VERSION
                                ),
                                "policy": {
                                    "allowed_fixture_root": "datasets/fixtures",
                                    "public_only": True,
                                    "confidential_source_documents_allowed": False,
                                },
                                "fixtures": fixtures,
                            }
                        ),
                        encoding="utf-8",
                    )
                    p9_manifest_path = temp_root / "datasets" / "p9.json"
                    p9_manifest_path.write_text(
                        json.dumps(
                            {
                                "schema_version": (
                                    evaluate_dataset.P9_EVALUATION_MANIFEST_SCHEMA_VERSION
                                ),
                                "fixture_manifest": "datasets/fixtures/manifest.json",
                                "required_categories": sorted(
                                    evaluate_dataset.P9_REQUIRED_SOURCE_CATEGORIES
                                ),
                                "samples": [
                                    {
                                        "id": sample_id,
                                        "category": category,
                                        "fixture_id": fixture_id,
                                        "dataset_status": "usable_fixture",
                                        "source_classification": "synthetic",
                                        "conversion_mode": conversion_mode,
                                    }
                                    for (
                                        sample_id,
                                        category,
                                        fixture_id,
                                        conversion_mode,
                                    ) in samples
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )

                    def conversion_success(
                        fixture: dict[str, object],
                        *,
                        fixture_path: Path,
                        mode: str,
                        llm_scenario: str,
                    ) -> dict[str, object]:
                        return {
                            "sample_id": fixture.get("sample_id"),
                            "fixture_id": fixture.get("id"),
                            "source_fixture_id": fixture.get("fixture_id"),
                            "conversion_mode": fixture.get("conversion_mode"),
                            "representative_mode": mode,
                            "llm_scenario": llm_scenario,
                            "ok": True,
                            "external_ai_api_guard_violation": False,
                        }

                    phase8_report = evaluate_dataset.LLMStabilityEvaluationReport(
                        llm_stability=evaluate_dataset.LLMStabilityMetrics(
                            input_id="pathless-fixture-test",
                            run_count=1,
                            plan_agreement_rate=1.0,
                            confirmed_value_agreement_rate=1.0,
                            schema_failure_rate=0.0,
                            repair_success_rate=1.0,
                            deterministic_fallback_rate=0.0,
                            external_ai_api_guard_violation_count=0,
                            distinct_plan_count=1,
                            distinct_confirmed_value_count=1,
                            unstable_example_count=0,
                            unstable_examples=(),
                        ),
                        poc_mode_comparison=evaluate_dataset.PoCComparisonMetrics(
                            mode_count=len(evaluate_dataset.REQUIRED_POC_MODES),
                            high_risk_false_auto_confirmed_count=0,
                            high_risk_false_auto_confirmed_target=0,
                            target_met=True,
                            manual_correction_time=(
                                evaluate_dataset.ManualCorrectionTimeMetrics(
                                    measurement_method="synthetic",
                                    baseline_minutes=10.0,
                                    assisted_minutes=4.0,
                                    reduction_minutes=6.0,
                                    reduction_rate=0.6,
                                    target_reduction_rate=0.5,
                                    target_met=True,
                                )
                            ),
                            modes=(),
                            mode_diffs=(),
                        ),
                        stability_source=LLM_STABILITY_RUNS_PATH,
                        poc_comparison_source=POC_COMPARISON_PATH,
                    )
                    with mock.patch.object(
                        evaluate_dataset,
                        "p9_conversion_result",
                        side_effect=conversion_success,
                    ), mock.patch.object(
                        evaluate_dataset,
                        "evaluate_llm_stability_report",
                        return_value=phase8_report,
                    ):
                        report = evaluate_dataset.evaluate_p9_harness(p9_manifest_path)

                failed_rows = [
                    result
                    for result in report.results
                    if result["sample_id"] == "p9-word-pathless"
                ]
                self.assertEqual(2, len(failed_rows))
                self.assertEqual(2, report.failure_count)
                for row in failed_rows:
                    self.assertFalse(row["ok"])
                    self.assertFalse(row["fail_closed"])
                    self.assertIsNone(row["mvp_before_gate_revision"])
                    self.assertIn("path is missing or null", row["failure_reason"])

    def test_p9_harness_cli_emits_machine_readable_report(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--p9-harness"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        payload = json.loads(completed.stdout)

        self.assertEqual(
            "veridoc-p9-poc-evaluation-harness/v0", payload["schema_version"]
        )
        self.assertGreater(payload["summary"]["case_count"], 0)
        self.assertIn("failure_reason", payload["results"][0])
        self.assertEqual(
            str(evaluate_dataset.DEFAULT_P9_HARNESS_MANIFEST),
            payload["dataset_manifest"],
        )

    def test_mvp_harness_cli_emits_acceptance_ready_evidence(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--mvp-harness"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        payload = json.loads(completed.stdout)

        self.assertEqual(
            "veridoc-mvp-evaluation-harness/v1",
            payload["schema_version"],
        )
        self.assertEqual(
            str(MVP_EVALUATION_MANIFEST_PATH.relative_to(REPO_ROOT)),
            payload["dataset_manifest"],
        )
        self.assertEqual(5, payload["summary"]["case_count"])
        self.assertEqual(
            payload["summary"]["case_count"],
            sum(payload["summary"]["acceptance_status_counts"].values()),
        )
        limits = self.valid_mvp_manifest_data()["acceptance_limits"]
        self.assertTrue(payload["results"])
        for result in payload["results"]:
            with self.subTest(case=result["case_id"]):
                self.assertIn(result["acceptance_status"], {"pass", "fail", "unknown"})
                self.assertIsInstance(result["processing_time_ms"], float)
                self.assertGreaterEqual(result["processing_time_ms"], 0.0)
                self.assertEqual(
                    {
                        "artifact",
                        "review",
                        "audit",
                        "input_size",
                        "processing_time",
                        "timeout",
                    },
                    set(result["evaluations"]),
                )
                self.assertTrue(
                    all(
                        evaluation["status"] in {"pass", "fail", "unknown"}
                        for evaluation in result["evaluations"].values()
                    )
                )
                self.assertEqual(
                    limits["max_upload_bytes"],
                    result["evaluations"]["input_size"]["limit_bytes"],
                )
                self.assertEqual(
                    limits["representative_processing_time_ms"],
                    result["evaluations"]["processing_time"]["threshold_ms"],
                )
                self.assertEqual(
                    limits["timeout_ms"],
                    result["evaluations"]["timeout"]["timeout_ms"],
                )

    def test_mvp_acceptance_report_cli_maps_15_3_items_and_carryovers(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--mvp-acceptance-report"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        payload = json.loads(completed.stdout)

        self.assertEqual(
            "veridoc-mvp-acceptance-report/v1",
            payload["schema_version"],
        )
        self.assertEqual(
            "docs/mvp-acceptance-traceability.md",
            payload["criteria_source"],
        )
        self.assertEqual(
            "veridoc-mvp-evaluation-harness/v1",
            payload["evidence_snapshot"]["harness_schema_version"],
        )
        self.assertRegex(payload["evidence_snapshot"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            "docs/mvp-scope-decisions.md",
            payload["evidence_snapshot"]["scope_decision_source"],
        )
        self.assertRegex(
            payload["evidence_snapshot"]["scope_decision_sha256"],
            r"^[0-9a-f]{64}$",
        )
        expected_ids = {
            "AC-UI",
            "AC-TEMPLATE",
            "AC-QUALITY",
            "AC-PROVENANCE",
            "AC-REVIEW",
            "AC-EFFICIENCY",
            "AC-PERFORMANCE",
            "AC-AUDIT",
            "AC-AUTH",
            "AC-SECURITY",
            "FC-HIGH-RISK",
            "FC-EVIDENCE",
            "FC-EXTERNAL-SEND",
            "FC-REVIEW-UI",
            "FC-REPRODUCIBILITY",
            "EM-USER-REVIEW",
            "EM-E2E",
            "OD-TEMPLATES",
            "OD-EFFICIENCY-SCOPE",
            "OD-SEGREGATION",
        }
        self.assertEqual(expected_ids, {item["item_id"] for item in payload["items"]})
        for item in payload["items"]:
            with self.subTest(item=item["item_id"]):
                self.assertIn(item["decision"], {"pass", "fail"})
                self.assertTrue(item["evidence"])
                if item["decision"] == "fail":
                    self.assertTrue(item["unmet"])

        self.assertIn("OD-SEGREGATION", payload["carryovers"]["phase13"])
        self.assertEqual([], payload["carryovers"]["phase14"])
        self.assertEqual("fail", payload["summary"]["overall_decision"])
        self.assertEqual(
            {"pass": 6, "fail": 14},
            payload["summary"]["decision_counts"],
        )
        self.assertEqual(
            "pass",
            next(
                item
                for item in payload["items"]
                if item["item_id"] == "AC-AUTH"
            )["decision"],
        )
        open_decisions = {
            item["item_id"]: item
            for item in payload["items"]
            if item["item_id"].startswith("OD-")
        }
        self.assertEqual(
            {
                "OD-TEMPLATES",
                "OD-EFFICIENCY-SCOPE",
                "OD-SEGREGATION",
            },
            set(open_decisions),
        )
        for item in open_decisions.values():
            self.assertEqual(
                {"status": "pass", "failures": []},
                item["evidence"]["decision_input_validation"],
            )

    def test_mvp_scope_decision_input_validation_fails_closed_on_drift(self) -> None:
        decision_record = MVP_SCOPE_DECISIONS_PATH.read_text(encoding="utf-8")
        manifest = json.loads(MVP_EVALUATION_MANIFEST_PATH.read_text(encoding="utf-8"))
        from services.api.poc_web import ROLE_PERMISSIONS

        parsed_role_permissions = evaluate_dataset.mvp_role_permissions_from_source(
            REPO_ROOT / "services" / "api" / "poc_web.py"
        )
        self.assertEqual(
            {
                role: frozenset(permissions)
                for role, permissions in ROLE_PERMISSIONS.items()
            },
            parsed_role_permissions,
        )

        current = evaluate_dataset.mvp_scope_decision_input_failures(
            decision_record=decision_record,
            manifest=manifest,
            manifest_source="datasets/mvp_evaluation_manifest_v1.json",
            role_permissions=ROLE_PERMISSIONS,
        )
        self.assertEqual(
            {
                "OD-TEMPLATES": (),
                "OD-EFFICIENCY-SCOPE": (),
                "OD-SEGREGATION": (),
            },
            current,
        )

        for label, original, replacement in (
            (
                "cohort",
                "at least three designated document reviewers",
                "at least two designated document reviewers",
            ),
            (
                "training",
                "one fixed, unscored practice task per arm",
                "two fixed, unscored practice tasks per arm",
            ),
            (
                "timing",
                "start when the participant receives the source",
                "start after the participant reviews the source",
            ),
            (
                "comparison",
                "paired cohort median",
                "unpaired cohort median",
            ),
            (
                "rejection",
                "unbalanced ordering",
                "unrecorded ordering",
            ),
        ):
            with self.subTest(efficiency_scope_drift=label):
                drifted_efficiency_record = decision_record.replace(
                    original,
                    replacement,
                )
                self.assertNotEqual(decision_record, drifted_efficiency_record)
                efficiency_failures = (
                    evaluate_dataset.mvp_scope_decision_input_failures(
                        decision_record=drifted_efficiency_record,
                        manifest=manifest,
                        manifest_source="datasets/mvp_evaluation_manifest_v1.json",
                        role_permissions=ROLE_PERMISSIONS,
                    )
                )
                self.assertEqual((), efficiency_failures["OD-TEMPLATES"])
                self.assertTrue(efficiency_failures["OD-EFFICIENCY-SCOPE"])
                self.assertEqual((), efficiency_failures["OD-SEGREGATION"])

        tampered_efficiency_pin = decision_record.replace(
            "3d9d05671895ec8d6e8b14f44b6a8dd7f99aa17b7b65871b78fb56a49966b6fb",
            "0" * 64,
        )
        pin_failures = evaluate_dataset.mvp_scope_decision_input_failures(
            decision_record=tampered_efficiency_pin,
            manifest=manifest,
            manifest_source="datasets/mvp_evaluation_manifest_v1.json",
            role_permissions=ROLE_PERMISSIONS,
        )
        self.assertEqual((), pin_failures["OD-TEMPLATES"])
        self.assertTrue(pin_failures["OD-EFFICIENCY-SCOPE"])
        self.assertEqual((), pin_failures["OD-SEGREGATION"])

        unapproved_revision = decision_record.replace(
            "Decision revision: `p12g-02-v1`",
            "Decision revision: `p12g-02-v2`",
        )
        self.assertNotEqual(decision_record, unapproved_revision)
        revision_failures = evaluate_dataset.mvp_scope_decision_input_failures(
            decision_record=unapproved_revision,
            manifest=manifest,
            manifest_source="datasets/mvp_evaluation_manifest_v1.json",
            role_permissions=ROLE_PERMISSIONS,
        )
        self.assertTrue(revision_failures["OD-TEMPLATES"])
        self.assertTrue(revision_failures["OD-EFFICIENCY-SCOPE"])
        self.assertTrue(revision_failures["OD-SEGREGATION"])

        manifest_failures = None
        for label, mutate in (
            (
                "source policy",
                lambda changed: changed.__setitem__(
                    "source_policy",
                    "different-source-policy",
                ),
            ),
            (
                "case id",
                lambda changed: changed["cases"][0].__setitem__(
                    "id",
                    "changed-case-id",
                ),
            ),
            (
                "fixture binding",
                lambda changed: changed["cases"][0].__setitem__(
                    "fixture_path",
                    "datasets/fixtures/word/changed.docx",
                ),
            ),
        ):
            with self.subTest(drift=label):
                drifted_manifest = copy.deepcopy(manifest)
                mutate(drifted_manifest)
                manifest_failures = (
                    evaluate_dataset.mvp_scope_decision_input_failures(
                        decision_record=decision_record,
                        manifest=drifted_manifest,
                        manifest_source="datasets/mvp_evaluation_manifest_v1.json",
                        role_permissions=ROLE_PERMISSIONS,
                    )
                )
                self.assertTrue(manifest_failures["OD-TEMPLATES"])
                self.assertEqual((), manifest_failures["OD-EFFICIENCY-SCOPE"])
                self.assertEqual((), manifest_failures["OD-SEGREGATION"])
        self.assertIsNotNone(manifest_failures)

        for label, original, replacement in (
            (
                "unauthenticated deny path",
                "unauthenticated requests cannot use protected API operations",
                "unauthenticated requests may use protected API operations",
            ),
            (
                "distinct preceding actor",
                "target from a distinct authenticated actor",
                "target from the same authenticated actor",
            ),
            (
                "Phase 13 deferral",
                "deferral that weakens an MVP deny path",
                "deferral that removes an MVP deny path",
            ),
        ):
            with self.subTest(segregation_scope_drift=label):
                drifted_segregation_record = decision_record.replace(
                    original,
                    replacement,
                )
                self.assertNotEqual(decision_record, drifted_segregation_record)
                segregation_failures = (
                    evaluate_dataset.mvp_scope_decision_input_failures(
                        decision_record=drifted_segregation_record,
                        manifest=manifest,
                        manifest_source="datasets/mvp_evaluation_manifest_v1.json",
                        role_permissions=ROLE_PERMISSIONS,
                    )
                )
                self.assertEqual((), segregation_failures["OD-TEMPLATES"])
                self.assertEqual(
                    (),
                    segregation_failures["OD-EFFICIENCY-SCOPE"],
                )
                self.assertTrue(segregation_failures["OD-SEGREGATION"])

        tampered_segregation_pin = decision_record.replace(
            "74d83d2d028c1ba79fffd6742ab9f13f7e345932248e36b86289e16c93a01476",
            "0" * 64,
        )
        segregation_pin_failures = (
            evaluate_dataset.mvp_scope_decision_input_failures(
                decision_record=tampered_segregation_pin,
                manifest=manifest,
                manifest_source="datasets/mvp_evaluation_manifest_v1.json",
                role_permissions=ROLE_PERMISSIONS,
            )
        )
        self.assertEqual((), segregation_pin_failures["OD-TEMPLATES"])
        self.assertEqual(
            (),
            segregation_pin_failures["OD-EFFICIENCY-SCOPE"],
        )
        self.assertTrue(segregation_pin_failures["OD-SEGREGATION"])

        drifted_roles = {
            role: set(permissions)
            for role, permissions in ROLE_PERMISSIONS.items()
        }
        drifted_roles["viewer"].add("templates:manage")
        role_failures = evaluate_dataset.mvp_scope_decision_input_failures(
            decision_record=decision_record,
            manifest=manifest,
            manifest_source="datasets/mvp_evaluation_manifest_v1.json",
            role_permissions=drifted_roles,
        )
        self.assertEqual((), role_failures["OD-TEMPLATES"])
        self.assertEqual((), role_failures["OD-EFFICIENCY-SCOPE"])
        self.assertTrue(role_failures["OD-SEGREGATION"])

        unapproved = decision_record.replace(
            "- Approval status: `approved`",
            "- Approval status: `pending`",
        )
        unapproved_failures = evaluate_dataset.mvp_scope_decision_input_failures(
            decision_record=unapproved,
            manifest=manifest,
            manifest_source="datasets/mvp_evaluation_manifest_v1.json",
            role_permissions=ROLE_PERMISSIONS,
        )
        self.assertTrue(all(unapproved_failures.values()))

        drift_items = evaluate_dataset.mvp_acceptance_traceability_items(
            MVP_ACCEPTANCE_TRACEABILITY_PATH.read_text(encoding="utf-8"),
            decision_input_failures=manifest_failures,
        )
        template_item = next(
            item for item in drift_items if item["item_id"] == "OD-TEMPLATES"
        )
        self.assertEqual("fail", template_item["decision"])
        self.assertIn("approved decision input drift", template_item["unmet"])
        self.assertEqual(
            "fail",
            template_item["evidence"]["decision_input_validation"]["status"],
        )

    def test_mvp_acceptance_report_rejects_missing_traceability_row(self) -> None:
        traceability = MVP_ACCEPTANCE_TRACEABILITY_PATH.read_text(encoding="utf-8")
        incomplete = "\n".join(
            line
            for line in traceability.splitlines()
            if not line.startswith("| AC-QUALITY |")
        )

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "missing required rows: AC-QUALITY",
        ):
            evaluate_dataset.mvp_acceptance_traceability_items(incomplete)

    def test_mvp_acceptance_report_fails_when_live_harness_fails(self) -> None:
        traceability = MVP_ACCEPTANCE_TRACEABILITY_PATH.read_text(encoding="utf-8")
        items = tuple(
            {**item, "decision": "pass"}
            for item in evaluate_dataset.mvp_acceptance_traceability_items(traceability)
        )
        harness = evaluate_dataset.MVPHarnessReport(
            manifest=evaluate_dataset.DEFAULT_MVP_HARNESS_MANIFEST,
            results=(
                {
                    "case_id": "failed-case",
                    "acceptance_status": "fail",
                    "conversion_mode": "deterministic",
                },
            ),
        )

        payload = evaluate_dataset.MVPAcceptanceReport(
            harness=harness,
            traceability_source=evaluate_dataset.DEFAULT_MVP_ACCEPTANCE_TRACEABILITY,
            traceability_text=traceability,
            items=items,
        ).as_dict()

        self.assertEqual("fail", payload["summary"]["overall_decision"])

    def test_mvp_acceptance_report_rejects_unknown_traceability_row(self) -> None:
        traceability = MVP_ACCEPTANCE_TRACEABILITY_PATH.read_text(encoding="utf-8")
        unknown_row = (
            "| AC-UNKNOWN | Unknown criterion | [#289](issue) | evidence | **達成** |"
        )
        malformed = traceability.replace(
            "| AC-UI |",
            f"{unknown_row}\n| AC-UI |",
            1,
        )

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "unrecognized MVP traceability row 'AC-UNKNOWN'",
        ):
            evaluate_dataset.mvp_acceptance_traceability_items(malformed)

    def test_mvp_acceptance_report_rejects_status_prefix_without_separator(
        self,
    ) -> None:
        traceability = MVP_ACCEPTANCE_TRACEABILITY_PATH.read_text(encoding="utf-8")
        for status in ("**達成予定** — pending", "Phase13以降ではない"):
            with self.subTest(status=status):
                malformed = self.traceability_with_cell(
                    traceability,
                    "AC-UI",
                    4,
                    status,
                )
                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError,
                    "row 'AC-UI' has no recognized status",
                ):
                    evaluate_dataset.mvp_acceptance_traceability_items(malformed)

    def test_mvp_acceptance_report_rejects_empty_scope_or_evidence(self) -> None:
        traceability = MVP_ACCEPTANCE_TRACEABILITY_PATH.read_text(encoding="utf-8")
        for cell_index, field_name in ((2, "linked issue/scope"), (3, "evidence")):
            with self.subTest(field_name=field_name):
                malformed = self.traceability_with_cell(
                    traceability,
                    "AC-UI",
                    cell_index,
                    "",
                )
                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError,
                    f"empty required cells: {field_name}",
                ):
                    evaluate_dataset.mvp_acceptance_traceability_items(malformed)

    def test_mvp_acceptance_report_rejects_failed_row_without_unmet_boundary(
        self,
    ) -> None:
        traceability = MVP_ACCEPTANCE_TRACEABILITY_PATH.read_text(encoding="utf-8")
        malformed = self.traceability_with_cell(
            traceability,
            "OD-TEMPLATES",
            4,
            "**未達**.",
        )

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "row 'OD-TEMPLATES' has no explicit unmet boundary",
        ):
            evaluate_dataset.mvp_acceptance_traceability_items(malformed)

    def test_mvp_acceptance_report_reads_traceability_from_manifest_checkout(
        self,
    ) -> None:
        traceability = MVP_ACCEPTANCE_TRACEABILITY_PATH.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = Path(temp_dir)
            manifest_path = checkout / evaluate_dataset.DEFAULT_MVP_HARNESS_MANIFEST
            fixture_manifest_path = checkout / evaluate_dataset.EXPECTED_DATASET_MANIFEST
            traceability_path = (
                checkout / evaluate_dataset.DEFAULT_MVP_ACCEPTANCE_TRACEABILITY
            )
            scope_decision_path = (
                checkout / evaluate_dataset.DEFAULT_MVP_SCOPE_DECISIONS
            )
            role_source_path = checkout / "services" / "api" / "poc_web.py"
            manifest_path.parent.mkdir(parents=True)
            fixture_manifest_path.parent.mkdir(parents=True)
            traceability_path.parent.mkdir(parents=True)
            role_source_path.parent.mkdir(parents=True)
            manifest_path.write_text("{}\n", encoding="utf-8")
            fixture_manifest_path.write_text("{}\n", encoding="utf-8")
            traceability_path.write_text(
                "<!-- alternate checkout -->\n" + traceability,
                encoding="utf-8",
            )
            scope_decision_path.write_text(
                MVP_SCOPE_DECISIONS_PATH.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            role_source_path.write_text(
                (REPO_ROOT / "services" / "api" / "poc_web.py").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            harness = evaluate_dataset.MVPHarnessReport(
                manifest=manifest_path,
                results=(),
            )

            with mock.patch.object(
                evaluate_dataset,
                "evaluate_mvp_harness",
                return_value=harness,
            ):
                report = evaluate_dataset.build_mvp_acceptance_report(manifest_path)

        self.assertTrue(report.traceability_text.startswith("<!-- alternate checkout -->"))
        self.assertEqual(
            evaluate_dataset.DEFAULT_MVP_ACCEPTANCE_TRACEABILITY,
            report.traceability_source,
        )
        template_item = next(
            item for item in report.items if item["item_id"] == "OD-TEMPLATES"
        )
        self.assertEqual("fail", template_item["decision"])
        self.assertEqual(
            "fail",
            template_item["evidence"]["decision_input_validation"]["status"],
        )

    def test_mvp_processing_time_evaluation_enforces_manifest_threshold(self) -> None:
        self.assertEqual(
            {
                "status": "pass",
                "processing_time_ms": 30_000.0,
                "threshold_ms": 30_000,
                "reason": None,
            },
            evaluate_dataset.mvp_processing_time_evaluation(
                30_000.0,
                threshold_ms=30_000,
            ),
        )
        self.assertEqual(
            {
                "status": "fail",
                "processing_time_ms": 30_000.001,
                "threshold_ms": 30_000,
                "reason": "processing time exceeded the 30000 ms MVP limit",
            },
            evaluate_dataset.mvp_processing_time_evaluation(
                30_000.001,
                threshold_ms=30_000,
            ),
        )

    def test_mvp_size_and_timeout_evaluations_fail_above_limits(self) -> None:
        self.assertEqual(
            "pass",
            evaluate_dataset.mvp_input_size_evaluation(
                2_097_152,
                limit_bytes=2_097_152,
            )["status"],
        )
        oversized = evaluate_dataset.mvp_input_size_evaluation(
            2_097_153,
            limit_bytes=2_097_152,
        )
        self.assertEqual("fail", oversized["status"])
        self.assertEqual(2_097_152, oversized["limit_bytes"])

        timed_out = evaluate_dataset.mvp_timeout_evaluation(
            30_000.001,
            timeout_ms=30_000,
        )
        self.assertEqual("fail", timed_out["status"])
        self.assertEqual("processing_timeout", timed_out["error"])

    def test_mvp_harness_stops_waiting_at_manifest_timeout(self) -> None:
        case = self.valid_mvp_case()
        acceptance_limits = self.valid_mvp_acceptance_limits()
        acceptance_limits["timeout_ms"] = 10

        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"mvp timeout fixture")
            fixture_file.flush()
            fixture_path = Path(fixture_file.name)

            def stalled_parser(_path: Path) -> object:
                time.sleep(1)
                raise AssertionError("parser should not complete after the deadline")

            started_at = time.perf_counter()
            with mock.patch(
                "services.api.poc_web.extract_docx_structure",
                side_effect=stalled_parser,
            ):
                result = evaluate_dataset.mvp_conversion_result(
                    case,
                    fixture_path=fixture_path,
                    acceptance_limits=acceptance_limits,
                )
            elapsed_seconds = time.perf_counter() - started_at

        self.assertLess(elapsed_seconds, 0.5)
        self.assertEqual("fail", result["acceptance_status"])
        self.assertEqual("fail", result["evaluations"]["timeout"]["status"])
        self.assertEqual(
            "processing_timeout",
            result["evaluations"]["timeout"]["error"],
        )
        self.assertIn("processing_timeout", result["failure_reason"])
        self.assertNotIn("DOCX parser failed", result["failure_reason"])

    def test_mvp_timeout_bypasses_broad_conversion_exception_handler(self) -> None:
        def conversion_with_fallback(**_: object) -> dict[str, object]:
            try:
                time.sleep(1)
            except Exception:
                return {"status": "fallback"}
            raise AssertionError("conversion should not complete after the deadline")

        with mock.patch(
            "services.api.poc_web.convert_uploaded_document",
            side_effect=conversion_with_fallback,
        ):
            with self.assertRaises(evaluate_dataset.MVPConversionTimeoutError):
                evaluate_dataset.mvp_convert_uploaded_document(
                    filename="representative.pdf",
                    content=b"representative fixture",
                    conversion_mode="auto",
                    timeout_ms=10,
                )

    def test_mvp_manifest_rejects_missing_acceptance_limits(self) -> None:
        manifest = self.valid_mvp_manifest_data()
        del manifest["acceptance_limits"]

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "must define acceptance_limits",
        ):
            evaluate_dataset.mvp_fixture_manifest_path(manifest, REPO_ROOT)

    def test_mvp_harness_rejects_case_fixture_path_drift(self) -> None:
        manifest = self.valid_mvp_manifest_data()
        fixture_manifest = evaluate_dataset.load_json(FIXTURE_MANIFEST_PATH)
        manifest["cases"][0]["fixture_path"] = (
            "datasets/fixtures/word/another-public-fixture.docx"
        )

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "fixture_path must match the authoritative fixture manifest path",
        ):
            evaluate_dataset.mvp_evaluation_cases(manifest, fixture_manifest)

    def test_mvp_harness_rejects_case_category_drift(self) -> None:
        manifest = self.valid_mvp_manifest_data()
        fixture_manifest = evaluate_dataset.load_json(FIXTURE_MANIFEST_PATH)
        manifest["cases"][0]["category"] = "excel"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "category must match the authoritative fixture source_type",
        ):
            evaluate_dataset.mvp_evaluation_cases(manifest, fixture_manifest)

    def test_mvp_harness_rejects_reduced_fixed_category_set(self) -> None:
        manifest = self.valid_mvp_manifest_data()
        fixture_manifest = evaluate_dataset.load_json(FIXTURE_MANIFEST_PATH)
        manifest["cases"] = [
            case for case in manifest["cases"] if case["category"] != "scanned_pdf"
        ]
        manifest["required_categories"].remove("scanned_pdf")

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "fixed MVP category set",
        ):
            evaluate_dataset.mvp_evaluation_cases(manifest, fixture_manifest)

    def test_mvp_harness_rejects_duplicate_case_category(self) -> None:
        manifest = self.valid_mvp_manifest_data()
        fixture_manifest = evaluate_dataset.load_json(FIXTURE_MANIFEST_PATH)
        duplicate_case = copy.deepcopy(manifest["cases"][0])
        duplicate_case["id"] = f"{duplicate_case['id']}-duplicate"
        manifest["cases"].append(duplicate_case)

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "duplicate MVP case category",
        ):
            evaluate_dataset.mvp_evaluation_cases(manifest, fixture_manifest)

    def test_mvp_harness_preserves_authoritative_fixture_expectations(self) -> None:
        manifest = self.valid_mvp_manifest_data()
        fixture_manifest = evaluate_dataset.load_json(FIXTURE_MANIFEST_PATH)
        manifest["cases"][0]["word_to_excel_expectations"] = {"sheets": []}

        case = evaluate_dataset.mvp_evaluation_cases(
            manifest,
            fixture_manifest,
        )[0]
        fixture = next(
            item
            for item in fixture_manifest["fixtures"]
            if item["id"] == manifest["cases"][0]["fixture_id"]
        )

        self.assertEqual(
            fixture["word_to_excel_expectations"],
            case["word_to_excel_expectations"],
        )

    def test_mvp_harness_fails_requires_review_without_review_items(self) -> None:
        case = self.valid_mvp_case()
        fixture_content = b"mvp review handoff fixture"
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(fixture_content)
            fixture_file.flush()
            fixture_path = Path(fixture_file.name)
            converted = self.valid_mvp_conversion_payload(
                case,
                fixture_path=fixture_path,
                fixture_content=fixture_content,
            )
            converted["review_items"] = []
            converted["audit"]["review_items"]["count"] = 0

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value=converted,
            ), mock.patch.object(
                evaluate_dataset,
                "p9_validate_artifact_expectations",
                return_value=[],
            ):
                result = evaluate_dataset.mvp_conversion_result(
                    case,
                    fixture_path=fixture_path,
                    acceptance_limits=self.valid_mvp_acceptance_limits(),
                )

        self.assertEqual(0, result["review_items_count"])
        self.assertEqual("fail", result["evaluations"]["review"]["status"])
        self.assertIn(
            "requires_review conversion did not emit review items",
            result["evaluations"]["review"]["reason"],
        )
        self.assertEqual("fail", result["acceptance_status"])

    def test_mvp_harness_records_authoritative_approver_decision(self) -> None:
        case = self.valid_mvp_case()
        case["review_decision"] = {
            "decision": "approved",
            "reason": "representative fixture review completed",
            "actor": "mvp-fixture-approver",
            "role": "approver",
        }
        fixture_content = b"mvp authoritative review fixture"
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(fixture_content)
            fixture_file.flush()
            fixture_path = Path(fixture_file.name)
            converted = self.valid_mvp_conversion_payload(
                case,
                fixture_path=fixture_path,
                fixture_content=fixture_content,
            )
            converted["review_items"] = [
                {
                    "document_id": "document-mvp-review-test",
                    "block_id": "review-block-mvp-test",
                    "source_id": "document-mvp-review-test:review-block-mvp-test",
                    "source_page": 1,
                    "text": "Review this representative value",
                    "warnings": ["representative value requires review"],
                }
            ]

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value=converted,
            ), mock.patch.object(
                evaluate_dataset,
                "p9_validate_artifact_expectations",
                return_value=[],
            ):
                result = evaluate_dataset.mvp_conversion_result(
                    case,
                    fixture_path=fixture_path,
                    acceptance_limits=self.valid_mvp_acceptance_limits(),
                )

        decision = result["review_decision"]
        expected_identity = hashlib.sha256(
            (
                f"{case['case_id']}:"
                f"{hashlib.sha256(fixture_content).hexdigest()}"
            ).encode("utf-8")
        ).hexdigest()[:20]
        self.assertEqual("pass", result["evaluations"]["review"]["status"])
        self.assertEqual("approved", decision["decision"])
        self.assertEqual("approver", decision["actor"]["role"])
        self.assertEqual(
            f"mvp-decision-{expected_identity}-1",
            decision["decision_id"],
        )
        self.assertEqual(
            evaluate_dataset.mvp_review_item_version(converted["review_items"][0]),
            decision["item_version"],
        )
        self.assertEqual(
            result["review_decisions"],
            result["evaluations"]["artifact"]["review_decisions"],
        )
        self.assertEqual(
            result["review_decisions"],
            result["evaluations"]["audit"]["review_decisions"],
        )
        self.assertEqual(
            decision["decision_id"],
            result["evaluations"]["audit"]["review_decision"]["decision_id"],
        )
        self.assertEqual(
            decision["version"],
            result["evaluations"]["artifact"]["review_decision"]["version"],
        )

    def test_mvp_harness_fails_closed_without_authoritative_decision(self) -> None:
        case = self.valid_mvp_case()
        del case["review_decision"]
        fixture_content = b"mvp missing review decision fixture"
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(fixture_content)
            fixture_file.flush()
            fixture_path = Path(fixture_file.name)
            converted = self.valid_mvp_conversion_payload(
                case,
                fixture_path=fixture_path,
                fixture_content=fixture_content,
            )

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value=converted,
            ), mock.patch.object(
                evaluate_dataset,
                "p9_validate_artifact_expectations",
                return_value=[],
            ):
                result = evaluate_dataset.mvp_conversion_result(
                    case,
                    fixture_path=fixture_path,
                    acceptance_limits=self.valid_mvp_acceptance_limits(),
                )

        self.assertEqual("fail", result["evaluations"]["review"]["status"])
        self.assertIn(
            "authoritative review decision is required",
            result["evaluations"]["review"]["reason"],
        )
        self.assertIsNone(result["review_decision"])

    def test_mvp_harness_rejects_forbidden_or_unresolved_high_risk_decision(
        self,
    ) -> None:
        scenarios = (
            (
                {"role": "reviewer", "decision": "approved"},
                False,
                "cannot record decision",
            ),
            (
                {"role": "reviewer", "decision": "edited"},
                True,
                "high-risk review item requires approver approval",
            ),
            (
                {"role": "reviewer", "decision": "edited"},
                False,
                "authoritative review decision did not approve the review item",
            ),
            (
                {"role": "approver", "decision": "rejected"},
                False,
                "authoritative review decision did not approve the review item",
            ),
        )
        for decision_overrides, high_risk, expected_error in scenarios:
            with self.subTest(
                role=decision_overrides["role"],
                decision=decision_overrides["decision"],
                high_risk=high_risk,
            ):
                case = self.valid_mvp_case()
                case["review_decision"].update(decision_overrides)
                fixture_content = b"mvp denied review decision fixture"
                with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
                    fixture_file.write(fixture_content)
                    fixture_file.flush()
                    fixture_path = Path(fixture_file.name)
                    converted = self.valid_mvp_conversion_payload(
                        case,
                        fixture_path=fixture_path,
                        fixture_content=fixture_content,
                    )
                    converted["review_items"][0].update(
                        {
                            "block_id": "review-block-mvp-test",
                            "document_id": "document-mvp-review-test",
                            "high_risk": high_risk,
                            "source_id": (
                                "document-mvp-review-test:review-block-mvp-test"
                            ),
                        }
                    )

                    with mock.patch(
                        "services.api.poc_web.convert_uploaded_document",
                        return_value=converted,
                    ), mock.patch.object(
                        evaluate_dataset,
                        "p9_validate_artifact_expectations",
                        return_value=[],
                    ):
                        result = evaluate_dataset.mvp_conversion_result(
                            case,
                            fixture_path=fixture_path,
                            acceptance_limits=self.valid_mvp_acceptance_limits(),
                        )

                self.assertEqual("fail", result["evaluations"]["review"]["status"])
                self.assertIn(
                    expected_error,
                    result["evaluations"]["review"]["reason"],
                )
                self.assertIsNone(result["review_decision"])

    def test_mvp_harness_fails_converted_with_review_items(self) -> None:
        case = self.valid_mvp_case(2)
        fixture_content = b"mvp converted review fixture"
        with tempfile.NamedTemporaryFile(suffix=".pdf") as fixture_file:
            fixture_file.write(fixture_content)
            fixture_file.flush()
            fixture_path = Path(fixture_file.name)
            converted = self.valid_mvp_conversion_payload(
                case,
                fixture_path=fixture_path,
                fixture_content=fixture_content,
            )
            converted["review_items"] = [{"id": "unexpected-review-item"}]
            converted["audit"]["review_items"]["count"] = 1

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value=converted,
            ), mock.patch.object(
                evaluate_dataset,
                "p9_validate_artifact_expectations",
                return_value=[],
            ):
                result = evaluate_dataset.mvp_conversion_result(
                    case,
                    fixture_path=fixture_path,
                    acceptance_limits=self.valid_mvp_acceptance_limits(),
                )

        self.assertEqual(1, result["review_items_count"])
        self.assertEqual("fail", result["evaluations"]["review"]["status"])
        self.assertIn(
            "converted conversion unexpectedly emitted review items",
            result["evaluations"]["review"]["reason"],
        )
        self.assertEqual("fail", result["acceptance_status"])

    def test_mvp_harness_fails_artifact_content_validation_mismatch(self) -> None:
        case = self.valid_mvp_case()
        fixture_content = b"mvp artifact review fixture"
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(fixture_content)
            fixture_file.flush()
            fixture_path = Path(fixture_file.name)
            converted = self.valid_mvp_conversion_payload(
                case,
                fixture_path=fixture_path,
                fixture_content=fixture_content,
            )

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value=converted,
            ), mock.patch.object(
                evaluate_dataset,
                "p9_validate_artifact_expectations",
                return_value=["xlsx cell values did not match fixture expectations"],
            ):
                result = evaluate_dataset.mvp_conversion_result(
                    case,
                    fixture_path=fixture_path,
                    acceptance_limits=self.valid_mvp_acceptance_limits(),
                )

        self.assertEqual(
            result["evaluations"]["artifact"]["expected_formats"],
            result["evaluations"]["artifact"]["actual_formats"],
        )
        self.assertEqual("fail", result["evaluations"]["artifact"]["status"])
        self.assertIn(
            "xlsx cell values did not match fixture expectations",
            result["evaluations"]["artifact"]["reason"],
        )
        self.assertEqual("fail", result["acceptance_status"])

    @unittest.skipUnless(PYMUPDF_AVAILABLE, "PyMuPDF eval dependency is not installed")
    def test_mvp_harness_validates_record_pdf_docx_content(self) -> None:
        from services.api.poc_web import CONVERSION_AUDIT_SCHEMA_VERSION

        case = self.valid_mvp_case(4)
        fixture_path = REPO_ROOT / str(case["fixture_path"])

        result = evaluate_dataset.mvp_conversion_result(
            case,
            fixture_path=fixture_path,
            acceptance_limits=self.valid_mvp_acceptance_limits(),
        )

        artifact_evaluation = result["evaluations"]["artifact"]
        self.assertEqual("pass", artifact_evaluation["status"])
        self.assertNotIn(
            "artifact content validation is unavailable",
            artifact_evaluation["reason"] or "",
        )
        content_validation = artifact_evaluation["content_validation"]
        self.assertEqual(
            "record_pdf_docx_content_v1",
            content_validation["validator"],
        )
        self.assertEqual("pass", content_validation["status"])
        self.assertEqual(
            {
                "section_order": "pass",
                "body_completeness": "pass",
                "expected_content": "pass",
                "source_linkage": "pass",
            },
            {
                check["id"]: check["status"]
                for check in content_validation["checks"]
            },
        )
        self.assertEqual(
            {
                "section_order_match": 1.0,
                "body_block_match_rate": 1.0,
                "expected_phrase_match_rate": 1.0,
                "source_linkage_rate": 1.0,
            },
            content_validation["metrics"],
        )
        self.assertEqual([1], content_validation["evidence"]["source_pages"])
        self.assertEqual(
            [
                "block-0001",
                "block-0002",
                "block-0003",
                "block-0004",
                "block-0005",
            ],
            content_validation["evidence"]["linked_block_ids"],
        )
        self.assertTrue(content_validation["evidence"]["artifact_id"])
        self.assertTrue(content_validation["evidence"]["conversion_id"])
        self.assertRegex(
            content_validation["evidence"]["source_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertEqual(
            CONVERSION_AUDIT_SCHEMA_VERSION,
            content_validation["evidence"]["audit_schema_version"],
        )

    def test_mvp_harness_omits_non_applicable_docx_content_validation(self) -> None:
        case = self.valid_mvp_case(1)
        fixture_path = REPO_ROOT / str(case["fixture_path"])

        result = evaluate_dataset.mvp_conversion_result(
            case,
            fixture_path=fixture_path,
            acceptance_limits=self.valid_mvp_acceptance_limits(),
        )

        artifact_evaluation = result["evaluations"]["artifact"]
        self.assertEqual("pass", artifact_evaluation["status"])
        self.assertIsNone(artifact_evaluation["content_validation"])

    @unittest.skipUnless(PYMUPDF_AVAILABLE, "PyMuPDF eval dependency is not installed")
    def test_mvp_harness_uses_separate_scanned_pdf_docx_validator(self) -> None:
        case = self.valid_mvp_case(3)
        fixture_path = REPO_ROOT / str(case["fixture_path"])

        result = evaluate_dataset.mvp_conversion_result(
            case,
            fixture_path=fixture_path,
            acceptance_limits=self.valid_mvp_acceptance_limits(),
        )

        content_validation = result["evaluations"]["artifact"]["content_validation"]
        self.assertEqual(
            "scanned_pdf_explicit_review_v1",
            content_validation["validator"],
        )
        self.assertEqual("pass", content_validation["status"])
        self.assertEqual(
            {
                "explicit_review_block": "pass",
                "review_guard": "pass",
                "source_linkage": "pass",
            },
            {
                check["id"]: check["status"]
                for check in content_validation["checks"]
            },
        )
        self.assertNotIn(
            "section_order",
            {check["id"] for check in content_validation["checks"]},
        )

    def test_pdf_to_word_content_validation_fails_closed_without_expectations(
        self,
    ) -> None:
        case = self.valid_mvp_case(4)
        del case["pdf_to_word_expectations"]
        content_validation: dict[str, object] = {}

        failures = evaluate_dataset.p9_validate_artifact_expectations(
            fixture=case,
            conversion_mode="pdf_to_word",
            representative_mode="pdf_to_word",
            primary_artifact={
                "kind": "primary",
                "format": "docx",
                "content": b"untrusted docx without authoritative expectations",
            },
            warnings=case["expected_warnings"],
            require_content_validation=True,
            content_validation=content_validation,
        )

        self.assertEqual("unavailable", content_validation["status"])
        self.assertIn(
            "artifact content validation is unavailable for pdf_to_word",
            failures,
        )

    @unittest.skipUnless(PYMUPDF_AVAILABLE, "PyMuPDF eval dependency is not installed")
    def test_record_pdf_docx_validator_ignores_blank_paragraphs_for_linkage(
        self,
    ) -> None:
        from services.api.poc_web import convert_uploaded_document

        case = self.valid_mvp_case(4)
        fixture_path = REPO_ROOT / str(case["fixture_path"])
        converted = convert_uploaded_document(
            filename=fixture_path.name,
            content=fixture_path.read_bytes(),
            conversion_mode="pdf_to_word",
        )
        artifact_content = converted["artifacts"][0]["content"]
        title_paragraph_end = f"{fixture_path.name}</w:t></w:r></w:p>"
        blank_paragraph_content = rewritten_docx(
            artifact_content,
            replacements={
                "word/document.xml": (
                    (
                        title_paragraph_end,
                        f"{title_paragraph_end}<w:p/>",
                    ),
                )
            },
        )
        content_validation: dict[str, object] = {}

        self.assertNotEqual(artifact_content, blank_paragraph_content)
        failures = evaluate_dataset.p9_validate_artifact_expectations(
            fixture=case,
            conversion_mode="pdf_to_word",
            representative_mode="pdf_to_word",
            primary_artifact={
                "kind": "primary",
                "format": "docx",
                "content": blank_paragraph_content,
            },
            warnings=case["expected_warnings"],
            require_content_validation=True,
            content_validation=content_validation,
        )

        self.assertEqual("pass", content_validation["status"])
        self.assertEqual(
            "pass",
            next(
                check["status"]
                for check in content_validation["checks"]
                if check["id"] == "source_linkage"
            ),
        )
        self.assertEqual([], failures)

    @unittest.skipUnless(PYMUPDF_AVAILABLE, "PyMuPDF eval dependency is not installed")
    def test_record_pdf_docx_validator_detects_negative_artifacts(self) -> None:
        from services.api.poc_web import convert_uploaded_document

        case = self.valid_mvp_case(4)
        fixture_path = REPO_ROOT / str(case["fixture_path"])
        converted = convert_uploaded_document(
            filename=fixture_path.name,
            content=fixture_path.read_bytes(),
            conversion_mode="pdf_to_word",
        )
        artifact_content = converted["artifacts"][0]["content"]
        first_section = "Batch: POC-REC-0001"
        second_section = "Product: Sample sterile fill lot"
        mutations = (
            (
                "missing body",
                rewritten_docx(
                    artifact_content,
                    replacements={
                        "word/document.xml": (
                            (
                                "Record field: release decision was documented in log.",
                                "",
                            ),
                        )
                    },
                ),
                "body_completeness",
            ),
            (
                "reordered sections",
                rewritten_docx(
                    artifact_content,
                    replacements={
                        "word/document.xml": (
                            (first_section, "__VERIDOC_SECTION_SWAP__"),
                            (second_section, first_section),
                            ("__VERIDOC_SECTION_SWAP__", second_section),
                        )
                    },
                ),
                "section_order",
            ),
            (
                "missing source linkage",
                rewritten_docx(
                    artifact_content,
                    omitted_parts=frozenset({"word/comments.xml"}),
                ),
                "source_linkage",
            ),
            (
                "stale comments part without document relationship",
                rewritten_docx(
                    artifact_content,
                    replacements={
                        "word/_rels/document.xml.rels": (
                            (
                                '  <Relationship Id="rId3" '
                                'Type="http://schemas.openxmlformats.org/'
                                'officeDocument/2006/relationships/comments" '
                                'Target="comments.xml"/>\n',
                                "",
                            ),
                        )
                    },
                ),
                "source_linkage",
            ),
            (
                "copied source block identity",
                rewritten_docx(
                    artifact_content,
                    replacements={
                        "word/comments.xml": tuple(
                            (f"block-{index:04d}", "block-0001")
                            for index in range(2, 6)
                        )
                    },
                ),
                "source_linkage",
            ),
            (
                "unusable source bbox",
                rewritten_docx(
                    artifact_content,
                    replacements={
                        "word/comments.xml": (("bbox=", "bbox=unusable:"),)
                    },
                ),
                "source_linkage",
            ),
        )

        for label, mutated_content, expected_failed_check in mutations:
            with self.subTest(label=label):
                self.assertNotEqual(artifact_content, mutated_content)
                content_validation: dict[str, object] = {}
                failures = evaluate_dataset.p9_validate_artifact_expectations(
                    fixture=case,
                    conversion_mode="pdf_to_word",
                    representative_mode="pdf_to_word",
                    primary_artifact={
                        "kind": "primary",
                        "format": "docx",
                        "content": mutated_content,
                    },
                    warnings=case["expected_warnings"],
                    require_content_validation=True,
                    content_validation=content_validation,
                )

                self.assertEqual("fail", content_validation["status"])
                self.assertEqual(
                    "fail",
                    next(
                        check["status"]
                        for check in content_validation["checks"]
                        if check["id"] == expected_failed_check
                    ),
                )
                self.assertTrue(failures)

    @unittest.skipUnless(PYMUPDF_AVAILABLE, "PyMuPDF eval dependency is not installed")
    def test_pdf_to_word_content_validation_rejects_unsupported_validator(
        self,
    ) -> None:
        from services.api.poc_web import convert_uploaded_document

        case = self.valid_mvp_case(4)
        case["pdf_to_word_expectations"]["validator"] = (
            "record_pdf_docx_content_typo"
        )
        fixture_path = REPO_ROOT / str(case["fixture_path"])
        converted = convert_uploaded_document(
            filename=fixture_path.name,
            content=fixture_path.read_bytes(),
            conversion_mode="pdf_to_word",
        )
        content_validation: dict[str, object] = {}

        failures = evaluate_dataset.p9_validate_artifact_expectations(
            fixture=case,
            conversion_mode="pdf_to_word",
            representative_mode="pdf_to_word",
            primary_artifact=converted["artifacts"][0],
            warnings=case["expected_warnings"],
            require_content_validation=True,
            content_validation=content_validation,
        )

        self.assertEqual("fail", content_validation["status"])
        self.assertEqual(
            [
                "unsupported docx content validator "
                "'record_pdf_docx_content_typo'"
            ],
            content_validation["failures"],
        )
        self.assertIn(content_validation["failures"][0], failures)

    @unittest.skipUnless(PYMUPDF_AVAILABLE, "PyMuPDF eval dependency is not installed")
    def test_pdf_to_word_content_validation_rejects_malformed_body_block(
        self,
    ) -> None:
        from services.api.poc_web import convert_uploaded_document

        case = self.valid_mvp_case(4)
        case["pdf_to_word_expectations"]["body_blocks"].append(
            {"kind": "paragraph", "source_page": 1}
        )
        fixture_path = REPO_ROOT / str(case["fixture_path"])
        converted = convert_uploaded_document(
            filename=fixture_path.name,
            content=fixture_path.read_bytes(),
            conversion_mode="pdf_to_word",
        )
        content_validation: dict[str, object] = {}

        failures = evaluate_dataset.p9_validate_artifact_expectations(
            fixture=case,
            conversion_mode="pdf_to_word",
            representative_mode="pdf_to_word",
            primary_artifact=converted["artifacts"][0],
            warnings=case["expected_warnings"],
            require_content_validation=True,
            content_validation=content_validation,
        )

        self.assertEqual("fail", content_validation["status"])
        self.assertEqual(
            ["docx body block expectation at index 5 is malformed"],
            content_validation["failures"],
        )
        self.assertIn(content_validation["failures"][0], failures)

    def test_pdf_to_word_content_validation_rejects_malformed_expected_phrase(
        self,
    ) -> None:
        case = self.valid_mvp_case(4)
        case["pdf_to_word_expectations"]["expected_phrases"].append(None)

        with mock.patch.object(
            evaluate_dataset,
            "p9_docx_source_linkage",
            return_value=[],
        ):
            result = evaluate_dataset.p9_validate_pdf_to_word_docx_content(
                Path("unused.docx"),
                case["pdf_to_word_expectations"],
                docx=mock.Mock(blocks=[]),
            )

        self.assertEqual("fail", result["status"])
        self.assertEqual(
            [
                "docx expected phrase expectations must be a non-empty list "
                "of non-empty strings"
            ],
            result["failures"],
        )

    def test_pdf_to_word_content_validation_rejects_malformed_section_order(
        self,
    ) -> None:
        case = self.valid_mvp_case(4)
        case["pdf_to_word_expectations"]["section_order"].append(None)

        with mock.patch.object(
            evaluate_dataset,
            "p9_docx_source_linkage",
            return_value=[],
        ):
            result = evaluate_dataset.p9_validate_pdf_to_word_docx_content(
                Path("unused.docx"),
                case["pdf_to_word_expectations"],
                docx=mock.Mock(blocks=[]),
            )

        self.assertEqual("fail", result["status"])
        self.assertEqual(
            [
                "docx section order expectations must be a non-empty list "
                "of non-empty strings"
            ],
            result["failures"],
        )

    def test_mvp_harness_fails_mismatched_audit_fields(self) -> None:
        case = self.valid_mvp_case()
        fixture_content = b"mvp audit review fixture"
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(fixture_content)
            fixture_file.flush()
            fixture_path = Path(fixture_file.name)
            mutations = (
                (
                    "schema",
                    lambda payload: payload["audit"].__setitem__(
                        "schema_version", "unsupported-audit-schema"
                    ),
                    "schema_version",
                ),
                (
                    "input filename",
                    lambda payload: payload["audit"]["input"].__setitem__(
                        "filename", "different.docx"
                    ),
                    "input filename",
                ),
                (
                    "conversion mode",
                    lambda payload: payload["audit"]["input"].__setitem__(
                        "conversion_mode", "excel_to_word"
                    ),
                    "input conversion_mode",
                ),
                (
                    "input hash",
                    lambda payload: payload["audit"]["input"].__setitem__(
                        "sha256", "0" * 64
                    ),
                    "input sha256",
                ),
                (
                    "warning count",
                    lambda payload: payload["audit"]["warnings"].__setitem__(
                        "count", 999
                    ),
                    "warnings count",
                ),
                (
                    "review count",
                    lambda payload: payload["audit"]["review_items"].__setitem__(
                        "count", 999
                    ),
                    "review_items count",
                ),
                (
                    "prompt version",
                    lambda payload: payload["audit"]["versions"]["prompt"].pop(
                        "version"
                    ),
                    "prompt lineage",
                ),
                (
                    "conversion plan schema",
                    lambda payload: payload["audit"]["versions"]["schemas"].pop(
                        "conversion_plan"
                    ),
                    "conversion_plan schema lineage",
                ),
                (
                    "document IR schema",
                    lambda payload: payload["audit"]["versions"]["schemas"].pop(
                        "document_ir"
                    ),
                    "document_ir schema lineage",
                ),
            )
            for label, mutate, expected_reason in mutations:
                with self.subTest(field=label):
                    converted = self.valid_mvp_conversion_payload(
                        case,
                        fixture_path=fixture_path,
                        fixture_content=fixture_content,
                    )
                    mutate(converted)
                    with mock.patch(
                        "services.api.poc_web.convert_uploaded_document",
                        return_value=converted,
                    ), mock.patch.object(
                        evaluate_dataset,
                        "p9_validate_artifact_expectations",
                        return_value=[],
                    ):
                        result = evaluate_dataset.mvp_conversion_result(
                            case,
                            fixture_path=fixture_path,
                            acceptance_limits=self.valid_mvp_acceptance_limits(),
                        )

                    self.assertEqual("fail", result["evaluations"]["audit"]["status"])
                    self.assertIn(
                        expected_reason,
                        result["evaluations"]["audit"]["reason"],
                    )
                    self.assertEqual("fail", result["acceptance_status"])

    def test_poc_acceptance_report_cli_maps_15_2_criteria(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--poc-acceptance-report"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        payload = json.loads(completed.stdout)

        self.assertEqual(
            "veridoc-poc-acceptance-report/v0", payload["schema_version"]
        )
        self.assertEqual("15.2_PoC受入基準", payload["criteria_source"])
        self.assertEqual(
            str(evaluate_dataset.DEFAULT_P9_HARNESS_MANIFEST),
            payload["evidence"]["dataset_manifest"],
        )
        self.assertIn("commit", payload["tested_environment"])
        self.assertEqual(
            [
                "functionality",
                "structured_output",
                "llm_control",
                "traceability",
                "safety",
                "logs",
                "security",
                "reproducibility",
            ],
            [row["criterion_id"] for row in payload["acceptance_matrix"]],
        )
        self.assertTrue(
            all(row["status"] in {"pass", "fail", "unknown"} for row in payload["acceptance_matrix"])
        )
        self.assertTrue(
            any(row["status"] == "fail" for row in payload["acceptance_matrix"])
        )
        self.assertIn("conversion_mode_results", payload)
        self.assertIn("llm_stability_acceptance_threshold", payload)
        self.assertIn("llm_stability_comparison", payload)
        self.assertIn("review_ui_observations", payload)
        self.assertIn("known_limitations", payload)
        self.assertIn("follow_up_issue_candidates", payload)
        expected_gate_revisions = {
            result["mvp_before_gate_revision"]
            for result in payload["p9_harness_results"]
            if result.get("fail_closed")
        }
        self.assertTrue(
            expected_gate_revisions.issubset(
                {
                    limitation.get("mvp_before_gate_revision")
                    for limitation in payload["known_limitations"]
                }
            )
        )
        self.assertTrue(
            any(
                all(
                    gate_revision in candidate["reason"]
                    for gate_revision in expected_gate_revisions
                )
                for candidate in payload["follow_up_issue_candidates"]
                if candidate["title"]
                == "Resolve fail-closed P9 MVP-before gate revisions"
            )
        )
        self.assertTrue(
            any(
                condition["condition_id"] == "external_transmission"
                and condition["status"] == "pass"
                for condition in payload["fail_closed_conditions"]
            )
        )
        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["llm_control"]["status"])
        self.assertIn(
            "llm_stability_acceptance_threshold",
            rows["llm_control"]["evidence_refs"],
        )
        self.assertEqual(
            [
                "plan_agreement_rate",
                "confirmed_value_agreement_rate",
                "schema_failure_rate",
                "deterministic_fallback_rate",
                "unstable_example_count",
            ],
            payload["matrix_evidence"]["llm_control"]["threshold_failures"],
        )
        self.assertEqual(
            0,
            payload["matrix_evidence"]["llm_control"][
                "external_ai_api_guard_violation_count"
            ],
        )

    def test_poc_acceptance_report_passes_security_when_auth_session_coverage_exists(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("pass", rows["llm_control"]["status"])
        self.assertIn(
            "llm_stability_acceptance_threshold",
            rows["llm_control"]["evidence_refs"],
        )
        self.assertEqual(
            [],
            payload["matrix_evidence"]["llm_control"]["threshold_failures"],
        )
        self.assertEqual(
            0,
            payload["matrix_evidence"]["llm_control"][
                "external_ai_api_guard_violation_count"
            ],
        )
        self.assertEqual("pass", rows["security"]["status"])
        self.assertIn(
            "authenticated PoC API session checked: True",
            rows["security"]["evidence"],
        )
        self.assertIn(
            "tests/test_poc_web_api.py::test_poc_http_api_authenticates_review_events_before_parsing_payload",
            rows["security"]["evidence_refs"],
        )
        self.assertIn(
            "tests/test_poc_web_api.py::test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
            rows["security"]["evidence_refs"],
        )
        self.assertIn(
            "tests/test_poc_web_api.py::test_poc_http_api_filters_review_action_audit_events_by_action",
            rows["security"]["evidence_refs"],
        )
        self.assertIn(
            "tests/test_poc_web_api.py::test_poc_http_api_allows_approval_with_revised_text_target",
            rows["security"]["evidence_refs"],
        )
        self.assertIn(
            "tests/test_poc_web_api.py::test_poc_http_api_requires_admin_role_for_retry_job_event",
            rows["security"]["evidence_refs"],
        )
        self.assertIn(
            "tests/test_poc_web_api.py::test_poc_http_api_requires_configured_local_auth_token_for_review_events",
            rows["security"]["evidence_refs"],
        )
        self.assertNotIn(
            "tests/test_poc_web_api.py::test_poc_http_api_records_desktop_upload_and_download_audit_events",
            rows["security"]["evidence_refs"],
        )
        self.assertTrue(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )
        self.assertTrue(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_evidence_inputs_tracked"
            ]
        )
        self.assertEqual("pass", payload["overall_status"])

    def test_poc_acceptance_report_checks_auth_session_coverage_in_manifest_repo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_is_present",
                return_value=True,
            ) as mocked_auth_coverage, mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        mocked_auth_coverage.assert_called_once_with(temp_root.resolve())
        self.assertTrue(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_successful_auth_session_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            fail_closed_only_refs = "\n".join(
                f"def {ref.split('::', 1)[1]}(): pass"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{fail_closed_only_refs}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_authenticated_success_markers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_without_auth = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 202 == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_without_auth}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_token_on_success_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_unused_token = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'reviewer:env-reviewer=reviewer-token',\n"
                "    )\n"
                "    unused_token = 'reviewer-token'\n"
                "    status = 202\n"
                "    assert status == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_unused_token}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_exact_success_token_match(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_embedded_token = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'reviewer:env-reviewer=reviewer-token',\n"
                "    )\n"
                "    status = _post_review_event_on_connection(\n"
                "        None, None, role_token='not-reviewer-token'\n"
                "    )\n"
                "    assert status == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_embedded_token}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_exact_authorization_token_match(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_embedded_header_token = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'reviewer:env-reviewer=reviewer-token',\n"
                "    )\n"
                "    status = _post_review_event_on_connection(\n"
                "        None,\n"
                "        None,\n"
                "        headers={'Authorization': 'Bearer not-reviewer-token'},\n"
                "    )\n"
                "    assert status == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_embedded_header_token}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_exact_bearer_scheme_match(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_lowercase_bearer = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'reviewer:env-reviewer=reviewer-token',\n"
                "    )\n"
                "    status, _body = _post_review_event_on_connection(\n"
                "        None,\n"
                "        None,\n"
                "        headers={'Authorization': 'bearer reviewer-token'},\n"
                "    )\n"
                "    assert status == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_lowercase_bearer}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_asserted_success_comparison(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_branch_comparison = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'reviewer:env-reviewer=reviewer-token',\n"
                "    )\n"
                "    status = _post_review_event_on_connection(\n"
                "        None, None, role_token='reviewer-token'\n"
                "    )\n"
                "    if status == 202:\n"
                "        pytest.fail('success status was not expected')\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_branch_comparison}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_alternative_failure_status_assertion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_alternative_failure_status = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'reviewer:env-reviewer=reviewer-token',\n"
                "    )\n"
                "    status = _post_review_event_on_connection(\n"
                "        None, None, role_token='reviewer-token'\n"
                "    )\n"
                "    assert status == 401 or status == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_alternative_failure_status}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_ties_auth_token_to_asserted_status(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_unrelated_status = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'reviewer:env-reviewer=reviewer-token',\n"
                "    )\n"
                "    _post_review_event_on_connection(\n"
                "        None, None, role_token='reviewer-token'\n"
                "    )\n"
                "    status = 202\n"
                "    assert status == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_unrelated_status}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_success_status_equality(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_negative_status = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'reviewer:env-reviewer=reviewer-token',\n"
                "    )\n"
                "    _post_review_event_on_connection(None, None, role_token='reviewer-token')\n"
                "    status = 202\n"
                "    assert status != 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_negative_status}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_observed_success_status_assertion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_constant_status = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'reviewer:env-reviewer=reviewer-token',\n"
                "    )\n"
                "    _post_review_event_on_connection(None, None, role_token='reviewer-token')\n"
                "    assert 202 == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_constant_status}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_env_before_success_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_late_env_setup = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    status, _body = _post_review_event_on_connection(\n"
                "        None, None, role_token='reviewer-token'\n"
                "    )\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'reviewer:env-reviewer=reviewer-token',\n"
                "    )\n"
                "    assert status == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_late_env_setup}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_same_statement_env_after_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    (\n"
                        "        connection.request(\n"
                        "            'POST',\n"
                        "            '/api/review-events',\n"
                        "            body=b'{\"conversion_id\":\"conversion-env-auth\"}',\n"
                        "            headers={'Authorization': 'Bearer env-reviewer-token'},\n"
                        "        ),\n"
                        "        monkeypatch.setenv(\n"
                        "            'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "            'reviewer:env-reviewer=env-reviewer-token',\n"
                        "        ),\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    assert response.status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_env_backed_auth_success_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_direct_tokens = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    server.local_auth_tokens = {'reviewer-token': 'reviewer'}\n"
                "    _post_review_event_on_connection(None, None, role_token='reviewer-token')\n"
                "    status = 202\n"
                "    assert status == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_direct_tokens}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_direct_auth_setup_for_direct_success_refs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            overrides: dict[str, str] = {}
            for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS:
                if ref in evaluate_dataset.POC_AUTH_SESSION_ENV_SUCCESS_COVERAGE_REFS:
                    continue
                test_name = ref.split("::", 1)[1]
                expectation = evaluate_dataset.POC_AUTH_SESSION_SUCCESS_REF_EXPECTATIONS[
                    ref
                ]
                token = next(iter(expectation["tokens"]))
                status_code = next(iter(expectation["status_codes"]))
                method = expectation.get("method") or "POST"
                path = expectation.get("path") or "/api/review-events"
                literals = "\n".join(
                    f"    marker_{index} = {literal!r}"
                    for index, literal in enumerate(expectation["required_literals"])
                )
                overrides[test_name] = (
                    f"{literals}\n"
                    "    connection.request(\n"
                    f"        {method!r},\n"
                    f"        {path!r},\n"
                    f"        headers={{'Authorization': 'Bearer {token}'}},\n"
                    "    )\n"
                    "    response = connection.getresponse()\n"
                    f"    assert response.status == {status_code}\n"
                )
            success_ref_names_without_direct_auth_setup = (
                valid_poc_auth_success_ref_source(
                    overrides,
                    include_direct_auth_setup=False,
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_without_direct_auth_setup}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_ties_success_literals_to_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_detached_retry_literal = (
                valid_poc_auth_success_ref_source(
                    {
                        "test_poc_http_api_requires_admin_role_for_retry_job_event": (
                            "    marker = 'retry_conversion'\n"
                            "    connection.request(\n"
                            "        'POST',\n"
                            "        '/api/job-events',\n"
                            "        body=b'{}',\n"
                            "        headers={'Authorization': 'Bearer admin-token'},\n"
                            "    )\n"
                            "    response = connection.getresponse()\n"
                            "    assert response.status == 202\n"
                        )
                    }
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_detached_retry_literal}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_clears_direct_auth_when_deleted_before_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_deleted_direct_auth = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_requires_admin_role_for_retry_job_event": (
                        "    del server.local_auth_tokens\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/job-events',\n"
                        "        body=b'{\"action\":\"retry_conversion\"}',\n"
                        "        headers={'Authorization': 'Bearer admin-token'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    assert response.status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_deleted_direct_auth}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_direct_helper_missing_success_token(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_incomplete_helper = valid_poc_auth_success_ref_source(
                local_auth_tokens_source=(
                    "def _local_auth_tokens():\n"
                    "    return {\n"
                    "        'viewer-token': {'role': 'viewer', 'principal_id': 'viewer'},\n"
                    "        'reviewer-token': {'role': 'reviewer', 'principal_id': 'reviewer'},\n"
                    "        'approver-token': {'role': 'approver', 'principal_id': 'approver'},\n"
                    "    }\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_incomplete_helper}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_setting_env_auth_variable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_env_mention = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.delenv('VERIDOC_LOCAL_AUTH_TOKENS', raising=False)\n"
                "    _post_review_event_on_connection(None, None, role_token='reviewer-token')\n"
                "    status = 202\n"
                "    assert status == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_env_mention}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_fake_environ_auth_assignment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_fake_environ = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    fake.environ['VERIDOC_LOCAL_AUTH_TOKENS'] = (\n"
                        "        'reviewer:env-reviewer=env-reviewer-token'\n"
                        "    )\n"
                        "    status, body = _post_review_event_on_connection(\n"
                        "        None,\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_fake_environ}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_fake_monkeypatch_setenv(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_fake_setenv = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    fake.setenv(\n"
                        "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "        'reviewer:env-reviewer=env-reviewer-token',\n"
                        "    )\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    status, body = _post_review_event_on_connection(\n"
                        "        connection,\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_fake_setenv}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_shadowed_monkeypatch_setenv(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_shadowed_monkeypatch = (
                valid_poc_auth_success_ref_source(
                    {
                        "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                            "    class FakeMonkeypatch:\n"
                            "        def setenv(self, name, value):\n"
                            "            self.name = name\n"
                            "            self.value = value\n"
                            "    monkeypatch = FakeMonkeypatch()\n"
                            "    monkeypatch.setenv(\n"
                            "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                            "        'reviewer:env-reviewer=env-reviewer-token',\n"
                            "    )\n"
                            "    status, body = _post_review_event_on_connection(\n"
                            "        None,\n"
                            "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                            "        role_token='env-reviewer-token',\n"
                            "    )\n"
                            "    assert status == 202\n"
                        )
                    }
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_shadowed_monkeypatch}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_with_shadowed_monkeypatch_setenv(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_shadowed_monkeypatch = (
                valid_poc_auth_success_ref_source(
                    {
                        "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                            "    with fake_patch() as monkeypatch:\n"
                            "        monkeypatch.setenv(\n"
                            "            'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                            "            'reviewer:env-reviewer=env-reviewer-token',\n"
                            "        )\n"
                            "    status, body = _post_review_event_on_connection(\n"
                            "        None,\n"
                            "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                            "        role_token='env-reviewer-token',\n"
                            "    )\n"
                            "    assert status == 202\n"
                        )
                    }
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_shadowed_monkeypatch}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_parametrized_monkeypatch_shadowing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source().replace(
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success(monkeypatch):",
                "class FakeMonkeypatch:\n"
                "    def setenv(self, *args):\n"
                "        return None\n"
                "\n"
                "@pytest.mark.parametrize('monkeypatch', [FakeMonkeypatch()])\n"
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success(monkeypatch):",
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_unittest_skipped_auth_ref(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source().replace(
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
                "@unittest.skip('auth evidence disabled')\n"
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_module_skipped_auth_refs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                "pytestmark = pytest.mark.skip('auth evidence disabled')\n"
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_top_level_skipped_auth_module(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                "pytest.skip('auth evidence disabled', allow_module_level=True)\n"
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_importorskip_auth_module(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                "pytest.importorskip('missing_auth_dependency')\n"
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_conditional_top_level_skip(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                "if True:\n"
                "    pytest.skip('auth evidence disabled', allow_module_level=True)\n"
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_module_usefixtures_mark(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                "pytestmark = pytest.mark.usefixtures('skip_all')\n"
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_unresolved_auth_ref_fixture(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source().replace(
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success(monkeypatch):",
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success(monkeypatch, missing_fixture):",
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_fixture_decorated_auth_ref(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source().replace(
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
                "@pytest.fixture\n"
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_empty_parametrized_auth_ref(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source().replace(
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
                "@pytest.mark.parametrize('case', [])\n"
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_indirect_empty_parametrized_auth_ref(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source().replace(
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
                "EMPTY_AUTH_CASES = []\n"
                "@pytest.mark.parametrize('auth_case', EMPTY_AUTH_CASES)\n"
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_all_skipped_parametrized_auth_ref(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source().replace(
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success(monkeypatch):",
                "@pytest.mark.parametrize(\n"
                "    'case',\n"
                "    [pytest.param('disabled-auth', marks=pytest.mark.skip)],\n"
                ")\n"
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success(monkeypatch, case):",
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_wrapper_decorated_auth_ref(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source().replace(
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
                "def auth_wrapper(fn):\n"
                "    return fn\n"
                "@auth_wrapper\n"
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_module_test_opt_out(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                "__test__ = False\n"
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_falsey_module_test_opt_out(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                "__test__ = 0\n"
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_function_test_opt_out(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            opted_out_test = (
                "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success"
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n"
                f"{opted_out_test}.__test__ = False\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_falsey_function_test_opt_out(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            opted_out_test = (
                "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success"
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n"
                f"{opted_out_test}.__test__ = 0\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_setattr_function_test_opt_out(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            opted_out_test = (
                "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success"
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n"
                f"setattr({opted_out_test}, '__test__', False)\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_async_auth_ref(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source().replace(
                "def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
                "async def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success",
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_direct_auth_principal_ids(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                local_auth_tokens_source=(
                    "def _local_auth_tokens():\n"
                    "    return {\n"
                    "        'viewer-token': {'role': 'viewer'},\n"
                    "        'reviewer-token': {'role': 'reviewer'},\n"
                    "        'approver-token': {'role': 'approver'},\n"
                    "        'admin-token': {'role': 'admin'},\n"
                    "    }\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_fixture_decorated_auth_token_helper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                local_auth_tokens_source=(
                    "@pytest.fixture\n"
                    "def _local_auth_tokens():\n"
                    "    return {\n"
                    "        'viewer-token': {'role': 'viewer', 'principal_id': 'viewer'},\n"
                    "        'reviewer-token': {'role': 'reviewer', 'principal_id': 'reviewer'},\n"
                    "        'approver-token': {'role': 'approver', 'principal_id': 'approver'},\n"
                    "        'admin-token': {'role': 'admin', 'principal_id': 'admin'},\n"
                    "    }\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_async_auth_token_helper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                local_auth_tokens_source=(
                    "async def _local_auth_tokens():\n"
                    "    return {\n"
                    "        'viewer-token': {'role': 'viewer', 'principal_id': 'viewer'},\n"
                    "        'reviewer-token': {'role': 'reviewer', 'principal_id': 'reviewer'},\n"
                    "        'approver-token': {'role': 'approver', 'principal_id': 'approver'},\n"
                    "        'admin-token': {'role': 'admin', 'principal_id': 'admin'},\n"
                    "    }\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_auth_token_helper_with_required_arg(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                local_auth_tokens_source=(
                    "def _local_auth_tokens(required):\n"
                    "    return {\n"
                    "        'viewer-token': {'role': 'viewer', 'principal_id': 'viewer'},\n"
                    "        'reviewer-token': {'role': 'reviewer', 'principal_id': 'reviewer'},\n"
                    "        'approver-token': {'role': 'approver', 'principal_id': 'approver'},\n"
                    "        'admin-token': {'role': 'admin', 'principal_id': 'admin'},\n"
                    "    }\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_auth_token_helper_called_with_args(
        self,
    ) -> None:
        test_source = valid_poc_auth_success_ref_source().replace(
            "server.local_auth_tokens = _local_auth_tokens()\n",
            "server.local_auth_tokens = _local_auth_tokens('viewer-token')\n",
        )
        test_source = f"{test_source}\n{valid_poc_auth_fail_closed_ref_source()}"

        self.assertFalse(
            self.poc_auth_session_coverage_is_present_for_source(test_source)
        )

    def test_poc_acceptance_report_rejects_shadowed_auth_token_helper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                local_auth_tokens_source=(
                    "def _local_auth_tokens():\n"
                    "    return {}\n"
                    "\n"
                    "def _local_auth_tokens():\n"
                    "    return {\n"
                    "        'viewer-token': {'role': 'viewer', 'principal_id': 'viewer'},\n"
                    "        'reviewer-token': {'role': 'reviewer', 'principal_id': 'reviewer'},\n"
                    "        'approver-token': {'role': 'approver', 'principal_id': 'approver'},\n"
                    "        'admin-token': {'role': 'admin', 'principal_id': 'admin'},\n"
                    "    }\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_ignores_unreachable_auth_helper_returns(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                local_auth_tokens_source=(
                    "def _local_auth_tokens():\n"
                    "    return None\n"
                    "    return {\n"
                    "        'admin-token': {'role': 'admin', 'principal_id': 'admin'},\n"
                    "    }\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_ignores_helper_returns_after_skip(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                local_auth_tokens_source=(
                    "def _local_auth_tokens():\n"
                    "    pytest.skip('auth helper disabled')\n"
                    "    return {\n"
                    "        'admin-token': {'role': 'admin', 'principal_id': 'admin'},\n"
                    "    }\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_fake_request_receiver(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_requires_admin_role_for_retry_job_event": (
                        "    action = 'retry_conversion'\n"
                        "    body = json.dumps({'action': action}).encode('utf-8')\n"
                        "    connection = FakeConnection()\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/job-events',\n"
                        "        body=body,\n"
                        "        headers={'Authorization': 'Bearer admin-token'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    assert response.status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_nested_authorization_header(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_requires_admin_role_for_retry_job_event": (
                        "    action = 'retry_conversion'\n"
                        "    body = json.dumps({'action': action}).encode('utf-8')\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/job-events',\n"
                        "        body=body,\n"
                        "        headers={'X-Debug': {'Authorization': 'Bearer admin-token'}},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    assert response.status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_fake_status_helper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    monkeypatch.setenv(\n"
                        "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "        'reviewer:env-reviewer=env-reviewer-token',\n"
                        "    )\n"
                        "    status, body = some_helper(\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_stubbed_trusted_status_helper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                trusted_helper_source=(
                    "def _post_review_event_on_connection(connection, audit_event, *, role_token):\n"
                    "    return 202, {}\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_duplicate_trusted_helper_binding(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    monkeypatch.setenv(\n"
                        "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "        'reviewer:env-reviewer=env-reviewer-token',\n"
                        "    )\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    status, body = _post_review_event_on_connection(\n"
                        "        connection,\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        audit_event=_review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_trusted_helper_without_posted_payload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                trusted_helper_source=(
                    "def _post_review_event_on_connection(connection, audit_event, *, role_token):\n"
                    "    connection.request(\n"
                    "        'POST',\n"
                    "        '/api/review-events',\n"
                    "        body=b'{}',\n"
                    "        headers={'Authorization': f'Bearer {role_token}'},\n"
                    "    )\n"
                    "    response = connection.getresponse()\n"
                    "    return response.status, {}\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_shadowed_trusted_status_helper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            shadowed_trusted_helper = (
                "def _post_review_event_on_connection(connection, audit_event, *, role_token):\n"
                "    return 202, {}\n"
                "\n"
                "def _post_review_event_on_connection(connection, audit_event, *, role_token):\n"
                "    payload = json.dumps({'audit_event': audit_event}).encode('utf-8')\n"
                "    connection.request(\n"
                "        'POST',\n"
                "        '/api/review-events',\n"
                "        body=payload,\n"
                "        headers={'Authorization': f'Bearer {role_token}'},\n"
                "    )\n"
                "    response = connection.getresponse()\n"
                "    return response.status, {}\n"
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                trusted_helper_source=shadowed_trusted_helper
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_trusted_helper_without_bearer_header(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                trusted_helper_source=(
                    "def _post_review_event_on_connection(connection, audit_event, *, role_token):\n"
                    "    connection.request(\n"
                    "        'POST',\n"
                    "        '/api/review-events',\n"
                    "        body=b'{}',\n"
                    "        headers={'Content-Type': 'application/json'},\n"
                    "    )\n"
                    "    response = connection.getresponse()\n"
                    "    return response.status, {}\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_trusted_helper_with_required_arg(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                trusted_helper_source=(
                    "def _post_review_event_on_connection(connection, audit_event, required, *, role_token):\n"
                    "    payload = json.dumps({'audit_event': audit_event}).encode('utf-8')\n"
                    "    connection.request(\n"
                    "        'POST',\n"
                    "        '/api/review-events',\n"
                    "        body=payload,\n"
                    "        headers={'Authorization': f'Bearer {role_token}'},\n"
                    "    )\n"
                    "    response = connection.getresponse()\n"
                    "    return response.status, {}\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_trusted_helper_extra_kwargs(
        self,
    ) -> None:
        success_ref_names = valid_poc_auth_success_ref_source(
            {
                "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                    "    monkeypatch.setenv(\n"
                    "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                    "        'reviewer:env-reviewer=env-reviewer-token',\n"
                    "    )\n"
                    "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                    "    status, body = _post_review_event_on_connection(\n"
                    "        connection,\n"
                    "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                    "        role_token='env-reviewer-token',\n"
                    "        trusted_by_name_only=True,\n"
                    "    )\n"
                    "    assert status == 202\n"
                )
            },
            trusted_helper_source=(
                "def _post_review_event_on_connection(connection, audit_event, *, role_token, **kwargs):\n"
                "    payload = json.dumps({'audit_event': audit_event}).encode('utf-8')\n"
                "    connection.request(\n"
                "        'POST',\n"
                "        '/api/review-events',\n"
                "        body=payload,\n"
                "        headers={'Authorization': f'Bearer {role_token}'},\n"
                "    )\n"
                "    response = connection.getresponse()\n"
                "    return response.status, {}\n"
            ),
        )
        test_source = f"{success_ref_names}\n{valid_poc_auth_fail_closed_ref_source()}"

        self.assertFalse(
            self.poc_auth_session_coverage_is_present_for_source(test_source)
        )

    def test_poc_acceptance_report_rejects_fake_fail_closed_request_receiver(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source(
                {
                    "test_poc_http_api_authenticates_review_events_before_parsing_payload": (
                        "    payload = b'{not valid json'\n"
                        "    connection = FakeConnection()\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/review-events',\n"
                        "        body=payload,\n"
                        "        headers={'Content-Type': 'application/json'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    body = {'error': 'auth_required'}\n"
                        "    assert response.status == 401\n"
                    )
                }
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_auth_config_for_fail_closed_refs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source(
                include_auth_setup=False
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_ties_fail_closed_error_to_response_body(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source(
                {
                    "test_poc_http_api_authenticates_review_events_before_parsing_payload": (
                        "    payload = b'{not valid json'\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/review-events',\n"
                        "        body=payload,\n"
                        "        headers={'Content-Type': 'application/json'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    body = {'error': 'auth_required'}\n"
                        "    assert response.status == 401\n"
                        "    assert body == {'error': 'auth_required'}\n"
                    )
                }
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_deleted_fail_closed_response(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source(
                {
                    "test_poc_http_api_authenticates_review_events_before_parsing_payload": (
                        "    payload = b'{not valid json'\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/review-events',\n"
                        "        body=payload,\n"
                        "        headers={'Content-Type': 'application/json'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    body = json.loads(response.read().decode('utf-8'))\n"
                        "    del response\n"
                        "    assert response.status == 401\n"
                        "    assert body == {'error': 'auth_required'}\n"
                    )
                }
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_ties_malformed_payload_to_fail_closed_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source(
                {
                    "test_poc_http_api_authenticates_review_events_before_parsing_payload": (
                        "    unused_payload = b'{not valid json'\n"
                        "    payload = b'{}'\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/review-events',\n"
                        "        body=payload,\n"
                        "        headers={'Content-Type': 'application/json'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    body = json.loads(response.read().decode('utf-8'))\n"
                        "    assert response.status == 401\n"
                        "    assert body == {'error': 'auth_required'}\n"
                    )
                }
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_mutated_fail_closed_payload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source(
                {
                    "test_poc_http_api_authenticates_review_events_before_parsing_payload": (
                        "    payload = bytearray(b'{not valid json')\n"
                        "    payload.clear()\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/review-events',\n"
                        "        body=payload,\n"
                        "        headers={'Content-Type': 'application/json'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    body = json.loads(response.read().decode('utf-8'))\n"
                        "    assert response.status == 401\n"
                        "    assert body == {'error': 'auth_required'}\n"
                    )
                }
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_cleared_event_payload_literals(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_requires_admin_role_for_retry_job_event": (
                        "    event = {'action': 'retry_conversion'}\n"
                        "    event.clear()\n"
                        "    body = json.dumps(event).encode('utf-8')\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/job-events',\n"
                        "        body=body,\n"
                        "        headers={'Authorization': 'Bearer admin-token'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    assert response.status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_bearer_on_auth_required_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source(
                {
                    "test_poc_http_api_authenticates_review_events_before_parsing_payload": (
                        "    payload = b'{not valid json'\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/review-events',\n"
                        "        body=payload,\n"
                        "        headers={\n"
                        "            'Authorization': 'Bearer admin-token',\n"
                        "            'Content-Type': 'application/json',\n"
                        "        },\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    body = json.loads(response.read().decode('utf-8'))\n"
                        "    assert response.status == 401\n"
                        "    assert body == {'error': 'auth_required'}\n"
                    )
                }
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_matches_fail_closed_bearer_to_auth_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source(
                {
                    "test_poc_http_api_rejects_read_only_review_role_before_parsing_payload": (
                        "    server.local_auth_tokens = {\n"
                        "        'reviewer-token': {'role': 'reviewer', 'principal_id': 'reviewer'},\n"
                        "    }\n"
                        "    payload = b'{not valid json'\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/review-events',\n"
                        "        body=payload,\n"
                        "        headers={\n"
                        "            'Authorization': 'Bearer viewer-token',\n"
                        "            'Content-Type': 'application/json',\n"
                        "        },\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    body = json.loads(response.read().decode('utf-8'))\n"
                        "    assert response.status == 403\n"
                        "    assert body == {'error': 'forbidden'}\n"
                    )
                }
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_allows_unrelated_local_auth_token_attribute(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    fixture.local_auth_tokens = {}\n"
                        "    monkeypatch.setenv(\n"
                        "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "        'reviewer:env-reviewer=env-reviewer-token',\n"
                        "    )\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    status, body = _post_review_event_on_connection(\n"
                        "        connection,\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("pass", rows["security"]["status"])
        self.assertTrue(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_early_exit_before_auth_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_early_exit = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    if os.environ.get('SKIP_AUTH_EVIDENCE'):\n"
                        "        return\n"
                        "    monkeypatch.setenv(\n"
                        "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "        'reviewer:env-reviewer=env-reviewer-token',\n"
                        "    )\n"
                        "    status, body = _post_review_event_on_connection(\n"
                        "        None,\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_early_exit}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_ignores_env_setup_in_nested_helper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_nested_env_setup = (
                valid_poc_auth_success_ref_source(
                    {
                        "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                            "    def configure_env():\n"
                            "        monkeypatch.setenv(\n"
                            "            'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                            "            'reviewer:env-reviewer=env-reviewer-token',\n"
                            "        )\n"
                            "    status, body = _post_review_event_on_connection(\n"
                            "        None,\n"
                            "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                            "        role_token='env-reviewer-token',\n"
                            "    )\n"
                            "    assert status == 202\n"
                        )
                    }
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_nested_env_setup}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_valid_env_auth_token_value(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_empty_env_token = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'reviewer:env-reviewer=',\n"
                "    )\n"
                "    _post_review_event_on_connection(None, None, role_token='reviewer-token')\n"
                "    status = 202\n"
                "    assert status == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_empty_env_token}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_expected_env_token_role(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_wrong_env_role = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    monkeypatch.setenv(\n"
                        "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "        'viewer:env-reviewer=env-reviewer-token',\n"
                        "    )\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    status, body = _post_review_event_on_connection(\n"
                        "        connection,\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_wrong_env_role}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_clears_env_auth_when_unset_before_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_unset_env = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    monkeypatch.setenv(\n"
                        "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "        'reviewer:env-reviewer=env-reviewer-token',\n"
                        "    )\n"
                        "    monkeypatch.delenv('VERIDOC_LOCAL_AUTH_TOKENS', raising=False)\n"
                        "    status, body = _post_review_event_on_connection(\n"
                        "        None,\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_unset_env}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_env_token_used_by_success_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_different_env_token = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'viewer:env-viewer=viewer-token',\n"
                "    )\n"
                "    status = _post_review_event_on_connection(\n"
                "        None, None, role_token='reviewer-token'\n"
                "    )\n"
                "    assert status == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_different_env_token}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_branch_local_auth_token_override(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    monkeypatch.setenv(\n"
                        "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "        'reviewer:env-reviewer=env-reviewer-token',\n"
                        "    )\n"
                        "    if use_direct_auth:\n"
                        "        server.local_auth_tokens = {\n"
                        "            'env-reviewer-token': {\n"
                        "                'role': 'reviewer',\n"
                        "                'principal_id': 'env-reviewer',\n"
                        "            },\n"
                        "        }\n"
                        "    status, body = _post_review_event_on_connection(\n"
                        "        None,\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_setattr_local_auth_token_override(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_setattr_override = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'reviewer:env-reviewer=reviewer-token',\n"
                "    )\n"
                "    setattr(server, 'local_auth_tokens', {'reviewer-token': 'reviewer'})\n"
                "    status = _post_review_event_on_connection(\n"
                "        None, None, role_token='reviewer-token'\n"
                "    )\n"
                "    assert status == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_setattr_override}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_fail_closed_status_assertions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    pass\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_fail_closed_error_assertions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source(
                {
                    "test_poc_http_api_authenticates_review_events_before_parsing_payload": (
                        "    payload = b'{not valid json'\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/review-events',\n"
                        "        body=payload,\n"
                        "        headers={'Content-Type': 'application/json'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    body = {'error': 'auth_required'}\n"
                        "    assert response.status == 401\n"
                    )
                }
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_fail_closed_route_and_scenario(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source(
                {
                    "test_poc_http_api_authenticates_job_events_before_parsing_payload": (
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/review-events',\n"
                        "        body=b'{not valid json',\n"
                        "        headers={'Content-Type': 'application/json'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    body = {'error': 'auth_required'}\n"
                        "    assert response.status == 401\n"
                    )
                }
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_env_setup_from_untaken_branch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_branch_env = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    if False:\n"
                        "        monkeypatch.setenv(\n"
                        "            'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "            'reviewer:env-reviewer=env-reviewer-token',\n"
                        "        )\n"
                        "    status = _post_review_event_on_connection(\n"
                        "        None,\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_branch_env}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_auth_evidence_after_return(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_unreachable_env_setup = (
                valid_poc_auth_success_ref_source(
                    {
                        "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                            "    return\n"
                            "    monkeypatch.setenv(\n"
                            "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                            "        'reviewer:env-reviewer=env-reviewer-token',\n"
                            "    )\n"
                            "    status, body = _post_review_event_on_connection(\n"
                            "        None,\n"
                            "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                            "        role_token='env-reviewer-token',\n"
                            "    )\n"
                            "    assert status == 202\n"
                        )
                    }
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_unreachable_env_setup}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_auth_evidence_after_failing_assert(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_unreachable_env_setup = (
                valid_poc_auth_success_ref_source(
                    {
                        "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                            "    assert False\n"
                            "    monkeypatch.setenv(\n"
                            "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                            "        'reviewer:env-reviewer=env-reviewer-token',\n"
                            "    )\n"
                            "    status, body = _post_review_event_on_connection(\n"
                            "        None,\n"
                            "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                            "        role_token='env-reviewer-token',\n"
                            "    )\n"
                            "    assert status == 202\n"
                        )
                    }
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_unreachable_env_setup}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_auth_evidence_after_pytest_fail(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    pytest.fail('stop before auth evidence')\n"
                        "    monkeypatch.setenv(\n"
                        "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "        'reviewer:env-reviewer=env-reviewer-token',\n"
                        "    )\n"
                        "    status, body = _post_review_event_on_connection(\n"
                        "        None,\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_http_connection_to_poc_host(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_requires_admin_role_for_retry_job_event": (
                        "    action = 'retry_conversion'\n"
                        "    body = json.dumps({'action': action}).encode('utf-8')\n"
                        "    connection = HTTPConnection('example.invalid', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/job-events',\n"
                        "        body=body,\n"
                        "        headers={'Authorization': 'Bearer admin-token'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    assert response.status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_server_address_tuple_as_host(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_requires_admin_role_for_retry_job_event": (
                        "    action = 'retry_conversion'\n"
                        "    body = json.dumps({'action': action}).encode('utf-8')\n"
                        "    connection = HTTPConnection(server.server_address, server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/job-events',\n"
                        "        body=body,\n"
                        "        headers={'Authorization': 'Bearer admin-token'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    assert response.status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_auth_coverage_rejects_rebound_required_test(
        self,
    ) -> None:
        success_ref_names = valid_poc_auth_success_ref_source()
        fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
        test_name = evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS[0].split(
            "::", 1
        )[1]

        self.assertFalse(
            self.poc_auth_session_coverage_is_present_for_source(
                f"{success_ref_names}\n{fail_closed_ref_names}",
                module_suffix=f"{test_name} = lambda: None\n",
            )
        )

    def test_poc_auth_coverage_rejects_module_http_connection_shadow(
        self,
    ) -> None:
        self.assertFalse(
            self.poc_auth_session_coverage_is_present_for_source(
                f"{valid_poc_auth_success_ref_source()}\n"
                f"{valid_poc_auth_fail_closed_ref_source()}",
                module_prefix=(
                    "def HTTPConnection(*args, **kwargs):\n"
                    "    return object()\n"
                ),
            )
        )

    def test_poc_auth_coverage_clears_env_auth_when_os_environ_cleared(
        self,
    ) -> None:
        env_ref_name = evaluate_dataset.POC_AUTH_SESSION_ENV_SUCCESS_COVERAGE_REFS[
            0
        ].split("::", 1)[1]
        success_ref_names = valid_poc_auth_success_ref_source(
            {
                env_ref_name: (
                    "    monkeypatch.setenv(\n"
                    "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                    "        'reviewer:env-reviewer=env-reviewer-token',\n"
                    "    )\n"
                    "    os.environ.clear()\n"
                    "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                    "    status, body = _post_review_event_on_connection(\n"
                    "        connection,\n"
                    "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                    "        role_token='env-reviewer-token',\n"
                    "    )\n"
                    "    assert status == 202\n"
                )
            }
        )

        self.assertFalse(
            self.poc_auth_session_coverage_is_present_for_source(
                f"{success_ref_names}\n{valid_poc_auth_fail_closed_ref_source()}",
                module_prefix="import os\n",
            )
        )

    def test_poc_auth_coverage_rejects_rebound_trusted_status_helper(
        self,
    ) -> None:
        env_ref_name = evaluate_dataset.POC_AUTH_SESSION_ENV_SUCCESS_COVERAGE_REFS[
            0
        ].split("::", 1)[1]
        success_ref_names = valid_poc_auth_success_ref_source(
            {
                env_ref_name: (
                    "    monkeypatch.setenv(\n"
                    "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                    "        'reviewer:env-reviewer=env-reviewer-token',\n"
                    "    )\n"
                    "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                    "    _post_review_event_on_connection = lambda *args, **kwargs: (202, {})\n"
                    "    status, body = _post_review_event_on_connection(\n"
                    "        connection,\n"
                    "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                    "        role_token='env-reviewer-token',\n"
                    "    )\n"
                    "    assert status == 202\n"
                )
            }
        )

        self.assertFalse(
            self.poc_auth_session_coverage_is_present_for_source(
                f"{success_ref_names}\n{valid_poc_auth_fail_closed_ref_source()}"
            )
        )

    def test_poc_auth_coverage_rejects_non_exact_bearer_helper(
        self,
    ) -> None:
        trusted_helper_source = (
            "def _post_review_event_on_connection(connection, audit_event, *, role_token):\n"
            "    payload = json.dumps({'audit_event': audit_event}).encode('utf-8')\n"
            "    connection.request(\n"
            "        'POST',\n"
            "        '/api/review-events',\n"
            "        body=payload,\n"
            "        headers={'Authorization': f'prefix Bearer {role_token}'},\n"
            "    )\n"
            "    response = connection.getresponse()\n"
            "    return response.status, {}\n"
        )

        self.assertFalse(
            self.poc_auth_session_coverage_is_present_for_source(
                f"{valid_poc_auth_success_ref_source(trusted_helper_source=trusted_helper_source)}\n"
                f"{valid_poc_auth_fail_closed_ref_source()}"
            )
        )

    def test_poc_acceptance_report_rejects_try_else_after_return(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_try_else = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    try:\n"
                        "        return\n"
                        "    except RuntimeError:\n"
                        "        raise\n"
                        "    else:\n"
                        "        monkeypatch.setenv(\n"
                        "            'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "            'reviewer:env-reviewer=env-reviewer-token',\n"
                        "        )\n"
                        "        connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "        status, body = _post_review_event_on_connection(\n"
                        "            connection,\n"
                        "            _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "            role_token='env-reviewer-token',\n"
                        "        )\n"
                        "        assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_try_else}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_auth_evidence_after_pytest_skip(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_unreachable_direct_auth = (
                valid_poc_auth_success_ref_source(
                    {
                        "test_poc_http_api_requires_admin_role_for_retry_job_event": (
                            "    pytest.skip('auth evidence disabled')\n"
                            "    connection.request(\n"
                            "        'POST',\n"
                            "        '/api/job-events',\n"
                            "        body=b'{\"action\":\"retry_conversion\"}',\n"
                            "        headers={'Authorization': 'Bearer admin-token'},\n"
                            "    )\n"
                            "    response = connection.getresponse()\n"
                            "    assert response.status == 202\n"
                        )
                    }
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_unreachable_direct_auth}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_auth_evidence_inside_pytest_raises(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    with pytest.raises(RuntimeError):\n"
                        "        monkeypatch.setenv(\n"
                        "            'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "            'reviewer:env-reviewer=env-reviewer-token',\n"
                        "        )\n"
                        "        status, body = _post_review_event_on_connection(\n"
                        "            None,\n"
                        "            _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "            role_token='env-reviewer-token',\n"
                        "        )\n"
                        "        assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_clears_stale_status_observations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_reassigned_status = (
                valid_poc_auth_success_ref_source(
                    {
                        "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                            "    monkeypatch.setenv(\n"
                            "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                            "        'reviewer:env-reviewer=env-reviewer-token',\n"
                            "    )\n"
                            "    status = _post_review_event_on_connection(\n"
                            "        None,\n"
                            "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                            "        role_token='env-reviewer-token',\n"
                            "    )\n"
                            "    status = 202\n"
                            "    assert status == 202\n"
                        )
                    }
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_reassigned_status}\n"
                f"{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_clears_status_observations_on_setattr(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    monkeypatch.setenv(\n"
                        "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "        'reviewer:env-reviewer=env-reviewer-token',\n"
                        "    )\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/review-events',\n"
                        "        body=json.dumps({'audit_event': _review_audit_event(conversion_id='conversion-env-auth')}).encode('utf-8'),\n"
                        "        headers={'Authorization': 'Bearer env-reviewer-token'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    setattr(response, 'status', 202)\n"
                        "    assert response.status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_clears_direct_auth_on_server_rebind(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_requires_admin_role_for_retry_job_event": (
                        "    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)\n"
                        "    action = 'retry_conversion'\n"
                        "    body = json.dumps({'action': action}).encode('utf-8')\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/job-events',\n"
                        "        body=body,\n"
                        "        headers={'Authorization': 'Bearer admin-token'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    assert response.status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_invalidates_direct_auth_on_token_map_mutation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_requires_admin_role_for_retry_job_event": (
                        "    server.local_auth_tokens.clear()\n"
                        "    action = 'retry_conversion'\n"
                        "    body = json.dumps({'action': action}).encode('utf-8')\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/job-events',\n"
                        "        body=body,\n"
                        "        headers={'Authorization': 'Bearer admin-token'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    assert response.status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_drains_pending_auth_request_on_nested_response(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names_with_drained_response = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    monkeypatch.setenv(\n"
                        "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "        'reviewer:env-reviewer=env-reviewer-token',\n"
                        "    )\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/review-events',\n"
                        "        body=b'{\"conversion_id\":\"conversion-env-auth\"}',\n"
                        "        headers={'Authorization': 'Bearer env-reviewer-token'},\n"
                        "    )\n"
                        "    body = json.loads(connection.getresponse().read().decode('utf-8'))\n"
                        "    connection.request('POST', '/api/review-events')\n"
                        "    response = connection.getresponse()\n"
                        "    assert response.status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names_with_drained_response}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_tracked_auth_evidence_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n"
                "    monkeypatch.setenv(\n"
                "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                "        'reviewer:env-reviewer=reviewer-token',\n"
                "    )\n"
                "    _post_review_event_on_connection(None, None, role_token='reviewer-token')\n"
                "    status = 202\n"
                "    assert status == 202\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_SUCCESS_COVERAGE_REFS
            )
            fail_closed_ref_names = "\n".join(
                f"def {ref.split('::', 1)[1]}():\n    assert 401 == 401\n"
                for ref in evaluate_dataset.POC_AUTH_SESSION_FAIL_CLOSED_COVERAGE_REFS
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )
            auth_input_paths = {
                path.resolve()
                for path in evaluate_dataset.poc_auth_session_coverage_input_paths(
                    temp_root
                )
            }

            def fake_tracked(path: Path, repo_root: Path) -> bool:
                return path.resolve() not in auth_input_paths

            with mock.patch.object(
                evaluate_dataset,
                "poc_acceptance_tracked_repo_path",
                side_effect=fake_tracked,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_evidence_inputs_tracked"
            ]
        )

    def test_poc_acceptance_report_requires_live_poc_server_auth_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source().replace(
                "    server = ThreadingHTTPServer(('127.0.0.1', 0), PocWebRequestHandler)\n",
                "    server = object()\n",
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_ambiguous_auth_token_expressions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    monkeypatch.setenv(\n"
                        "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "        'reviewer:env-reviewer=env-reviewer-token',\n"
                        "    )\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    status, body = _post_review_event_on_connection(\n"
                        "        connection,\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token' if use_env else 'viewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_trusted_helper_observed_status(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                trusted_helper_source=(
                    "def _post_review_event_on_connection(connection, audit_event, *, role_token):\n"
                    "    payload = json.dumps({'audit_event': audit_event}).encode('utf-8')\n"
                    "    connection.request(\n"
                    "        'POST',\n"
                    "        '/api/review-events',\n"
                    "        body=payload,\n"
                    "        headers={'Authorization': f'Bearer {role_token}'},\n"
                    "    )\n"
                    "    response = connection.getresponse()\n"
                    "    return 202, {}\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_shadowed_os_environ_auth_setup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    class FakeOS:\n"
                        "        environ = {}\n"
                        "    os = FakeOS()\n"
                        "    os.environ['VERIDOC_LOCAL_AUTH_TOKENS'] = (\n"
                        "        'reviewer:env-reviewer=env-reviewer-token'\n"
                        "    )\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    status, body = _post_review_event_on_connection(\n"
                        "        connection,\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_shadowed_http_connection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success": (
                        "    class FakeConnection:\n"
                        "        def request(self, *args, **kwargs):\n"
                        "            return None\n"
                        "        def getresponse(self):\n"
                        "            return type('Response', (), {'status': 202})()\n"
                        "    monkeypatch.setenv(\n"
                        "        'VERIDOC_LOCAL_AUTH_TOKENS',\n"
                        "        'reviewer:env-reviewer=env-reviewer-token',\n"
                        "    )\n"
                        "    HTTPConnection = FakeConnection\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    status, body = _post_review_event_on_connection(\n"
                        "        connection,\n"
                        "        _review_audit_event(conversion_id='conversion-env-auth'),\n"
                        "        role_token='env-reviewer-token',\n"
                        "    )\n"
                        "    assert status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_rebound_trusted_helper_connection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                trusted_helper_source=(
                    "def _post_review_event_on_connection(connection, audit_event, *, role_token):\n"
                    "    class FakeConnection:\n"
                    "        def request(self, *args, **kwargs):\n"
                    "            return None\n"
                    "        def getresponse(self):\n"
                    "            return type('Response', (), {'status': 202})()\n"
                    "    payload = json.dumps({'audit_event': audit_event}).encode('utf-8')\n"
                    "    connection = FakeConnection()\n"
                    "    connection.request(\n"
                    "        'POST',\n"
                    "        '/api/review-events',\n"
                    "        body=payload,\n"
                    "        headers={'Authorization': f'Bearer {role_token}'},\n"
                    "    )\n"
                    "    response = connection.getresponse()\n"
                    "    return response.status, {}\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_helper_response_before_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                trusted_helper_source=(
                    "def _post_review_event_on_connection(connection, audit_event, *, role_token):\n"
                    "    response = connection.getresponse()\n"
                    "    payload = json.dumps({'audit_event': audit_event}).encode('utf-8')\n"
                    "    connection.request(\n"
                    "        'POST',\n"
                    "        '/api/review-events',\n"
                    "        body=payload,\n"
                    "        headers={'Authorization': f'Bearer {role_token}'},\n"
                    "    )\n"
                    "    return response.status, {}\n"
                )
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_rejects_ambiguous_payload_literals(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source(
                {
                    "test_poc_http_api_requires_admin_role_for_retry_job_event": (
                        "    action = 'retry_conversion' if False else 'other'\n"
                        "    body = json.dumps({'action': action}).encode('utf-8')\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/job-events',\n"
                        "        body=body,\n"
                        "        headers={'Authorization': 'Bearer admin-token'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    assert response.status == 202\n"
                    )
                }
            )
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source()
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_requires_response_body_assertions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source(
                {
                    "test_poc_http_api_rejects_read_only_review_role_before_parsing_payload": (
                        "    payload = b'{not valid json'\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/review-events',\n"
                        "        body=payload,\n"
                        "        headers={'Authorization': 'Bearer viewer-token'},\n"
                        "    )\n"
                        "    response = connection.getresponse()\n"
                        "    body = dict(json.loads(response.read().decode('utf-8')), error='forbidden')\n"
                        "    assert response.status == 403\n"
                        "    assert body == {'error': 'forbidden'}\n"
                    )
                }
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_report_clears_pending_request_on_connection_rebind(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tests").mkdir()
            (temp_root / "README.md").write_text(
                "## Local PoC API authentication\n"
                "Set VERIDOC_LOCAL_AUTH_TOKENS for local role tokens.\n",
                encoding="utf-8",
            )
            success_ref_names = valid_poc_auth_success_ref_source()
            fail_closed_ref_names = valid_poc_auth_fail_closed_ref_source(
                {
                    "test_poc_http_api_authenticates_review_events_before_parsing_payload": (
                        "    payload = b'{not valid json'\n"
                        "    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)\n"
                        "    connection.request(\n"
                        "        'POST',\n"
                        "        '/api/review-events',\n"
                        "        body=payload,\n"
                        "        headers={'Content-Type': 'application/json'},\n"
                        "    )\n"
                        "    connection = FakeConnection()\n"
                        "    response = connection.getresponse()\n"
                        "    body = json.loads(response.read().decode('utf-8'))\n"
                        "    assert response.status == 401\n"
                        "    assert body == {'error': 'auth_required'}\n"
                    )
                }
            )
            (temp_root / "tests" / "test_poc_web_api.py").write_text(
                f"{success_ref_names}\n{fail_closed_ref_names}\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                evaluate_dataset,
                "poc_auth_session_coverage_inputs_tracked_in_repo",
                return_value=True,
            ):
                payload = self.poc_acceptance_payload(harness_repo_root=temp_root)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("unknown", rows["security"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["security"][
                "authenticated_poc_api_session_checked"
            ]
        )

    def test_poc_acceptance_reproducibility_tracks_auth_evidence_inputs(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        llm_report = evaluate_dataset.evaluate_llm_stability_report(
            LLM_STABILITY_RUNS_PATH,
            POC_COMPARISON_PATH,
        )
        harness = evaluate_dataset.P9HarnessReport(
            manifest=evaluate_dataset.DEFAULT_P9_HARNESS_MANIFEST,
            results=tuple(payload["p9_harness_results"]),
            llm_stability=llm_report.llm_stability,
            poc_mode_comparison=llm_report.poc_mode_comparison,
            llm_stability_source=evaluate_dataset.DEFAULT_LLM_STABILITY_RUNS,
            poc_comparison_source=evaluate_dataset.DEFAULT_POC_COMPARISON,
            repo_root=REPO_ROOT,
        )

        input_paths = evaluate_dataset.poc_acceptance_p9_input_paths(harness)

        self.assertIn(Path("README.md"), input_paths)
        self.assertIn(Path("tests/test_poc_web_api.py"), input_paths)

    def test_poc_acceptance_report_preserves_custom_evidence_paths(self) -> None:
        llm_stability_source = Path("datasets/custom/stability_runs.json")
        poc_comparison_source = Path("datasets/custom/comparison.json")

        payload = self.poc_acceptance_payload(
            llm_stability_source=llm_stability_source,
            poc_comparison_source=poc_comparison_source,
        )

        self.assertEqual(
            str(llm_stability_source), payload["evidence"]["llm_stability_runs"]
        )
        self.assertEqual(
            str(poc_comparison_source), payload["evidence"]["poc_mode_comparison"]
        )

    def test_poc_acceptance_report_records_actual_generation_command(self) -> None:
        command = evaluate_dataset.poc_acceptance_generation_command(
            manifest_path=Path("datasets/custom/p9_manifest.json"),
            llm_stability_runs_path=Path("datasets/custom/stability_runs.json"),
            poc_comparison_path=Path("datasets/custom/comparison.json"),
        )

        self.assertEqual(
            "python3 scripts/evaluate_dataset.py --poc-acceptance-report "
            "datasets/custom/p9_manifest.json --llm-stability-runs "
            "datasets/custom/stability_runs.json --poc-comparison "
            "datasets/custom/comparison.json",
            command,
        )

    def test_poc_acceptance_report_includes_matrix_evidence_rows(self) -> None:
        payload = self.poc_acceptance_payload()

        self.assertIn("p9_harness", payload)
        self.assertIn("p9_harness_results", payload)
        self.assertIn("matrix_evidence", payload)
        self.assertIn("poc_mode_comparison", payload)
        self.assertEqual(
            payload["p9_harness_results"],
            payload["p9_harness"]["results"],
        )
        self.assertEqual(
            payload["p9_harness_summary"],
            payload["p9_harness"]["summary"],
        )
        first_harness_result = payload["p9_harness"]["results"][0]
        for evidence_field in (
            "artifact_expectations_met",
            "audit_present",
            "representative_mode",
            "sample_category",
        ):
            self.assertIn(evidence_field, first_harness_result)
        self.assertTrue(
            any(
                row["sample_category"] == "record_pdf"
                for row in payload["p9_harness_results"]
            )
        )
        self.assertEqual(
            ["no_llm", "standard", "high_quality"],
            [mode["mode"] for mode in payload["poc_mode_comparison"]["modes"]],
        )
        self.assertEqual(
            [],
            payload["matrix_evidence"]["functionality"][
                "missing_source_categories"
            ],
        )
        self.assertIn(
            "manual_correction_time",
            payload["matrix_evidence"]["functionality"],
        )
        self.assertIn(
            "rows",
            payload["matrix_evidence"]["structured_output"],
        )

    def test_poc_acceptance_report_matrix_refs_resolve_to_payload_evidence(
        self,
    ) -> None:
        base_payload = self.poc_acceptance_payload()
        results = list(base_payload["p9_harness_results"])
        results[0] = {
            **results[0],
            "ok": False,
            "failure_reason": "no_llm scenario LLM status 'enabled' is not disabled",
            "llm_status": "enabled",
        }
        payload = self.poc_acceptance_payload(
            results=results,
            unstable_example_count=1,
        )

        def resolve_ref(ref: str) -> list[object]:
            if "::" in ref:
                public_path = REPO_ROOT / ref.split("::", 1)[0]
                return [public_path] if public_path.exists() else []
            if " " in ref:
                public_path = REPO_ROOT / ref.split(" ", 1)[0]
                return [public_path] if public_path.exists() else []
            values: list[object] = [payload]
            for part in ref.split("."):
                next_values: list[object] = []
                if part.endswith("[]"):
                    key = part[:-2]
                    for value in values:
                        if isinstance(value, dict) and isinstance(value.get(key), list):
                            next_values.extend(value[key])
                    values = next_values
                    continue
                for value in values:
                    if isinstance(value, dict) and part in value:
                        next_values.append(value[part])
                values = next_values
            return values

        unresolved_refs = [
            ref
            for row in payload["acceptance_matrix"]
            for ref in row["evidence_refs"]
            if not resolve_ref(ref)
        ]

        self.assertEqual([], unresolved_refs)

    def test_poc_acceptance_report_fail_closed_matrix_keeps_backing_evidence(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        results = list(payload["p9_harness_results"])
        for result in results:
            if result["sample_category"] == "record_pdf":
                result["sample_category"] = "text_pdf"
                break

        payload = self.poc_acceptance_payload(
            results=results,
            unstable_example_count=1,
            llm_plan_agreement_rate=2 / 3,
            llm_confirmed_value_agreement_rate=2 / 3,
            llm_schema_failure_rate=2 / 3,
            llm_deterministic_fallback_rate=1 / 3,
        )

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual({"fail": 2, "pass": 6, "unknown": 0}, payload["criterion_status_counts"])
        self.assertEqual("fail", rows["functionality"]["status"])
        self.assertIn("record_pdf", rows["functionality"]["evidence"])
        self.assertIn(
            "p9_harness.results[].sample_category",
            rows["functionality"]["evidence_refs"],
        )
        self.assertEqual(
            ["record_pdf"],
            payload["matrix_evidence"]["functionality"][
                "missing_source_categories"
            ],
        )
        self.assertEqual("fail", rows["llm_control"]["status"])
        self.assertIn("threshold failures", rows["llm_control"]["evidence"])
        self.assertEqual(
            [
                "plan_agreement_rate",
                "confirmed_value_agreement_rate",
                "schema_failure_rate",
                "deterministic_fallback_rate",
                "unstable_example_count",
            ],
            payload["matrix_evidence"]["llm_control"]["threshold_failures"],
        )
        self.assertEqual(
            0,
            payload["matrix_evidence"]["llm_control"][
                "external_ai_api_guard_violation_count"
            ],
        )
        self.assertEqual("pass", rows["security"]["status"])
        self.assertEqual("fail", payload["overall_status"])
        self.assertTrue(
            all(
                "artifact_expectations_met" in result and "audit_present" in result
                for result in payload["p9_harness_results"]
            )
        )
        self.assertEqual(
            ["no_llm", "standard", "high_quality"],
            [mode["mode"] for mode in payload["poc_mode_comparison"]["modes"]],
        )

    def test_poc_acceptance_report_fails_llm_control_on_external_transmission(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload(llm_external_violation_count=1)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        conditions = {
            condition["condition_id"]: condition
            for condition in payload["fail_closed_conditions"]
        }
        self.assertEqual("fail", rows["llm_control"]["status"])
        self.assertEqual("fail", rows["security"]["status"])
        self.assertIn("External AI API guard violations: 1", rows["security"]["evidence"])
        self.assertEqual("fail", conditions["llm_correction_or_completion"]["status"])
        self.assertEqual("fail", conditions["external_transmission"]["status"])
        self.assertEqual("fail", payload["overall_status"])

    def test_poc_acceptance_report_counts_harness_external_transmission_in_llm_control(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        results = list(payload["p9_harness_results"])
        results[0] = {
            **results[0],
            "external_ai_api_guard_violation": True,
        }

        payload = self.poc_acceptance_payload(results=results)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        conditions = {
            condition["condition_id"]: condition
            for condition in payload["fail_closed_conditions"]
        }
        self.assertEqual("fail", rows["llm_control"]["status"])
        self.assertIn(
            "External AI API guard violations: 1",
            rows["llm_control"]["evidence"],
        )
        self.assertEqual("fail", conditions["llm_correction_or_completion"]["status"])
        self.assertEqual("fail", conditions["external_transmission"]["status"])
        self.assertEqual(
            ["external_ai_api_guard_violation_count"],
            payload["matrix_evidence"]["llm_control"]["threshold_failures"],
        )
        self.assertEqual(
            1,
            payload["matrix_evidence"]["llm_control"][
                "external_ai_api_guard_violation_count"
            ],
        )

    def test_poc_acceptance_report_feeds_threshold_failures_to_follow_ups(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload(llm_plan_agreement_rate=0.5)

        self.assertTrue(
            any(
                candidate["title"]
                == "Resolve LLM stability acceptance threshold failures"
                and "plan_agreement_rate" in candidate["reason"]
                for candidate in payload["follow_up_issue_candidates"]
            )
        )
        self.assertEqual(
            ["plan_agreement_rate"],
            payload["matrix_evidence"]["llm_control"]["threshold_failures"],
        )

    def test_poc_acceptance_report_fails_structured_output_on_duplicate_primary_artifacts(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        results = list(payload["p9_harness_results"])
        results[0] = {
            **results[0],
            "ok": False,
            "artifact_expectations_met": True,
            "artifact_count": 2,
            "failure_reason": "expected exactly one primary artifact, got 2",
        }

        payload = self.poc_acceptance_payload(results=results)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        conditions = {
            condition["condition_id"]: condition
            for condition in payload["fail_closed_conditions"]
        }
        self.assertEqual("fail", rows["structured_output"]["status"])
        self.assertEqual("fail", conditions["unknown_source_normal_output"]["status"])
        self.assertIn("1 runs failed primary artifact structure", rows["structured_output"]["evidence"])
        self.assertEqual(
            "expected exactly one primary artifact, got 2",
            payload["matrix_evidence"]["structured_output"]["rows"][0][
                "failure_reason"
            ],
        )

    def test_poc_acceptance_report_fails_llm_control_on_harness_scenario_violation(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        results = list(payload["p9_harness_results"])
        results[0] = {
            **results[0],
            "ok": False,
            "llm_scenario": "no_llm",
            "llm_status": "enabled",
            "failure_reason": "no_llm scenario LLM status 'enabled' is not disabled",
        }

        payload = self.poc_acceptance_payload(results=results)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        conditions = {
            condition["condition_id"]: condition
            for condition in payload["fail_closed_conditions"]
        }
        self.assertEqual("fail", rows["llm_control"]["status"])
        self.assertEqual("fail", conditions["llm_correction_or_completion"]["status"])
        self.assertIn("harness LLM scenario failures: 1", rows["llm_control"]["evidence"])
        self.assertIn(
            "p9_harness.results[].llm_status",
            rows["llm_control"]["evidence_refs"],
        )

    def test_poc_acceptance_report_excludes_fail_closed_rows_from_llm_control(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        results = list(payload["p9_harness_results"])
        results[0] = {
            **results[0],
            "llm_scenario": "llm_requested",
            "llm_requested": True,
            "llm_status": "not_run",
            "ok": True,
            "fail_closed": True,
            "mvp_before_gate_revision": "p9-mvp-before-pdf-eval-dependency-gate",
            "artifact_expectations_met": False,
            "artifact_expectation_failures": [
                "fail-closed MVP-before gate revision: "
                "p9-mvp-before-pdf-eval-dependency-gate"
            ],
            "failure_reason": "optional PDF dependency unavailable",
        }

        payload = self.poc_acceptance_payload(results=results)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("pass", rows["llm_control"]["status"])
        self.assertIn("harness LLM scenario failures: 0", rows["llm_control"]["evidence"])
        self.assertEqual(
            [],
            payload["matrix_evidence"]["llm_control"]["scenario_failures"],
        )
        self.assertEqual(
            "p9-mvp-before-pdf-eval-dependency-gate",
            payload["matrix_evidence"]["structured_output"]["rows"][0][
                "mvp_before_gate_revision"
            ],
        )
        self.assertTrue(
            any(
                candidate["title"]
                == "Resolve fail-closed P9 MVP-before gate revisions"
                and "p9-mvp-before-pdf-eval-dependency-gate"
                in candidate["reason"]
                for candidate in payload["follow_up_issue_candidates"]
            )
        )
        self.assertFalse(
            any(
                candidate["title"]
                == "Resolve failing P9 representative conversion harness rows"
                for candidate in payload["follow_up_issue_candidates"]
            )
        )
        self.assertTrue(
            any(
                limitation["id"] == f"p9_fail_closed_gate_{results[0]['sample_id']}"
                and limitation["mvp_before_gate_revision"]
                == "p9-mvp-before-pdf-eval-dependency-gate"
                for limitation in payload["known_limitations"]
            )
        )

    def test_poc_acceptance_report_keeps_fail_closed_rows_out_of_generic_follow_up(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        base_results = list(payload["p9_harness_results"])
        gate_results = []
        for index, result in enumerate(base_results):
            gate_results.append(
                {
                    **result,
                    "sample_id": f"gate-sample-{index}",
                    "ok": False,
                    "fail_closed": True,
                    "audit_present": False,
                    "mvp_before_gate_revision": (
                        "p9-mvp-before-placeholder-fixture-gate"
                    ),
                    "artifact_expectations_met": False,
                    "artifact_expectation_failures": [
                        "fail-closed MVP-before gate revision: "
                        "p9-mvp-before-placeholder-fixture-gate"
                    ],
                    "failure_reason": (
                        "representative fixture path is unavailable; "
                        "conversion audit missing"
                    ),
                }
            )
        non_gate_failure = {
            **base_results[0],
            "sample_id": "non-gate-failure",
            "ok": False,
            "fail_closed": False,
            "audit_present": False,
            "mvp_before_gate_revision": None,
            "artifact_expectations_met": False,
            "artifact_expectation_failures": ["artifact hash missing"],
            "failure_reason": "artifact hash missing",
        }
        results = [*gate_results, non_gate_failure]

        payload = self.poc_acceptance_payload(results=results)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["functionality"]["status"])
        self.assertEqual("fail", rows["logs"]["status"])
        self.assertEqual(
            [
                "Resolve fail-closed P9 MVP-before gate revisions",
                "Resolve failing P9 representative conversion harness rows",
                "Require audit evidence for all P9 harness outcomes",
            ],
            [
                candidate["title"]
                for candidate in payload["follow_up_issue_candidates"]
                if candidate["title"]
                in {
                    "Resolve fail-closed P9 MVP-before gate revisions",
                    "Resolve failing P9 representative conversion harness rows",
                    "Require audit evidence for all P9 harness outcomes",
                }
            ],
        )
        self.assertTrue(
            any(
                candidate["title"]
                == "Resolve failing P9 representative conversion harness rows"
                and "1 harness rows are not acceptance-ready." in candidate["reason"]
                for candidate in payload["follow_up_issue_candidates"]
            )
        )
        self.assertTrue(
            any(
                limitation["id"] == "p9_harness_failure_non-gate-failure"
                for limitation in payload["known_limitations"]
            )
        )
        self.assertFalse(
            any(
                str(limitation["id"]).startswith("p9_harness_failure_gate-sample-")
                for limitation in payload["known_limitations"]
            )
        )
        self.assertTrue(
            all(
                any(
                    limitation["id"] == f"p9_fail_closed_gate_gate-sample-{index}"
                    for limitation in payload["known_limitations"]
                )
                for index in range(len(gate_results))
            )
        )

    def test_poc_acceptance_report_fails_llm_control_from_harness_fields(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        results = list(payload["p9_harness_results"])
        results[0] = {
            **results[0],
            "ok": False,
            "llm_scenario": "no_llm",
            "llm_status": "enabled",
            "failure_reason": "primary artifact expectation failed",
        }

        payload = self.poc_acceptance_payload(results=results)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["llm_control"]["status"])
        self.assertIn("harness LLM scenario failures: 1", rows["llm_control"]["evidence"])
        self.assertEqual(
            "enabled",
            payload["matrix_evidence"]["llm_control"]["scenario_failures"][0][
                "llm_status"
            ],
        )

    def test_poc_acceptance_report_requires_representative_mode_coverage(
        self,
    ) -> None:
        results = [
            {
                "sample_id": "sample-word-to-excel",
                "conversion_mode": "word_to_excel",
                "representative_mode": "word_to_excel",
                "llm_scenario": "no_llm",
                "ok": True,
                "artifact_expectations_met": True,
                "audit_present": True,
                "external_ai_api_guard_violation": False,
            }
        ]

        payload = self.poc_acceptance_payload(results=results)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["functionality"]["status"])
        self.assertIn("missing representative modes", rows["functionality"]["evidence"])
        self.assertEqual("fail", payload["overall_status"])

    def test_poc_acceptance_report_requires_source_category_coverage(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        results = list(payload["p9_harness_results"])
        for result in results:
            if result["sample_category"] == "record_pdf":
                result["sample_category"] = "text_pdf"
                break

        payload = self.poc_acceptance_payload(results=results)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["functionality"]["status"])
        self.assertIn("missing source categories", rows["functionality"]["evidence"])
        self.assertIn("record_pdf", rows["functionality"]["evidence"])

    def test_poc_acceptance_report_counts_only_successful_rows_for_coverage(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        results = list(payload["p9_harness_results"])
        for index, result in enumerate(results):
            if result["sample_category"] == "record_pdf":
                results[index] = {
                    **result,
                    "ok": False,
                    "failure_reason": "representative fixture path is unavailable",
                    "artifact_expectations_met": False,
                }
                break

        payload = self.poc_acceptance_payload(results=results)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        functionality_evidence = payload["matrix_evidence"]["functionality"]
        self.assertEqual("fail", rows["functionality"]["status"])
        self.assertIn("record_pdf", functionality_evidence["missing_source_categories"])
        self.assertIn("pdf_to_word", functionality_evidence["missing_representative_modes"])
        self.assertNotIn("record_pdf", functionality_evidence["observed_source_categories"])

    def test_poc_acceptance_report_requires_target_mode_traceability(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload(
            source_linkage_rates={
                "no_llm": 1.0,
                "standard": 1.0,
                "high_quality": 0.5,
            }
        )

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["traceability"]["status"])
        self.assertIn(
            "High-quality PoC source linkage rate: 0.500",
            rows["traceability"]["evidence"],
        )
        self.assertEqual("fail", payload["overall_status"])

    def test_poc_acceptance_report_surfaces_failed_manual_correction_target(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload(manual_correction_target_met=False)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["functionality"]["status"])
        self.assertIn(
            "manual correction target met: False",
            rows["functionality"]["evidence"],
        )
        self.assertTrue(
            any(
                candidate["title"]
                == "Close the PoC manual-correction-time acceptance gap"
                for candidate in payload["follow_up_issue_candidates"]
            )
        )

    def test_poc_acceptance_report_fails_reproducibility_without_clean_commit(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload(commit="unknown", commit_is_clean=False)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["reproducibility"]["status"])
        self.assertEqual("unknown", payload["tested_environment"]["commit"])
        self.assertFalse(payload["tested_environment"]["commit_is_clean"])

    def test_poc_acceptance_report_fails_reproducibility_for_external_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            external_source = Path(temp_dir) / "external_llm_runs.json"

            payload = self.poc_acceptance_payload(
                llm_stability_source=external_source,
            )

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["reproducibility"]["status"])
        self.assertIn(
            "evidence inputs tracked in manifest repo: False",
            rows["reproducibility"]["evidence"],
        )
        self.assertFalse(
            payload["matrix_evidence"]["reproducibility"][
                "evidence_inputs_tracked_in_manifest_repo"
            ]
        )

    def test_poc_acceptance_report_fails_reproducibility_for_untracked_fixture(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        results = list(payload["p9_harness_results"])
        results[0] = {
            **results[0],
            "path": "datasets/fixtures/word/untracked-fixture.docx",
        }

        payload = self.poc_acceptance_payload(results=results)

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["reproducibility"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["reproducibility"][
                "evidence_inputs_tracked_in_manifest_repo"
            ]
        )

    def test_poc_acceptance_report_fails_reproducibility_for_untracked_comparison_input(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(REPO_ROOT / "datasets", temp_root / "datasets")
            (temp_root / ".gitignore").write_text(
                "datasets/gold/high_risk_labels_v0.json\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "init"],
                cwd=temp_root,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "add", "."],
                cwd=temp_root,
                check=True,
                capture_output=True,
                text=True,
            )

            payload = self.poc_acceptance_payload(
                manifest=temp_root / "datasets" / "poc_evaluation_manifest_v1.json",
                llm_stability_source=(
                    temp_root / "datasets" / "gold" / "llm_stability_runs_v0.json"
                ),
                poc_comparison_source=(
                    temp_root / "datasets" / "gold" / "poc_mode_comparison_v1.json"
                ),
            )

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["reproducibility"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["reproducibility"][
                "evidence_inputs_tracked_in_manifest_repo"
            ]
        )

    def test_poc_acceptance_report_fails_reproducibility_for_dirty_evaluator(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload(
            evaluator_commit="evaluator-head",
            evaluator_commit_is_clean=False,
        )

        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["reproducibility"]["status"])
        self.assertEqual(
            "evaluator-head",
            payload["tested_environment"]["evaluator_commit"],
        )
        self.assertFalse(
            payload["matrix_evidence"]["reproducibility"][
                "evaluator_commit_is_clean"
            ]
        )

    def test_git_cleanliness_can_ignore_generated_report_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            (temp_root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=temp_root, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=temp_root, check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=VeriDoc Test",
                    "-c",
                    "user.email=veridoc-test@example.invalid",
                    "commit",
                    "-m",
                    "seed",
                ],
                cwd=temp_root,
                check=True,
                capture_output=True,
            )
            report_output = temp_root / "reports" / "poc_acceptance.json"
            report_output.parent.mkdir()
            report_output.write_text("{}", encoding="utf-8")

            self.assertFalse(evaluate_dataset.current_git_worktree_clean(temp_root))
            self.assertTrue(
                evaluate_dataset.current_git_worktree_clean(
                    temp_root,
                    ignored_paths=(report_output,),
                )
            )
            self.assertTrue(
                evaluate_dataset.current_git_worktree_clean(
                    temp_root,
                    include_untracked=False,
                )
            )
            (temp_root / "tracked.txt").write_text("modified\n", encoding="utf-8")
            self.assertFalse(
                evaluate_dataset.current_git_worktree_clean(
                    temp_root,
                    include_untracked=False,
                )
            )
            (temp_root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            (temp_root / "other.json").write_text("{}", encoding="utf-8")
            self.assertFalse(
                evaluate_dataset.current_git_worktree_clean(
                    temp_root,
                    ignored_paths=(report_output,),
                )
            )

    def test_poc_acceptance_report_cleanliness_counts_untracked_files(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        llm_report = evaluate_dataset.evaluate_llm_stability_report(
            LLM_STABILITY_RUNS_PATH,
            POC_COMPARISON_PATH,
        )
        harness = evaluate_dataset.P9HarnessReport(
            manifest=POC_EVALUATION_MANIFEST_PATH,
            results=tuple(payload["p9_harness_results"]),
            llm_stability=llm_report.llm_stability,
            poc_mode_comparison=llm_report.poc_mode_comparison,
            llm_stability_source=LLM_STABILITY_RUNS_PATH,
            poc_comparison_source=POC_COMPARISON_PATH,
        )
        clean_calls: list[dict[str, object]] = []

        def fake_clean(
            repo_root: Path,
            *,
            ignored_paths: tuple[Path, ...] = (),
            include_untracked: bool = True,
        ) -> bool:
            clean_calls.append(
                {
                    "repo_root": repo_root,
                    "ignored_paths": ignored_paths,
                    "include_untracked": include_untracked,
                }
            )
            return False if include_untracked else True

        generated_report = REPO_ROOT / "reports" / "poc_acceptance.json"
        with (
            mock.patch.object(
                evaluate_dataset,
                "evaluate_p9_harness",
                return_value=harness,
            ),
            mock.patch.object(
                evaluate_dataset,
                "current_git_commit",
                return_value="tracked-head",
            ),
            mock.patch.object(
                evaluate_dataset,
                "current_stdout_path",
                return_value=generated_report,
            ),
            mock.patch.object(
                evaluate_dataset,
                "current_git_worktree_clean",
                side_effect=fake_clean,
            ),
        ):
            report = evaluate_dataset.build_poc_acceptance_report(
                POC_EVALUATION_MANIFEST_PATH,
            )

        self.assertGreaterEqual(len(clean_calls), 2)
        self.assertTrue(
            all(call["include_untracked"] is True for call in clean_calls)
        )
        self.assertTrue(
            all(generated_report in call["ignored_paths"] for call in clean_calls)
        )
        payload = report.as_dict()
        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["reproducibility"]["status"])

    def test_poc_acceptance_report_build_path_rejects_external_evidence(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        llm_report = evaluate_dataset.evaluate_llm_stability_report(
            LLM_STABILITY_RUNS_PATH,
            POC_COMPARISON_PATH,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            external_source = Path(temp_dir) / "external_llm_runs.json"
            harness = evaluate_dataset.P9HarnessReport(
                manifest=POC_EVALUATION_MANIFEST_PATH,
                results=tuple(payload["p9_harness_results"]),
                llm_stability=llm_report.llm_stability,
                poc_mode_comparison=llm_report.poc_mode_comparison,
                llm_stability_source=external_source,
                poc_comparison_source=POC_COMPARISON_PATH,
            )
            with (
                mock.patch.object(
                    evaluate_dataset,
                    "evaluate_p9_harness",
                    return_value=harness,
                ) as mocked_harness,
                mock.patch.object(
                    evaluate_dataset,
                    "current_git_commit",
                    return_value="tracked-head",
                ),
                mock.patch.object(
                    evaluate_dataset,
                    "current_git_worktree_clean",
                    return_value=True,
                ),
            ):
                report = evaluate_dataset.build_poc_acceptance_report(
                    POC_EVALUATION_MANIFEST_PATH,
                    llm_stability_runs_path=external_source,
                    poc_comparison_path=POC_COMPARISON_PATH,
                )

        mocked_harness.assert_called_once_with(
            POC_EVALUATION_MANIFEST_PATH,
            llm_stability_runs_path=external_source,
            poc_comparison_path=POC_COMPARISON_PATH,
        )
        payload = report.as_dict()
        rows = {row["criterion_id"]: row for row in payload["acceptance_matrix"]}
        self.assertEqual("fail", rows["reproducibility"]["status"])
        self.assertFalse(
            payload["matrix_evidence"]["reproducibility"][
                "evidence_inputs_tracked_in_manifest_repo"
            ]
        )

    def test_poc_acceptance_report_resolves_explicit_inputs_from_manifest_repo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(REPO_ROOT / "datasets", temp_root / "datasets")
            custom_manifest_path = temp_root / "datasets" / "custom_p9_manifest.json"
            shutil.copy2(POC_EVALUATION_MANIFEST_PATH, custom_manifest_path)
            relative_stability = Path("datasets/custom/stability_runs.json")
            relative_comparison = Path("datasets/custom/comparison.json")
            harness = evaluate_dataset.P9HarnessReport(
                manifest=custom_manifest_path,
                results=(),
                llm_stability=evaluate_dataset.LLMStabilityMetrics(
                    input_id="synthetic",
                    run_count=1,
                    plan_agreement_rate=1.0,
                    confirmed_value_agreement_rate=1.0,
                    schema_failure_rate=0.0,
                    repair_success_rate=1.0,
                    deterministic_fallback_rate=0.0,
                    external_ai_api_guard_violation_count=0,
                    distinct_plan_count=1,
                    distinct_confirmed_value_count=1,
                    unstable_example_count=0,
                    unstable_examples=(),
                ),
                poc_mode_comparison=evaluate_dataset.PoCComparisonMetrics(
                    mode_count=0,
                    high_risk_false_auto_confirmed_count=0,
                    high_risk_false_auto_confirmed_target=0,
                    target_met=True,
                    manual_correction_time=evaluate_dataset.ManualCorrectionTimeMetrics(
                        measurement_method="synthetic",
                        baseline_minutes=1.0,
                        assisted_minutes=0.5,
                        reduction_minutes=0.5,
                        reduction_rate=0.5,
                        target_reduction_rate=0.5,
                        target_met=True,
                    ),
                    modes=(),
                    mode_diffs=(),
                ),
                llm_stability_source=temp_root / relative_stability,
                poc_comparison_source=temp_root / relative_comparison,
            )
            with (
                mock.patch.object(
                    evaluate_dataset,
                    "evaluate_p9_harness",
                    return_value=harness,
                ) as mocked_harness,
                mock.patch.object(
                    evaluate_dataset,
                    "current_git_commit",
                    return_value="tracked-head",
                ),
                mock.patch.object(
                    evaluate_dataset,
                    "current_git_worktree_clean",
                    return_value=True,
                ),
            ):
                evaluate_dataset.build_poc_acceptance_report(
                    custom_manifest_path,
                    llm_stability_runs_path=relative_stability,
                    poc_comparison_path=relative_comparison,
                )

        mocked_harness.assert_called_once_with(
            custom_manifest_path.resolve(),
            llm_stability_runs_path=(temp_root / relative_stability).resolve(),
            poc_comparison_path=(temp_root / relative_comparison).resolve(),
        )

    def test_llm_stability_report_resolves_custom_comparison_repo_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(REPO_ROOT / "datasets", temp_root / "datasets")
            custom_comparison_path = temp_root / "datasets" / "custom" / "comparison.json"
            custom_comparison_path.parent.mkdir()
            shutil.copy2(POC_COMPARISON_PATH, custom_comparison_path)

            with mock.patch.object(
                evaluate_dataset,
                "evaluate_poc_mode_comparison",
                return_value=evaluate_dataset.evaluate_poc_mode_comparison(
                    self.valid_poc_comparison_data(),
                    repo_root=REPO_ROOT,
                ),
            ) as mocked_comparison:
                evaluate_dataset.evaluate_llm_stability_report(
                    temp_root / "datasets" / "gold" / "llm_stability_runs_v0.json",
                    custom_comparison_path,
                )

        mocked_comparison.assert_called_once()
        self.assertEqual(
            temp_root.resolve(),
            mocked_comparison.call_args.kwargs["repo_root"].resolve(),
        )

    def test_p9_harness_passes_manifest_repo_to_llm_stability_report(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(REPO_ROOT / "datasets", temp_root / "datasets")
            custom_manifest_path = temp_root / "datasets" / "custom_p9_manifest.json"
            shutil.copy2(POC_EVALUATION_MANIFEST_PATH, custom_manifest_path)
            custom_comparison_path = temp_root / "reports" / "comparison.json"
            custom_comparison_path.parent.mkdir()
            shutil.copy2(POC_COMPARISON_PATH, custom_comparison_path)
            stability_path = temp_root / "datasets" / "gold" / "llm_stability_runs_v0.json"
            llm_report = evaluate_dataset.evaluate_llm_stability_report(
                stability_path,
                custom_comparison_path,
                repo_root=temp_root,
            )

            with (
                mock.patch.object(
                    evaluate_dataset,
                    "p9_evaluation_samples",
                    return_value=(),
                ),
                mock.patch.object(
                    evaluate_dataset,
                    "evaluate_llm_stability_report",
                    return_value=llm_report,
                ) as mocked_llm_report,
            ):
                evaluate_dataset.evaluate_p9_harness(
                    custom_manifest_path,
                    llm_stability_runs_path=stability_path,
                    poc_comparison_path=custom_comparison_path,
                )

        mocked_llm_report.assert_called_once_with(
            stability_path,
            custom_comparison_path,
            repo_root=temp_root.resolve(),
        )

    def test_poc_acceptance_report_resolves_relative_manifest_before_harness(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(REPO_ROOT / "datasets", temp_root / "datasets")
            custom_manifest_path = temp_root / "datasets" / "custom_p9_manifest.json"
            shutil.copy2(POC_EVALUATION_MANIFEST_PATH, custom_manifest_path)
            relative_manifest_path = Path(os.path.relpath(custom_manifest_path, REPO_ROOT))
            harness = evaluate_dataset.P9HarnessReport(
                manifest=custom_manifest_path.resolve(),
                results=(),
                llm_stability=evaluate_dataset.LLMStabilityMetrics(
                    input_id="synthetic",
                    run_count=1,
                    plan_agreement_rate=1.0,
                    confirmed_value_agreement_rate=1.0,
                    schema_failure_rate=0.0,
                    repair_success_rate=1.0,
                    deterministic_fallback_rate=0.0,
                    external_ai_api_guard_violation_count=0,
                    distinct_plan_count=1,
                    distinct_confirmed_value_count=1,
                    unstable_example_count=0,
                    unstable_examples=(),
                ),
                poc_mode_comparison=evaluate_dataset.PoCComparisonMetrics(
                    mode_count=0,
                    high_risk_false_auto_confirmed_count=0,
                    high_risk_false_auto_confirmed_target=0,
                    target_met=True,
                    manual_correction_time=evaluate_dataset.ManualCorrectionTimeMetrics(
                        measurement_method="synthetic",
                        baseline_minutes=1.0,
                        assisted_minutes=0.5,
                        reduction_minutes=0.5,
                        reduction_rate=0.5,
                        target_reduction_rate=0.5,
                        target_met=True,
                    ),
                    modes=(),
                    mode_diffs=(),
                ),
                llm_stability_source=(
                    temp_root / "datasets" / "gold" / "llm_stability_runs_v0.json"
                ),
                poc_comparison_source=(
                    temp_root / "datasets" / "gold" / "poc_mode_comparison_v1.json"
                ),
            )
            with (
                mock.patch.object(
                    evaluate_dataset,
                    "evaluate_p9_harness",
                    return_value=harness,
                ) as mocked_harness,
                mock.patch.object(
                    evaluate_dataset,
                    "current_git_commit",
                    return_value="tracked-head",
                ),
                mock.patch.object(
                    evaluate_dataset,
                    "current_git_worktree_clean",
                    return_value=True,
                ),
            ):
                report = evaluate_dataset.build_poc_acceptance_report(
                    relative_manifest_path,
                )

        mocked_harness.assert_called_once()
        self.assertEqual(custom_manifest_path.resolve(), mocked_harness.call_args.args[0])
        payload = report.as_dict()
        self.assertEqual(
            str(custom_manifest_path.resolve()),
            payload["evidence"]["dataset_manifest"],
        )

    def test_p9_harness_resolves_custom_manifest_under_datasets_from_repo_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(REPO_ROOT / "datasets", temp_root / "datasets")
            custom_manifest_path = temp_root / "datasets" / "custom_p9_manifest.json"
            shutil.copy2(POC_EVALUATION_MANIFEST_PATH, custom_manifest_path)

            report = evaluate_dataset.evaluate_p9_harness(
                custom_manifest_path,
                llm_stability_runs_path=(
                    temp_root / "datasets" / "gold" / "llm_stability_runs_v0.json"
                ),
                poc_comparison_path=(
                    temp_root / "datasets" / "gold" / "poc_mode_comparison_v1.json"
                ),
            )

        payload = report.as_dict()
        self.assertEqual(str(custom_manifest_path), payload["dataset_manifest"])
        self.assertEqual(temp_root.resolve(), report.repo_root)
        self.assertNotIn("repo_root", payload)
        self.assertGreater(payload["summary"]["case_count"], 0)

    def test_poc_acceptance_reproducibility_uses_harness_repo_root(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        llm_report = evaluate_dataset.evaluate_llm_stability_report(
            LLM_STABILITY_RUNS_PATH,
            POC_COMPARISON_PATH,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(REPO_ROOT / "datasets", temp_root / "datasets")
            harness = evaluate_dataset.P9HarnessReport(
                manifest=evaluate_dataset.DEFAULT_P9_HARNESS_MANIFEST,
                results=tuple(payload["p9_harness_results"]),
                llm_stability=llm_report.llm_stability,
                poc_mode_comparison=llm_report.poc_mode_comparison,
                llm_stability_source=evaluate_dataset.DEFAULT_LLM_STABILITY_RUNS,
                poc_comparison_source=evaluate_dataset.DEFAULT_POC_COMPARISON,
                repo_root=temp_root,
            )
            with mock.patch.object(
                evaluate_dataset,
                "poc_acceptance_tracked_repo_path",
                return_value=True,
            ) as mocked_tracked:
                tracked = (
                    evaluate_dataset.poc_acceptance_evidence_inputs_tracked_in_manifest_repo(
                        harness
                    )
                )

        self.assertTrue(tracked)
        self.assertTrue(mocked_tracked.call_args_list)
        self.assertEqual(
            {temp_root.resolve()},
            {call.args[1] for call in mocked_tracked.call_args_list},
        )

    def test_poc_acceptance_report_uses_custom_manifest_repo_for_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(REPO_ROOT / "datasets", temp_root / "datasets")
            custom_manifest_path = temp_root / "datasets" / "custom_p9_manifest.json"
            shutil.copy2(POC_EVALUATION_MANIFEST_PATH, custom_manifest_path)
            with mock.patch.object(
                evaluate_dataset,
                "current_git_commit",
                return_value="manifest-head",
            ) as mocked_commit:
                report = evaluate_dataset.build_poc_acceptance_report(
                    custom_manifest_path,
                    llm_stability_runs_path=(
                        temp_root / "datasets" / "gold" / "llm_stability_runs_v0.json"
                    ),
                    poc_comparison_path=(
                        temp_root / "datasets" / "gold" / "poc_mode_comparison_v1.json"
                    ),
                )

        mocked_commit.assert_has_calls(
            [mock.call(temp_root.resolve()), mock.call(REPO_ROOT)]
        )
        payload = report.as_dict()
        self.assertEqual("manifest-head", payload["tested_environment"]["commit"])
        self.assertEqual(
            "manifest-head",
            payload["tested_environment"]["evaluator_commit"],
        )
        self.assertEqual(
            str(custom_manifest_path.resolve()),
            payload["evidence"]["dataset_manifest"],
        )

    def test_poc_acceptance_report_resolves_implicit_comparison_inputs_from_manifest_repo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            shutil.copytree(REPO_ROOT / "datasets", temp_root / "datasets")
            custom_manifest_path = temp_root / "datasets" / "custom_p9_manifest.json"
            shutil.copy2(POC_EVALUATION_MANIFEST_PATH, custom_manifest_path)
            with (
                mock.patch.object(
                    evaluate_dataset,
                    "current_git_commit",
                    return_value="manifest-head",
                ),
                mock.patch.object(
                    evaluate_dataset,
                    "current_git_worktree_clean",
                    return_value=True,
                ),
            ):
                report = evaluate_dataset.build_poc_acceptance_report(
                    custom_manifest_path,
                )

        payload = report.as_dict()
        self.assertEqual(
            str(
                temp_root.resolve()
                / "datasets"
                / "gold"
                / "llm_stability_runs_v0.json"
            ),
            payload["evidence"]["llm_stability_runs"],
        )
        self.assertEqual(
            str(
                temp_root.resolve()
                / "datasets"
                / "gold"
                / "poc_mode_comparison_v1.json"
            ),
            payload["evidence"]["poc_mode_comparison"],
        )

    def test_poc_acceptance_report_resolves_default_inputs_from_repo_when_cwd_differs(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        llm_report = evaluate_dataset.evaluate_llm_stability_report(
            LLM_STABILITY_RUNS_PATH,
            POC_COMPARISON_PATH,
        )
        harness = evaluate_dataset.P9HarnessReport(
            manifest=POC_EVALUATION_MANIFEST_PATH.resolve(),
            results=tuple(payload["p9_harness_results"]),
            llm_stability=llm_report.llm_stability,
            poc_mode_comparison=llm_report.poc_mode_comparison,
            llm_stability_source=LLM_STABILITY_RUNS_PATH.resolve(),
            poc_comparison_source=POC_COMPARISON_PATH.resolve(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            previous_cwd = Path.cwd()
            os.chdir(temp_dir)
            try:
                with (
                    mock.patch.object(
                        evaluate_dataset,
                        "evaluate_p9_harness",
                        return_value=harness,
                    ) as mocked_harness,
                    mock.patch.object(
                        evaluate_dataset,
                        "current_git_commit",
                        return_value="tracked-head",
                    ),
                    mock.patch.object(
                        evaluate_dataset,
                        "current_git_worktree_clean",
                        return_value=True,
                    ),
                ):
                    report = evaluate_dataset.build_poc_acceptance_report(
                        POC_EVALUATION_MANIFEST_PATH.resolve(),
                    )
            finally:
                os.chdir(previous_cwd)

        mocked_harness.assert_called_once_with(
            POC_EVALUATION_MANIFEST_PATH.resolve(),
            llm_stability_runs_path=LLM_STABILITY_RUNS_PATH.resolve(),
            poc_comparison_path=POC_COMPARISON_PATH.resolve(),
        )
        payload = report.as_dict()
        self.assertEqual(
            str(evaluate_dataset.DEFAULT_LLM_STABILITY_RUNS),
            payload["evidence"]["llm_stability_runs"],
        )
        self.assertEqual(
            str(evaluate_dataset.DEFAULT_POC_COMPARISON),
            payload["evidence"]["poc_mode_comparison"],
        )

    def test_poc_acceptance_report_resolves_default_manifest_from_repo_when_cwd_differs(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        llm_report = evaluate_dataset.evaluate_llm_stability_report(
            LLM_STABILITY_RUNS_PATH,
            POC_COMPARISON_PATH,
        )
        harness = evaluate_dataset.P9HarnessReport(
            manifest=POC_EVALUATION_MANIFEST_PATH.resolve(),
            results=tuple(payload["p9_harness_results"]),
            llm_stability=llm_report.llm_stability,
            poc_mode_comparison=llm_report.poc_mode_comparison,
            llm_stability_source=LLM_STABILITY_RUNS_PATH.resolve(),
            poc_comparison_source=POC_COMPARISON_PATH.resolve(),
            repo_root=REPO_ROOT,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            previous_cwd = Path.cwd()
            os.chdir(temp_dir)
            try:
                with (
                    mock.patch.object(
                        evaluate_dataset,
                        "evaluate_p9_harness",
                        return_value=harness,
                    ) as mocked_harness,
                    mock.patch.object(
                        evaluate_dataset,
                        "current_git_commit",
                        return_value="tracked-head",
                    ),
                    mock.patch.object(
                        evaluate_dataset,
                        "current_git_worktree_clean",
                        return_value=True,
                    ),
                ):
                    report = evaluate_dataset.build_poc_acceptance_report()
            finally:
                os.chdir(previous_cwd)

        mocked_harness.assert_called_once_with(
            POC_EVALUATION_MANIFEST_PATH.resolve(),
            llm_stability_runs_path=LLM_STABILITY_RUNS_PATH.resolve(),
            poc_comparison_path=POC_COMPARISON_PATH.resolve(),
        )
        payload = report.as_dict()
        self.assertEqual(
            str(evaluate_dataset.DEFAULT_P9_HARNESS_MANIFEST),
            payload["evidence"]["dataset_manifest"],
        )

    def test_poc_acceptance_report_resolves_explicit_inputs_from_repo_when_cwd_differs(
        self,
    ) -> None:
        payload = self.poc_acceptance_payload()
        llm_report = evaluate_dataset.evaluate_llm_stability_report(
            LLM_STABILITY_RUNS_PATH,
            POC_COMPARISON_PATH,
        )
        harness = evaluate_dataset.P9HarnessReport(
            manifest=POC_EVALUATION_MANIFEST_PATH.resolve(),
            results=tuple(payload["p9_harness_results"]),
            llm_stability=llm_report.llm_stability,
            poc_mode_comparison=llm_report.poc_mode_comparison,
            llm_stability_source=LLM_STABILITY_RUNS_PATH.resolve(),
            poc_comparison_source=POC_COMPARISON_PATH.resolve(),
            repo_root=REPO_ROOT,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            previous_cwd = Path.cwd()
            os.chdir(temp_dir)
            try:
                with (
                    mock.patch.object(
                        evaluate_dataset,
                        "evaluate_p9_harness",
                        return_value=harness,
                    ) as mocked_harness,
                    mock.patch.object(
                        evaluate_dataset,
                        "current_git_commit",
                        return_value="tracked-head",
                    ),
                    mock.patch.object(
                        evaluate_dataset,
                        "current_git_worktree_clean",
                        return_value=True,
                    ),
                ):
                    report = evaluate_dataset.build_poc_acceptance_report(
                        POC_EVALUATION_MANIFEST_PATH.resolve(),
                        llm_stability_runs_path=(
                            evaluate_dataset.DEFAULT_LLM_STABILITY_RUNS
                        ),
                        poc_comparison_path=evaluate_dataset.DEFAULT_POC_COMPARISON,
                    )
            finally:
                os.chdir(previous_cwd)

        mocked_harness.assert_called_once_with(
            POC_EVALUATION_MANIFEST_PATH.resolve(),
            llm_stability_runs_path=LLM_STABILITY_RUNS_PATH.resolve(),
            poc_comparison_path=POC_COMPARISON_PATH.resolve(),
        )
        payload = report.as_dict()
        self.assertEqual(
            str(evaluate_dataset.DEFAULT_LLM_STABILITY_RUNS),
            payload["evidence"]["llm_stability_runs"],
        )
        self.assertEqual(
            str(evaluate_dataset.DEFAULT_POC_COMPARISON),
            payload["evidence"]["poc_mode_comparison"],
        )

    def test_p9_harness_counts_blocked_conversion_status_as_failure(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "blocked-fixture",
                "sample_id": "p9-blocked",
                "path": "datasets/fixtures/word/blocked.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
            }

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "blocked",
                    "document_ir": {"document": {"title": "blocked"}},
                    "artifacts": [{"kind": "debug", "id": "debug-json"}],
                    "warnings": ["primary artifact generation skipped"],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {"status": "disabled"},
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="word_to_excel",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertEqual("blocked", result["conversion_status"])
        self.assertIn("conversion status blocked", str(result["failure_reason"]))
        self.assertFalse(result["artifact_generated"])

    def test_p9_harness_rejects_invalid_conversion_status(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "invalid-status-fixture",
                "sample_id": "p9-invalid-status",
                "path": "datasets/fixtures/word/invalid-status.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "excel"
                / "excel-to-word-representative.xlsx"
            ).read_bytes()

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "done",
                    "document_ir": {"document": {"title": "invalid status"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-xlsx",
                            "format": "xlsx",
                            "content": artifact_content,
                        }
                    ],
                    "warnings": [],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {"status": "disabled"},
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="word_to_excel",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertIn(
            "conversion status 'done' is not a valid terminal status",
            str(result["failure_reason"]),
        )

    def test_p9_evaluation_samples_rejects_non_representative_fixture_link(self) -> None:
        p9_manifest = evaluate_dataset.load_json(POC_EVALUATION_MANIFEST_PATH)
        fixture_manifest = evaluate_dataset.load_json(FIXTURE_MANIFEST_PATH)
        fixture_manifest["fixtures"].append(
            {
                "id": "word-not-representative",
                "path": "datasets/fixtures/word/word-to-excel-report.docx",
                "source_type": "word",
                "format": "docx",
            }
        )
        for sample in p9_manifest["samples"]:
            if sample["id"] == "p9-word-001":
                sample["fixture_id"] = "word-not-representative"
                break

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "must declare word_to_excel_representative",
        ):
            evaluate_dataset.p9_evaluation_samples(p9_manifest, fixture_manifest)

    def test_p9_evaluation_samples_requires_source_categories(self) -> None:
        p9_manifest = evaluate_dataset.load_json(POC_EVALUATION_MANIFEST_PATH)
        fixture_manifest = evaluate_dataset.load_json(FIXTURE_MANIFEST_PATH)
        p9_manifest["samples"] = [
            sample
            for sample in p9_manifest["samples"]
            if sample.get("category") != "record_pdf"
        ]

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "source category 'record_pdf'",
        ):
            evaluate_dataset.p9_evaluation_samples(p9_manifest, fixture_manifest)

    def test_p9_evaluation_samples_rejects_duplicate_sample_ids(self) -> None:
        p9_manifest = evaluate_dataset.load_json(POC_EVALUATION_MANIFEST_PATH)
        fixture_manifest = evaluate_dataset.load_json(FIXTURE_MANIFEST_PATH)
        duplicate_sample = copy.deepcopy(p9_manifest["samples"][0])
        duplicate_sample["fixture_id"] = p9_manifest["samples"][1]["fixture_id"]
        p9_manifest["samples"].insert(1, duplicate_sample)

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "duplicate P9 sample id",
        ):
            evaluate_dataset.p9_evaluation_samples(p9_manifest, fixture_manifest)

    def test_p9_evaluation_samples_rejects_usable_sample_without_fixture_link(
        self,
    ) -> None:
        p9_manifest = evaluate_dataset.load_json(POC_EVALUATION_MANIFEST_PATH)
        fixture_manifest = evaluate_dataset.load_json(FIXTURE_MANIFEST_PATH)
        for sample in p9_manifest["samples"]:
            if sample["id"] == "p9-word-001":
                sample["fixture_id"] = None
                sample["dataset_status"] = "usable_fixture"
                break

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "must reference a fixture_manifest fixture",
        ):
            evaluate_dataset.p9_evaluation_samples(p9_manifest, fixture_manifest)

    def test_p9_evaluation_samples_track_missing_required_mode_placeholder(
        self,
    ) -> None:
        p9_manifest = evaluate_dataset.load_json(POC_EVALUATION_MANIFEST_PATH)
        fixture_manifest = evaluate_dataset.load_json(FIXTURE_MANIFEST_PATH)

        samples = evaluate_dataset.p9_evaluation_samples(p9_manifest, fixture_manifest)

        self.assertFalse(
            any(sample["sample_id"] == "p9-word-004" for sample in samples)
        )
        placeholders = [
            sample
            for sample in samples
            if sample.get("dataset_status") == "manifest_placeholder"
        ]
        self.assertEqual(
            ["p9-scanned-pdf-001"],
            [sample["sample_id"] for sample in placeholders],
        )
        self.assertEqual(
            ["scanned_pdf_ocr"],
            [sample["representative_mode"] for sample in placeholders],
        )

    def test_p9_evaluation_samples_track_missing_required_category_placeholder(
        self,
    ) -> None:
        p9_manifest = copy.deepcopy(
            evaluate_dataset.load_json(POC_EVALUATION_MANIFEST_PATH)
        )
        fixture_manifest = evaluate_dataset.load_json(FIXTURE_MANIFEST_PATH)
        p9_manifest["samples"] = [
            sample
            for sample in p9_manifest["samples"]
            if sample["id"] != "p9-record-pdf-001"
        ]

        samples = evaluate_dataset.p9_evaluation_samples(
            p9_manifest, fixture_manifest
        )

        placeholders = [
            sample
            for sample in samples
            if sample.get("dataset_status") == "manifest_placeholder"
        ]
        self.assertIn(
            ("p9-record-pdf-002", "record_pdf", "pdf_to_word"),
            [
                (
                    sample["sample_id"],
                    sample["sample_category"],
                    sample["representative_mode"],
                )
                for sample in placeholders
            ],
        )
        self.assertIn(
            ("p9-record-pdf-003", "record_pdf", "pdf_to_excel"),
            [
                (
                    sample["sample_id"],
                    sample["sample_category"],
                    sample["representative_mode"],
                )
                for sample in placeholders
            ],
        )

    def test_p9_harness_counts_artifact_expectation_mismatch_as_failure(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "mismatch-fixture",
                "sample_id": "p9-mismatch",
                "path": "datasets/fixtures/word/mismatch.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
                "word_to_excel_expectations": {"warnings": []},
            }

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "mismatch"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-xlsx",
                            "format": "xlsx",
                            "content": b"not-an-xlsx",
                        }
                    ],
                    "warnings": [],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {"status": "disabled"},
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="word_to_excel",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertFalse(result["artifact_expectations_met"])
        self.assertEqual(1, len(result["artifact_expectation_failures"]))
        self.assertIn("artifact validation failed", result["artifact_expectation_failures"][0])
        self.assertIn("artifact expectation mismatch", str(result["failure_reason"]))

    def test_p9_harness_closes_temp_artifact_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_artifact_path = Path(temp_dir) / "artifact.xlsx"

            class TemporaryArtifactSpy:
                name = str(temp_artifact_path)

                def __init__(self) -> None:
                    self.is_open = False

                def __enter__(self) -> "TemporaryArtifactSpy":
                    self.is_open = True
                    return self

                def __exit__(self, *args: object) -> None:
                    self.is_open = False

                def write(self, content: bytes) -> None:
                    temp_artifact_path.write_bytes(content)

                def flush(self) -> None:
                    return None

            temp_file_spy = TemporaryArtifactSpy()

            def validate_after_close(
                artifact_path: Path,
                expectations: dict[str, object],
                fixture_id: object,
            ) -> list[str]:
                self.assertFalse(temp_file_spy.is_open)
                self.assertEqual(temp_artifact_path, artifact_path)
                self.assertEqual("closed-temp-fixture", fixture_id)
                return []

            fixture = {
                "id": "closed-temp-fixture",
                "sample_id": "p9-closed-temp",
                "path": "datasets/fixtures/word/closed-temp.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
                "word_to_excel_expectations": {"warnings": []},
            }
            with mock.patch(
                "tempfile.NamedTemporaryFile",
                return_value=temp_file_spy,
            ), mock.patch.object(
                evaluate_dataset,
                "p9_validate_xlsx_artifact",
                side_effect=validate_after_close,
            ):
                failures = evaluate_dataset.p9_validate_artifact_expectations(
                    fixture=fixture,
                    conversion_mode="word_to_excel",
                    representative_mode="word_to_excel",
                    primary_artifact={
                        "kind": "primary",
                        "id": "primary-xlsx",
                        "format": "xlsx",
                        "content": b"workbook bytes",
                    },
                    warnings=[],
                )

            self.assertEqual([], failures)
            self.assertFalse(temp_artifact_path.exists())

    def test_p9_harness_requires_primary_artifact_without_fixture_expectations(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "pdf-without-expectations",
                "sample_id": "p9-pdf-no-expectations",
                "path": "datasets/fixtures/pdf/no-expectations.pdf",
                "source_type": "record_pdf",
                "format": "pdf",
                "conversion_mode": "pdf_to_word",
            }

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "no expectations"}},
                    "artifacts": [{"kind": "debug", "id": "debug-json"}],
                    "warnings": [],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {"status": "disabled"},
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="pdf_to_word",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertFalse(result["artifact_generated"])
        self.assertFalse(result["artifact_expectations_met"])
        self.assertEqual(
            ["primary artifact is missing"],
            result["artifact_expectation_failures"],
        )
        self.assertIn("artifact expectation mismatch", str(result["failure_reason"]))

    def test_p9_harness_rejects_unexpected_warnings_for_exact_expectations(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "unexpected-warning-fixture",
                "sample_id": "p9-unexpected-warning",
                "path": "datasets/fixtures/word/unexpected-warning.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
                "word_to_excel_expectations": {"warnings": []},
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "excel"
                / "excel-to-word-representative.xlsx"
            ).read_bytes()

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "unexpected warning"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-xlsx",
                            "format": "xlsx",
                            "content": artifact_content,
                        }
                    ],
                    "warnings": ["spurious warning"],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {"status": "disabled"},
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="word_to_excel",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertFalse(result["artifact_expectations_met"])
        self.assertEqual(
            ["unexpected warning 'spurious warning' was emitted"],
            result["artifact_expectation_failures"],
        )
        self.assertIn("artifact expectation mismatch", str(result["failure_reason"]))

    def test_p9_harness_rejects_extra_warnings_for_nonempty_expectations(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "extra-warning-fixture",
                "sample_id": "p9-extra-warning",
                "path": "datasets/fixtures/pdf/extra-warning.pdf",
                "source_type": "text_pdf",
                "format": "pdf",
                "conversion_mode": "pdf_to_excel",
                "pdf_to_excel_expectations": {
                    "warnings": ["conversion mode pdf_to_excel selected"],
                },
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "excel"
                / "excel-to-word-representative.xlsx"
            ).read_bytes()

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "extra warning"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-xlsx",
                            "format": "xlsx",
                            "content": artifact_content,
                        }
                    ],
                    "warnings": [
                        "conversion mode pdf_to_excel selected",
                        "unexpected review warning",
                    ],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {"status": "disabled"},
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="pdf_to_excel",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertIn(
            "unexpected warning 'unexpected review warning' was emitted",
            result["artifact_expectation_failures"],
        )

    def test_p9_harness_allows_review_only_runtime_warnings(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "review-warning-fixture",
                "sample_id": "p9-review-warning",
                "path": "datasets/fixtures/word/review-warning.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
                "word_to_excel_expectations": {"warnings": []},
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "excel"
                / "excel-to-word-representative.xlsx"
            ).read_bytes()

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "requires_review",
                    "document_ir": {"document": {"title": "review warning"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-xlsx",
                            "format": "xlsx",
                            "content": artifact_content,
                        }
                    ],
                    "warnings": [
                        "conversion mode word_to_excel selected",
                        "blocks[0].bbox missing; block marked requires_review",
                    ],
                    "review_items": [
                        {
                            "id": "review-1",
                            "warnings": [
                                "blocks[0].bbox missing; block marked requires_review"
                            ],
                        }
                    ],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {"status": "disabled"},
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="word_to_excel",
                    llm_scenario="no_llm",
                )

        self.assertTrue(result["ok"])
        self.assertTrue(result["artifact_expectations_met"])
        self.assertEqual([], result["artifact_expectation_failures"])

    def test_p9_harness_marks_unavailable_fail_closed_rows_as_audit_failures(
        self,
    ) -> None:
        result = evaluate_dataset.p9_result_for_unavailable_fixture(
            {
                "id": "placeholder-scanned-pdf",
                "sample_id": "p9-placeholder-scanned-pdf",
                "sample_category": "scanned_pdf_ocr",
                "source_type": "scanned_pdf",
                "format": "pdf",
                "conversion_mode": "pdf_to_excel",
            },
            mode="scanned_pdf_ocr",
            llm_scenario="no_llm",
            failure_reason="representative fixture path is unavailable",
            fail_closed=True,
            mvp_before_gate_revision="p9-mvp-before-placeholder-fixture-gate",
        )

        self.assertFalse(result["ok"])
        self.assertTrue(result["fail_closed"])
        self.assertFalse(result["audit_present"])
        self.assertIn("conversion audit missing", str(result["failure_reason"]))

    def test_p9_harness_marks_optional_pdf_dependency_errors_fail_closed(self) -> None:
        class PocServerDependencyError(RuntimeError):
            pass

        with tempfile.NamedTemporaryFile(suffix=".pdf") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "pdf-dependency-fixture",
                "sample_id": "p9-pdf-dependency",
                "path": "datasets/fixtures/pdf/pdf-dependency.pdf",
                "source_type": "text_pdf",
                "format": "pdf",
                "conversion_mode": "pdf_to_word",
            }

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                side_effect=PocServerDependencyError("pdf dependency unavailable"),
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="pdf_to_word",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertTrue(result["fail_closed"])
        self.assertEqual(
            "p9-mvp-before-pdf-eval-dependency-gate",
            result["mvp_before_gate_revision"],
        )
        self.assertFalse(result["audit_present"])
        self.assertIn("conversion audit missing", str(result["failure_reason"]))
        self.assertFalse(result["artifact_expectations_met"])
        self.assertEqual(
            [
                "fail-closed MVP-before gate revision: "
                "p9-mvp-before-pdf-eval-dependency-gate"
            ],
            result["artifact_expectation_failures"],
        )

    def test_p9_runtime_review_warning_allowlist_is_exact(self) -> None:
        self.assertTrue(
            evaluate_dataset.p9_runtime_warning_is_review_only(
                "blocks[12].bbox missing; block marked requires_review",
                conversion_mode="excel_to_word",
                allowed_prefixes=(),
            )
        )
        self.assertTrue(
            evaluate_dataset.p9_runtime_warning_is_review_only(
                (
                    "PDF table extraction candidate unavailable: tabula; "
                    "xlsx artifact requires review"
                ),
                conversion_mode="pdf_to_excel",
                allowed_prefixes=(),
            )
        )
        self.assertFalse(
            evaluate_dataset.p9_runtime_warning_is_review_only(
                "unexpected parser degradation requires review by coincidence",
                conversion_mode="pdf_to_excel",
                allowed_prefixes=(),
            )
        )

    def test_p9_harness_checks_mode_specific_xlsx_expectations(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "wrong-cell-fixture",
                "sample_id": "p9-wrong-cell",
                "path": "datasets/fixtures/word/wrong-cell.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
                "word_to_excel_expectations": {
                    "cells": {
                        "A1": {
                            "value": "Unexpected header",
                            "value_type": "inline_string",
                        }
                    }
                },
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "excel"
                / "excel-to-word-representative.xlsx"
            ).read_bytes()

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "wrong cell"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-xlsx",
                            "format": "xlsx",
                            "content": artifact_content,
                        }
                    ],
                    "warnings": [],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {"status": "disabled"},
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="word_to_excel",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertFalse(result["artifact_expectations_met"])
        self.assertIn(
            "expected cell A1",
            str(result["artifact_expectation_failures"]),
        )
        self.assertIn("artifact expectation mismatch", str(result["failure_reason"]))

    def test_p9_harness_rejects_primary_artifact_format_mismatch(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "wrong-format-fixture",
                "sample_id": "p9-wrong-format",
                "path": "datasets/fixtures/word/wrong-format.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
                "word_to_excel_expectations": {
                    "cells": {
                        "A1": {
                            "value": "Expected header",
                            "value_type": "inline_string",
                        }
                    }
                },
            }

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "wrong format"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-docx",
                            "format": "docx",
                            "content": b"not-a-workbook",
                        }
                    ],
                    "warnings": [],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {"status": "disabled"},
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="word_to_excel",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertFalse(result["artifact_expectations_met"])
        self.assertIn(
            "primary artifact format 'docx' did not match expected 'xlsx'",
            str(result["artifact_expectation_failures"]),
        )
        self.assertIn("artifact expectation mismatch", str(result["failure_reason"]))

    def test_p9_harness_requires_docx_paragraph_expectations_to_match_exactly(
        self,
    ) -> None:
        blocks = [
            mock.Mock(kind="paragraph", text="first paragraph"),
            mock.Mock(kind="paragraph", text="second paragraph"),
        ]
        docx = mock.Mock(blocks=blocks)

        with mock.patch(
            "core.parsers.docx_extraction.extract_docx_structure",
            return_value=docx,
        ):
            failures = evaluate_dataset.p9_validate_docx_artifact(
                Path("unused.docx"),
                {"paragraph_texts": ["first paragraph"]},
            )

        self.assertEqual(
            ["docx paragraph texts did not match expectations"],
            failures,
        )

    def test_p9_harness_uses_scanned_pdf_ocr_expectations_for_pdf_conversion(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "scanned-ocr-fixture",
                "sample_id": "p9-scanned-ocr",
                "path": "datasets/fixtures/pdf/scanned-ocr.pdf",
                "source_type": "scanned_pdf",
                "format": "pdf",
                "conversion_mode": "pdf_to_word",
                "scanned_pdf_ocr_expectations": {
                    "warnings": ["ocr confidence below review threshold"],
                },
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "word"
                / "word-to-excel-report.docx"
            ).read_bytes()

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "scanned OCR"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-docx",
                            "format": "docx",
                            "content": artifact_content,
                        }
                    ],
                    "warnings": [],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {"status": "disabled"},
                            "use_ocr": {"status": "enabled"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="scanned_pdf_ocr",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertFalse(result["artifact_expectations_met"])
        self.assertIn(
            "expected warning 'ocr confidence below review threshold' was not emitted",
            result["artifact_expectation_failures"],
        )
        self.assertIn("artifact expectation mismatch", str(result["failure_reason"]))

    def test_p9_harness_validates_declared_pdf_table_size_expectations(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "pdf-table-size-fixture",
                "sample_id": "p9-pdf-table-size",
                "path": "datasets/fixtures/pdf/pdf-table-size.pdf",
                "source_type": "text_pdf",
                "format": "pdf",
                "conversion_mode": "pdf_to_excel",
                "pdf_to_excel_expectations": {
                    "table_row_count": 999,
                    "table_column_count": 999,
                },
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "excel"
                / "excel-to-word-representative.xlsx"
            ).read_bytes()

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "pdf table size"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-xlsx",
                            "format": "xlsx",
                            "content": artifact_content,
                        }
                    ],
                    "warnings": [],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {"status": "disabled"},
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="pdf_to_excel",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertFalse(result["artifact_expectations_met"])
        self.assertIn("expected 999 table rows", str(result["artifact_expectation_failures"]))
        self.assertIn("expected 999 table columns", str(result["artifact_expectation_failures"]))

    def test_p9_harness_fails_scanned_ocr_when_ocr_stays_unsupported(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "scanned-ocr-unsupported-fixture",
                "sample_id": "p9-scanned-ocr-unsupported",
                "path": "datasets/fixtures/pdf/scanned-ocr-unsupported.pdf",
                "source_type": "scanned_pdf",
                "format": "pdf",
                "conversion_mode": "pdf_to_word",
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "word"
                / "word-to-excel-report.docx"
            ).read_bytes()

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "scanned OCR"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-docx",
                            "format": "docx",
                            "content": artifact_content,
                        }
                    ],
                    "warnings": [],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {"status": "disabled"},
                            "use_ocr": {"status": "unsupported"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="scanned_pdf_ocr",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertEqual("unsupported", result["use_ocr_status"])
        self.assertIn("OCR status 'unsupported' is not enabled", str(result["failure_reason"]))

    def test_p9_harness_fails_rows_missing_required_ir_or_audit(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "missing-required-output-fixture",
                "sample_id": "p9-missing-required-output",
                "path": "datasets/fixtures/word/missing-required-output.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "excel"
                / "excel-to-word-representative.xlsx"
            ).read_bytes()
            base_response = {
                "status": "converted",
                "document_ir": {"document": {"title": "required outputs"}},
                "artifacts": [
                    {
                        "kind": "primary",
                        "id": "primary-xlsx",
                        "format": "xlsx",
                        "content": artifact_content,
                    }
                ],
                "warnings": [],
                "review_items": [],
                "audit": {
                    "conversion_settings": {
                        "use_llm": {"status": "disabled"},
                        "use_ocr": {"status": "disabled"},
                    },
                    "conversion_plan": {"status": "disabled"},
                },
            }
            for omitted_key, expected_failure in (
                ("document_ir", "document IR missing"),
                ("audit", "conversion audit missing"),
            ):
                response = copy.deepcopy(base_response)
                response.pop(omitted_key)
                with self.subTest(omitted_key=omitted_key), mock.patch(
                    "services.api.poc_web.convert_uploaded_document",
                    return_value=response,
                ):
                    result = evaluate_dataset.p9_conversion_result(
                        fixture,
                        fixture_path=Path(fixture_file.name),
                        mode="word_to_excel",
                        llm_scenario="no_llm",
                    )

                self.assertFalse(result["ok"])
                self.assertIn(expected_failure, str(result["failure_reason"]))

    def test_p9_harness_enforces_no_llm_scenario_stays_llm_free(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "no-llm-leak-fixture",
                "sample_id": "p9-no-llm-leak",
                "path": "datasets/fixtures/word/no-llm-leak.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "excel"
                / "excel-to-word-representative.xlsx"
            ).read_bytes()

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "no LLM leak"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-xlsx",
                            "format": "xlsx",
                            "content": artifact_content,
                        }
                    ],
                    "warnings": [],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {
                                "requested": True,
                                "enabled": True,
                                "status": "enabled",
                            },
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "enabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="word_to_excel",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertEqual("enabled", result["llm_status"])
        self.assertIn(
            "no_llm scenario LLM status 'enabled' is not disabled",
            str(result["failure_reason"]),
        )

    def test_p9_harness_rejects_malformed_conversion_settings_as_row_failure(
        self,
    ) -> None:
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "malformed-settings-fixture",
                "sample_id": "p9-malformed-settings",
                "path": "datasets/fixtures/word/malformed-settings.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "excel"
                / "excel-to-word-representative.xlsx"
            ).read_bytes()

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "malformed settings"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-xlsx",
                            "format": "xlsx",
                            "content": artifact_content,
                        }
                    ],
                    "warnings": [],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": ["not", "a", "mapping"],
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="word_to_excel",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertIn(
            "conversion settings missing or malformed",
            str(result["failure_reason"]),
        )
        self.assertNotIn("AttributeError", str(result["failure_reason"]))

    def test_p9_harness_fails_rows_with_external_ai_guard_violation(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "external-ai-fixture",
                "sample_id": "p9-external-ai",
                "path": "datasets/fixtures/word/external-ai.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "excel"
                / "excel-to-word-representative.xlsx"
            ).read_bytes()

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "external ai"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-xlsx",
                            "format": "xlsx",
                            "content": artifact_content,
                        }
                    ],
                    "warnings": [],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {
                                "requested": True,
                                "enabled": True,
                                "status": "enabled",
                            },
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "enabled"},
                        "llm": {
                            "enabled": True,
                            "base_url_type": "external",
                        },
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="word_to_excel",
                    llm_scenario="llm_requested",
                )

        self.assertFalse(result["ok"])
        self.assertTrue(result["external_ai_api_guard_violation"])
        self.assertIn("external AI API guard violation", str(result["failure_reason"]))

    def test_p9_harness_enforces_llm_requested_scenario_uses_llm_path(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "llm-request-ignored-fixture",
                "sample_id": "p9-llm-request-ignored",
                "path": "datasets/fixtures/word/llm-request-ignored.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "excel"
                / "excel-to-word-representative.xlsx"
            ).read_bytes()

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "llm request ignored"}},
                    "artifacts": [
                        {
                            "kind": "primary",
                            "id": "primary-xlsx",
                            "format": "xlsx",
                            "content": artifact_content,
                        }
                    ],
                    "warnings": [],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {
                                "requested": False,
                                "enabled": False,
                                "status": "disabled",
                            },
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="word_to_excel",
                    llm_scenario="llm_requested",
                )

        self.assertFalse(result["ok"])
        self.assertEqual("disabled", result["llm_status"])
        self.assertIn(
            "llm_requested scenario LLM status 'disabled' did not request LLM",
            str(result["failure_reason"]),
        )

    def test_p9_harness_rejects_duplicate_primary_artifacts(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".docx") as fixture_file:
            fixture_file.write(b"fixture")
            fixture_file.flush()
            fixture = {
                "id": "duplicate-primary-fixture",
                "sample_id": "p9-duplicate-primary",
                "path": "datasets/fixtures/word/duplicate-primary.docx",
                "source_type": "word",
                "format": "docx",
                "conversion_mode": "word_to_excel",
            }
            artifact_content = (
                REPO_ROOT
                / "datasets"
                / "fixtures"
                / "excel"
                / "excel-to-word-representative.xlsx"
            ).read_bytes()
            primary_artifact = {
                "kind": "primary",
                "format": "xlsx",
                "content": artifact_content,
            }

            with mock.patch(
                "services.api.poc_web.convert_uploaded_document",
                return_value={
                    "status": "converted",
                    "document_ir": {"document": {"title": "duplicate primary"}},
                    "artifacts": [
                        {**primary_artifact, "id": "primary-xlsx-1"},
                        {**primary_artifact, "id": "primary-xlsx-2"},
                    ],
                    "warnings": [],
                    "review_items": [],
                    "audit": {
                        "conversion_settings": {
                            "use_llm": {"status": "disabled"},
                            "use_ocr": {"status": "disabled"},
                        },
                        "conversion_plan": {"status": "disabled"},
                    },
                },
            ):
                result = evaluate_dataset.p9_conversion_result(
                    fixture,
                    fixture_path=Path(fixture_file.name),
                    mode="word_to_excel",
                    llm_scenario="no_llm",
                )

        self.assertFalse(result["ok"])
        self.assertTrue(result["artifact_generated"])
        self.assertEqual(2, result["artifact_count"])
        self.assertIn("expected exactly one primary artifact, got 2", str(result["failure_reason"]))

    def test_p9_harness_rejects_gmp_acceptance_flag_combination(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--p9-harness",
                "--gmp-acceptance",
                str(GMP_ACCEPTANCE_PATH),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(2, completed.returncode)
        self.assertIn(
            "--p9-harness cannot be combined with --gmp-acceptance",
            completed.stderr,
        )
        self.assertEqual("", completed.stdout)
        self.assertNotIn("AttributeError", completed.stderr)

    def test_poc_mode_comparison_rejects_missing_required_mode_before_scoring(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"] = [mode for mode in data["modes"] if mode["mode"] != "high_quality"]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "exactly no_llm"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_missing_dataset_manifest_before_scoring(self) -> None:
        data = self.valid_poc_comparison_data()
        data.pop("dataset_manifest")

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "dataset_manifest"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_missing_manual_correction_time(self) -> None:
        data = self.valid_poc_comparison_data()
        data.pop("manual_correction_time")

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "manual_correction_time"
        ):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_allows_zero_assisted_manual_correction_time(self) -> None:
        data = self.valid_poc_comparison_data()
        data["manual_correction_time"]["assisted_minutes"] = 0.0

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(0.0, metrics.manual_correction_time.assisted_minutes)
        self.assertEqual(12.0, metrics.manual_correction_time.reduction_minutes)
        self.assertEqual(1.0, metrics.manual_correction_time.reduction_rate)
        self.assertTrue(metrics.manual_correction_time.target_met)

    def test_poc_mode_comparison_reports_slower_assisted_manual_correction_time(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["manual_correction_time"]["assisted_minutes"] = 13.0

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(13.0, metrics.manual_correction_time.assisted_minutes)
        self.assertEqual(-1.0, metrics.manual_correction_time.reduction_minutes)
        self.assertEqual(-1 / 12, metrics.manual_correction_time.reduction_rate)
        self.assertFalse(metrics.manual_correction_time.target_met)
        self.assertFalse(metrics.target_met)

    def test_poc_mode_comparison_rejects_legacy_schema_without_manual_times(self) -> None:
        data = self.valid_poc_comparison_data()
        data["schema_version"] = "veridoc-poc-mode-comparison/v0"
        data.pop("manual_correction_time")

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "unsupported PoC comparison schema_version"
        ):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_non_canonical_dataset_manifest_before_scoring(
        self,
    ) -> None:
        for manifest_path in (
            str(REPO_ROOT / "datasets" / "fixtures" / "manifest.json"),
            "datasets/fixtures/side-manifest.json",
            "datasets/fixtures/../fixtures/manifest.json",
        ):
            data = self.valid_poc_comparison_data()
            data["dataset_manifest"] = manifest_path

            with self.subTest(manifest_path=manifest_path), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                "dataset_manifest must be datasets/fixtures/manifest.json",
            ):
                evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_missing_evaluation_cases_before_scoring(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data.pop("evaluation_cases")

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "evaluation_cases"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_missing_high_risk_label_coverage_per_mode(
        self,
    ) -> None:
        for mode_index, mode_name in enumerate(evaluate_dataset.REQUIRED_POC_MODES):
            data = self.valid_poc_comparison_data()
            data["modes"][mode_index]["high_risk_items"] = data["modes"][mode_index][
                "high_risk_items"
            ][:1]

            with self.subTest(mode=mode_name), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError, "cover all"
            ):
                evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_treats_missing_legacy_warning_lists_as_empty(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][1].pop("warnings")

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(0, metrics.as_dict()["modes"][1]["warning_count"])
        self.assertEqual([], metrics.as_dict()["mode_diffs"][0]["added_warnings"])
        self.assertEqual(
            [
                "lot-number-mismatch",
                "missing-source-anchor",
            ],
            metrics.as_dict()["mode_diffs"][0]["removed_warnings"],
        )

    def test_poc_mode_comparison_rejects_high_risk_label_drift_before_scoring(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][0]["high_risk_items"][0]["expected_value"] = "SAMPLE-LOT-999"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "high-risk labels"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_high_risk_label_index_allows_same_taxonomy_label_in_distinct_blocks(
        self,
    ) -> None:
        data = self.valid_high_risk_labels_data()
        duplicate_label = copy.deepcopy(data["items"][0])
        duplicate_label["id"] = "gold-duplicate-block"
        duplicate_label["block_id"] = "block-003"
        data["items"].append(duplicate_label)

        labels = evaluate_dataset.high_risk_label_index(data)

        self.assertIn(
            ("sample-document-ir-v0", "block-002", "lot_number"),
            labels,
        )
        self.assertIn(
            ("sample-document-ir-v0", "block-003", "lot_number"),
            labels,
        )

    def test_high_risk_label_index_rejects_missing_expected_value(self) -> None:
        data = self.valid_high_risk_labels_data()
        del data["items"][0]["expected_value"]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "expected_value"):
            evaluate_dataset.high_risk_label_index(data)

    def test_high_risk_label_index_rejects_label_outside_taxonomy(self) -> None:
        data = self.valid_high_risk_labels_data()
        data["items"][0]["label_id"] = "lot_numbre"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "label_taxonomy"):
            evaluate_dataset.high_risk_label_index(data)

    def test_high_risk_labels_reject_unknown_fixture_block_id(self) -> None:
        data = self.valid_high_risk_labels_data()
        data["items"][0]["block_id"] = "block-missing"
        labels = evaluate_dataset.high_risk_label_index(data)
        fixture_paths = evaluate_dataset.fixture_paths_from_manifest(
            evaluate_dataset.load_json(REPO_ROOT / "datasets" / "fixtures" / "manifest.json"),
            REPO_ROOT,
        )

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "block_id"):
            evaluate_dataset.validate_high_risk_labels_against_fixtures(labels, fixture_paths)

    def test_poc_mode_comparison_rejects_duplicate_value_from_different_block(
        self,
    ) -> None:
        labels_data = self.valid_high_risk_labels_data()
        duplicate_label = copy.deepcopy(labels_data["items"][0])
        duplicate_label["id"] = "gold-duplicate-block-value"
        duplicate_label["block_id"] = "block-003"
        labels_data["items"].append(duplicate_label)
        labels = evaluate_dataset.high_risk_label_index(labels_data)

        fixture_paths = evaluate_dataset.fixture_paths_from_manifest(
            evaluate_dataset.load_json(REPO_ROOT / "datasets" / "fixtures" / "manifest.json"),
            REPO_ROOT,
        )
        label_blocks = evaluate_dataset.validate_high_risk_labels_against_fixtures(
            evaluate_dataset.high_risk_label_index(self.valid_high_risk_labels_data()),
            fixture_paths,
        )
        label_blocks[
            ("sample-document-ir-v0", "block-003", "lot_number")
        ] = {
            "id": "block-003",
            "text": "Lot Number: SAMPLE-LOT-001",
            "source": {
                "source_page": 1,
                "bbox": {
                    "x": 300.0,
                    "y": 112.0,
                    "width": 180.0,
                    "height": 18.0,
                },
            },
            "requires_review": True,
        }
        mode_record = self.valid_poc_comparison_data()["modes"][2]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "captured actual value"):
            evaluate_dataset.poc_mode_actual_values_by_high_risk_label(
                mode_record,
                self.valid_cases_data(),
                labels,
                label_blocks,
                "modes[2]",
            )

    def test_poc_mode_comparison_rejects_missing_high_risk_block_binding(self) -> None:
        data = self.valid_poc_comparison_data()
        del data["modes"][0]["high_risk_items"][0]["block_id"]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "block_id"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_missing_expected_high_risk_value(self) -> None:
        data = self.valid_poc_comparison_data()
        del data["modes"][0]["high_risk_items"][0]["expected_value"]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "expected_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_numeric_expected_value_for_boolean_label(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][0]["high_risk_items"][1]["expected_value"] = 1

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "expected_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_missing_actual_high_risk_value(self) -> None:
        data = self.valid_poc_comparison_data()
        del data["modes"][0]["high_risk_items"][0]["actual_value"]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "actual_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_confirmed_actual_high_risk_mismatch(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][0]["high_risk_items"][0]["status"] = "confirmed"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "status"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_confirmed_matching_high_risk_value(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][0]["status"] = "confirmed"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "requires_review"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_reviewed_mismatch_in_inflated_rate(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][0]["actual_value"] = "SAMPLE-LOT-002"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "actual_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_actual_high_risk_value_from_wrong_cell(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][0]["actual_value"] = "Lot number"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "actual_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_prefix_high_risk_identifier_match(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["cases"][0]["actual"]["tables"][0]["cells"][1][
            "text"
        ] = "SAMPLE-LOT-001-REV2"
        data["modes"][2]["metrics"]["cell_match_rate"] = 0.5

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "actual_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_non_string_high_risk_value_mismatch(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][1]["actual_value"] = False

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "actual_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_string_actual_for_non_string_high_risk_label(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][1]["actual_value"] = "SAMPLE-LOT-001"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "actual_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_high_risk_item_accepts_semantically_equal_numeric_value(self) -> None:
        labels = {
            ("sample-document-ir-v0", "block-002", "numeric_value"): {
                "expected_value": 1,
                "risk_level": "high",
                "requires_review": True,
            }
        }
        item = {
            "fixture_id": "sample-document-ir-v0",
            "block_id": "block-002",
            "label_id": "numeric_value",
            "expected_value": 1,
            "actual_value": 1.0,
            "risk_level": "high",
            "requires_review": True,
            "status": "requires_review",
        }

        self.assertTrue(
            evaluate_dataset.validate_poc_high_risk_item_against_label(
                item, labels, "modes[0].high_risk_items[0]"
            )
        )

    def test_poc_high_risk_item_rejects_large_numeric_drift(self) -> None:
        labels = {
            ("sample-document-ir-v0", "block-002", "numeric_value"): {
                "expected_value": 10**12,
                "risk_level": "high",
                "requires_review": True,
            }
        }
        item = {
            "fixture_id": "sample-document-ir-v0",
            "block_id": "block-002",
            "label_id": "numeric_value",
            "expected_value": 10**12,
            "actual_value": 10**12 + 1,
            "risk_level": "high",
            "requires_review": True,
            "status": "requires_review",
        }

        self.assertFalse(
            evaluate_dataset.validate_poc_high_risk_item_against_label(
                item, labels, "modes[0].high_risk_items[0]"
            )
        )

    def test_poc_mode_high_risk_values_accept_parsed_full_field_cell(self) -> None:
        cases_data = self.valid_cases_data()
        mode_record = self.valid_poc_comparison_data()["modes"][2]
        cases_data["cases"][0]["expected"]["tables"][0]["cells"][1][
            "text"
        ] = "Lot Number: SAMPLE-LOT-001"
        mode_record["cases"][0]["actual"]["tables"][0]["cells"][1][
            "text"
        ] = "Lot Number: SAMPLE-LOT-001"
        labels = evaluate_dataset.high_risk_label_index(self.valid_high_risk_labels_data())
        fixture_paths = evaluate_dataset.fixture_paths_from_manifest(
            evaluate_dataset.load_json(REPO_ROOT / "datasets" / "fixtures" / "manifest.json"),
            REPO_ROOT,
        )
        label_blocks = evaluate_dataset.validate_high_risk_labels_against_fixtures(
            labels,
            fixture_paths,
        )

        actual_values = evaluate_dataset.poc_mode_actual_values_by_high_risk_label(
            mode_record,
            cases_data,
            labels,
            label_blocks,
            "modes[2]",
        )

        self.assertIn(
            "SAMPLE-LOT-001",
            actual_values[("sample-document-ir-v0", "block-002", "lot_number")],
        )

    def test_poc_mode_comparison_recomputes_cell_match_rate_from_mode_cases(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["cases"][0]["actual"]["tables"][0]["cells"][0]["text"] = "Batch"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "cell_match_rate"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_recomputes_table_extraction_rate_from_mode_cases(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["cases"][0]["actual"]["tables"] = []

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "table_extraction_rate"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_recomputes_source_linkage_rate_from_mode_cases(self) -> None:
        data = self.valid_poc_comparison_data()
        del data["modes"][2]["cases"][0]["actual"]["tables"][0]["cells"][1]["source"]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "source_linkage_rate"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_counts_high_risk_auto_confirmation_failures(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][0]["high_risk_items"][0]["auto_confirmed"] = True

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(1, metrics.high_risk_false_auto_confirmed_count)
        self.assertFalse(metrics.target_met)

    def test_poc_mode_comparison_counts_captured_cell_auto_confirmation_failures(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["cases"][0]["actual"]["tables"][0]["cells"][1][
            "auto_confirmed"
        ] = True

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(
            1,
            metrics.as_dict()["modes"][2]["high_risk_false_auto_confirmed_count"],
        )
        self.assertEqual(1, metrics.high_risk_false_auto_confirmed_count)
        self.assertFalse(metrics.target_met)

    def test_poc_mode_comparison_counts_wrong_captured_cell_auto_confirmation(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][0]["cases"][0]["actual"]["tables"][0]["cells"][1][
            "auto_confirmed"
        ] = True

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(
            1,
            metrics.as_dict()["modes"][0]["high_risk_false_auto_confirmed_count"],
        )
        self.assertEqual(1, metrics.high_risk_false_auto_confirmed_count)
        self.assertFalse(metrics.target_met)

    def test_poc_mode_comparison_deduplicates_mirrored_auto_confirmation_sources(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][0]["auto_confirmed"] = True
        data["modes"][2]["cases"][0]["actual"]["tables"][0]["cells"][1][
            "auto_confirmed"
        ] = True

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(
            1,
            metrics.as_dict()["modes"][2]["high_risk_false_auto_confirmed_count"],
        )
        self.assertEqual(1, metrics.high_risk_false_auto_confirmed_count)
        self.assertFalse(metrics.target_met)

    def test_poc_mode_comparison_counts_duplicate_warnings_once(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][0]["warnings"].append(data["modes"][0]["warnings"][0])

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(2, metrics.as_dict()["modes"][0]["warning_count"])
        self.assertEqual(1, metrics.as_dict()["mode_diffs"][0]["warning_removed_count"])
        self.assertEqual(2, metrics.as_dict()["mode_diffs"][1]["warning_removed_count"])

    def test_poc_mode_comparison_counts_boolean_review_cell_auto_confirmation(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["cases"][0]["actual"]["tables"][0]["cells"][0][
            "auto_confirmed"
        ] = True

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(
            1,
            metrics.as_dict()["modes"][2]["high_risk_false_auto_confirmed_count"],
        )
        self.assertEqual(1, metrics.high_risk_false_auto_confirmed_count)
        self.assertFalse(metrics.target_met)

    def test_gmp_acceptance_reports_15_7_criteria(self) -> None:
        metrics = evaluate_dataset.evaluate_gmp_acceptance(
            self.valid_gmp_acceptance_data(), repo_root=REPO_ROOT
        )

        report = metrics.as_dict()

        self.assertTrue(report["target_met"])
        self.assertEqual(8, report["criterion_count"])
        self.assertEqual(0, report["failed_criterion_count"])
        self.assertEqual(0, report["high_risk_false_auto_confirmed_count"])
        self.assertEqual("datasets/gold/poc_mode_comparison_v1.json", report["poc_comparison"])
        self.assertEqual(
            [
                "high_risk_review",
                "missed_detection_zero",
                "source_traceability",
                "originality",
                "audit_trail",
                "completeness",
                "reproducibility",
                "segregation_of_duties",
            ],
            [criterion["id"] for criterion in report["criteria"]],
        )
        self.assertTrue(all(criterion["status"] == "pass" for criterion in report["criteria"]))
        segregation = report["criteria"][-1]
        self.assertEqual("segregation_of_duties", segregation["id"])
        self.assertEqual(
            "review approval flows with authenticated actor identity",
            segregation["scope"],
        )
        self.assertNotIn("excluded_contexts", segregation)
        self.assertIn("Authenticated role-token flows", segregation["notes"])
        self.assertIn("no-auth approval attempts are forbidden", segregation["notes"])

    def test_gmp_acceptance_requires_canonical_dataset_manifest(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["dataset_manifest"] = "datasets/fixtures/alternate_manifest.json"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "dataset_manifest must be datasets/fixtures/manifest.json",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_requires_rerun_command(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["verification_commands"] = ["python3 -m pytest tests -q"]

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "verification_commands must include",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_absolute_path_verification_command(self) -> None:
        commands = (
            f"python3 {'/' + 'private' + '/recompute.py'}",
            evaluate_dataset.EXPECTED_GMP_ACCEPTANCE_COMMAND
            + " "
            + f"python3 {'/' + 'private' + '/recompute.py'}",
            "PYTHONHOME="
            + "/"
            + "private "
            + "python3 scripts/evaluate_dataset.py",
            "PYTHONPATH="
            + "D:"
            + "\\private "
            + "python3 scripts/evaluate_dataset.py",
        )
        for command in commands:
            with self.subTest(command=command):
                data = self.valid_gmp_acceptance_data()
                data["verification_commands"].append(command)

                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError,
                    r"verification_commands\[\d+\] must not contain absolute paths",
                ):
                    evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_non_public_verification_command_path(self) -> None:
        commands = (
            "python3 private/recompute.py",
            evaluate_dataset.EXPECTED_GMP_ACCEPTANCE_COMMAND
            + " python3 private/recompute.py",
            "PYTHONHOME=private "
            + evaluate_dataset.EXPECTED_GMP_ACCEPTANCE_COMMAND,
            "PYTHONPATH=private "
            + evaluate_dataset.EXPECTED_GMP_ACCEPTANCE_COMMAND,
        )
        for command in commands:
            with self.subTest(command=command):
                data = self.valid_gmp_acceptance_data()
                data["verification_commands"].append(command)

                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError,
                    r"verification_commands\[\d+\] must reference public repository files",
                ):
                    evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_non_public_path_option_assignment(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["verification_commands"].append(
            "python3 -m pytest --rootdir=private tests -q"
        )

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            r"verification_commands\[\d+\] must reference public repository files",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_bare_non_public_command_path(self) -> None:
        commands = (
            "python3 private_runner",
            "pytest private",
        )
        for command in commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temp_dir:
                temp_root = Path(temp_dir)
                self.prepare_gmp_acceptance_repo(temp_root)
                (temp_root / "private").mkdir()
                (temp_root / "private_runner").write_text(
                    "not public verification evidence",
                    encoding="utf-8",
                )
                data = self.valid_gmp_acceptance_data()
                data["verification_commands"].append(command)

                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError,
                    r"verification_commands\[\d+\] must reference public repository files",
                ):
                    evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

    def test_gmp_acceptance_rejects_private_python_module_target(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["verification_commands"].append("python3 -m private_runner")

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            r"verification_commands\[\d+\] must reference public repository files",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_shell_control_verification_command(self) -> None:
        commands = (
            "python3 scripts/evaluate_dataset.py;/" + "private/recompute.py",
            evaluate_dataset.EXPECTED_GMP_ACCEPTANCE_COMMAND
            + " && python3 scripts/evaluate_dataset.py",
        )
        for command in commands:
            with self.subTest(command=command):
                data = self.valid_gmp_acceptance_data()
                data["verification_commands"].append(command)

                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError,
                    r"verification_commands\[\d+\] must not contain shell control operators",
                ):
                    evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_shell_expansion_verification_command(self) -> None:
        commands = (
            "python3 $PRIVATE_RECOMPUTE",
            "PYTHONPATH=$PRIVATE_LIB python3 scripts/evaluate_dataset.py",
            "python3 $(pwd)/scripts/evaluate_dataset.py",
            "pytest *",
            "python3 ~",
            "python3 scripts/*.py",
        )
        for command in commands:
            with self.subTest(command=command):
                data = self.valid_gmp_acceptance_data()
                data["verification_commands"].append(command)

                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError,
                    r"verification_commands\[\d+\] must not contain shell expansion tokens",
                ):
                    evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_missing_public_verification_command_path(
        self,
    ) -> None:
        data = self.valid_gmp_acceptance_data()
        data["verification_commands"].append("python3 scripts/ci/deleted_gate.py")

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            r"verification_commands\[\d+\] must reference existing public repository paths",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_missing_criterion_evidence_ref(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["criteria"][0]["evidence_refs"] = ["datasets/gold/deleted-evidence.json"]

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            r"criteria\[0\]\.evidence_refs\[0\] must reference an existing file",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_non_public_criterion_evidence_ref(self) -> None:
        data = self.valid_gmp_acceptance_data()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            self.prepare_gmp_acceptance_repo(temp_root)
            private_evidence = temp_root / "private" / "confidential-record.pdf"
            private_evidence.parent.mkdir()
            private_evidence.write_text("not public synthetic evidence", encoding="utf-8")
            data["criteria"][0]["evidence_refs"] = [
                private_evidence.relative_to(temp_root).as_posix()
            ]

            with self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                r"criteria\[0\]\.evidence_refs\[0\] must reference public synthetic GMP evidence",
            ):
                evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

    def test_gmp_acceptance_rejects_public_path_to_non_public_evidence_target(
        self,
    ) -> None:
        data = self.valid_gmp_acceptance_data()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            self.prepare_gmp_acceptance_repo(temp_root)
            private_evidence = temp_root / "private" / "confidential-record.pdf"
            private_evidence.parent.mkdir()
            private_evidence.write_text("not public synthetic evidence", encoding="utf-8")
            public_ref = Path("datasets/gold/confidential-record.pdf")
            try:
                os.symlink(private_evidence, temp_root / public_ref)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            data["criteria"][0]["evidence_refs"] = [public_ref.as_posix()]

            with self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                r"criteria\[0\]\.evidence_refs\[0\] must reference public synthetic GMP evidence",
            ):
                evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

    def test_gmp_acceptance_rejects_unmanifested_fixture_evidence_ref(self) -> None:
        data = self.valid_gmp_acceptance_data()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            self.prepare_gmp_acceptance_repo(temp_root)
            undeclared_evidence = Path("datasets/fixtures/raw-record.pdf")
            (temp_root / undeclared_evidence).write_text(
                "not manifest-declared synthetic fixture evidence",
                encoding="utf-8",
            )
            data["criteria"][0]["evidence_refs"] = [undeclared_evidence.as_posix()]

            with self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                r"criteria\[0\]\.evidence_refs\[0\] must reference manifest-declared "
                "public synthetic GMP fixture evidence",
            ):
                evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

    def test_gmp_acceptance_rejects_source_traceability_without_recomputed_linkage(
        self,
    ) -> None:
        data = self.valid_gmp_acceptance_data()
        poc_data = self.valid_poc_comparison_data()
        for mode in poc_data["modes"]:
            if mode["mode"] == "high_quality":
                mode["cases"][0]["actual"]["tables"][0]["cells"][0].pop("source")
                mode["metrics"]["source_linkage_rate"] = 0.5

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            self.prepare_gmp_acceptance_repo(temp_root)
            (temp_root / POC_COMPARISON_PATH.relative_to(REPO_ROOT)).write_text(
                json.dumps(poc_data),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                "source_traceability cannot pass when high_quality source linkage is incomplete",
            ):
                evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

    def test_gmp_acceptance_rejects_tampered_review_evidence_package(self) -> None:
        def omit_dataset_manifest(
            data: dict[str, object], poc_data: dict[str, object]
        ) -> None:
            data.pop("dataset_manifest")

        def replace_rerun_command(
            data: dict[str, object], poc_data: dict[str, object]
        ) -> None:
            data["verification_commands"] = ["python3 -m pytest tests -q"]

        def delete_evidence_ref(
            data: dict[str, object], poc_data: dict[str, object]
        ) -> None:
            data["criteria"][0]["evidence_refs"] = ["datasets/gold/deleted-evidence.json"]

        def remove_high_quality_source_anchor(
            data: dict[str, object], poc_data: dict[str, object]
        ) -> None:
            for mode in poc_data["modes"]:
                if mode["mode"] == "high_quality":
                    mode["cases"][0]["actual"]["tables"][0]["cells"][0].pop("source")
                    mode["metrics"]["source_linkage_rate"] = 0.5

        cases = (
            ("dataset_manifest", omit_dataset_manifest, "dataset_manifest"),
            ("verification_commands", replace_rerun_command, "verification_commands"),
            (
                "evidence_refs",
                delete_evidence_ref,
                r"criteria\[0\]\.evidence_refs\[0\] must reference an existing file",
            ),
            (
                "source_traceability",
                remove_high_quality_source_anchor,
                "source_traceability cannot pass when high_quality source linkage is incomplete",
            ),
        )
        for name, tamper, expected_error in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                temp_root = Path(temp_dir)
                data = self.valid_gmp_acceptance_data()
                poc_data = self.valid_poc_comparison_data()
                tamper(data, poc_data)
                self.prepare_gmp_acceptance_repo(temp_root)
                (temp_root / POC_COMPARISON_PATH.relative_to(REPO_ROOT)).write_text(
                    json.dumps(poc_data),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError, expected_error
                ):
                    evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

    def test_gmp_acceptance_ignores_manual_correction_timing_gate(self) -> None:
        data = self.valid_gmp_acceptance_data()
        poc_data = self.valid_poc_comparison_data()
        poc_data["manual_correction_time"]["assisted_minutes"] = 13.0

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            self.prepare_gmp_acceptance_repo(temp_root)
            (temp_root / POC_COMPARISON_PATH.relative_to(REPO_ROOT)).write_text(
                json.dumps(poc_data),
                encoding="utf-8",
            )

            metrics = evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

        self.assertTrue(metrics.target_met)
        self.assertEqual(0, metrics.failed_criterion_count)
        self.assertEqual((), metrics.failed_criteria)

    def test_gmp_acceptance_rejects_unqualified_sod_pass(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["criteria"][7].pop("scope")

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            r"criteria\[7\]\.scope must qualify segregation_of_duties pass status",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_fails_when_audit_evidence_is_unmet(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["criteria"][4]["status"] = "fail"

        metrics = evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

        self.assertFalse(metrics.target_met)
        self.assertEqual(1, metrics.failed_criterion_count)
        self.assertEqual("audit_trail", metrics.failed_criteria[0]["id"])

    def test_change_management_requires_gmp_acceptance_gate(self) -> None:
        docs = (
            REPO_ROOT / "docs" / "change-management-reevaluation.md"
        ).read_text(encoding="utf-8")
        command = evaluate_dataset.EXPECTED_GMP_ACCEPTANCE_COMMAND
        gate_start = docs.index("### GMP Acceptance Gate")
        checklist_start = docs.index("## PR Checklist")

        self.assertIn(command, docs[gate_start:checklist_start])
        self.assertIn(command, docs[checklist_start:])

    def test_llm_stability_agreement_rates_do_not_depend_on_run_order(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"] = [data["runs"][2], data["runs"][1], data["runs"][0]]

        metrics = evaluate_dataset.evaluate_llm_stability(data)

        self.assertEqual(2 / 3, metrics.plan_agreement_rate)
        self.assertEqual(2 / 3, metrics.confirmed_value_agreement_rate)
        self.assertEqual(
            (
                {
                    "reference_run_id": "run-001",
                    "run_id": "run-002",
                    "changed": "confirmed_values",
                },
                {
                    "reference_run_id": "run-001",
                    "run_id": "run-003",
                    "changed": "conversion_plan",
                },
            ),
            metrics.unstable_examples,
        )

    def test_llm_stability_reference_run_matches_plan_and_value_majorities(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][2]["confirmed_values"] = copy.deepcopy(data["runs"][1]["confirmed_values"])

        metrics = evaluate_dataset.evaluate_llm_stability(data)

        self.assertEqual(
            (
                {
                    "reference_run_id": "run-002",
                    "run_id": "run-001",
                    "changed": "confirmed_values",
                },
                {
                    "reference_run_id": "run-002",
                    "run_id": "run-003",
                    "changed": "conversion_plan",
                },
            ),
            metrics.unstable_examples,
        )

    def test_llm_stability_reports_separate_references_without_joint_majority(self) -> None:
        data = self.valid_llm_stability_data()
        run_004 = copy.deepcopy(data["runs"][2])
        run_004["run_id"] = "run-004"
        run_004["conversion_plan"]["operations"][0]["rationale"] = (
            "The alternate synthetic record wording labels the release date directly."
        )
        data["runs"].append(run_004)
        data["n"] = 4
        data["runs"][0]["confirmed_values"][1]["value"] = "2026-01-17"

        metrics = evaluate_dataset.evaluate_llm_stability(data)

        self.assertEqual(4, metrics.unstable_example_count)
        self.assertEqual(
            (
                {
                    "reference_plan_run_id": "run-001",
                    "reference_confirmed_values_run_id": "run-003",
                    "run_id": "run-001",
                    "changed": "confirmed_values",
                },
                {
                    "reference_plan_run_id": "run-001",
                    "reference_confirmed_values_run_id": "run-003",
                    "run_id": "run-002",
                    "changed": "confirmed_values",
                },
                {
                    "reference_plan_run_id": "run-001",
                    "reference_confirmed_values_run_id": "run-003",
                    "run_id": "run-003",
                    "changed": "conversion_plan",
                },
            ),
            metrics.unstable_examples,
        )

    def test_llm_stability_rejects_empty_confirmed_values_before_scoring(self) -> None:
        data = self.valid_llm_stability_data()
        for run in data["runs"]:
            run["confirmed_values"] = []

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "at least one public confirmed value"
        ):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_rejects_non_public_source_kind_before_scoring(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][0]["conversion_plan"]["source_kind"] = "real_confidential_record"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "public-only synthetic or anonymized"
        ):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_rejects_invalid_run_count_before_scoring(self) -> None:
        data = self.valid_llm_stability_data()
        data["n"] = 4

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "runs length"):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_rejects_invalid_conversion_plan_before_scoring(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][0]["conversion_plan"]["constraints"]["external_transmission"] = True

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "conversion_plan"):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_treats_missing_legacy_outcome_as_all_passed(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][0].pop("outcome")

        metrics = evaluate_dataset.evaluate_llm_stability(data)

        self.assertEqual(2 / 3, metrics.schema_failure_rate)
        self.assertEqual(1 / 2, metrics.repair_success_rate)
        self.assertEqual(1 / 3, metrics.deterministic_fallback_rate)
        self.assertEqual(0, metrics.external_ai_api_guard_violation_count)

    def test_llm_stability_rejects_schema_passed_fallback(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][0]["outcome"]["deterministic_fallback_used"] = True

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "fallback requires"
        ):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_rejects_schema_failure_without_repair_or_fallback(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][1]["outcome"] = {
            "schema_validation_passed": False,
            "repair_attempted": True,
            "repair_succeeded": False,
            "deterministic_fallback_used": False,
            "external_ai_api_transmission_attempted": False,
        }

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "repaired or use"):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_rejects_repaired_run_that_also_uses_fallback(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][1]["outcome"] = {
            "schema_validation_passed": False,
            "repair_attempted": True,
            "repair_succeeded": True,
            "deterministic_fallback_used": True,
            "external_ai_api_transmission_attempted": False,
        }

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "both repair"):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_counts_external_ai_api_guard_violations(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][0]["outcome"]["external_ai_api_transmission_attempted"] = True

        metrics = evaluate_dataset.evaluate_llm_stability(data)

        self.assertEqual(1, metrics.external_ai_api_guard_violation_count)

    def test_missing_actual_cell_counts_as_missing_source_link(self) -> None:
        data = self.valid_cases_data()
        case = data["cases"][0]
        actual_table = case["actual"]["tables"][0]
        actual_table["cells"] = actual_table["cells"][:1]

        metrics = self.evaluate_valid_cases(data)

        self.assertEqual(2, metrics.expected_source_link_count)
        self.assertEqual(1, metrics.matched_source_link_count)
        self.assertEqual(0.5, metrics.source_linkage_rate)

    def test_rejects_unknown_case_fixture_id(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["fixture_id"] = "missing-fixture"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "unknown fixture_id"):
            self.evaluate_valid_cases(data)

    def test_rejects_placeholder_fixture_without_path(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["fixture_id"] = "placeholder-text-pdf"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "unknown fixture_id"):
            self.evaluate_valid_cases(data)

    def test_rejects_non_public_manifest_fixture_before_scoring(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "public confidentiality"):
            self.evaluate_with_fixture(
                data,
                fixture,
                fixture_metadata={"confidentiality": "confidential"},
            )

    def test_rejects_pending_anonymization_fixture_with_path_before_scoring(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "synthetic or anonymized"
        ):
            self.evaluate_with_fixture(
                data,
                fixture,
                fixture_metadata={"anonymization": "pending_synthetic_fixture"},
            )

    def test_rejects_fixture_root_policy_traversal_before_scoring(self) -> None:
        for allowed_root, fixture_relpath in (
            ("../fixtures", "datasets/fixtures/fixture.json"),
            ("datasets/..", "secret/fixture.json"),
            ("fixtures", "fixtures/fixture.json"),
        ):
            data = self.valid_cases_data()
            fixture = evaluate_dataset.load_json(
                REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
            )

            with self.subTest(allowed_root=allowed_root), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError, "allowed_fixture_root"
            ):
                self.evaluate_with_fixture(
                    data,
                    fixture,
                    manifest_policy={"allowed_fixture_root": allowed_root},
                    fixture_relpath=fixture_relpath,
                )

    def test_rejects_non_canonical_dataset_manifest_before_scoring(self) -> None:
        for manifest_path in (
            str(REPO_ROOT / "datasets" / "fixtures" / "manifest.json"),
            "datasets/fixtures/side-manifest.json",
            "datasets/fixtures/../fixtures/manifest.json",
        ):
            data = self.valid_cases_data()
            data["dataset_manifest"] = manifest_path

            with self.subTest(manifest_path=manifest_path), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                "dataset_manifest must be datasets/fixtures/manifest.json",
            ):
                self.evaluate_valid_cases(data)

    def test_rejects_expected_table_missing_from_declared_fixture(self) -> None:
        data = self.valid_cases_data()
        expected_table = data["cases"][0]["expected"]["tables"][0]
        expected_table["id"] = "missing-table"
        expected_table["fixture_table_id"] = "missing-table"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "is not present in fixture"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_expected_table_with_mismatched_fixture_anchor(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["expected"]["tables"][0]["fixture_table_id"] = "other-table"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "matching fixture_table_id"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_unsupported_fixture_schema_version(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )
        fixture["schema_version"] = "veridoc-evaluation-fixture/v999"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "unsupported fixture schema_version"
        ):
            self.evaluate_with_fixture(data, fixture)

    def test_rejects_case_document_id_drift_from_fixture(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["document_id"] = "other-document"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "document_id"):
            self.evaluate_valid_cases(data)

    def test_rejects_expected_cell_text_or_source_drift_from_fixture(self) -> None:
        data = self.valid_cases_data()
        cell = data["cases"][0]["expected"]["tables"][0]["cells"][1]
        cell["text"] = "SAMPLE-LOT-999"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "text does not match"):
            self.evaluate_valid_cases(data)

        data = self.valid_cases_data()
        cell = data["cases"][0]["expected"]["tables"][0]["cells"][1]
        cell["source"]["bbox"]["x"] = 999.0

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "source does not match"):
            self.evaluate_valid_cases(data)

    def test_rejects_expected_cell_requires_review_drift_from_fixture(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["expected"]["tables"][0]["cells"][1]["requires_review"] = False

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "requires_review"):
            self.evaluate_valid_cases(data)

    def test_rejects_expected_cell_requires_review_non_boolean_before_scoring(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )
        fixture["tables"][0]["cells"][1]["requires_review"] = False
        data["cases"][0]["expected"]["tables"][0]["cells"][1]["requires_review"] = 0

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "requires_review must be a boolean"
        ):
            self.evaluate_with_fixture(data, fixture)

    def test_rejects_malformed_fixture_source_anchor_before_scoring(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )
        fixture["tables"][0]["cells"][1]["source"] = {}
        data["cases"][0]["expected"]["tables"][0]["cells"][1]["source"] = {}

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "source must define"):
            self.evaluate_with_fixture(data, fixture)

    def test_rejects_non_finite_fixture_geometry_before_scoring(self) -> None:
        for update_fixture in (
            lambda fixture: fixture["pages"][0].update({"width": float("inf")}),
            lambda fixture: fixture["tables"][0]["cells"][1]["source"]["bbox"].update(
                {"x": float("nan")}
            ),
        ):
            data = self.valid_cases_data()
            fixture = evaluate_dataset.load_json(
                REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
            )
            update_fixture(fixture)
            data["cases"][0]["expected"]["tables"][0]["cells"][1]["source"] = copy.deepcopy(
                fixture["tables"][0]["cells"][1]["source"]
            )

            with self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError, "non-finite JSON number"
            ):
                self.evaluate_with_fixture(data, fixture)

    def test_rejects_oversized_integer_fixture_geometry_before_scoring(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )
        fixture["pages"][0]["width"] = 10**400

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "page width"):
            self.evaluate_with_fixture(data, fixture)

    def test_rejects_missing_or_non_string_expected_cell_text_before_scoring(self) -> None:
        for text_value in (None, 123, "   "):
            data = self.valid_cases_data()
            expected_cell = data["cases"][0]["expected"]["tables"][0]["cells"][1]
            if text_value is None:
                del expected_cell["text"]
            else:
                expected_cell["text"] = text_value

            with self.subTest(text_value=text_value), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError, "text must be a non-empty string"
            ):
                self.evaluate_valid_cases(data)

    def test_rejects_missing_or_non_string_fixture_cell_text_before_scoring(self) -> None:
        for text_value in (None, 123, "   "):
            data = self.valid_cases_data()
            fixture = evaluate_dataset.load_json(
                REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
            )
            fixture_cell = fixture["tables"][0]["cells"][1]
            expected_cell = data["cases"][0]["expected"]["tables"][0]["cells"][1]
            if text_value is None:
                del fixture_cell["text"]
                del expected_cell["text"]
            else:
                fixture_cell["text"] = text_value
                expected_cell["text"] = text_value

            with self.subTest(text_value=text_value), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError, "text must be a non-empty string"
            ):
                self.evaluate_with_fixture(data, fixture)

    def test_source_matching_requires_concrete_anchor(self) -> None:
        self.assertFalse(
            evaluate_dataset.source_matches(
                {"source": {}},
                {"source": {}},
            )
        )

    def test_actual_source_anchor_must_be_valid_before_credit(self) -> None:
        data = self.valid_cases_data()
        actual_source = data["cases"][0]["actual"]["tables"][0]["cells"][0]["source"]
        actual_source["source_page"] = True

        metrics = self.evaluate_valid_cases(data)

        self.assertEqual(0, metrics.matched_source_link_count)
        self.assertEqual(0.0, metrics.source_linkage_rate)

    def test_rejects_source_anchor_outside_declared_page_geometry_before_scoring(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )
        fixture["tables"][0]["cells"][1]["source"]["source_page"] = 99
        data["cases"][0]["expected"]["tables"][0]["cells"][1]["source"]["source_page"] = 99

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "source_page"):
            self.evaluate_with_fixture(data, fixture)

        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )
        fixture["tables"][0]["cells"][1]["source"]["bbox"]["x"] = 580.0
        data["cases"][0]["expected"]["tables"][0]["cells"][1]["source"]["bbox"]["x"] = 580.0

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "page geometry"):
            self.evaluate_with_fixture(data, fixture)

    def test_direct_evaluation_uses_explicit_manifest_root_from_any_cwd(self) -> None:
        data = self.valid_cases_data()
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as other_dir:
            try:
                os.chdir(other_dir)
                metrics = self.evaluate_valid_cases(data)
            finally:
                os.chdir(original_cwd)

        self.assertEqual(1.0, metrics.table_extraction_rate)

    def test_rejects_unsupported_evaluation_schema_version(self) -> None:
        data = self.valid_cases_data()
        data["schema_version"] = "veridoc-evaluation-cases/v999"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "unsupported evaluation schema_version"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_non_phase0_scope_before_scoring(self) -> None:
        data = self.valid_cases_data()
        data["scope"]["phase"] = "phase1"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "phase0"):
            self.evaluate_valid_cases(data)

    def test_rejects_duplicate_table_ids_before_indexing(self) -> None:
        data = self.valid_cases_data()
        expected = data["cases"][0]["expected"]
        expected["tables"].append(copy.deepcopy(expected["tables"][0]))

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "duplicate table id"):
            self.evaluate_valid_cases(data)

    def test_rejects_empty_expected_tables_before_scoring(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["expected"]["tables"] = []

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "tables must contain at least one table"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_non_object_case_sections_before_indexing(self) -> None:
        for section in ("expected", "actual"):
            data = self.valid_cases_data()
            data["cases"][0][section] = []

            with self.subTest(section=section), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                "expected and actual sections must be objects",
            ):
                self.evaluate_valid_cases(data)

    def test_rejects_duplicate_cell_ids_before_indexing(self) -> None:
        data = self.valid_cases_data()
        cells = data["cases"][0]["expected"]["tables"][0]["cells"]
        cells.append(copy.deepcopy(cells[0]))

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "duplicate cell id"):
            self.evaluate_valid_cases(data)

    def test_rejects_empty_expected_table_cells_before_scoring(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["expected"]["tables"][0]["cells"] = []

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "cells must contain at least one cell"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_empty_evaluation_case_list_before_scoring(self) -> None:
        data = self.valid_cases_data()
        data["cases"] = []

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "at least one evaluation case"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_duplicate_case_ids_before_scoring(self) -> None:
        data = self.valid_cases_data()
        data["cases"].append(copy.deepcopy(data["cases"][0]))

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "duplicate case id"):
            self.evaluate_valid_cases(data)

    def test_rejects_non_string_actual_cell_text_before_scoring(self) -> None:
        data = self.valid_cases_data()
        actual_cell = data["cases"][0]["actual"]["tables"][0]["cells"][0]
        actual_cell["text"] = None

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "actual cell 'table-001-r1-c1': text"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_non_string_extra_actual_cell_text_before_scoring(self) -> None:
        data = self.valid_cases_data()
        actual_cells = data["cases"][0]["actual"]["tables"][0]["cells"]
        actual_cells.append(
            {
                "id": "table-001-extra",
                "text": 123,
                "source": {},
                "auto_confirmed": False,
            }
        )

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "actual cell 'table-001-extra': text"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_non_boolean_actual_auto_confirmed_before_scoring(self) -> None:
        data = self.valid_cases_data()
        actual_cell = data["cases"][0]["actual"]["tables"][0]["cells"][1]
        actual_cell["auto_confirmed"] = "true"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "actual cell 'table-001-r1-c2': auto_confirmed must be a boolean",
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_non_boolean_extra_actual_auto_confirmed_before_scoring(self) -> None:
        data = self.valid_cases_data()
        actual_cells = data["cases"][0]["actual"]["tables"][0]["cells"]
        actual_cells.append(
            {
                "id": "table-001-extra",
                "text": "extra",
                "source": {},
                "auto_confirmed": "yes",
            }
        )

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "actual cell 'table-001-extra': auto_confirmed must be a boolean",
        ):
            self.evaluate_valid_cases(data)

    def test_cli_emits_json_metrics_for_local_or_ci_verification(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--cases", str(CASES_PATH)],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        metrics = json.loads(proc.stdout)
        self.assertEqual(1.0, metrics["table_extraction_rate"])
        self.assertEqual(0.5, metrics["cell_match_rate"])
        self.assertEqual(0.5, metrics["source_linkage_rate"])
        self.assertEqual(1, metrics["false_auto_confirmed_count"])

    def test_cli_emits_llm_stability_metrics_for_phase1_scope_decision(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--llm-stability-runs",
                str(LLM_STABILITY_RUNS_PATH),
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        metrics = json.loads(proc.stdout)
        self.assertEqual(3, metrics["run_count"])
        self.assertEqual(2 / 3, metrics["plan_agreement_rate"])
        self.assertEqual(2 / 3, metrics["confirmed_value_agreement_rate"])
        self.assertEqual(2 / 3, metrics["schema_failure_rate"])
        self.assertEqual(1 / 2, metrics["repair_success_rate"])
        self.assertEqual(1 / 3, metrics["deterministic_fallback_rate"])
        self.assertEqual(0, metrics["external_ai_api_guard_violation_count"])
        self.assertEqual(2, metrics["unstable_example_count"])

    def test_cli_emits_poc_mode_comparison_for_phase1_acceptance(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--poc-comparison",
                str(POC_COMPARISON_PATH),
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        metrics = json.loads(proc.stdout)
        self.assertEqual(["no_llm", "standard", "high_quality"], metrics["required_modes"])
        self.assertEqual(0, metrics["high_risk_false_auto_confirmed_count"])
        self.assertEqual(7.0, metrics["manual_correction_time"]["reduction_minutes"])
        self.assertEqual(7 / 12, metrics["manual_correction_time"]["reduction_rate"])
        self.assertEqual(2, metrics["mode_diffs"][1]["warning_removed_count"])
        self.assertTrue(metrics["target_met"])

    def test_cli_emits_llm_stability_report_for_phase9_handoff(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--llm-stability-report",
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        report = json.loads(proc.stdout)
        self.assertEqual(
            "veridoc-llm-stability-evaluation/v0",
            report["schema_version"],
        )
        self.assertEqual(
            0,
            report["llm_stability"]["external_ai_api_guard_violation_count"],
        )
        self.assertEqual(
            2,
            report["poc_mode_comparison"]["mode_diffs"][1]["warning_removed_count"],
        )
        self.assertEqual(
            "datasets/gold/llm_stability_runs_v0.json",
            report["phase9_handoff"]["stability_source"],
        )

    def test_cli_llm_stability_report_preserves_custom_input_paths(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--llm-stability-report",
                "--llm-stability-runs",
                "datasets/gold/llm_stability_runs_v0.json",
                "--poc-comparison",
                "datasets/gold/poc_mode_comparison_v1.json",
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        report = json.loads(proc.stdout)
        self.assertEqual(
            "datasets/gold/llm_stability_runs_v0.json",
            report["phase9_handoff"]["stability_source"],
        )
        self.assertEqual(
            "datasets/gold/poc_mode_comparison_v1.json",
            report["phase9_handoff"]["poc_comparison_source"],
        )

    def test_cli_emits_gmp_acceptance_for_phase0_acceptance(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--gmp-acceptance",
                str(GMP_ACCEPTANCE_PATH),
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        metrics = json.loads(proc.stdout)
        self.assertEqual(8, metrics["criterion_count"])
        self.assertEqual(0, metrics["failed_criterion_count"])
        self.assertTrue(metrics["target_met"])

    def test_cli_fails_when_gmp_acceptance_target_is_unmet(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["criteria"][4]["status"] = "fail"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            self.prepare_gmp_acceptance_repo(temp_root)
            gmp_path = temp_root / GMP_ACCEPTANCE_PATH.relative_to(REPO_ROOT)
            gmp_path.write_text(json.dumps(data), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--gmp-acceptance",
                    str(gmp_path),
                ],
                cwd=REPO_ROOT,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual("", proc.stderr)
        self.assertEqual(1, proc.returncode)
        metrics = json.loads(proc.stdout)
        self.assertFalse(metrics["target_met"])
        self.assertEqual(1, metrics["failed_criterion_count"])
        self.assertEqual("audit_trail", metrics["failed_criteria"][0]["id"])


if __name__ == "__main__":
    unittest.main()
