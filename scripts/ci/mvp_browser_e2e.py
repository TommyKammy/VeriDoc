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
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from zipfile import ZipFile

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.api.job_queue import JobQueue
from services.api.poc_web import (
    JobAuditEventStore,
    PocWebRequestHandler,
    ReviewAuditEventStore,
)

FIXTURE_PATH = (
    REPO_ROOT / "datasets" / "fixtures" / "pdf" / "pdf-to-word-representative.pdf"
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


@contextmanager
def _poc_server(state_root: Path, *, auth_token: str) -> Iterator[str]:
    previous_auth = os.environ.get("VERIDOC_LOCAL_AUTH_TOKENS")
    os.environ["VERIDOC_LOCAL_AUTH_TOKENS"] = (
        f"approver:e2e-{uuid.uuid4().hex}={auth_token}"
    )
    database_path = state_root / "veridoc.sqlite3"
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue(
        database_path=database_path,
        artifact_store_root=state_root / "artifacts",
    )
    server.job_event_store = JobAuditEventStore(database_path=database_path)
    server.review_event_store = ReviewAuditEventStore()
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
    try:
        return playwright.chromium.launch(channel="chrome", headless=True)
    except Exception:
        return playwright.chromium.launch(headless=True)


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
    api_result_path = run_dir / "api-result.json"
    auth_token = uuid.uuid4().hex

    with tempfile.TemporaryDirectory(prefix="veridoc-browser-e2e-") as state_dir:
        with _poc_server(
            Path(state_dir), auth_token=auth_token
        ) as base_url, sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
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
                api_result_path.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                job_id = result.get("job_id")
                if not isinstance(job_id, str) or not job_id:
                    status_text = page.locator("#page-status").inner_text()
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
                approve.click()
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
                with page.expect_download(timeout=10_000) as download_info:
                    page.locator("#download-link").click()
                download = download_info.value
                download_name = f"download-{download.suggested_filename}"
                download_path = run_dir / download_name
                download.save_as(download_path)
                downloaded_sha256 = _sha256(download_path)
                if downloaded_sha256 != artifact.get("sha256"):
                    raise AssertionError("downloaded artifact hash did not match the API result")
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
                audit_response = context.request.get(
                    base_url + audit_artifact["href"], headers=auth_headers
                )
                if not audit_response.ok:
                    raise AssertionError("audit JSON artifact download failed")
                audit_payload = _json_response(audit_response)
                audit_artifact_path = run_dir / "audit-artifact.json"
                audit_artifact_path.write_text(
                    json.dumps(audit_payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

                page.locator('[data-nav-target="audit"]').click()
                page.locator("#refresh-audit").click()
                expect(page.locator("#audit-body tr").first).to_be_visible(timeout=10_000)
                page.screenshot(path=str(audit_screenshot), full_page=True)

                job_events = _events(
                    _json_response(
                        context.request.get(
                            base_url + f"/api/job-events?job_id={job_id}",
                            headers=auth_headers,
                        )
                    )
                )
                review_events = _events(
                    _json_response(
                        context.request.get(
                            base_url + "/api/review-events", headers=auth_headers
                        )
                    )
                )
                review_event = review_events[-1]
                evidence = {
                    "schema_version": "veridoc-mvp-browser-e2e/v1",
                    "run_id": run_id,
                    "correlation": {
                        "run_id": run_id,
                        "upload": {
                            "source_filename": FIXTURE_PATH.name,
                            "source_sha256": _sha256(FIXTURE_PATH),
                        },
                        "job": {
                            "job_id": job_id,
                            "status": job_status,
                            "conversion_status": conversion_status,
                        },
                        "review": {
                            "document_id": review_event.get("document_id"),
                            "block_id": review_event.get("block_id"),
                            "action": review_event.get("action"),
                        },
                        "artifact": {
                            "artifact_id": artifact.get("artifact_id"),
                            "filename": artifact.get("filename"),
                            "sha256": downloaded_sha256,
                        },
                        "audit": {
                            "artifact_sha256": artifact_audit_sha256,
                            "job_event_count": len(job_events),
                            "review_event_count": len(review_events),
                        },
                    },
                    "recovery": {
                        "user_visible_error": recovery_message,
                        "retry_mode": "pdf_to_word",
                        "result": "completed",
                    },
                    "files": {
                        "trace": trace_path.name,
                        "screenshots": [
                            recovery_screenshot.name,
                            completed_screenshot.name,
                            audit_screenshot.name,
                        ],
                        "api_result": api_result_path.name,
                        "audit_artifact": audit_artifact_path.name,
                        "download": download_name,
                    },
                }
                (run_dir / "evidence.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
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
