from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import socket
import tempfile
import unittest
from collections.abc import Callable
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from scripts.ci.mvp_browser_e2e import (
    ACCEPTANCE_CRITERIA,
    AUTHORIZATION_ROLE_MATRIX,
    FIXTURE_PATH,
    LocalNetworkBoundaryObserver,
    NetworkBoundaryViolation,
    _acceptance_network_boundary,
    _assert_clean_git_checkout,
    _build_accepted_rerun_package,
    _dependency_snapshot,
    _inference_environment_snapshot,
    _launch_browser,
    _request_local_api_get,
    _retain_redacted_trace,
    _require_audit_payload_matches_result,
    _require_matching_event,
    _retained_evidence_paths,
    evaluate_acceptance_evidence,
    assert_rerun_equivalent,
    build_rerun_package,
    main,
    seal_rerun_package,
    validate_endpoint_configuration,
    validate_rerun_package_envelope,
    validate_rerun_package_for_workspace,
    validate_rerun_runtime_dependencies,
    run_browser_e2e,
)


def _browser_e2e_runtime_available() -> bool:
    if importlib.util.find_spec("playwright") is None:
        return False
    if os.environ.get("VERIDOC_E2E_BROWSER_CHANNEL"):
        return True
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).is_file()
    except Exception:
        return False


BROWSER_E2E_RUNTIME_AVAILABLE = _browser_e2e_runtime_available()


def _seal_event_chain(
    events: list[dict[str, object]],
) -> list[dict[str, object]]:
    previous_hash: str | None = None
    for sequence, event in enumerate(events, start=1):
        event.pop("event_hash", None)
        event.update(
            {
                "integrity_algorithm": "sha256-canonical-json-chain-v1",
                "sequence": sequence,
                "prev_event_hash": previous_hash,
            }
        )
        canonical = json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        event["event_hash"] = hashlib.sha256(canonical).hexdigest()
        previous_hash = event["event_hash"]
    return events


def _write_complete_evidence_package(run_dir: Path) -> dict[str, object]:
    run_id = "run-current"
    source_filename = "source.pdf"
    source_sha256 = "a" * 64
    job_id = "job-current"
    conversion_id = "conversion-current"
    document_id = "document-current"
    block_id = "block-current"
    artifact_id = "artifact-current"
    actor = {"id": "approver-current", "role": "approver"}
    reviewer_actor = {"id": "reviewer-current", "role": "reviewer"}
    source_bbox = {
        "x": 1.0,
        "y": 2.0,
        "width": 3.0,
        "height": 4.0,
        "unit": "pt",
        "origin": "top-left",
    }

    upload_event = _seal_event_chain(
        [{
            "event_type": "web.job_operation",
            "action": "browser_upload",
            "job_id": job_id,
            "filename": source_filename,
            "source_sha256": source_sha256,
        }]
    )[0]
    review_events = _seal_event_chain(
        [{
            "event_type": "conversion_review.action_requested",
            "action": "edit",
            "conversion_id": conversion_id,
            "document_id": document_id,
            "block_id": block_id,
            "actor": reviewer_actor,
            "source_page": 1,
            "source_bbox": source_bbox,
            "original_text": "Approved source text",
            "revised_text": "Approved source text",
            "warnings": [],
        },
        {
            "event_type": "conversion_review.action_requested",
            "action": "approve",
            "conversion_id": conversion_id,
            "document_id": document_id,
            "block_id": block_id,
            "actor": actor,
            "source_page": 1,
            "source_bbox": source_bbox,
            "original_text": "Approved source text",
            "revised_text": "Approved source text",
            "warnings": [],
        },
        {
            "event_type": "conversion_review.action_requested",
            "action": "reject",
            "conversion_id": "conversion-high-risk",
            "document_id": "document-high-risk",
            "block_id": "block-high-risk",
            "actor": actor,
        }]
    )
    review_event = review_events[1]
    (run_dir / "job-events.json").write_text(
        json.dumps([upload_event], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "review-events.json").write_text(
        json.dumps(review_events, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    created_at = "2026-07-18T12:00:00+09:00"
    (run_dir / "job-response.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "filename": source_filename,
                "status": "succeeded",
                "display_status": "completed",
                "created_at": created_at,
                "hashes": {"source_sha256": source_sha256},
                "hash_verification": {
                    "source": {
                        "status": "recorded",
                        "sha256": source_sha256,
                    },
                },
                "has_result": True,
                "artifacts": [
                    {
                        "id": "primary-docx",
                        "artifact_id": artifact_id,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    artifact_bytes = b"complete artifact"
    (run_dir / "result.docx").write_bytes(artifact_bytes)
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    prompt = {"id": "veridoc_conversion_plan", "version": "poc-08"}
    audit = {
        "schema_version": "veridoc-poc-conversion-audit/v1",
        "conversion_id": conversion_id,
        "source_filename": source_filename,
        "source_sha256": source_sha256,
        "input": {
            "filename": source_filename,
            "sha256": source_sha256,
        },
        "versions": {
            "model": None,
            "prompt": prompt,
            "schemas": {
                "conversion_audit": "veridoc-poc-conversion-audit/v1",
                "conversion_plan": 1,
                "document_ir": "document-ir/v1",
            },
        },
        "llm": {
            "requested": False,
            "model": None,
            "prompt": prompt,
            "schema_version": 1,
        },
    }
    audit_bytes = (
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    (run_dir / "audit.json").write_bytes(audit_bytes)
    audit_sha256 = hashlib.sha256(audit_bytes).hexdigest()
    api_result = {
        "status": "converted",
        "conversion_id": conversion_id,
        "audit": audit,
        "hashes": {"source_sha256": source_sha256},
        "review_items": [
            {
                "document_id": document_id,
                "block_id": block_id,
                "source_page": 1,
                "source_bbox": source_bbox,
            }
        ],
        "artifacts": [
            {
                "id": "primary-docx",
                "kind": "primary",
                "artifact_id": artifact_id,
                "sha256": artifact_sha256,
                "metadata": {
                    "role": "primary",
                    "source_filename": source_filename,
                    "source_sha256": source_sha256,
                    "output_sha256": artifact_sha256,
                },
            },
            {"id": "audit-json", "sha256": audit_sha256},
        ],
    }
    (run_dir / "api-result.json").write_text(
        json.dumps(api_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {
        "schema_version": "veridoc-mvp-browser-e2e/v1",
        "run_id": run_id,
        "authorization": {
            "decision_revision": "p12g-02-v1",
            "role_matrix": deepcopy(AUTHORIZATION_ROLE_MATRIX),
            "token_lifecycle": {
                "states": [
                    {
                        "state": "missing",
                        "status": 401,
                        "error": "auth_required",
                    },
                    {
                        "state": "rejected",
                        "status": 401,
                        "error": "auth_required",
                    },
                    {
                        "state": "forbidden",
                        "status": 403,
                        "error": "forbidden",
                        "role": "operator",
                    },
                    {
                        "state": "cleared",
                        "status": 401,
                        "error": "auth_required",
                    },
                    {
                        "state": "re_authenticated",
                        "status": 200,
                        "role": "approver",
                    },
                ],
                "re_authenticated_role": "approver",
            },
            "segregation": {
                "preceding_action": "edit",
                "reviewer_role": "reviewer",
                "approver_role": "approver",
                "reviewer_actor": reviewer_actor,
                "approver_actor": actor,
                "distinct_actor": True,
            },
        },
        "correlation": {
            "run_id": run_id,
            "upload": {
                "source_filename": source_filename,
                "source_sha256": source_sha256,
            },
            "job": {
                "job_id": job_id,
                "status": "succeeded",
                "conversion_status": "converted",
                "created_at": created_at,
            },
            "review": {
                "conversion_id": conversion_id,
                "document_id": document_id,
                "block_id": block_id,
                "action": "approve",
                "actor": actor,
            },
            "provenance": {
                "source_filename": source_filename,
                "source_sha256": source_sha256,
                "document_id": document_id,
                "block_id": block_id,
                "source_page": 1,
                "source_bbox": source_bbox,
            },
            "artifact": {
                "artifact_id": artifact_id,
                "sha256": artifact_sha256,
            },
            "audit": {
                "audit_artifact_sha256": audit_sha256,
                "job_event_count": 1,
                "review_event_count": len(review_events),
                "job_terminal_event_hash": upload_event["event_hash"],
                "review_terminal_event_hash": review_events[-1]["event_hash"],
            },
        },
        "evidence_surfaces": {
            "browser_run": {
                "correlation_id": run_id,
                "job_id": job_id,
            },
            "harness_result": {
                "correlation_id": run_id,
                "conversion_id": conversion_id,
            },
            "download_artifact": {
                "correlation_id": run_id,
                "artifact_id": artifact_id,
            },
            "audit_events": {
                "correlation_id": run_id,
                "job_event_hash": upload_event["event_hash"],
                "review_event_hash": review_event["event_hash"],
                "job_event_count": 1,
                "review_event_count": len(review_events),
                "job_terminal_event_hash": upload_event["event_hash"],
                "review_terminal_event_hash": review_events[-1]["event_hash"],
            },
        },
        "files": {
            "api_result": "api-result.json",
            "audit_artifact": "audit.json",
            "download": "result.docx",
            "job_events": "job-events.json",
            "job_response": "job-response.json",
            "review_events": "review-events.json",
        },
    }


def _evaluate_mutated_audit(
    run_dir: Path,
    evidence: dict[str, object],
    mutate: Callable[[dict[str, object]], None],
) -> dict[str, object]:
    api_result_path = run_dir / "api-result.json"
    audit_path = run_dir / "audit.json"
    api_result = json.loads(api_result_path.read_text(encoding="utf-8"))
    audit = deepcopy(api_result["audit"])
    mutate(audit)
    audit_bytes = (
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    audit_path.write_bytes(audit_bytes)
    audit_sha256 = hashlib.sha256(audit_bytes).hexdigest()
    api_result["audit"] = audit
    next(
        artifact
        for artifact in api_result["artifacts"]
        if artifact.get("id") == "audit-json"
    )["sha256"] = audit_sha256
    api_result_path.write_text(
        json.dumps(api_result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    evidence["correlation"]["audit"]["audit_artifact_sha256"] = audit_sha256
    return evaluate_acceptance_evidence(evidence, run_dir=run_dir)


def _rewrite_event_chain(
    run_dir: Path,
    evidence: dict[str, object],
    chain: str,
    mutate: Callable[[list[dict[str, object]]], None],
) -> list[dict[str, object]]:
    events_path = run_dir / f"{chain}-events.json"
    events = json.loads(events_path.read_text(encoding="utf-8"))
    mutate(events)
    _seal_event_chain(events)
    events_path.write_text(
        json.dumps(events, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    audit = evidence["correlation"]["audit"]
    surface = evidence["evidence_surfaces"]["audit_events"]
    audit[f"{chain}_event_count"] = len(events)
    audit[f"{chain}_terminal_event_hash"] = events[-1]["event_hash"]
    surface[f"{chain}_event_count"] = len(events)
    surface[f"{chain}_terminal_event_hash"] = events[-1]["event_hash"]
    correlated_event = next(
        (
            event
            for event in events
            if chain != "review" or event.get("action") == "approve"
        ),
        events[0],
    )
    surface[f"{chain}_event_hash"] = correlated_event["event_hash"]
    return events


class BrowserLaunchSelectionTest(unittest.TestCase):
    def test_default_launch_uses_playwright_managed_chromium(self) -> None:
        class Chromium:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def launch(self, **kwargs: object) -> object:
                self.calls.append(kwargs)
                return object()

        chromium = Chromium()
        playwright = type("Playwright", (), {"chromium": chromium})()

        with patch.dict(os.environ, {}, clear=True):
            _launch_browser(playwright)

        self.assertEqual(chromium.calls, [{"headless": True}])

    def test_empty_channel_uses_playwright_managed_chromium(self) -> None:
        class Chromium:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def launch(self, **kwargs: object) -> object:
                self.calls.append(kwargs)
                return object()

        chromium = Chromium()
        playwright = type("Playwright", (), {"chromium": chromium})()

        with patch.dict(
            os.environ,
            {"VERIDOC_E2E_BROWSER_CHANNEL": ""},
            clear=True,
        ):
            _launch_browser(playwright)

        self.assertEqual(chromium.calls, [{"headless": True}])

    def test_configured_channel_is_an_explicit_override(self) -> None:
        class Chromium:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def launch(self, **kwargs: object) -> object:
                self.calls.append(kwargs)
                return object()

        chromium = Chromium()
        playwright = type("Playwright", (), {"chromium": chromium})()

        with patch.dict(
            os.environ,
            {"VERIDOC_E2E_BROWSER_CHANNEL": "chrome"},
            clear=True,
        ):
            _launch_browser(playwright)

        self.assertEqual(
            chromium.calls,
            [{"channel": "chrome", "headless": True}],
        )


class TraceRedactionTest(unittest.TestCase):
    def test_all_ephemeral_role_tokens_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw_trace = root / "raw.zip"
            retained_trace = root / "retained.zip"
            secrets = {
                "reviewer": "reviewer-secret-token",
                "approver": "approver-secret-token",
            }
            with ZipFile(raw_trace, "w") as archive:
                archive.writestr(
                    "trace.network",
                    (
                        "Authorization: Bearer reviewer-secret-token\n"
                        "Authorization: Bearer approver-secret-token\n"
                    ),
                )

            _retain_redacted_trace(
                raw_trace,
                retained_trace,
                secrets=secrets,
            )

            with ZipFile(retained_trace) as archive:
                content = archive.read("trace.network")

        self.assertNotIn(b"reviewer-secret-token", content)
        self.assertNotIn(b"approver-secret-token", content)
        self.assertIn(b"<redacted-e2e-reviewer-token>", content)
        self.assertIn(b"<redacted-e2e-approver-token>", content)


class LocalApiRequestTest(unittest.TestCase):
    def test_get_disables_redirects_before_the_response_is_observed(self) -> None:
        response = object()

        class Request:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def get(self, url: str, **kwargs: object) -> object:
                self.calls.append((url, kwargs))
                return response

        request = Request()

        self.assertIs(
            _request_local_api_get(
                request,
                "http://127.0.0.1:8788/api/jobs/job-test",
                headers={"Authorization": "Bearer local-token"},
            ),
            response,
        )
        self.assertEqual(
            request.calls,
            [
                (
                    "http://127.0.0.1:8788/api/jobs/job-test",
                    {
                        "headers": {"Authorization": "Bearer local-token"},
                        "max_redirects": 0,
                    },
                )
            ],
        )


class EvidenceBoundaryValidationTest(unittest.TestCase):
    def test_complete_evidence_package_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "pass")
        self.assertEqual(acceptance["failure_reasons"], [])

    def test_authorization_evidence_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            evidence.pop("authorization")

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_AUTHORIZATION_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_segregation_evidence_rejects_same_reviewer_and_approver(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            authorization = evidence["authorization"]
            segregation = authorization["segregation"]
            segregation["reviewer_actor"] = segregation["approver_actor"]
            segregation["distinct_actor"] = False

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_SEGREGATION_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_source_bbox_rejects_invalid_origin_even_when_event_matches(
        self,
    ) -> None:
        invalid_origins = (
            ("x", -1),
            ("y", -1),
            ("x", float("inf")),
            ("y", float("nan")),
        )
        for field, invalid_value in invalid_origins:
            with (
                self.subTest(field=field, invalid_value=invalid_value),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                run_dir = Path(temporary_directory)
                evidence = _write_complete_evidence_package(run_dir)
                evidence["correlation"]["provenance"]["source_bbox"][
                    field
                ] = invalid_value

                def mutate_source_bbox(
                    events: list[dict[str, object]],
                ) -> None:
                    for event in events[:2]:
                        event["source_bbox"][field] = invalid_value  # type: ignore[index]

                _rewrite_event_chain(
                    run_dir,
                    evidence,
                    "review",
                    mutate_source_bbox,
                )

                acceptance = evaluate_acceptance_evidence(
                    evidence,
                    run_dir=run_dir,
                )

            self.assertEqual(acceptance["status"], "fail")
            self.assertIn(
                "EVIDENCE_PROVENANCE_MISSING",
                {failure["code"] for failure in acceptance["failure_reasons"]},
            )

    def test_source_bbox_rejects_oversized_integer_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            evidence["correlation"]["provenance"]["source_bbox"]["x"] = 10**400

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_PROVENANCE_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_non_approval_review_event_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            review_events = _rewrite_event_chain(
                run_dir,
                evidence,
                "review",
                lambda events: events[1].__setitem__("action", "reject"),
            )
            evidence["correlation"]["review"]["action"] = "reject"
            evidence["evidence_surfaces"]["audit_events"][
                "review_event_hash"
            ] = review_events[1]["event_hash"]

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_AUDIT_EVENT_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_audit_payload_must_match_reviewed_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)

            acceptance = _evaluate_mutated_audit(
                run_dir,
                evidence,
                lambda audit: audit.__setitem__(
                    "conversion_id",
                    "conversion-stale",
                ),
            )

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_CORRELATION_MISMATCH",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_api_result_must_match_reviewed_conversion(self) -> None:
        for conversion_id in (None, "conversion-stale"):
            with (
                self.subTest(conversion_id=conversion_id),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                run_dir = Path(temporary_directory)
                evidence = _write_complete_evidence_package(run_dir)
                api_result_path = run_dir / "api-result.json"
                api_result = json.loads(
                    api_result_path.read_text(encoding="utf-8")
                )
                if conversion_id is None:
                    api_result.pop("conversion_id")
                else:
                    api_result["conversion_id"] = conversion_id
                api_result_path.write_text(
                    json.dumps(api_result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

                acceptance = evaluate_acceptance_evidence(
                    evidence,
                    run_dir=run_dir,
                )

            self.assertEqual(acceptance["status"], "fail")
            self.assertIn(
                "EVIDENCE_CORRELATION_MISMATCH",
                {failure["code"] for failure in acceptance["failure_reasons"]},
            )

    def test_surface_ids_fail_closed_for_unhashable_values(self) -> None:
        for invalid_value in ([], {}):
            with (
                self.subTest(invalid_value=invalid_value),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                run_dir = Path(temporary_directory)
                evidence = _write_complete_evidence_package(run_dir)
                evidence["evidence_surfaces"]["browser_run"][
                    "correlation_id"
                ] = invalid_value

                acceptance = evaluate_acceptance_evidence(
                    evidence,
                    run_dir=run_dir,
                )

                self.assertEqual(acceptance["status"], "fail")
                self.assertIn(
                    "EVIDENCE_CORRELATION_MISMATCH",
                    {failure["code"] for failure in acceptance["failure_reasons"]},
                )

    def test_early_return_preserves_missing_evidence_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            acceptance = evaluate_acceptance_evidence(
                {},
                run_dir=Path(temporary_directory),
            )

        self.assertEqual(acceptance["status"], "fail")
        self.assertEqual(
            {
                "EVIDENCE_PROVENANCE_MISSING",
                "EVIDENCE_AUDIT_MISSING",
            },
            {
                failure["code"]
                for failure in acceptance["failure_reasons"]
            },
        )

    def test_hash_fields_fail_closed_for_unhashable_values(self) -> None:
        paths = (
            ("correlation", "upload", "source_sha256"),
            ("correlation", "artifact", "sha256"),
            ("correlation", "audit", "audit_artifact_sha256"),
        )
        for path in paths:
            for invalid_value in ([], {}):
                with (
                    self.subTest(path=path, invalid_value=invalid_value),
                    tempfile.TemporaryDirectory() as temporary_directory,
                ):
                    run_dir = Path(temporary_directory)
                    evidence = _write_complete_evidence_package(run_dir)
                    target = evidence
                    for field in path[:-1]:
                        target = target[field]
                    target[path[-1]] = invalid_value

                    acceptance = evaluate_acceptance_evidence(
                        evidence,
                        run_dir=run_dir,
                    )

                    self.assertEqual(acceptance["status"], "fail")
                    self.assertIn(
                        "EVIDENCE_HASH_MISMATCH",
                        {
                            failure["code"]
                            for failure in acceptance["failure_reasons"]
                        },
                    )

    def test_schema_lineage_requires_authoritative_versions(self) -> None:
        invalid_versions = {
            "conversion_audit": "",
            "conversion_plan": 999,
            "document_ir": "document-ir/v0",
        }
        for field, invalid_value in invalid_versions.items():
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                run_dir = Path(temporary_directory)
                evidence = _write_complete_evidence_package(run_dir)

                acceptance = _evaluate_mutated_audit(
                    run_dir,
                    evidence,
                    lambda audit: audit["versions"]["schemas"].__setitem__(
                        field,
                        invalid_value,
                    ),
                )

            self.assertEqual(acceptance["status"], "fail")
            self.assertIn(
                "EVIDENCE_VERSION_MISMATCH",
                {failure["code"] for failure in acceptance["failure_reasons"]},
            )

    def test_audit_payload_schema_requires_authoritative_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)

            acceptance = _evaluate_mutated_audit(
                run_dir,
                evidence,
                lambda audit: audit.__setitem__(
                    "schema_version",
                    "veridoc-poc-conversion-audit/v0",
                ),
            )

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_VERSION_MISMATCH",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_prompt_lineage_requires_authoritative_prompt(self) -> None:
        def replace_prompt(audit: dict[str, object]) -> None:
            stale_prompt = {
                "id": "veridoc_conversion_plan_stale",
                "version": "poc-07",
            }
            audit["versions"]["prompt"] = stale_prompt
            audit["llm"]["prompt"] = stale_prompt

        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)

            acceptance = _evaluate_mutated_audit(
                run_dir,
                evidence,
                replace_prompt,
            )

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_VERSION_MISMATCH",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_audit_input_metadata_must_match_uploaded_source(self) -> None:
        invalid_input = {
            "sha256": "b" * 64,
            "filename": "source-stale.pdf",
        }
        for field, invalid_value in invalid_input.items():
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                run_dir = Path(temporary_directory)
                evidence = _write_complete_evidence_package(run_dir)

                acceptance = _evaluate_mutated_audit(
                    run_dir,
                    evidence,
                    lambda audit: audit["input"].__setitem__(
                        field,
                        invalid_value,
                    ),
                )

            self.assertEqual(acceptance["status"], "fail")
            self.assertIn(
                (
                    "EVIDENCE_HASH_MISMATCH"
                    if field == "sha256"
                    else "EVIDENCE_PROVENANCE_MISMATCH"
                ),
                {failure["code"] for failure in acceptance["failure_reasons"]},
            )

    def test_artifact_metadata_must_match_uploaded_source(self) -> None:
        invalid_metadata = {
            "source_sha256": "b" * 64,
            "source_filename": "source-stale.pdf",
        }
        for field, invalid_value in invalid_metadata.items():
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                run_dir = Path(temporary_directory)
                evidence = _write_complete_evidence_package(run_dir)
                api_result_path = run_dir / "api-result.json"
                api_result = json.loads(
                    api_result_path.read_text(encoding="utf-8")
                )
                api_result["artifacts"][0]["metadata"][field] = invalid_value
                api_result_path.write_text(
                    json.dumps(api_result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

                acceptance = evaluate_acceptance_evidence(
                    evidence,
                    run_dir=run_dir,
                )

            self.assertEqual(acceptance["status"], "fail")
            self.assertIn(
                (
                    "EVIDENCE_HASH_MISMATCH"
                    if field == "source_sha256"
                    else "EVIDENCE_PROVENANCE_MISMATCH"
                ),
                {failure["code"] for failure in acceptance["failure_reasons"]},
            )

    def test_upload_event_must_match_uploaded_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            job_events_path = run_dir / "job-events.json"
            upload_event = json.loads(
                job_events_path.read_text(encoding="utf-8")
            )[0]
            upload_event["filename"] = "source-stale.pdf"
            upload_event.pop("event_hash")
            canonical = json.dumps(
                upload_event,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            upload_event["event_hash"] = hashlib.sha256(canonical).hexdigest()
            job_events_path.write_text(
                json.dumps([upload_event], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            evidence["evidence_surfaces"]["audit_events"][
                "job_event_hash"
            ] = upload_event["event_hash"]

            acceptance = evaluate_acceptance_evidence(
                evidence,
                run_dir=run_dir,
            )

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_AUDIT_EVENT_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_failed_job_state_cannot_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            evidence["correlation"]["job"].update(
                {"status": "failed", "conversion_status": "failed"}
            )
            job_response = json.loads(
                (run_dir / "job-response.json").read_text(encoding="utf-8")
            )
            job_response["status"] = "failed"
            (run_dir / "job-response.json").write_text(
                json.dumps(job_response, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_JOB_INCOMPLETE",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_browser_evidence_schema_must_be_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            evidence["schema_version"] = "veridoc-mvp-browser-e2e/v0"

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_SCHEMA_UNSUPPORTED",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_conversion_status_must_match_browser_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            api_result_path = run_dir / "api-result.json"
            api_result = json.loads(api_result_path.read_text(encoding="utf-8"))
            api_result["status"] = "blocked"
            api_result_path.write_text(
                json.dumps(api_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_JOB_STATE_MISMATCH",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_job_display_status_binds_result_without_top_level_status(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            api_result_path = run_dir / "api-result.json"
            api_result = json.loads(api_result_path.read_text(encoding="utf-8"))
            api_result.pop("status")
            api_result_path.write_text(
                json.dumps(api_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "pass")
        self.assertEqual(acceptance["failure_reasons"], [])

    def test_job_response_must_match_uploaded_source(self) -> None:
        invalid_source = {
            "filename": "source-stale.pdf",
            "source_sha256": "b" * 64,
        }
        for field, invalid_value in invalid_source.items():
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                run_dir = Path(temporary_directory)
                evidence = _write_complete_evidence_package(run_dir)
                job_response_path = run_dir / "job-response.json"
                job_response = json.loads(
                    job_response_path.read_text(encoding="utf-8")
                )
                if field == "filename":
                    job_response["filename"] = invalid_value
                else:
                    job_response["hashes"][field] = invalid_value
                job_response_path.write_text(
                    json.dumps(job_response, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

                acceptance = evaluate_acceptance_evidence(
                    evidence,
                    run_dir=run_dir,
                )

            self.assertEqual(acceptance["status"], "fail")
            self.assertIn(
                "EVIDENCE_JOB_STATE_MISMATCH",
                {failure["code"] for failure in acceptance["failure_reasons"]},
            )

    def test_job_response_must_reference_primary_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            job_response_path = run_dir / "job-response.json"
            job_response = json.loads(
                job_response_path.read_text(encoding="utf-8")
            )
            job_response["artifacts"][0]["artifact_id"] = "artifact-stale"
            job_response_path.write_text(
                json.dumps(job_response, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_ARTIFACT_MISMATCH",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_missing_job_identifier_cannot_self_validate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            evidence["correlation"]["job"].pop("job_id")
            evidence["evidence_surfaces"]["browser_run"].pop("job_id")
            _rewrite_event_chain(
                run_dir,
                evidence,
                "job",
                lambda events: events[0].pop("job_id"),
            )
            job_response = json.loads(
                (run_dir / "job-response.json").read_text(encoding="utf-8")
            )
            job_response.pop("job_id")
            (run_dir / "job-response.json").write_text(
                json.dumps(job_response, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_IDENTIFIER_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_malformed_artifact_list_returns_fail_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            api_result_path = run_dir / "api-result.json"
            api_result = json.loads(api_result_path.read_text(encoding="utf-8"))
            api_result["artifacts"] = None
            api_result_path.write_text(
                json.dumps(api_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_ARTIFACT_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_correlated_download_must_be_primary_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            api_result_path = run_dir / "api-result.json"
            api_result = json.loads(api_result_path.read_text(encoding="utf-8"))
            selected = api_result["artifacts"][0]
            selected.update({"id": "debug-json", "kind": "debug"})
            selected["metadata"]["role"] = "debug"
            api_result_path.write_text(
                json.dumps(api_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_ARTIFACT_MISMATCH",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_provenance_must_match_authoritative_result_review_item(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            changed_bbox = {
                **evidence["correlation"]["provenance"]["source_bbox"],
                "x": 12.0,
            }
            evidence["correlation"]["provenance"]["source_bbox"] = changed_bbox
            _rewrite_event_chain(
                run_dir,
                evidence,
                "review",
                lambda events: events[1].__setitem__(
                    "source_bbox",
                    changed_bbox,
                ),
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_PROVENANCE_MISMATCH",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_provenance_rejects_unsupported_bbox_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            changed_bbox = {
                **evidence["correlation"]["provenance"]["source_bbox"],
                "unit": "bogus",
            }
            evidence["correlation"]["provenance"]["source_bbox"] = changed_bbox

            api_result_path = run_dir / "api-result.json"
            api_result = json.loads(api_result_path.read_text(encoding="utf-8"))
            api_result["review_items"][0]["source_bbox"] = changed_bbox
            api_result_path.write_text(
                json.dumps(api_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _rewrite_event_chain(
                run_dir,
                evidence,
                "review",
                lambda events: events[1].__setitem__(
                    "source_bbox",
                    changed_bbox,
                ),
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_PROVENANCE_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_approval_requires_nonempty_actor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            evidence["correlation"]["review"]["actor"] = None
            _rewrite_event_chain(
                run_dir,
                evidence,
                "review",
                lambda events: events[1].__setitem__("actor", None),
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_ACTOR_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_source_filename_cannot_self_validate_as_blank(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            evidence["correlation"]["upload"]["source_filename"] = ""
            evidence["correlation"]["provenance"]["source_filename"] = ""

            job_response_path = run_dir / "job-response.json"
            job_response = json.loads(
                job_response_path.read_text(encoding="utf-8")
            )
            job_response["filename"] = ""
            job_response_path.write_text(
                json.dumps(job_response, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            api_result_path = run_dir / "api-result.json"
            api_result = json.loads(api_result_path.read_text(encoding="utf-8"))
            audit = api_result["audit"]
            audit["source_filename"] = ""
            audit["input"]["filename"] = ""
            next(
                artifact
                for artifact in api_result["artifacts"]
                if artifact.get("kind") == "primary"
            )["metadata"]["source_filename"] = ""
            audit_bytes = (
                json.dumps(audit, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            (run_dir / "audit.json").write_bytes(audit_bytes)
            audit_sha256 = hashlib.sha256(audit_bytes).hexdigest()
            next(
                artifact
                for artifact in api_result["artifacts"]
                if artifact.get("id") == "audit-json"
            )["sha256"] = audit_sha256
            api_result_path.write_text(
                json.dumps(api_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            evidence["correlation"]["audit"]["audit_artifact_sha256"] = (
                audit_sha256
            )
            _rewrite_event_chain(
                run_dir,
                evidence,
                "job",
                lambda events: events[0].__setitem__("filename", ""),
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_IDENTIFIER_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_approval_requires_review_event_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            _rewrite_event_chain(
                run_dir,
                evidence,
                "review",
                lambda events: events[1].__setitem__(
                    "event_type",
                    "conversion_review.unrelated",
                ),
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_AUDIT_EVENT_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_approval_requires_review_event_text_payload(self) -> None:
        for field in ("original_text", "revised_text"):
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                run_dir = Path(temporary_directory)
                evidence = _write_complete_evidence_package(run_dir)
                _rewrite_event_chain(
                    run_dir,
                    evidence,
                    "review",
                    lambda events, field=field: events[1].pop(field),
                )

                acceptance = evaluate_acceptance_evidence(
                    evidence,
                    run_dir=run_dir,
                )

                self.assertEqual(acceptance["status"], "fail")
                self.assertIn(
                    "EVIDENCE_AUDIT_EVENT_MISSING",
                    {failure["code"] for failure in acceptance["failure_reasons"]},
                )

    def test_invalid_utf8_review_text_fails_closed(self) -> None:
        for field in ("original_text", "revised_text"):
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                run_dir = Path(temporary_directory)
                evidence = _write_complete_evidence_package(run_dir)
                review_events_path = run_dir / "review-events.json"
                review_events = json.loads(
                    review_events_path.read_text(encoding="utf-8")
                )
                review_events[1][field] = "\ud800"
                review_events_path.write_text(
                    json.dumps(review_events, ensure_ascii=True, indent=2) + "\n",
                    encoding="utf-8",
                )

                acceptance = evaluate_acceptance_evidence(
                    evidence,
                    run_dir=run_dir,
                )

                self.assertEqual(acceptance["status"], "fail")
                self.assertIn(
                    "EVIDENCE_AUDIT_CHAIN_INVALID",
                    {failure["code"] for failure in acceptance["failure_reasons"]},
                )

    def test_upload_requires_job_operation_event_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            _rewrite_event_chain(
                run_dir,
                evidence,
                "job",
                lambda events: events[0].__setitem__(
                    "event_type",
                    "web.unrelated",
                ),
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_AUDIT_EVENT_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_truncated_audit_chain_rejects_valid_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            review_events_path = run_dir / "review-events.json"
            review_events = json.loads(
                review_events_path.read_text(encoding="utf-8")
            )
            review_events_path.write_text(
                json.dumps(review_events[:1], ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_AUDIT_CHAIN_TRUNCATED",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_boolean_source_page_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            evidence["correlation"]["provenance"]["source_page"] = True
            _rewrite_event_chain(
                run_dir,
                evidence,
                "review",
                lambda events: events[0].__setitem__("source_page", True),
            )

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_PROVENANCE_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_job_timestamp_must_match_retained_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            evidence = _write_complete_evidence_package(run_dir)
            evidence["correlation"]["job"][
                "created_at"
            ] = "2026-07-18T13:00:00+09:00"

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_JOB_STATE_MISMATCH",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_retained_rerun_allows_job_audit_and_response_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo_root = Path(temporary_directory)
            run_dir = repo_root / f"p12g03-{'a' * 32}"
            run_dir.mkdir()
            rerun_package_path = run_dir / "rerun-package.json"
            rerun_package_path.write_text("{}\n", encoding="utf-8")
            (run_dir / "job-events.json").write_text("[]\n", encoding="utf-8")
            (run_dir / "job-response.json").write_text("{}\n", encoding="utf-8")

            retained = _retained_evidence_paths(
                rerun_package_path,
                repo_root=repo_root,
            )

        self.assertEqual(
            {path.name for path in retained},
            {"rerun-package.json", "job-events.json", "job-response.json"},
        )

    def test_evidence_files_cannot_escape_correlation_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            evidence_root = Path(temporary_directory)
            previous_run = evidence_root / "previous-run"
            previous_run.mkdir()
            evidence = _write_complete_evidence_package(previous_run)
            current_run = evidence_root / "current-run"
            current_run.mkdir()
            evidence["files"] = {
                name: f"../previous-run/{filename}"
                for name, filename in evidence["files"].items()
            }

            acceptance = evaluate_acceptance_evidence(
                evidence,
                run_dir=current_run,
            )

        self.assertEqual(acceptance["status"], "fail")
        self.assertIn(
            "EVIDENCE_AUDIT_MISSING",
            {failure["code"] for failure in acceptance["failure_reasons"]},
        )

    def test_existing_artifact_does_not_pass_without_provenance_or_audit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_dir = Path(temporary_directory)
            (run_dir / "result.docx").write_bytes(b"artifact exists")
            evidence = {
                "schema_version": "veridoc-mvp-browser-e2e/v1",
                "run_id": "run-current",
                "correlation": {
                    "run_id": "run-current",
                    "artifact": {
                        "sha256": hashlib.sha256(b"artifact exists").hexdigest(),
                    },
                },
                "files": {"download": "result.docx"},
            }

            acceptance = evaluate_acceptance_evidence(evidence, run_dir=run_dir)

        self.assertEqual(acceptance["status"], "fail")
        self.assertEqual(
            acceptance["failure_reasons"],
            [
                {
                    "code": "EVIDENCE_PROVENANCE_MISSING",
                    "boundary": "provenance",
                    "message": (
                        "Source page and bounding-box provenance are required "
                        "before acceptance can pass."
                    ),
                },
                {
                    "code": "EVIDENCE_AUDIT_MISSING",
                    "boundary": "audit",
                    "message": (
                        "Job and review audit events are required before "
                        "acceptance can pass."
                    ),
                },
            ],
        )

    def test_matching_event_requires_every_authoritative_field(self) -> None:
        expected_fields = {
            "action": "browser_upload",
            "job_id": "job-current",
            "source_sha256": "a" * 64,
        }
        current_event = {**expected_fields, "event_type": "web.job_operation"}
        stale_event = {**current_event, "source_sha256": "b" * 64}

        event, count = _require_matching_event(
            [stale_event, current_event],
            expected_fields=expected_fields,
            description="browser upload audit event",
        )

        self.assertIs(event, current_event)
        self.assertEqual(count, 1)

    def test_matching_event_rejects_approval_for_another_block(self) -> None:
        expected_fields = {
            "action": "approve",
            "conversion_id": "conversion-current",
            "document_id": "document-current",
            "block_id": "block-current",
        }

        with self.assertRaisesRegex(
            AssertionError,
            "browser approval audit event was not bound to the browser run",
        ):
            _require_matching_event(
                [{**expected_fields, "block_id": "block-stale"}],
                expected_fields=expected_fields,
                description="browser approval audit event",
            )

    def test_audit_artifact_must_equal_current_result_audit(self) -> None:
        current_audit = {
            "conversion_id": "conversion-current",
            "source_sha256": "a" * 64,
            "schema_version": "veridoc-poc-conversion-audit/v1",
        }
        self.assertIs(
            _require_audit_payload_matches_result(
                current_audit,
                {"audit": current_audit},
            ),
            current_audit,
        )

        with self.assertRaisesRegex(
            AssertionError,
            "did not match the current browser result audit",
        ):
            _require_audit_payload_matches_result(
                {**current_audit, "conversion_id": "conversion-stale"},
                {"audit": current_audit},
            )


class LocalNetworkBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.observer = LocalNetworkBoundaryObserver(
            allowed_origins=("http://127.0.0.1:8765",)
        )

    def test_local_http_attempt_is_recorded_without_external_send(self) -> None:
        self.observer.observe_http_attempt(
            "http://127.0.0.1:8765/api/jobs",
            method="POST",
            source="playwright",
        )

        result = self.observer.result()

        self.assertEqual(result["http_attempt_count"], 1)
        self.assertEqual(result["external_attempt_count"], 0)
        self.assertEqual(result["external_ai_api_send_count"], 0)
        self.assertEqual(result["status"], "pass")

    def test_external_http_attempt_is_recorded_and_rejected(self) -> None:
        with self.assertRaisesRegex(
            NetworkBoundaryViolation,
            "external HTTP attempt blocked",
        ):
            self.observer.observe_http_attempt(
                "https://api.example.invalid/v1/chat/completions",
                method="POST",
                source="playwright",
            )

        result = self.observer.result()
        self.assertEqual(result["external_attempt_count"], 1)
        self.assertEqual(result["external_ai_api_send_count"], 1)
        self.assertEqual(result["status"], "fail")

    def test_external_dns_attempt_is_recorded_and_rejected(self) -> None:
        with self.assertRaisesRegex(
            NetworkBoundaryViolation,
            "external DNS attempt blocked",
        ):
            self.observer.observe_dns_attempt(
                "api.example.invalid",
                source="python_socket",
            )

        result = self.observer.result()
        self.assertEqual(result["dns_attempt_count"], 1)
        self.assertEqual(result["external_attempt_count"], 1)

    def test_external_endpoint_configuration_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            NetworkBoundaryViolation,
            "OPENAI_BASE_URL.*external endpoint",
        ):
            validate_endpoint_configuration(
                {"OPENAI_BASE_URL": "https://api.example.invalid/v1"},
                allowed_origins=self.observer.allowed_origins,
            )

    def test_external_profile_endpoint_configuration_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            NetworkBoundaryViolation,
            "VERIDOC_STANDARD_OPENAI_BASE_URL.*external endpoint",
        ):
            validate_endpoint_configuration(
                {
                    "VERIDOC_STANDARD_OPENAI_BASE_URL": (
                        "https://api.example.invalid/v1"
                    )
                },
                allowed_origins=self.observer.allowed_origins,
            )

    def test_malformed_endpoint_port_is_rejected_by_network_boundary(self) -> None:
        with self.assertRaisesRegex(
            NetworkBoundaryViolation,
            "VERIDOC_STANDARD_OPENAI_BASE_URL.*malformed endpoint",
        ):
            validate_endpoint_configuration(
                {
                    "VERIDOC_STANDARD_OPENAI_BASE_URL": (
                        "http://127.0.0.1:api_key=secret/v1"
                    )
                },
                allowed_origins=self.observer.allowed_origins,
            )

    def test_private_profile_endpoint_configuration_is_allowed(self) -> None:
        configured = validate_endpoint_configuration(
            {
                "VERIDOC_STANDARD_OPENAI_BASE_URL": (
                    "http://192.168.1.10:8000/v1"
                )
            },
            allowed_origins=self.observer.allowed_origins,
        )

        self.assertEqual(
            configured,
            [
                {
                    "name": "VERIDOC_STANDARD_OPENAI_BASE_URL",
                    "origin": "http://192.168.1.10:8000",
                }
            ],
        )

    def test_configured_private_endpoint_is_allowed_by_runtime_guards(self) -> None:
        endpoint_environment = {
            "VERIDOC_STANDARD_OPENAI_BASE_URL": "http://192.168.1.10:8000/v1",
            "VERIDOC_HIGH_QUALITY_OPENAI_BASE_URL": "http://[fd00::10]:8001/v1",
        }
        with patch.dict(os.environ, endpoint_environment, clear=True):
            with _acceptance_network_boundary(
                "http://127.0.0.1:8765"
            ) as observer:
                observer.observe_dns_attempt(
                    "192.168.1.10",
                    source="python_socket",
                )
                observer.observe_socket_attempt(
                    ("192.168.1.10", 8000),
                    source="python_socket",
                )
                observer.observe_dns_attempt(
                    "fd00::10",
                    source="python_socket",
                )
                observer.observe_socket_attempt(
                    ("fd00::10", 8001, 0, 0),
                    source="python_socket",
                )

        result = observer.result()
        self.assertIn("http://192.168.1.10:8000", result["allowed_origins"])
        self.assertIn("http://[fd00::10]:8001", result["allowed_origins"])
        self.assertEqual(result["external_attempt_count"], 0)

    def test_unconfigured_private_socket_target_is_rejected(self) -> None:
        observer = LocalNetworkBoundaryObserver(
            allowed_origins=(
                "http://127.0.0.1:8765",
                "http://192.168.1.10:8000",
            )
        )

        with self.assertRaisesRegex(
            NetworkBoundaryViolation,
            "external socket attempt blocked",
        ):
            observer.observe_socket_attempt(
                ("192.168.1.10", 8001),
                source="python_socket",
            )

        self.assertEqual(observer.result()["external_attempt_count"], 1)

    def test_unconfigured_loopback_socket_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            NetworkBoundaryViolation,
            "external socket attempt blocked",
        ):
            with self.observer.observe_python_network():
                with socket.socket() as client:
                    client.connect_ex(("127.0.0.1", 8766))

        result = self.observer.result()
        self.assertEqual(result["external_attempt_count"], 1)
        self.assertEqual(result["attempts"][0]["target"], "127.0.0.1:8766")

    def test_external_connect_ex_attempt_is_recorded_and_rejected(self) -> None:
        with self.assertRaisesRegex(
            NetworkBoundaryViolation,
            "external socket attempt blocked",
        ):
            with self.observer.observe_python_network():
                with socket.socket() as client:
                    client.connect_ex(("192.0.2.10", 443))

        result = self.observer.result()
        self.assertEqual(result["external_attempt_count"], 1)
        self.assertEqual(result["attempts"][0]["kind"], "socket")


class RerunPackageValidationTest(unittest.TestCase):
    def test_dependency_snapshot_uses_override_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            requirements_path = repo_root / "requirements-browser-e2e.txt"
            requirements_path.write_text("playwright==1.49.0\n", encoding="utf-8")
            expected_hash = hashlib.sha256(requirements_path.read_bytes()).hexdigest()
            distribution = type(
                "Distribution",
                (),
                {"version": "1.49.0", "requires": []},
            )()

            with patch(
                "scripts.ci.mvp_browser_e2e.importlib.metadata.distribution",
                return_value=distribution,
            ):
                snapshot = _dependency_snapshot(
                    browser_version="test-browser",
                    repo_root=repo_root,
                )

        self.assertEqual(
            snapshot["requirement_files"],
            [
                {
                    "path": "requirements-browser-e2e.txt",
                    "sha256": expected_hash,
                }
            ],
        )

    def test_inference_snapshot_requires_model_to_select_profile(self) -> None:
        profiles = {
            "profiles": [
                {
                    "id": "standard",
                    "base_url_env": "VERIDOC_STANDARD_OPENAI_BASE_URL",
                    "model_env": "VERIDOC_STANDARD_MODEL",
                    "api_key_env": "VERIDOC_STANDARD_OPENAI_API_KEY",
                    "optional_env": [],
                }
            ]
        }

        snapshot = _inference_environment_snapshot(
            profiles,
            environment={
                "VERIDOC_STANDARD_OPENAI_BASE_URL": "http://127.0.0.1:8000/v1"
            },
        )

        self.assertEqual(snapshot["mode"], "deterministic-fallback")
        self.assertIsNone(snapshot["selected_profile"])

    def test_inference_snapshot_preserves_exact_runtime_values(self) -> None:
        profiles = {
            "profiles": [
                {
                    "id": "standard",
                    "base_url_env": "VERIDOC_STANDARD_OPENAI_BASE_URL",
                    "model_env": "VERIDOC_STANDARD_MODEL",
                    "api_key_env": "VERIDOC_STANDARD_OPENAI_API_KEY",
                    "optional_env": [],
                }
            ]
        }
        endpoint = " http://127.0.0.1:8000/v1 "
        model = " qwen "

        snapshot = _inference_environment_snapshot(
            profiles,
            environment={
                "VERIDOC_STANDARD_OPENAI_BASE_URL": endpoint,
                "VERIDOC_STANDARD_MODEL": model,
            },
        )

        environment = snapshot["profiles"][0]["environment"]
        self.assertEqual(environment["VERIDOC_STANDARD_OPENAI_BASE_URL"], endpoint)
        self.assertEqual(environment["VERIDOC_STANDARD_MODEL"], model)
        self.assertEqual(snapshot["mode"], "local-llm")

    def test_dirty_checkout_is_rejected_before_package_sealing(self) -> None:
        with patch(
            "scripts.ci.mvp_browser_e2e._git_status_porcelain",
            return_value=" M scripts/ci/mvp_browser_e2e.py\n",
        ):
            with self.assertRaisesRegex(ValueError, "dirty checkout"):
                _assert_clean_git_checkout()

    def test_generated_evidence_dir_is_excluded_from_clean_check(self) -> None:
        repo_root = Path("repo").resolve()
        generated_evidence_dir = repo_root / "evidence" / "run"
        with patch("scripts.ci.mvp_browser_e2e.subprocess.run") as run:
            run.return_value.stdout = ""

            _assert_clean_git_checkout(
                repo_root,
                excluded_paths=(generated_evidence_dir,),
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".",
                ":(exclude,top,literal)evidence/run",
            ],
        )

    def test_dirty_checkout_is_rejected_before_rerun_validation(self) -> None:
        with patch(
            "scripts.ci.mvp_browser_e2e._git_status_porcelain",
            return_value=" M services/api/poc_web.py\n",
        ):
            with self.assertRaisesRegex(ValueError, "dirty checkout"):
                validate_rerun_package_for_workspace({})

    def test_retained_package_is_excluded_from_rerun_clean_check(self) -> None:
        repo_root = Path("repo").resolve()
        rerun_package_path = repo_root / "evidence" / "rerun-package.json"
        with patch("scripts.ci.mvp_browser_e2e.subprocess.run") as run:
            run.return_value.stdout = ""
            with self.assertRaises(ValueError):
                validate_rerun_package_for_workspace(
                    {},
                    repo_root=repo_root,
                    rerun_package_path=rerun_package_path,
                )

        self.assertEqual(
            run.call_args.args[0],
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".",
                ":(exclude,top,literal)evidence/rerun-package.json",
            ],
        )

    def test_retained_evidence_bundle_is_excluded_from_rerun_clean_check(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            run_dir = repo_root / f"p12g03-{'a' * 32}"
            run_dir.mkdir()
            retained_names = (
                "01-recovery.png",
                "download-result.xlsx",
                "evidence.json",
                "rerun-package.json",
                "trace.zip",
            )
            for name in retained_names:
                (run_dir / name).write_text("retained\n", encoding="utf-8")
            rerun_package_path = run_dir / "rerun-package.json"
            with patch("scripts.ci.mvp_browser_e2e.subprocess.run") as run:
                run.return_value.stdout = ""
                with self.assertRaises(ValueError):
                    validate_rerun_package_for_workspace(
                        {},
                        repo_root=repo_root,
                        rerun_package_path=rerun_package_path,
                    )

        self.assertEqual(
            run.call_args.args[0],
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".",
                *[
                    f":(exclude,top,literal){run_dir.name}/{name}"
                    for name in retained_names
                ],
            ],
        )

    def test_unexpected_retained_evidence_file_remains_in_clean_check(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            run_dir = repo_root / f"p12g03-{'a' * 32}"
            run_dir.mkdir()
            rerun_package_path = run_dir / "rerun-package.json"
            rerun_package_path.write_text("{}\n", encoding="utf-8")
            (run_dir / "unexpected.txt").write_text("dirty\n", encoding="utf-8")
            with patch("scripts.ci.mvp_browser_e2e.subprocess.run") as run:
                run.return_value.stdout = (
                    f"?? {run_dir.name}/unexpected.txt\n"
                )
                with self.assertRaisesRegex(ValueError, "dirty checkout"):
                    validate_rerun_package_for_workspace(
                        {},
                        repo_root=repo_root,
                        rerun_package_path=rerun_package_path,
                    )

        self.assertNotIn(
            f":(exclude,top,literal){run_dir.name}/unexpected.txt",
            run.call_args.args[0],
        )

    def test_retained_package_is_excluded_from_final_package_sealing(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        generated_evidence_dir = Path("generated-evidence").resolve()
        retained_package_path = Path("retained-rerun-package.json").resolve()
        with patch(
            "scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"
        ) as assert_clean:
            build_rerun_package(
                evidence,
                browser_version="test-browser",
                generated_evidence_dir=generated_evidence_dir,
                retained_rerun_package_path=retained_package_path,
            )

        self.assertEqual(
            assert_clean.call_args.kwargs["excluded_paths"],
            (generated_evidence_dir, retained_package_path),
        )

    def test_cli_passes_retained_package_path_to_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rerun_package_path = Path(temp_dir) / "rerun-package.json"
            rerun_package_path.write_text("{}\n", encoding="utf-8")
            with (
                patch(
                    "sys.argv",
                    [
                        "mvp_browser_e2e.py",
                        "--rerun-package",
                        str(rerun_package_path),
                    ],
                ),
                patch(
                    "scripts.ci.mvp_browser_e2e.validate_rerun_package_for_workspace",
                    return_value={"validated": True},
                ) as validate,
                patch(
                    "scripts.ci.mvp_browser_e2e.run_browser_e2e",
                    return_value={"run_id": "rerun"},
                ) as run,
                patch("builtins.print"),
            ):
                self.assertEqual(main(), 0)

        validate.assert_called_once_with(
            {},
            rerun_package_path=rerun_package_path,
        )
        self.assertEqual(
            run.call_args.kwargs["retained_rerun_package_path"],
            rerun_package_path,
        )

    def test_package_payload_tampering_is_rejected(self) -> None:
        envelope = seal_rerun_package(
            {
                "schema_version": "veridoc-mvp-rerun-package/v1",
                "commit": "a" * 40,
                "inputs": [],
            }
        )
        envelope["package"]["commit"] = "b" * 40

        with self.assertRaisesRegex(ValueError, "rerun package integrity"):
            validate_rerun_package_envelope(envelope)

    def test_generated_package_pins_complete_inputs_and_rejects_hash_drift(
        self,
    ) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
            envelope = build_rerun_package(
                evidence,
                browser_version="test-browser",
            )
        with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
            package = validate_rerun_package_for_workspace(envelope)

        self.assertRegex(package["commit"], r"^[0-9a-f]{40}$")
        self.assertGreaterEqual(len(package["inputs"]), 5)
        self.assertTrue(package["configuration"]["profiles"])
        self.assertIn(
            "inference_environment",
            package["configuration"],
        )
        self.assertTrue(package["dependencies"]["requirement_files"])
        self.assertIn("prompt", package["versions"])
        self.assertIn("<rerun-package-path>", package["commands"]["rerun"])

        package["inputs"][0]["sha256"] = "0" * 64
        drifted_envelope = seal_rerun_package(package)
        with self.assertRaisesRegex(ValueError, "input hash mismatch"):
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                validate_rerun_package_for_workspace(drifted_envelope)

    def test_resealed_package_metadata_drift_is_rejected(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
            baseline = build_rerun_package(
                evidence,
                browser_version="test-browser",
            )["package"]

        mutations = (
            (
                "configuration sha256",
                ("configuration", "sha256"),
                "0" * 64,
                "configuration metadata",
            ),
            (
                "configuration profiles",
                ("configuration", "profiles"),
                [],
                "configuration metadata",
            ),
            (
                "configuration network boundary",
                ("configuration", "network_boundary"),
                {"mode": "tampered"},
                "configuration metadata",
            ),
            (
                "prompt version",
                ("versions", "prompt"),
                "tampered-prompt",
                "version metadata",
            ),
            (
                "schema version",
                ("versions", "schemas", "conversion_plan"),
                "tampered-schema",
                "version metadata",
            ),
            (
                "initial command",
                ("commands", "initial"),
                "python3 tampered.py",
                "command metadata",
            ),
            (
                "equivalence rule",
                ("equivalence", "rule"),
                "tampered rule",
                "equivalence metadata",
            ),
        )
        for label, path, replacement, error in mutations:
            with self.subTest(label=label):
                package = json.loads(json.dumps(baseline))
                target = package
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = replacement
                with self.assertRaisesRegex(ValueError, error):
                    with patch(
                        "scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"
                    ):
                        validate_rerun_package_for_workspace(
                            seal_rerun_package(package)
                        )

    def test_package_missing_required_input_is_rejected(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
            envelope = build_rerun_package(
                evidence,
                browser_version="test-browser",
            )
        package = envelope["package"]
        package["inputs"].pop()

        with self.assertRaisesRegex(ValueError, "required input set"):
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                validate_rerun_package_for_workspace(seal_rerun_package(package))

    def test_package_runtime_dependency_drift_is_rejected(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
            envelope = build_rerun_package(
                evidence,
                browser_version="test-browser",
            )
        package = envelope["package"]
        package["dependencies"]["runtime"]["python"] = "0.0.0"

        with self.assertRaisesRegex(ValueError, "runtime dependencies"):
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                validate_rerun_package_for_workspace(seal_rerun_package(package))

    def test_package_transitive_runtime_dependency_drift_is_rejected(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        real_distribution = importlib.metadata.distribution

        def distribution(name: str) -> object:
            normalized_name = name.lower().replace("_", "-")
            if normalized_name == "playwright":
                return type(
                    "Distribution",
                    (),
                    {"version": "1.49.0", "requires": ["pyee>=12,<14"]},
                )()
            if normalized_name == "pyee":
                return type(
                    "Distribution",
                    (),
                    {"version": "13.0.0", "requires": []},
                )()
            return real_distribution(name)

        with patch(
            "scripts.ci.mvp_browser_e2e.importlib.metadata.distribution",
            side_effect=distribution,
        ):
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                package = build_rerun_package(
                    evidence,
                    browser_version="test-browser",
                )["package"]
            distributions = package["dependencies"]["runtime"]["distributions"]
            self.assertIn("pyee", distributions)
            distributions["pyee"] = "0.0.0"

            with self.assertRaisesRegex(ValueError, "runtime dependencies"):
                with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                    validate_rerun_package_for_workspace(
                        seal_rerun_package(package)
                    )

    def test_resealed_baseline_must_match_retained_evidence(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
            package = build_rerun_package(
                evidence,
                browser_version="test-browser",
            )["package"]
        baseline = package["equivalence"]["baseline"]
        baseline["network"]["external_attempt_count"] = 1
        package["equivalence"]["baseline_sha256"] = hashlib.sha256(
            json.dumps(
                baseline,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            rerun_package_path = run_dir / "rerun-package.json"
            (run_dir / "evidence.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError,
                "baseline does not match retained evidence",
            ):
                with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                    validate_rerun_package_for_workspace(
                        seal_rerun_package(package),
                        rerun_package_path=rerun_package_path,
                    )

    def test_rerun_package_seals_all_acceptance_evidence_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            evidence = _write_complete_evidence_package(run_dir)
            evidence["files"].update(
                {
                    "trace": "trace.zip",
                    "screenshots": ["01.png", "02.png"],
                    "rerun_package": "rerun-package.json",
                }
            )
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                envelope = _build_accepted_rerun_package(
                    evidence,
                    run_dir=run_dir,
                    browser_version="test-browser",
                )

        records = envelope["package"]["equivalence"]["retained_files"]
        self.assertEqual(
            {record["role"] for record in records},
            {
                "api_result",
                "audit_artifact",
                "download",
                "job_events",
                "job_response",
                "review_events",
            },
        )
        self.assertTrue(all(len(record["sha256"]) == 64 for record in records))

    def test_rerun_validation_rejects_modified_retained_files(self) -> None:
        for filename in (
            "api-result.json",
            "audit.json",
            "result.docx",
            "job-events.json",
            "job-response.json",
            "review-events.json",
        ):
            with (
                self.subTest(filename=filename),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                run_dir = Path(temp_dir)
                rerun_package_path = run_dir / "rerun-package.json"
                evidence = _write_complete_evidence_package(run_dir)
                with patch(
                    "scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"
                ):
                    envelope = _build_accepted_rerun_package(
                        evidence,
                        run_dir=run_dir,
                        browser_version="test-browser",
                    )
                (run_dir / "evidence.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                tampered_path = run_dir / filename
                tampered_path.write_bytes(tampered_path.read_bytes() + b"\n")

                with self.assertRaisesRegex(
                    ValueError,
                    "retained evidence files do not match rerun package",
                ):
                    with patch(
                        "scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"
                    ):
                        validate_rerun_package_for_workspace(
                            envelope,
                            rerun_package_path=rerun_package_path,
                        )

    def test_resealed_retained_file_metadata_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            rerun_package_path = run_dir / "rerun-package.json"
            evidence = _write_complete_evidence_package(run_dir)
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                envelope = _build_accepted_rerun_package(
                    evidence,
                    run_dir=run_dir,
                    browser_version="test-browser",
                )
            (run_dir / "evidence.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            for mutation in ("missing", "hash", "path"):
                with self.subTest(mutation=mutation):
                    package = deepcopy(envelope["package"])
                    records = package["equivalence"]["retained_files"]
                    if mutation == "missing":
                        records.pop()
                    elif mutation == "hash":
                        records[0]["sha256"] = "0" * 64
                    else:
                        records[0]["path"] = "unrelated.json"

                    with self.assertRaisesRegex(
                        ValueError,
                        "retained evidence files do not match rerun package",
                    ):
                        with patch(
                            "scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"
                        ):
                            validate_rerun_package_for_workspace(
                                seal_rerun_package(package),
                                rerun_package_path=rerun_package_path,
                            )

    def test_acceptance_snapshot_is_sealed_into_rerun_baseline(self) -> None:
        acceptance_snapshot = {
            "status": "pass",
            "correlation_id": "run-current",
            "criteria": list(ACCEPTANCE_CRITERIA),
            "failure_reasons": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            evidence = _write_complete_evidence_package(run_dir)
            evidence["network_observation"] = {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            }
            with (
                patch(
                    "scripts.ci.mvp_browser_e2e.evaluate_acceptance_evidence",
                    return_value=acceptance_snapshot,
                ),
                patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"),
            ):
                envelope = _build_accepted_rerun_package(
                    evidence,
                    run_dir=run_dir,
                    browser_version="test-browser",
                )

        expected_hash = hashlib.sha256(
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            evidence["acceptance_snapshot"],
            acceptance_snapshot,
        )
        self.assertEqual(
            envelope["package"]["equivalence"]["baseline_evidence"]["sha256"],
            expected_hash,
        )

    def test_failed_acceptance_snapshot_cannot_be_a_rerun_baseline(self) -> None:
        failure_snapshot = {
            "status": "fail",
            "correlation_id": "run-current",
            "criteria": list(ACCEPTANCE_CRITERIA),
            "failure_reasons": [
                {
                    "code": "EVIDENCE_CORRELATION_MISMATCH",
                    "boundary": "correlation",
                    "message": "retained baseline did not pass",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            rerun_package_path = run_dir / "rerun-package.json"
            evidence = _write_complete_evidence_package(run_dir)
            with (
                patch(
                    "scripts.ci.mvp_browser_e2e.evaluate_acceptance_evidence",
                    return_value=failure_snapshot,
                ),
                patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"),
            ):
                envelope = _build_accepted_rerun_package(
                    evidence,
                    run_dir=run_dir,
                    browser_version="test-browser",
                )
            (run_dir / "evidence.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "retained baseline acceptance did not pass",
            ):
                with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                    validate_rerun_package_for_workspace(
                        envelope,
                        rerun_package_path=rerun_package_path,
                    )

    def test_resealed_pass_snapshot_must_match_retained_evidence(self) -> None:
        forged_pass_snapshot = {
            "status": "pass",
            "correlation_id": "run-current",
            "criteria": list(ACCEPTANCE_CRITERIA),
            "failure_reasons": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            rerun_package_path = run_dir / "rerun-package.json"
            evidence = _write_complete_evidence_package(run_dir)
            evidence["correlation"]["upload"]["source_sha256"] = "b" * 64
            with (
                patch(
                    "scripts.ci.mvp_browser_e2e.evaluate_acceptance_evidence",
                    return_value=forged_pass_snapshot,
                ),
                patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"),
            ):
                envelope = _build_accepted_rerun_package(
                    evidence,
                    run_dir=run_dir,
                    browser_version="test-browser",
                )
            (run_dir / "evidence.json").write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "retained acceptance snapshot does not match evidence",
            ):
                with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                    validate_rerun_package_for_workspace(
                        envelope,
                        rerun_package_path=rerun_package_path,
                    )

    def test_package_browser_runtime_drift_is_rejected(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
            package = build_rerun_package(
                evidence,
                browser_version="expected-browser",
            )["package"]

        with self.assertRaisesRegex(ValueError, "runtime dependencies"):
            validate_rerun_runtime_dependencies(
                package,
                browser_version="different-browser",
            )

    def test_package_inference_environment_drift_is_rejected(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        baseline_environment = {
            "VERIDOC_STANDARD_OPENAI_BASE_URL": "http://127.0.0.1:8000/v1",
            "VERIDOC_STANDARD_MODEL": "baseline-model",
            "VERIDOC_STANDARD_OPENAI_API_KEY": "local-secret",
        }
        with patch.dict(os.environ, baseline_environment):
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                envelope = build_rerun_package(
                    evidence,
                    browser_version="test-browser",
                )

        inference_environment = envelope["package"]["configuration"][
            "inference_environment"
        ]
        self.assertNotIn("local-secret", json.dumps(inference_environment))
        with patch.dict(
            os.environ,
            {
                **baseline_environment,
                "VERIDOC_STANDARD_MODEL": "rerun-model",
            },
        ):
            with self.assertRaisesRegex(ValueError, "inference environment"):
                with patch(
                    "scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"
                ):
                    validate_rerun_package_for_workspace(envelope)

    def test_package_redacts_url_userinfo_and_pins_credential_hash(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        endpoint_name = "VERIDOC_STANDARD_OPENAI_BASE_URL"
        endpoint = "http://operator:secret@127.0.0.1:8000/v1"
        with patch.dict(os.environ, {endpoint_name: endpoint}):
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                envelope = build_rerun_package(
                    evidence,
                    browser_version="test-browser",
                )

        inference_environment = envelope["package"]["configuration"][
            "inference_environment"
        ]
        serialized = json.dumps(inference_environment)
        self.assertNotIn("operator", serialized)
        self.assertNotIn("secret", serialized)
        standard_profile = next(
            profile
            for profile in inference_environment["profiles"]
            if profile["id"] == "standard"
        )
        self.assertEqual(
            standard_profile["environment"][endpoint_name],
            "http://127.0.0.1:8000/v1",
        )
        self.assertEqual(
            standard_profile["credential_fingerprints"][endpoint_name],
            {
                "configured": True,
                "sha256": hashlib.sha256(
                    b"operator:secret"
                ).hexdigest(),
            },
        )
        with patch.dict(
            os.environ,
            {endpoint_name: "http://operator:changed@127.0.0.1:8000/v1"},
        ):
            with self.assertRaisesRegex(ValueError, "inference environment"):
                with patch(
                    "scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"
                ):
                    validate_rerun_package_for_workspace(envelope)

    def test_package_browser_channel_drift_is_rejected(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        with patch.dict(
            os.environ,
            {"VERIDOC_E2E_BROWSER_CHANNEL": "chrome"},
        ):
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                envelope = build_rerun_package(
                    evidence,
                    browser_version="test-browser",
                )

        with patch.dict(
            os.environ,
            {"VERIDOC_E2E_BROWSER_CHANNEL": "chromium"},
        ):
            with self.assertRaisesRegex(ValueError, "browser channel"):
                with patch(
                    "scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"
                ):
                    validate_rerun_package_for_workspace(envelope)

    def test_equivalence_ignores_run_identity_and_timing_only(self) -> None:
        expected = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "run_id": "first",
            "processing_time_ms": 1.0,
            "correlation": {
                "run_id": "first",
                "job": {
                    "job_id": "job-first",
                    "status": "succeeded",
                    "conversion_status": "converted",
                },
            },
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
            "recovery": {"result": "completed"},
        }
        actual = json.loads(json.dumps(expected))
        actual["run_id"] = "second"
        actual["processing_time_ms"] = 9.0
        actual["correlation"]["run_id"] = "second"
        actual["correlation"]["job"]["job_id"] = "job-second"

        comparison = assert_rerun_equivalent(expected, actual)

        self.assertTrue(comparison["equivalent"])
        actual["network_observation"]["external_ai_api_send_count"] = 1
        with self.assertRaisesRegex(AssertionError, "rerun result is not equivalent"):
            assert_rerun_equivalent(expected, actual)


class MvpBrowserE2ETest(unittest.TestCase):
    @unittest.skipUnless(
        BROWSER_E2E_RUNTIME_AVAILABLE,
        "Playwright and its Chromium runtime are optional browser E2E dependencies",
    )
    def test_upload_to_download_evidence_is_bound_to_one_run(self) -> None:
        with ExitStack() as stack:
            configured_root = os.environ.get("VERIDOC_E2E_EVIDENCE_DIR")
            evidence_root = (
                Path(configured_root)
                if configured_root
                else Path(stack.enter_context(tempfile.TemporaryDirectory()))
            )
            evidence = run_browser_e2e(evidence_root=evidence_root)
            run_dir = evidence_root / evidence["run_id"]
            acceptance = evidence["acceptance_snapshot"]

            self.assertEqual(evidence["schema_version"], "veridoc-mvp-browser-e2e/v1")
            self.assertEqual(acceptance["status"], "pass")
            self.assertEqual(acceptance["correlation_id"], evidence["run_id"])
            self.assertEqual(
                acceptance["criteria"],
                list(ACCEPTANCE_CRITERIA),
            )
            self.assertEqual(acceptance["failure_reasons"], [])
            self.assertEqual(evidence["network_observation"]["status"], "pass")
            self.assertEqual(
                evidence["network_observation"]["external_ai_api_send_count"],
                0,
            )
            self.assertEqual(evidence["network_observation"]["external_attempt_count"], 0)
            self.assertEqual(evidence["run_id"], evidence["correlation"]["run_id"])
            self.assertEqual(evidence["correlation"]["job"]["status"], "succeeded")
            self.assertIn(
                evidence["correlation"]["job"]["conversion_status"],
                {"converted", "requires_review"},
            )
            self.assertEqual(
                evidence["correlation"]["artifact"]["sha256"],
                evidence["correlation"]["audit"]["artifact_sha256"],
            )

            artifact_path = run_dir / evidence["files"]["download"]
            self.assertEqual(
                hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                evidence["correlation"]["artifact"]["sha256"],
            )
            self.assertEqual(evidence["recovery"]["result"], "completed")
            self.assertTrue(evidence["recovery"]["user_visible_error"])
            self.assertGreaterEqual(evidence["correlation"]["audit"]["review_event_count"], 1)
            self.assertGreaterEqual(evidence["correlation"]["audit"]["job_event_count"], 1)
            self.assertEqual(evidence["correlation"]["review"]["action"], "approve")
            self.assertEqual(
                evidence["correlation"]["review"]["conversion_id"],
                json.loads(
                    (run_dir / evidence["files"]["api_result"]).read_text()
                )["audit"]["conversion_id"],
            )
            self.assertEqual(
                evidence["correlation"]["review"]["actor"]["role"],
                "approver",
            )
            self.assertTrue(evidence["correlation"]["review"]["actor"]["id"])
            review_flow = evidence["review_flow"]
            self.assertTrue(review_flow["keyboard_only"])
            self.assertGreaterEqual(len(review_flow["focus_trace"]), 1)
            self.assertTrue(
                all(step["visible_focus"] for step in review_flow["focus_trace"])
            )
            self.assertEqual(
                set(review_flow["actions"]),
                {"edit", "approve", "reject", "needs_fix"},
            )
            self.assertEqual(
                review_flow["actions"],
                ["edit", "needs_fix", "approve", "reject"],
            )
            self.assertEqual(review_flow["high_risk"]["auto_confirmed_count"], 0)
            self.assertGreaterEqual(review_flow["high_risk"]["review_target_count"], 1)
            self.assertTrue(
                review_flow["high_risk"]["approval_blocked_while_unresolved"]
            )
            self.assertIn(
                "needs-fix is unresolved",
                review_flow["high_risk"]["approval_block_reason"],
            )
            self.assertEqual(
                review_flow["source_jump"]["source_filename"],
                FIXTURE_PATH.name,
            )
            self.assertEqual(review_flow["source_jump"]["source_type"], "pdf")
            self.assertEqual(
                review_flow["source_jump"]["source_sha256"],
                hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                review_flow["source_jump"]["page"],
                review_flow["source_jump"]["review_item_page"],
            )
            for coordinate in ("x", "y", "width", "height"):
                self.assertAlmostEqual(
                    review_flow["source_jump"]["bbox"][coordinate],
                    review_flow["source_jump"]["review_item_bbox"][coordinate],
                    places=3,
                )
            self.assertEqual(
                review_flow["source_jump"]["bbox"]["unit"],
                review_flow["source_jump"]["review_item_bbox"]["unit"],
            )
            self.assertEqual(
                review_flow["source_jump"]["bbox"]["origin"],
                review_flow["source_jump"]["review_item_bbox"]["origin"],
            )
            self.assertTrue(review_flow["unresolved"]["blocked_before_approval"])
            for warning in review_flow["warnings"]:
                self.assertEqual(
                    set(warning),
                    {"code", "severity", "message", "remediation"},
                )

            evidence_path = run_dir / "evidence.json"
            self.assertEqual(json.loads(evidence_path.read_text()), evidence)
            rerun_package_path = run_dir / evidence["files"]["rerun_package"]
            validate_rerun_package_envelope(
                json.loads(rerun_package_path.read_text(encoding="utf-8"))
            )
            self.assertTrue((run_dir / evidence["files"]["trace"]).is_file())
            self.assertTrue((run_dir / evidence["files"]["api_result"]).is_file())
            high_risk_api_result = json.loads(
                (run_dir / evidence["files"]["high_risk_api_result"]).read_text()
            )
            self.assertEqual(
                high_risk_api_result["audit"]["conversion_id"],
                review_flow["high_risk"]["conversion_id"],
            )
            high_risk_api_items = [
                item
                for item in high_risk_api_result["review_items"]
                if item.get("high_risk") is True
            ]
            self.assertEqual(
                len(high_risk_api_items),
                review_flow["high_risk"]["review_target_count"],
            )
            self.assertEqual(
                sum(item.get("auto_confirmed") is True for item in high_risk_api_items),
                review_flow["high_risk"]["auto_confirmed_count"],
            )
            self.assertEqual(
                high_risk_api_items[0]["warning_details"][0],
                review_flow["warnings"][0],
            )
            review_events = json.loads(
                (run_dir / evidence["files"]["review_events"]).read_text()
            )
            self.assertTrue(
                {"edit", "approve", "reject", "needs_fix"}.issubset(
                    {event["action"] for event in review_events}
                )
            )
            high_risk_review_actions = {
                event["action"]
                for event in review_events
                if event.get("conversion_id")
                == review_flow["high_risk"]["conversion_id"]
            }
            self.assertNotIn("approve", high_risk_review_actions)
            self.assertTrue(
                {"edit", "needs_fix", "reject"}.issubset(
                    high_risk_review_actions
                )
            )
            audit_artifact_path = run_dir / evidence["files"]["audit_artifact"]
            self.assertEqual(
                hashlib.sha256(audit_artifact_path.read_bytes()).hexdigest(),
                evidence["correlation"]["audit"]["audit_artifact_sha256"],
            )
            self.assertGreaterEqual(len(evidence["files"]["screenshots"]), 2)
            for screenshot in evidence["files"]["screenshots"]:
                self.assertTrue((run_dir / screenshot).is_file())

            missing_provenance = deepcopy(evidence)
            del missing_provenance["correlation"]["provenance"]
            provenance_failure = evaluate_acceptance_evidence(
                missing_provenance,
                run_dir=run_dir,
            )
            self.assertEqual(provenance_failure["status"], "fail")
            self.assertIn(
                "EVIDENCE_PROVENANCE_MISSING",
                {
                    failure["code"]
                    for failure in provenance_failure["failure_reasons"]
                },
            )

            review_events_path = run_dir / evidence["files"]["review_events"]
            original_review_events = review_events_path.read_bytes()
            review_events_path.write_text(
                json.dumps(
                    [
                        event
                        for event in review_events
                        if not (
                            event.get("conversion_id")
                            == evidence["correlation"]["review"]["conversion_id"]
                            and event.get("action") == "approve"
                        )
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            try:
                audit_event_failure = evaluate_acceptance_evidence(
                    evidence,
                    run_dir=run_dir,
                )
            finally:
                review_events_path.write_bytes(original_review_events)
            self.assertEqual(audit_event_failure["status"], "fail")
            self.assertIn(
                "EVIDENCE_AUDIT_EVENT_MISSING",
                {
                    failure["code"]
                    for failure in audit_event_failure["failure_reasons"]
                },
            )

            tampered_hash = deepcopy(evidence)
            tampered_hash["correlation"]["artifact"]["sha256"] = "0" * 64
            hash_failure = evaluate_acceptance_evidence(
                tampered_hash,
                run_dir=run_dir,
            )
            self.assertIn(
                "EVIDENCE_HASH_MISMATCH",
                {failure["code"] for failure in hash_failure["failure_reasons"]},
            )

            api_result_path = run_dir / evidence["files"]["api_result"]
            original_api_result = api_result_path.read_bytes()
            original_audit_artifact = audit_artifact_path.read_bytes()
            tampered_result = json.loads(original_api_result)
            tampered_audit = deepcopy(tampered_result["audit"])
            tampered_audit["versions"]["prompt"]["version"] = "tampered"
            tampered_result["audit"] = tampered_audit
            api_result_path.write_text(
                json.dumps(tampered_result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            audit_artifact_path.write_text(
                json.dumps(tampered_audit, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                version_failure = evaluate_acceptance_evidence(
                    evidence,
                    run_dir=run_dir,
                )
            finally:
                api_result_path.write_bytes(original_api_result)
                audit_artifact_path.write_bytes(original_audit_artifact)
            self.assertIn(
                "EVIDENCE_VERSION_MISMATCH",
                {
                    failure["code"]
                    for failure in version_failure["failure_reasons"]
                },
            )

            mixed_run = deepcopy(evidence)
            mixed_run["evidence_surfaces"]["audit_events"][
                "correlation_id"
            ] = "run-stale"
            correlation_failure = evaluate_acceptance_evidence(
                mixed_run,
                run_dir=run_dir,
            )
            self.assertIn(
                "EVIDENCE_CORRELATION_MISMATCH",
                {
                    failure["code"]
                    for failure in correlation_failure["failure_reasons"]
                },
            )
            for failure in (
                provenance_failure["failure_reasons"]
                + audit_event_failure["failure_reasons"]
                + hash_failure["failure_reasons"]
                + version_failure["failure_reasons"]
                + correlation_failure["failure_reasons"]
            ):
                self.assertEqual(set(failure), {"code", "boundary", "message"})


if __name__ == "__main__":
    unittest.main()
