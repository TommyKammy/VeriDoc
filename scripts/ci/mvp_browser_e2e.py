#!/usr/bin/env python3
"""Run the repo-owned MVP upload-to-download browser scenario."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from zipfile import ZipFile

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.ir.document_ir_v1 import SCHEMA_VERSION as DOCUMENT_IR_SCHEMA_VERSION
from services.api.job_queue import JobQueue
from services.api.poc_web import (
    CONVERSION_AUDIT_SCHEMA_VERSION,
    CONVERSION_PLAN_SCHEMA_VERSION,
    JobAuditEventStore,
    PocWebRequestHandler,
    ReviewAuditEventStore,
    TemplateStore,
)

FIXTURE_PATH = (
    REPO_ROOT / "datasets" / "fixtures" / "pdf" / "pdf-to-word-representative.pdf"
)
HIGH_RISK_FIXTURE_PATH = (
    REPO_ROOT
    / "datasets"
    / "fixtures"
    / "templates"
    / "synthetic-batch-template-regression.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _retain_redacted_trace(
    raw_trace_path: Path, retained_trace_path: Path, *, secret: str
) -> None:
    """Copy a Playwright trace while removing the ephemeral bearer credential."""
    secret_bytes = secret.encode("utf-8")
    with ZipFile(raw_trace_path) as raw_trace, ZipFile(retained_trace_path, "w") as retained:
        for entry in raw_trace.infolist():
            retained.writestr(
                entry,
                raw_trace.read(entry).replace(secret_bytes, b"<redacted-e2e-token>"),
            )
    with ZipFile(retained_trace_path) as retained:
        if any(
            secret_bytes in retained.read(entry) for entry in retained.infolist()
        ):
            raise AssertionError("retained browser trace contains the bearer credential")


def _json_response(response: Any) -> dict[str, Any]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise AssertionError("browser E2E API response must be a JSON object")
    return payload


def _events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = payload.get(
        "events",
        payload.get(
            "audit_events",
            payload.get("job_events", payload.get("review_events", [])),
        ),
    )
    if not isinstance(events, list):
        raise AssertionError("audit response did not contain an event list")
    return [event for event in events if isinstance(event, dict)]


def _require_matching_event(
    events: list[dict[str, Any]],
    *,
    expected_fields: dict[str, Any],
    description: str,
) -> tuple[dict[str, Any], int]:
    matching_events = [
        event
        for event in events
        if all(event.get(field) == expected for field, expected in expected_fields.items())
    ]
    if not matching_events:
        raise AssertionError(
            f"{description} was not bound to the browser run: "
            f"expected fields {expected_fields!r}"
        )
    return matching_events[-1], len(matching_events)


def _require_audit_payload_matches_result(
    audit_payload: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    result_audit = result.get("audit")
    if not isinstance(result_audit, dict):
        raise AssertionError("completed browser result did not contain audit metadata")
    if audit_payload != result_audit:
        raise AssertionError(
            "downloaded audit artifact did not match the current browser result audit"
        )
    return result_audit


def _evidence_failure(code: str, boundary: str, message: str) -> dict[str, str]:
    return {"code": code, "boundary": boundary, "message": message}


def _load_evidence_json(run_dir: Path, filename: object) -> object | None:
    if not isinstance(filename, str) or not filename:
        return None
    try:
        return json.loads((run_dir / filename).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None


def _valid_source_bbox(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        all(isinstance(value.get(field), (int, float)) for field in ("x", "y"))
        and all(
            isinstance(value.get(field), (int, float)) and value[field] > 0
            for field in ("width", "height")
        )
        and isinstance(value.get("unit"), str)
        and bool(value["unit"])
        and value.get("origin") == "top-left"
    )


def _audit_chain_is_valid(events: object) -> bool:
    if not isinstance(events, list) or not events:
        return False
    previous_hash: str | None = None
    for sequence, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            return False
        if event.get("integrity_algorithm") != "sha256-canonical-json-chain-v1":
            return False
        if event.get("sequence") != sequence:
            return False
        if event.get("prev_event_hash") != previous_hash:
            return False
        event_hash = event.get("event_hash")
        canonical = json.dumps(
            {key: value for key, value in event.items() if key != "event_hash"},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        expected_hash = hashlib.sha256(canonical).hexdigest()
        if event_hash != expected_hash:
            return False
        previous_hash = event_hash
    return True


def _matching_events(
    events: object,
    *,
    expected_fields: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        return []
    return [
        event
        for event in events
        if isinstance(event, dict)
        and all(event.get(field) == value for field, value in expected_fields.items())
    ]


def evaluate_acceptance_evidence(
    evidence: dict[str, Any],
    *,
    run_dir: Path,
) -> dict[str, Any]:
    """Evaluate one browser evidence package without inferring missing linkage."""

    failures: list[dict[str, str]] = []

    def fail(code: str, boundary: str, message: str) -> None:
        failure = _evidence_failure(code, boundary, message)
        if failure not in failures:
            failures.append(failure)

    correlation = evidence.get("correlation")
    correlation = correlation if isinstance(correlation, dict) else {}
    provenance = correlation.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    if (
        not isinstance(provenance.get("source_page"), int)
        or provenance["source_page"] < 1
        or not _valid_source_bbox(provenance.get("source_bbox"))
    ):
        fail(
            "EVIDENCE_PROVENANCE_MISSING",
            "provenance",
            "Source page and bounding-box provenance are required before acceptance can pass.",
        )

    files = evidence.get("files")
    files = files if isinstance(files, dict) else {}
    job_events = _load_evidence_json(run_dir, files.get("job_events"))
    review_events = _load_evidence_json(run_dir, files.get("review_events"))
    if not isinstance(job_events, list) or not isinstance(review_events, list):
        fail(
            "EVIDENCE_AUDIT_MISSING",
            "audit",
            "Job and review audit events are required before acceptance can pass.",
        )

    run_id = evidence.get("run_id")
    surfaces = evidence.get("evidence_surfaces")
    surfaces = surfaces if isinstance(surfaces, dict) else {}
    required_surfaces = {
        "browser_run",
        "harness_result",
        "download_artifact",
        "audit_events",
    }
    surface_ids = {
        surface.get("correlation_id")
        for surface in surfaces.values()
        if isinstance(surface, dict)
    }
    if (
        not isinstance(run_id, str)
        or not run_id
        or correlation.get("run_id") != run_id
        or set(surfaces) != required_surfaces
        or surface_ids != {run_id}
    ):
        fail(
            "EVIDENCE_CORRELATION_MISMATCH",
            "correlation",
            "Browser, harness, artifact, and audit evidence must share one correlation ID.",
        )

    if failures and {
        failure["code"] for failure in failures
    } >= {"EVIDENCE_PROVENANCE_MISSING", "EVIDENCE_AUDIT_MISSING"}:
        return {
            "status": "fail",
            "correlation_id": run_id,
            "criteria": ["AC-PROVENANCE", "AC-AUDIT", "FC-EVIDENCE"],
            "failure_reasons": failures[:2],
        }

    api_result = _load_evidence_json(run_dir, files.get("api_result"))
    audit_artifact = _load_evidence_json(run_dir, files.get("audit_artifact"))
    if not isinstance(api_result, dict) or not isinstance(audit_artifact, dict):
        fail(
            "EVIDENCE_AUDIT_MISSING",
            "audit",
            "Conversion result and downloadable audit evidence are both required.",
        )
        result_audit: dict[str, Any] = {}
    else:
        audit_value = api_result.get("audit")
        result_audit = audit_value if isinstance(audit_value, dict) else {}
        if not result_audit or audit_artifact != result_audit:
            fail(
                "EVIDENCE_AUDIT_TAMPERED",
                "audit",
                "The downloadable audit record must exactly match the browser result audit.",
            )

    upload = correlation.get("upload")
    upload = upload if isinstance(upload, dict) else {}
    artifact = correlation.get("artifact")
    artifact = artifact if isinstance(artifact, dict) else {}
    review = correlation.get("review")
    review = review if isinstance(review, dict) else {}
    job = correlation.get("job")
    job = job if isinstance(job, dict) else {}
    audit_input = result_audit.get("input")
    audit_input = audit_input if isinstance(audit_input, dict) else {}

    source_hashes = {
        upload.get("source_sha256"),
        provenance.get("source_sha256"),
        result_audit.get("source_sha256"),
        audit_input.get("sha256"),
    }
    hashes = api_result.get("hashes") if isinstance(api_result, dict) else None
    if isinstance(hashes, dict):
        source_hashes.add(hashes.get("source_sha256"))
    if (
        len(source_hashes) != 1
        or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in source_hashes
        )
    ):
        fail(
            "EVIDENCE_HASH_MISMATCH",
            "input_hash",
            (
                "The uploaded source hash must match provenance and all "
                "conversion audit input records."
            ),
        )

    download_name = files.get("download")
    download_path = run_dir / download_name if isinstance(download_name, str) else None
    downloaded_hash = (
        _sha256(download_path)
        if download_path is not None and download_path.is_file()
        else None
    )
    result_artifact = None
    if isinstance(api_result, dict):
        result_artifact = next(
            (
                item
                for item in api_result.get("artifacts", [])
                if isinstance(item, dict)
                and item.get("artifact_id") == artifact.get("artifact_id")
            ),
            None,
        )
    output_hashes = {
        downloaded_hash,
        artifact.get("sha256"),
        (
            result_artifact.get("sha256")
            if isinstance(result_artifact, dict)
            else None
        ),
        (
            result_artifact.get("metadata", {}).get("output_sha256")
            if isinstance(result_artifact, dict)
            and isinstance(result_artifact.get("metadata"), dict)
            else None
        ),
    }
    if (
        len(output_hashes) != 1
        or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in output_hashes
        )
    ):
        fail(
            "EVIDENCE_HASH_MISMATCH",
            "output_hash",
            "The downloaded artifact hash must match the result and artifact audit metadata.",
        )

    audit_download_name = files.get("audit_artifact")
    audit_download_path = (
        run_dir / audit_download_name
        if isinstance(audit_download_name, str)
        else None
    )
    audit_downloaded_hash = (
        _sha256(audit_download_path)
        if audit_download_path is not None and audit_download_path.is_file()
        else None
    )
    result_audit_artifact = None
    if isinstance(api_result, dict):
        result_audit_artifact = next(
            (
                item
                for item in api_result.get("artifacts", [])
                if isinstance(item, dict) and item.get("id") == "audit-json"
            ),
            None,
        )
    audit_hashes = {
        audit_downloaded_hash,
        correlation.get("audit", {}).get("audit_artifact_sha256")
        if isinstance(correlation.get("audit"), dict)
        else None,
        (
            result_audit_artifact.get("sha256")
            if isinstance(result_audit_artifact, dict)
            else None
        ),
    }
    if (
        len(audit_hashes) != 1
        or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in audit_hashes
        )
    ):
        fail(
            "EVIDENCE_HASH_MISMATCH",
            "audit_hash",
            "The downloadable audit hash must match the result and evidence manifest.",
        )

    versions = result_audit.get("versions")
    versions = versions if isinstance(versions, dict) else {}
    prompt = versions.get("prompt")
    schemas = versions.get("schemas")
    expected_schemas = {
        "conversion_audit": CONVERSION_AUDIT_SCHEMA_VERSION,
        "conversion_plan": CONVERSION_PLAN_SCHEMA_VERSION,
        "document_ir": DOCUMENT_IR_SCHEMA_VERSION,
    }
    llm_audit = result_audit.get("llm")
    explicit_no_model = (
        isinstance(llm_audit, dict)
        and llm_audit.get("requested") is False
        and llm_audit.get("model") is None
        and versions.get("model") is None
    )
    explicit_model = (
        isinstance(llm_audit, dict)
        and llm_audit.get("requested") is True
        and isinstance(llm_audit.get("model"), str)
        and bool(llm_audit["model"])
        and llm_audit.get("model") == versions.get("model")
    )
    if (
        "model" not in versions
        or not isinstance(prompt, dict)
        or not all(
            isinstance(prompt.get(field), str) and prompt[field]
            for field in ("id", "version")
        )
        or not isinstance(schemas, dict)
        or any(
            schemas.get(field) != expected
            for field, expected in expected_schemas.items()
        )
        or not isinstance(llm_audit, dict)
        or "model" not in llm_audit
        or not (explicit_no_model or explicit_model)
        or llm_audit.get("prompt") != prompt
        or llm_audit.get("schema_version") != schemas.get("conversion_plan")
    ):
        fail(
            "EVIDENCE_VERSION_MISMATCH",
            "version_lineage",
            "Model, prompt, and schema lineage must be complete and mutually consistent.",
        )

    created_at = job.get("created_at")
    try:
        parsed_created_at = (
            datetime.fromisoformat(created_at)
            if isinstance(created_at, str)
            else None
        )
    except ValueError:
        parsed_created_at = None
    if parsed_created_at is None or parsed_created_at.tzinfo is None:
        fail(
            "EVIDENCE_TIMESTAMP_MISSING",
            "timestamp",
            "The authoritative job timestamp is required in the correlated evidence.",
        )

    if isinstance(job_events, list) and isinstance(review_events, list):
        browser_surface = surfaces.get("browser_run")
        browser_surface = browser_surface if isinstance(browser_surface, dict) else {}
        harness_surface = surfaces.get("harness_result")
        harness_surface = harness_surface if isinstance(harness_surface, dict) else {}
        artifact_surface = surfaces.get("download_artifact")
        artifact_surface = artifact_surface if isinstance(artifact_surface, dict) else {}
        audit_surface = surfaces.get("audit_events")
        audit_surface = audit_surface if isinstance(audit_surface, dict) else {}
        if not _audit_chain_is_valid(job_events) or not _audit_chain_is_valid(
            review_events
        ):
            fail(
                "EVIDENCE_AUDIT_CHAIN_INVALID",
                "audit_hash_chain",
                "Every audit event must belong to a valid canonical hash chain.",
            )
        upload_matches = _matching_events(
            job_events,
            expected_fields={
                "action": "browser_upload",
                "job_id": job.get("job_id"),
                "source_sha256": upload.get("source_sha256"),
            },
        )
        review_matches = _matching_events(
            review_events,
            expected_fields={
                "action": "approve",
                "conversion_id": review.get("conversion_id"),
                "document_id": review.get("document_id"),
                "block_id": review.get("block_id"),
                "actor": review.get("actor"),
                "source_page": provenance.get("source_page"),
                "source_bbox": provenance.get("source_bbox"),
            },
        )
        if (
            review.get("action") != "approve"
            or len(upload_matches) != 1
            or len(review_matches) != 1
        ):
            fail(
                "EVIDENCE_AUDIT_EVENT_MISSING",
                "audit",
                (
                    "Exactly one upload event and one approval decision must "
                    "match the correlated run."
                ),
            )
        elif (
            browser_surface.get("job_id") != job.get("job_id")
            or harness_surface.get("conversion_id")
            != review.get("conversion_id")
            or result_audit.get("conversion_id") != review.get("conversion_id")
            or artifact_surface.get("artifact_id")
            != artifact.get("artifact_id")
            or audit_surface.get("job_event_hash")
            != upload_matches[0].get("event_hash")
            or audit_surface.get("review_event_hash")
            != review_matches[0].get("event_hash")
        ):
            fail(
                "EVIDENCE_CORRELATION_MISMATCH",
                "correlation",
                "Surface identifiers and audit hashes must remain bound to this run.",
            )

    if (
        provenance.get("source_filename") != upload.get("source_filename")
        or result_audit.get("source_filename") != upload.get("source_filename")
        or audit_input.get("filename") != upload.get("source_filename")
        or provenance.get("document_id") != review.get("document_id")
        or provenance.get("block_id") != review.get("block_id")
    ):
        fail(
            "EVIDENCE_PROVENANCE_MISMATCH",
            "provenance",
            "Source coordinates must remain bound to the reviewed record in this run.",
        )

    return {
        "status": "fail" if failures else "pass",
        "correlation_id": run_id,
        "criteria": ["AC-PROVENANCE", "AC-AUDIT", "FC-EVIDENCE"],
        "failure_reasons": failures,
    }


def _high_risk_fixture() -> dict[str, Any]:
    fixture = json.loads(HIGH_RISK_FIXTURE_PATH.read_text(encoding="utf-8"))
    if not isinstance(fixture, dict):
        raise AssertionError("high-risk browser fixture must be a JSON object")
    return fixture


def _high_risk_template_store() -> TemplateStore:
    fixture = _high_risk_fixture()
    definition = fixture.get("template_definition")
    if not isinstance(definition, dict):
        raise AssertionError("high-risk browser fixture is missing template_definition")
    definition = deepcopy(definition)
    anchors = definition.get("anchors")
    fields = definition.get("fields")
    validation_rules = definition.get("validation_rules")
    output_mapping = definition.get("output_mapping")
    field_map = (
        output_mapping.get("field_map")
        if isinstance(output_mapping, dict)
        else None
    )
    if (
        not isinstance(anchors, list)
        or not anchors
        or not isinstance(anchors[0], dict)
        or not isinstance(fields, list)
        or not fields
        or not isinstance(fields[0], dict)
        or not isinstance(validation_rules, list)
        or not validation_rules
        or not isinstance(output_mapping, dict)
        or not isinstance(field_map, list)
        or not field_map
    ):
        raise AssertionError("high-risk browser template fixture is malformed")
    definition["anchors"] = [{**anchors[0], "text": "Manufacturing Summary"}]
    definition["fields"] = [{**fields[0], "label": "Batch"}]
    definition["tables"] = []
    definition["validation_rules"] = validation_rules[:1]
    output_mapping["field_map"] = field_map[:1]
    output_mapping["table_map"] = []
    registration = {
        key: value
        for key, value in definition.items()
        if key not in {"version", "template_version", "status", "effective"}
    }
    registration.update(
        {
            "name": "Real PDF high-risk browser review",
            "category": "manufacturing",
            "change_reason": "Register committed high-risk browser review fixture",
            "actor": {"principal_id": "e2e-template-admin", "role": "admin"},
        }
    )
    store = TemplateStore()
    store.register_template(registration)
    return store


@contextmanager
def _poc_server(
    state_root: Path, *, auth_token: str, approver_actor: str
) -> Iterator[str]:
    previous_auth = os.environ.get("VERIDOC_LOCAL_AUTH_TOKENS")
    os.environ["VERIDOC_LOCAL_AUTH_TOKENS"] = (
        f"approver:{approver_actor}={auth_token}"
    )
    database_path = state_root / "veridoc.sqlite3"
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue(
        database_path=database_path,
        artifact_store_root=state_root / "artifacts",
    )
    server.job_event_store = JobAuditEventStore(database_path=database_path)
    server.review_event_store = ReviewAuditEventStore()
    server.template_store = _high_risk_template_store()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if previous_auth is None:
            os.environ.pop("VERIDOC_LOCAL_AUTH_TOKENS", None)
        else:
            os.environ["VERIDOC_LOCAL_AUTH_TOKENS"] = previous_auth


def _launch_browser(playwright: Any) -> Any:
    requested_channel = os.environ.get("VERIDOC_E2E_BROWSER_CHANNEL")
    if requested_channel:
        return playwright.chromium.launch(channel=requested_channel, headless=True)
    return playwright.chromium.launch(headless=True)


def _record_active_focus(page: Any, focus_trace: list[dict[str, Any]]) -> dict[str, Any]:
    focused = page.evaluate(
        """() => {
          const element = document.activeElement;
          const style = element ? getComputedStyle(element) : null;
          return {
            tag: element?.tagName?.toLowerCase() || "",
            id: element?.id || "",
            aria_label: element?.getAttribute?.("aria-label") || "",
            review_action: element?.dataset?.reviewActionName || "",
            visible_focus: Boolean(
              element &&
              element.matches(":focus-visible") &&
              style &&
              style.outlineStyle !== "none" &&
              parseFloat(style.outlineWidth) > 0
            ),
          };
        }"""
    )
    if not isinstance(focused, dict):
        raise AssertionError("keyboard focus inspection did not return an object")
    if focused["tag"] in {"a", "button", "input", "select", "textarea"}:
        focus_trace.append(focused)
    return focused


def _tab_to(
    page: Any,
    selector: str,
    focus_trace: list[dict[str, Any]],
    *,
    limit: int = 120,
) -> Any:
    target = page.locator(selector).first
    for _ in range(limit):
        page.keyboard.press("Tab")
        focused = _record_active_focus(page, focus_trace)
        if target.evaluate("(target) => target === document.activeElement"):
            if not focused["visible_focus"]:
                raise AssertionError(f"keyboard target did not expose visible focus: {selector}")
            return target
    raise AssertionError(f"keyboard target was not reachable in tab order: {selector}")


def _keyboard_activate(
    page: Any,
    selector: str,
    focus_trace: list[dict[str, Any]],
) -> Any:
    target = _tab_to(page, selector, focus_trace)
    page.keyboard.press("Enter")
    return target


def run_browser_e2e(*, evidence_root: Path) -> dict[str, Any]:
    """Exercise recovery and upload-to-download paths and return evidence metadata."""
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised by setup failures
        raise RuntimeError(
            "Playwright is required; install requirements-browser-e2e.txt and run "
            "`python3 -m playwright install chromium`."
        ) from exc

    run_id = f"p12g03-{uuid.uuid4().hex}"
    run_dir = evidence_root / run_id
    run_dir.mkdir(parents=True)
    trace_path = run_dir / "trace.zip"
    recovery_screenshot = run_dir / "01-recovery.png"
    completed_screenshot = run_dir / "02-completed-review.png"
    audit_screenshot = run_dir / "03-audit.png"
    keyboard_screenshot = run_dir / "04-keyboard-high-risk-review.png"
    api_result_path = run_dir / "api-result.json"
    high_risk_api_result_path = run_dir / "high-risk-api-result.json"
    job_events_path = run_dir / "job-events.json"
    review_events_path = run_dir / "review-events.json"
    auth_token = uuid.uuid4().hex
    approver_actor = f"e2e-{uuid.uuid4().hex}"
    source_sha256 = _sha256(FIXTURE_PATH)

    with tempfile.TemporaryDirectory(prefix="veridoc-browser-e2e-") as state_dir:
        high_risk_fixture = _high_risk_fixture()
        with _poc_server(
            Path(state_dir),
            auth_token=auth_token,
            approver_actor=approver_actor,
        ) as base_url, sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            focus_trace: list[dict[str, Any]] = []
            tracing_started = False
            raw_trace_path = Path(state_dir) / "trace.zip"
            try:
                page.goto(base_url, wait_until="domcontentloaded")
                page.locator("#auth-token").fill(auth_token)
                page.locator("#save-auth-token").click()
                expect(page.locator('#auth-status[data-auth-state="configured"]')).to_be_visible()
                page.locator("#auth-token").fill("")
                expect(page.locator("#auth-token")).to_have_value("")
                context.tracing.start(screenshots=True, snapshots=True, sources=True)
                tracing_started = True
                page.locator('[data-nav-target="upload"]').click()
                page.locator("#document-file").set_input_files(str(FIXTURE_PATH))

                # Deliberately choose an incompatible mode to prove the visible
                # failure/recovery path before retrying with the correct setting.
                page.locator("#direct-conversion-mode").select_option("word_to_excel")
                page.locator("#convert-button").click()
                expect(page.locator("#direct-convert-error")).to_be_visible(timeout=30_000)
                recovery_message = page.locator("#direct-convert-error").inner_text().strip()
                if not recovery_message:
                    raise AssertionError("recovery path did not expose a user-visible error")
                page.screenshot(path=str(recovery_screenshot), full_page=True)

                page.locator('[data-nav-target="upload"]').click()
                page.locator("#direct-conversion-mode").select_option("pdf_to_word")
                page.locator("#convert-button").click()
                expect(page.locator("#status")).to_contain_text(
                    re.compile(r"converted|requires_review"), timeout=30_000
                )
                conversion_status = page.locator("#status").inner_text().strip()
                if conversion_status not in {"converted", "requires_review"}:
                    raise AssertionError(
                        f"completed conversion has unexpected status: {conversion_status!r}"
                    )
                expect(page.locator("#artifact-downloads-panel")).to_be_visible(timeout=10_000)
                expect(page.locator("#pdf-preview-panel")).to_be_visible(timeout=10_000)
                preview_canvas = page.locator("#pdf-page-canvas")
                expect(preview_canvas).to_be_visible(timeout=30_000)
                preview_size = preview_canvas.evaluate(
                    "(canvas) => ({width: canvas.width, height: canvas.height})"
                )
                if preview_size["width"] <= 0 or preview_size["height"] <= 0:
                    raise AssertionError("PDF preview canvas did not render any pixels")
                expect(page.locator("#review-list .review-item").first).to_be_visible(
                    timeout=10_000
                )

                result = json.loads(page.locator("#raw-result").inner_text())
                if not isinstance(result, dict):
                    raise AssertionError("completed browser result must be a JSON object")
                api_result_path.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                result_audit = result.get("audit")
                if not isinstance(result_audit, dict):
                    raise AssertionError(
                        "completed browser result did not contain audit metadata"
                    )
                conversion_id = result_audit.get("conversion_id")
                if not isinstance(conversion_id, str) or not conversion_id:
                    raise AssertionError(
                        "completed browser result did not bind an audit conversion ID"
                    )
                if result_audit.get("source_sha256") != source_sha256:
                    raise AssertionError(
                        "completed browser result audit did not match the uploaded fixture"
                    )
                job_id = result.get("job_id")
                if not isinstance(job_id, str) or not job_id:
                    page_status = page.locator("#page-status")
                    expect(page_status).to_contain_text(
                        re.compile(r"Conversion job job-[a-zA-Z0-9_-]+ finished\."),
                        timeout=30_000,
                    )
                    status_text = page_status.inner_text()
                    match = re.search(r"(job-[a-zA-Z0-9_-]+)", status_text)
                    if not match:
                        raise AssertionError("completed browser result did not expose a job ID")
                    job_id = match.group(1)
                auth_headers = {"Authorization": f"Bearer {auth_token}"}
                job_response = context.request.get(
                    base_url + f"/api/jobs/{job_id}", headers=auth_headers
                )
                if not job_response.ok:
                    raise AssertionError("completed browser job could not be reloaded")
                authoritative_job = _json_response(job_response).get("job", {})
                job_status = authoritative_job.get("status")
                if job_status != "succeeded":
                    raise AssertionError(
                        f"completed browser job has unexpected status: {job_status!r}"
                    )

                approve = page.locator(
                    '#review-list button[data-review-action-name="approve"]:not([disabled])'
                ).first
                expect(approve).to_be_visible(timeout=10_000)
                review_action_key = approve.get_attribute("data-review-action-key")
                review_items = result.get("review_items")
                if not isinstance(review_items, list):
                    raise AssertionError(
                        "completed browser result did not contain review items"
                    )
                review_item = next(
                    (
                        item
                        for item in review_items
                        if isinstance(item, dict)
                        and review_action_key
                        == f"{item.get('document_id')}:{item.get('block_id')}"
                    ),
                    None,
                )
                if review_item is None:
                    raise AssertionError(
                        "browser approval target was not bound to a current result review item"
                    )
                review_document_id = review_item.get("document_id")
                review_block_id = review_item.get("block_id")
                review_source_page = review_item.get("source_page")
                review_source_bbox = review_item.get("source_bbox")
                if (
                    not isinstance(review_document_id, str)
                    or not review_document_id
                    or not isinstance(review_block_id, str)
                    or not review_block_id
                    or not isinstance(review_source_page, int)
                    or review_source_page < 1
                    or not _valid_source_bbox(review_source_bbox)
                ):
                    raise AssertionError(
                        "browser approval target did not expose authoritative review "
                        "provenance"
                    )
                _keyboard_activate(
                    page,
                    (
                        '#review-list button[data-review-action-name="approve"]'
                        ':not([disabled])'
                    ),
                    focus_trace,
                )
                expect(page.locator("#review-action-status")).to_contain_text(
                    "queued for audit", timeout=10_000
                )
                page.screenshot(path=str(completed_screenshot), full_page=True)

                artifact = next(
                    (
                        item
                        for item in result.get("artifacts", [])
                        if isinstance(item, dict) and item.get("id", "").startswith("primary-")
                    ),
                    None,
                )
                if artifact is None:
                    raise AssertionError("completed browser result did not contain a primary artifact")
                artifact_id = artifact.get("artifact_id")
                if not isinstance(artifact_id, str) or not artifact_id:
                    raise AssertionError(
                        "completed browser result did not bind the primary artifact ID"
                    )
                artifact_href = artifact.get("href")
                if artifact_href != f"/api/artifacts/{artifact_id}":
                    raise AssertionError(
                        "completed browser result did not expose a persisted primary artifact"
                    )
                persisted_artifact_response = context.request.get(
                    base_url + artifact_href, headers=auth_headers
                )
                if not persisted_artifact_response.ok:
                    raise AssertionError("persisted primary artifact download failed")
                persisted_artifact_content = persisted_artifact_response.body()
                persisted_artifact_sha256 = hashlib.sha256(
                    persisted_artifact_content
                ).hexdigest()
                if persisted_artifact_sha256 != artifact.get("sha256"):
                    raise AssertionError(
                        "persisted primary artifact hash did not match the API result"
                    )
                with page.expect_download(timeout=10_000) as download_info:
                    page.locator("#download-link").click()
                download = download_info.value
                download_name = f"download-{download.suggested_filename}"
                download_path = run_dir / download_name
                download.save_as(download_path)
                downloaded_sha256 = _sha256(download_path)
                if downloaded_sha256 != persisted_artifact_sha256:
                    raise AssertionError(
                        "browser download did not match the persisted primary artifact"
                    )
                artifact_audit_sha256 = artifact.get("metadata", {}).get("output_sha256")
                if downloaded_sha256 != artifact_audit_sha256:
                    raise AssertionError(
                        "downloaded artifact hash did not match its audit metadata"
                    )

                audit_artifact = next(
                    (
                        item
                        for item in result.get("artifacts", [])
                        if isinstance(item, dict) and item.get("id") == "audit-json"
                    ),
                    None,
                )
                if audit_artifact is None:
                    raise AssertionError(
                        "completed browser result did not contain the audit-json artifact"
                    )
                audit_artifact_id = audit_artifact.get("artifact_id")
                if not isinstance(audit_artifact_id, str) or not audit_artifact_id:
                    raise AssertionError(
                        "completed browser result did not bind the audit artifact ID"
                    )
                audit_artifact_href = audit_artifact.get("href")
                if audit_artifact_href != f"/api/artifacts/{audit_artifact_id}":
                    raise AssertionError(
                        "completed browser result did not expose a persisted audit artifact"
                    )
                audit_response = context.request.get(
                    base_url + audit_artifact_href, headers=auth_headers
                )
                if not audit_response.ok:
                    raise AssertionError("audit JSON artifact download failed")
                audit_content = audit_response.body()
                audit_downloaded_sha256 = hashlib.sha256(audit_content).hexdigest()
                if audit_downloaded_sha256 != audit_artifact.get("sha256"):
                    raise AssertionError(
                        "downloaded audit artifact hash did not match the API result"
                    )
                try:
                    audit_payload = json.loads(audit_content)
                except (TypeError, ValueError) as exc:
                    raise AssertionError(
                        "downloaded audit artifact was not valid JSON"
                    ) from exc
                if not isinstance(audit_payload, dict):
                    raise AssertionError(
                        "downloaded audit artifact must be a JSON object"
                    )
                _require_audit_payload_matches_result(audit_payload, result)
                audit_artifact_path = run_dir / "audit-artifact.json"
                audit_artifact_path.write_bytes(audit_content)

                page.locator('[data-nav-target="audit"]').click()
                page.locator("#refresh-audit").click()
                expect(page.locator("#audit-body tr").first).to_be_visible(timeout=10_000)
                page.screenshot(path=str(audit_screenshot), full_page=True)

                page.locator('[data-nav-target="upload"]').click()
                page.locator("#document-file").set_input_files(
                    str(FIXTURE_PATH)
                )
                page.locator("#direct-conversion-mode").select_option("auto")
                high_risk_template_id = high_risk_fixture["template_definition"][
                    "template_id"
                ]
                page.locator("#direct-template").select_option(high_risk_template_id)
                page.locator("#convert-button").click()
                expect(page.locator("#status")).to_have_text(
                    "requires_review",
                    timeout=30_000,
                )
                high_risk_items = page.locator(
                    '#review-list .review-item[data-review-risk="high"]'
                )
                expect(high_risk_items.first).to_be_visible(timeout=10_000)
                if high_risk_items.count() < 1:
                    raise AssertionError(
                        "high-risk browser fixture did not expose a review target"
                    )
                high_risk_result = json.loads(page.locator("#raw-result").inner_text())
                if not isinstance(high_risk_result, dict):
                    raise AssertionError(
                        "high-risk browser result must be a JSON object"
                    )
                high_risk_audit = high_risk_result.get("audit")
                if not isinstance(high_risk_audit, dict) or (
                    high_risk_audit.get("source_filename") != FIXTURE_PATH.name
                    or high_risk_audit.get("source_type") != "pdf"
                    or high_risk_audit.get("source_sha256") != source_sha256
                ):
                    raise AssertionError(
                        "high-risk review evidence was not bound to the real PDF source"
                    )
                high_risk_api_result_path.write_text(
                    json.dumps(high_risk_result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                high_risk_api_items = [
                    item
                    for item in high_risk_result.get("review_items", [])
                    if isinstance(item, dict) and item.get("high_risk") is True
                ]
                if len(high_risk_api_items) != high_risk_items.count():
                    raise AssertionError(
                        "high-risk API targets did not match the review UI"
                    )
                auto_confirmed_count = sum(
                    item.get("auto_confirmed") is True for item in high_risk_api_items
                )
                if auto_confirmed_count:
                    raise AssertionError("high-risk review target was auto-confirmed")

                first_high_risk_item = high_risk_api_items[0]
                first_high_risk_block = first_high_risk_item["block_id"]
                first_item_selector = (
                    '#review-list .review-item[data-block-id="'
                    + first_high_risk_block
                    + '"]'
                )
                warning_button = _keyboard_activate(
                    page,
                    first_item_selector + " .warning-badge",
                    focus_trace,
                )
                focused_overlay = page.locator(
                    '#bbox-layer .bbox-overlay[data-block-id="'
                    + first_high_risk_block
                    + '"]'
                )
                expect(focused_overlay).to_be_focused(timeout=10_000)
                _record_active_focus(page, focus_trace)
                source_jump_page = int(
                    page.locator("#preview-page-select").input_value()
                )
                overlay_percent = focused_overlay.evaluate(
                    """(element) => ({
                      x: parseFloat(element.style.left),
                      y: parseFloat(element.style.top),
                      width: parseFloat(element.style.width),
                      height: parseFloat(element.style.height),
                    })"""
                )
                source_geometry = first_high_risk_item["source_page_geometry"]
                source_jump_bbox = {
                    key: round(
                        overlay_percent[key]
                        * source_geometry[
                            "width" if key in {"x", "width"} else "height"
                        ]
                        / 100,
                        3,
                    )
                    for key in ("x", "y", "width", "height")
                }
                source_jump_bbox.update(
                    {
                        "unit": first_high_risk_item["source_bbox"]["unit"],
                        "origin": first_high_risk_item["source_bbox"]["origin"],
                    }
                )
                warning_details_payload = first_high_risk_item.get("warning_details")
                if not isinstance(warning_details_payload, list) or not warning_details_payload:
                    raise AssertionError(
                        "high-risk review target did not expose warning details"
                    )
                warning_evidence = warning_details_payload[0]
                warning_text = warning_button.inner_text()
                for warning_field in ("code", "message", "remediation"):
                    warning_value = warning_evidence.get(warning_field)
                    if not isinstance(warning_value, str) or warning_value not in warning_text:
                        raise AssertionError(
                            f"warning UI did not match API {warning_field}"
                        )

                edit = _tab_to(
                    page,
                    first_item_selector + " .review-edit",
                    focus_trace,
                )
                page.keyboard.press("ControlOrMeta+A")
                revised_text = first_high_risk_item["text"] + " verified"
                page.keyboard.type(revised_text)
                expect(edit).to_have_value(revised_text)
                _keyboard_activate(
                    page,
                    first_item_selector
                    + ' button[data-review-action-name="edit"]:not([disabled])',
                    focus_trace,
                )
                expect(page.locator("#review-action-status")).to_contain_text(
                    "queued for audit",
                    timeout=10_000,
                )
                _keyboard_activate(
                    page,
                    first_item_selector
                    + ' button[data-review-action-name="needs_fix"]:not([disabled])',
                    focus_trace,
                )
                expect(
                    page.locator(
                        first_item_selector + ' [data-review-state-for="'
                        + first_high_risk_block
                        + '"]'
                    )
                ).to_have_text("needs fix")
                blocked_before_approval = (
                    high_risk_items.first.get_attribute("data-review-state")
                    == "needs_fix"
                )
                approval_selector = (
                    first_item_selector
                    + ' button[data-review-action-name="approve"]:not([disabled])'
                )
                approval_state = page.locator(
                    first_item_selector + ' [data-review-state-for="'
                    + first_high_risk_block
                    + '"]'
                )
                _keyboard_activate(
                    page,
                    approval_selector,
                    focus_trace,
                )
                approval_status = page.locator("#review-action-status")
                expect(approval_status).to_contain_text(
                    "review approval is blocked while needs-fix is unresolved",
                    timeout=10_000,
                )
                expect(approval_state).to_have_text("needs fix")
                approval_block_message = approval_status.inner_text().strip()
                with page.expect_response(
                    lambda response: (
                        response.request.method == "POST"
                        and response.url == base_url + "/api/review-events"
                    )
                ) as reject_response_info:
                    _keyboard_activate(
                        page,
                        first_item_selector
                        + ' button[data-review-action-name="reject"]:not([disabled])',
                        focus_trace,
                    )
                reject_response = reject_response_info.value
                reject_payload = _json_response(reject_response)
                reject_event = reject_payload.get("audit_event")
                if (
                    not reject_response.ok
                    or not isinstance(reject_event, dict)
                    or reject_event.get("action") != "reject"
                    or reject_event.get("block_id") != first_high_risk_block
                ):
                    raise AssertionError(
                        "keyboard reject audit request was not accepted for the target"
                    )

                page.screenshot(path=str(keyboard_screenshot), full_page=True)

                job_events_response = context.request.get(
                    base_url + "/api/job-events",
                    headers=auth_headers,
                )
                if not job_events_response.ok:
                    raise AssertionError("job audit event lookup failed")
                job_events = _events(_json_response(job_events_response))
                upload_event, upload_event_count = _require_matching_event(
                    job_events,
                    expected_fields={
                        "action": "browser_upload",
                        "job_id": job_id,
                        "filename": FIXTURE_PATH.name,
                        "source_sha256": source_sha256,
                    },
                    description="browser upload audit event",
                )
                job_events_path.write_text(
                    json.dumps(job_events, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                review_events_response = context.request.get(
                    base_url + "/api/review-events", headers=auth_headers
                )
                if not review_events_response.ok:
                    raise AssertionError("review audit event lookup failed")
                review_events = _events(_json_response(review_events_response))
                expected_review_actor = {
                    "id": f"local-principal:{approver_actor}",
                    "role": "approver",
                }
                review_event, approval_event_count = _require_matching_event(
                    review_events,
                    expected_fields={
                        "action": "approve",
                        "conversion_id": conversion_id,
                        "document_id": review_document_id,
                        "block_id": review_block_id,
                        "actor": expected_review_actor,
                        "source_page": review_source_page,
                        "source_bbox": review_source_bbox,
                    },
                    description="browser approval audit event",
                )
                high_risk_conversion_id = high_risk_result["audit"]["conversion_id"]
                for action, target in (
                    ("edit", first_high_risk_item),
                    ("needs_fix", first_high_risk_item),
                    ("reject", first_high_risk_item),
                ):
                    _require_matching_event(
                        review_events,
                        expected_fields={
                            "action": action,
                            "conversion_id": high_risk_conversion_id,
                            "document_id": target["document_id"],
                            "block_id": target["block_id"],
                            "actor": expected_review_actor,
                        },
                        description=f"keyboard {action} audit event",
                    )
                review_events_path.write_text(
                    json.dumps(review_events, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                review_actor = review_event["actor"]
                evidence = {
                    "schema_version": "veridoc-mvp-browser-e2e/v1",
                    "run_id": run_id,
                    "correlation": {
                        "run_id": run_id,
                        "upload": {
                            "source_filename": FIXTURE_PATH.name,
                            "source_sha256": source_sha256,
                        },
                        "job": {
                            "job_id": job_id,
                            "status": job_status,
                            "conversion_status": conversion_status,
                            "created_at": authoritative_job.get("created_at"),
                        },
                        "review": {
                            "conversion_id": review_event.get("conversion_id"),
                            "document_id": review_event.get("document_id"),
                            "block_id": review_event.get("block_id"),
                            "action": review_event.get("action"),
                            "actor": review_actor,
                        },
                        "provenance": {
                            "source_filename": result_audit.get("source_filename"),
                            "source_sha256": result_audit.get("source_sha256"),
                            "document_id": review_document_id,
                            "block_id": review_block_id,
                            "source_page": review_source_page,
                            "source_bbox": review_source_bbox,
                        },
                        "artifact": {
                            "artifact_id": artifact_id,
                            "filename": artifact.get("filename"),
                            "sha256": downloaded_sha256,
                        },
                        "audit": {
                            "artifact_sha256": artifact_audit_sha256,
                            "audit_artifact_sha256": audit_downloaded_sha256,
                            "job_event_count": upload_event_count,
                            "review_event_count": approval_event_count,
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
                            "job_event_hash": upload_event.get("event_hash"),
                            "review_event_hash": review_event.get("event_hash"),
                        },
                    },
                    "recovery": {
                        "user_visible_error": recovery_message,
                        "retry_mode": "pdf_to_word",
                        "result": "completed",
                    },
                    "review_flow": {
                        "keyboard_only": True,
                        "focus_trace": focus_trace,
                        "actions": ["edit", "needs_fix", "approve", "reject"],
                        "warnings": [warning_evidence],
                        "high_risk": {
                            "conversion_id": high_risk_conversion_id,
                            "review_target_count": len(high_risk_api_items),
                            "auto_confirmed_count": auto_confirmed_count,
                            "approval_blocked_while_unresolved": True,
                            "approval_block_reason": approval_block_message,
                        },
                        "source_jump": {
                            "block_id": first_high_risk_block,
                            "source_filename": high_risk_audit["source_filename"],
                            "source_type": high_risk_audit["source_type"],
                            "source_sha256": high_risk_audit["source_sha256"],
                            "page": source_jump_page,
                            "review_item_page": first_high_risk_item["source_page"],
                            "bbox": source_jump_bbox,
                            "review_item_bbox": first_high_risk_item["source_bbox"],
                        },
                        "unresolved": {
                            "blocked_before_approval": blocked_before_approval,
                            "block_id": first_high_risk_block,
                            "state": "needs_fix",
                        },
                    },
                    "files": {
                        "trace": trace_path.name,
                        "screenshots": [
                            recovery_screenshot.name,
                            completed_screenshot.name,
                            audit_screenshot.name,
                            keyboard_screenshot.name,
                        ],
                        "api_result": api_result_path.name,
                        "high_risk_api_result": high_risk_api_result_path.name,
                        "job_events": job_events_path.name,
                        "review_events": review_events_path.name,
                        "audit_artifact": audit_artifact_path.name,
                        "download": download_name,
                    },
                }
                evidence["acceptance_snapshot"] = evaluate_acceptance_evidence(
                    evidence,
                    run_dir=run_dir,
                )
                (run_dir / "evidence.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                if evidence["acceptance_snapshot"]["status"] != "pass":
                    raise AssertionError(
                        "browser acceptance evidence failed closed: "
                        + json.dumps(
                            evidence["acceptance_snapshot"]["failure_reasons"],
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
                return evidence
            finally:
                if tracing_started:
                    context.tracing.stop(path=str(raw_trace_path))
                    _retain_redacted_trace(
                        raw_trace_path, trace_path, secret=auth_token
                    )
                context.close()
                browser.close()


def main() -> int:
    evidence_root = Path(
        os.environ.get("VERIDOC_E2E_EVIDENCE_DIR", "artifacts/mvp-browser-e2e")
    )
    evidence = run_browser_e2e(evidence_root=evidence_root)
    print(evidence_root / evidence["run_id"] / "evidence.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
