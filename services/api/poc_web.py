from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import binascii
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from threading import Lock
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from xml.etree.ElementTree import ParseError as XmlParseError
from zipfile import BadZipFile

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.ir.document_ir_v1 import (
    DocumentIRV1,
    UNITS,
    adapt_document_ir_v0_blocks,
    from_parser_output,
    validate_document_ir_v1,
)
from core.parsers.docx_extraction import extract_docx_structure
from core.parsers.pdf_text_extraction import MissingPdfExtractorDependency, parse_text_pdf_to_document_ir
from core.parsers.xlsx_extraction import extract_xlsx_structure
from services.api.job_queue import JobQueue, JobRecord

WEB_ROOT = REPO_ROOT / "apps" / "web"
INFERENCE_PROFILES_PATH = REPO_ROOT / "services" / "api" / "inference_profiles.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_REQUEST_BYTES = (MAX_UPLOAD_BYTES * 4 // 3) + 4096
# Extracted document text can be much larger than the uploaded source bytes,
# especially for compressed formats. Review events can carry original and
# revised text snapshots; quote/backslash-heavy text doubles again when
# JSON-escaped.
MAX_REVIEW_EVENT_TEXT_BYTES = 8 * 1024 * 1024
MAX_REVIEW_EVENT_REQUEST_BYTES = (MAX_REVIEW_EVENT_TEXT_BYTES * 4) + (64 * 1024)
SOURCE_TYPES = {"pdf", "docx", "xlsx", "unknown"}
KNOWN_SOURCE_TYPES = SOURCE_TYPES - {"unknown"}
LOCAL_AUTH_TOKENS_ENV = "VERIDOC_LOCAL_AUTH_TOKENS"
ROLES = {"viewer", "reviewer", "approver", "admin"}
ROLE_PERMISSIONS = {
    "viewer": {
        "job_events:read",
        "jobs:read",
        "review_events:read",
        "templates:read",
    },
    "reviewer": {
        "convert",
        "job_events:read",
        "jobs:create",
        "jobs:read",
        "review_events:edit",
        "review_events:read",
        "templates:read",
    },
    "approver": {
        "convert",
        "job_events:read",
        "jobs:create",
        "jobs:read",
        "review_events:approve",
        "review_events:edit",
        "review_events:read",
        "templates:read",
    },
    "admin": {
        "convert",
        "job_events:read",
        "jobs:create",
        "jobs:read",
        "jobs:retry",
        "review_events:approve",
        "review_events:edit",
        "review_events:read",
        "templates:manage",
        "templates:read",
    },
}
HTTP_CONTENT_TYPE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+"
    r"(?:[ \t]*;[ \t]*[A-Za-z0-9!#$&^_.+-]+=[A-Za-z0-9!#$&^_.+-]+)*$"
)
DEFAULT_JOB_QUEUE = JobQueue()
AUDIT_INTEGRITY_ALGORITHM = "sha256-canonical-json-chain-v1"


class PocServerDependencyError(RuntimeError):
    """Raised when the PoC server is missing an optional parser dependency."""


class ReviewAuditEventStore:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._integrity_checkpoint = _audit_event_integrity_checkpoint(self._events)
        self._lock = Lock()

    def record(self, audit_event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_integrity_locked()
            event = _audit_event_with_integrity(
                audit_event,
                previous_events=self._events,
                checkpoint=self._integrity_checkpoint,
            )
            self._events.append(event)
            self._integrity_checkpoint = _audit_event_integrity_checkpoint(self._events)
        return deepcopy(event)

    def record_validated(
        self,
        audit_event: dict[str, Any],
        validate: Callable[[dict[str, Any], list[dict[str, Any]]], None],
    ) -> dict[str, Any]:
        event = deepcopy(audit_event)
        with self._lock:
            self._require_integrity_locked()
            validate(event, [_review_workflow_event_view(item) for item in self._events])
            event = _audit_event_with_integrity(
                event,
                previous_events=self._events,
                checkpoint=self._integrity_checkpoint,
            )
            self._events.append(event)
            self._integrity_checkpoint = _audit_event_integrity_checkpoint(self._events)
        return deepcopy(event)

    def _require_integrity_locked(self) -> None:
        _raise_for_audit_event_integrity_violation(
            self._events,
            checkpoint=self._integrity_checkpoint,
        )

    def list_events(self, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if filters:
                events = [
                    event
                    for event in self._events
                    if _review_event_matches_filters(event, filters)
                ]
            else:
                events = self._events
            return deepcopy(events)

    def verify_integrity(self) -> dict[str, Any]:
        with self._lock:
            return _verify_audit_event_integrity(
                self._events,
                checkpoint=self._integrity_checkpoint,
            )


class JobAuditEventStore:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._integrity_checkpoint = _audit_event_integrity_checkpoint(self._events)
        self._lock = Lock()

    def record(self, audit_event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._require_integrity_locked()
            event = _audit_event_with_integrity(
                audit_event,
                previous_events=self._events,
                checkpoint=self._integrity_checkpoint,
            )
            self._events.append(event)
            self._integrity_checkpoint = _audit_event_integrity_checkpoint(self._events)
        return deepcopy(event)

    def record_once(
        self,
        audit_event: dict[str, Any],
        *,
        dedupe: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            self._require_integrity_locked()
            for event in self._events:
                if all(event.get(name) == value for name, value in dedupe.items()):
                    return deepcopy(event)
            event = _audit_event_with_integrity(
                audit_event,
                previous_events=self._events,
                checkpoint=self._integrity_checkpoint,
            )
            self._events.append(event)
            self._integrity_checkpoint = _audit_event_integrity_checkpoint(self._events)
        return deepcopy(event)

    def find_once(self, *, dedupe: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            self._require_integrity_locked()
            for event in self._events:
                if all(event.get(name) == value for name, value in dedupe.items()):
                    return deepcopy(event)
        return None

    def require_integrity(self) -> None:
        with self._lock:
            self._require_integrity_locked()

    def _require_integrity_locked(self) -> None:
        _raise_for_audit_event_integrity_violation(
            self._events,
            checkpoint=self._integrity_checkpoint,
        )

    def list_events(self, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if filters:
                events = [
                    event
                    for event in self._events
                    if _job_event_matches_filters(event, filters)
                ]
            else:
                events = self._events
            return deepcopy(events)

    def verify_integrity(self) -> dict[str, Any]:
        with self._lock:
            return _verify_audit_event_integrity(
                self._events,
                checkpoint=self._integrity_checkpoint,
            )


def _audit_event_with_integrity(
    audit_event: dict[str, Any],
    *,
    previous_events: list[dict[str, Any]],
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    event = deepcopy(audit_event)
    event["integrity_algorithm"] = AUDIT_INTEGRITY_ALGORITHM
    expected_terminal_sequence = checkpoint.get("terminal_sequence")
    if expected_terminal_sequence != len(previous_events):
        raise ValueError(
            "audit log integrity violation: audit log terminal sequence mismatch"
        )
    event["sequence"] = expected_terminal_sequence + 1
    previous_hash = previous_events[-1].get("event_hash") if previous_events else None
    event["prev_event_hash"] = previous_hash if isinstance(previous_hash, str) else None
    event["event_hash"] = _audit_event_hash(event)
    return event


def _audit_event_integrity_checkpoint(events: list[dict[str, Any]]) -> dict[str, Any]:
    head_hash = events[-1].get("event_hash") if events else None
    return {
        "terminal_sequence": len(events),
        "head_event_hash": head_hash if isinstance(head_hash, str) else None,
    }


def _verify_audit_event_integrity(
    events: list[dict[str, Any]],
    *,
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    previous_hash: str | None = None
    for index, event in enumerate(events):
        sequence = index + 1
        if event.get("integrity_algorithm") != AUDIT_INTEGRITY_ALGORITHM:
            errors.append(f"event[{index}] integrity algorithm mismatch")
        if event.get("sequence") != sequence:
            errors.append(f"event[{index}] sequence mismatch")
        if event.get("prev_event_hash") != previous_hash:
            errors.append(f"event[{index}] previous hash mismatch")
        event_hash = event.get("event_hash")
        if not isinstance(event_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", event_hash):
            errors.append(f"event[{index}] hash missing")
        elif event_hash != _audit_event_hash(event):
            errors.append(f"event[{index}] hash mismatch")
        previous_hash = event_hash if isinstance(event_hash, str) else None
    errors.extend(_audit_event_checkpoint_errors(events, checkpoint=checkpoint))
    ok = not errors
    return {"ok": ok, "errors": errors}


def _audit_event_checkpoint_errors(
    events: list[dict[str, Any]],
    *,
    checkpoint: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    expected_terminal_sequence = checkpoint.get("terminal_sequence")
    if expected_terminal_sequence != len(events):
        errors.append("audit log terminal sequence mismatch")
    expected_head_hash = checkpoint.get("head_event_hash")
    actual_head_hash = events[-1].get("event_hash") if events else None
    if expected_head_hash != actual_head_hash:
        errors.append("audit log head hash mismatch")
    return errors


def _raise_for_audit_event_integrity_violation(
    events: list[dict[str, Any]],
    *,
    checkpoint: dict[str, Any],
) -> None:
    result = _verify_audit_event_integrity(events, checkpoint=checkpoint)
    if not result["ok"]:
        details = "; ".join(result["errors"])
        raise ValueError(f"audit log integrity violation: {details}")


def _audit_event_hash(event: dict[str, Any]) -> str:
    hash_input = {key: value for key, value in event.items() if key != "event_hash"}
    canonical = json.dumps(
        hash_input,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class TemplateStore:
    def __init__(self) -> None:
        self._templates: dict[str, dict[str, Any]] = {}
        self._lock = Lock()

    @classmethod
    def with_representative_defaults(cls) -> "TemplateStore":
        store = cls()
        for template in _representative_templates():
            store.register_template(template)
        return store

    def register_template(self, request: dict[str, Any]) -> dict[str, Any]:
        template_id = _validate_template_id(request.get("template_id"))
        name = _validate_template_text_field(request.get("name"), "name")
        category = _validate_template_text_field(request.get("category"), "category")
        fields = _validate_template_fields(request.get("fields"))
        change_reason = _validate_template_text_field(request.get("change_reason"), "change_reason")
        actor = _validate_template_actor(request.get("actor"), "actor")
        approved_by = request.get("approved_by")
        approval = _template_change_approval(approved_by)
        with self._lock:
            existing = self._templates.get(template_id)
            versions = [] if existing is None else existing["versions"]
            latest_version = None if not versions else versions[-1]
            status_default = "active" if existing is None else existing.get("status", "active")
            status_value = request["status"] if "status" in request else status_default
            if status_value is None:
                status_value = status_default
            status = _validate_template_status(status_value)
            document_type = _validate_template_text_field(
                _template_version_value(request, latest_version, "document_type", category),
                "document_type",
            )
            anchors = _validate_template_json_list(
                _template_version_value(request, latest_version, "anchors", []), "anchors"
            )
            tables = _validate_template_json_list(
                _template_version_value(request, latest_version, "tables", []), "tables"
            )
            risk_rank = _validate_template_json_object(
                _template_version_value(request, latest_version, "risk_rank", {}), "risk_rank"
            )
            validation_rules = _validate_template_json_list(
                _template_version_value(request, latest_version, "validation_rules", []),
                "validation_rules",
            )
            output_mapping = _validate_template_json_object(
                _template_version_value(request, latest_version, "output_mapping", {}),
                "output_mapping",
            )
            if "content" in request:
                content = _validate_template_text_field(request.get("content"), "content")
            elif latest_version is not None:
                content = latest_version.get("content", "")
            else:
                content = ""
            version_number = len(versions) + 1
            created_at = datetime.now(timezone.utc).isoformat()
            action = _template_change_action(existing, status)
            change_event = {
                "event_type": "template.change_recorded",
                "action": action,
                "template_id": template_id,
                "version": version_number,
                "change_reason": change_reason,
                "actor": deepcopy(actor),
                "approval": deepcopy(approval),
                "recorded_at": created_at,
            }
            version = {
                "version": version_number,
                "status": status,
                "document_type": document_type,
                "anchors": deepcopy(anchors),
                "fields": deepcopy(fields),
                "tables": deepcopy(tables),
                "risk_rank": deepcopy(risk_rank),
                "validation_rules": deepcopy(validation_rules),
                "output_mapping": deepcopy(output_mapping),
                "content": content,
                "created_at": created_at,
                "change_history": [deepcopy(change_event)],
            }
            if existing is None:
                record = {
                    "template_id": template_id,
                    "name": name,
                    "category": category,
                    "document_type": document_type,
                    "status": status,
                    "current_version": version_number,
                    "versions": [version],
                    "change_history": [deepcopy(change_event)],
                    "updated_at": created_at,
                }
                self._templates[template_id] = record
            else:
                existing["name"] = name
                existing["category"] = category
                existing["document_type"] = document_type
                existing["status"] = status
                existing["current_version"] = version_number
                existing["versions"].append(version)
                existing.setdefault("change_history", []).append(deepcopy(change_event))
                existing["updated_at"] = created_at
                record = existing
            return deepcopy(record)

    def list_templates(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                _template_summary(record)
                for record in sorted(
                    self._templates.values(),
                    key=lambda item: item["template_id"],
                )
            ]

    def get_template(self, template_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                return deepcopy(self._templates[template_id])
            except KeyError as exc:
                raise KeyError(f"unknown template_id: {template_id}") from exc

    def latest_job_snapshot(self, template_id: str) -> dict[str, Any]:
        record = self.get_template(template_id)
        if record.get("status", "active") != "active":
            raise ValueError("template_id is inactive")
        return {
            "template_id": record["template_id"],
            "template_version": record["current_version"],
            "name": record["name"],
        }


def _template_version_value(
    request: dict[str, Any],
    latest_version: dict[str, Any] | None,
    field_name: str,
    default: Any,
) -> Any:
    if field_name in request:
        return request[field_name]
    if latest_version is not None:
        return latest_version.get(field_name, default)
    return default


def _review_workflow_event_view(audit_event: dict[str, Any]) -> dict[str, Any]:
    actor = audit_event.get("actor")
    actor_view = {
        "id": actor.get("id") if isinstance(actor, dict) else None,
        "role": actor.get("role") if isinstance(actor, dict) else None,
    }
    return {
        "action": audit_event.get("action"),
        "conversion_id": audit_event.get("conversion_id"),
        "document_id": audit_event.get("document_id"),
        "block_id": audit_event.get("block_id"),
        "original_text": audit_event.get("original_text"),
        "revised_text": audit_event.get("revised_text"),
        "actor": actor_view,
    }


DEFAULT_REVIEW_AUDIT_EVENTS = ReviewAuditEventStore()
DEFAULT_JOB_AUDIT_EVENTS = JobAuditEventStore()
LLM_EXTRACTOR_NAME_TOKENS = ("llm", "gpt", "openai")
LLM_INFERENCE_PROFILE_FIELDS = ("id", "label", "provider", "model_family", "recommended_model")


def convert_uploaded_document(*, filename: str, content: bytes) -> dict[str, Any]:
    """Convert one uploaded PoC document into IR, review details, and download bytes."""
    safe_filename = _safe_filename(filename)
    conversion_id = _conversion_id()
    source_sha256 = _sha256_hex(content)
    parser_output, input_warnings = _parser_output_from_upload(safe_filename, content)
    document_ir = from_parser_output(
        parser_output,
        document_id=_document_id_from_parser_output(safe_filename, parser_output),
        title=_document_title_from_parser_output(safe_filename, parser_output),
        source_type=_source_type(safe_filename, parser_output),
    )
    validation = validate_document_ir_v1(document_ir)
    review_items = _review_items(document_ir)
    download_payload = {
        "document_ir": document_ir.to_dict(),
        "validation": asdict(validation),
        "review_items": review_items,
        "warnings": [*input_warnings, *validation.warnings],
    }
    download_content = _strict_json_bytes(download_payload, indent=2)
    output_sha256 = _sha256_hex(download_content)
    return {
        "status": _status(validation.ok, validation.requires_review),
        "conversion_id": conversion_id,
        "hashes": {
            "source_sha256": source_sha256,
            "output_sha256": output_sha256,
        },
        "hash_verification": {
            "source": {
                "status": "recorded",
                "sha256": source_sha256,
            },
            "output": {
                "status": "match",
                "expected_sha256": output_sha256,
                "actual_sha256": output_sha256,
            },
        },
        **download_payload,
        "download": {
            "filename": f"{Path(safe_filename).stem}.veridoc-result.json",
            "content_type": "application/json; charset=utf-8",
            "content": download_content,
        },
    }


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Run the stdlib PoC API server."""
    server = ThreadingHTTPServer((host, port), PocWebRequestHandler)
    print(f"VeriDoc PoC web API listening on http://{host}:{port}")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--check"]:
        return 0
    run()
    return 0


class PocWebRequestHandler(BaseHTTPRequestHandler):
    server_version = "VeriDocPoC/0.1"

    def do_GET(self) -> None:
        parsed_url = urlsplit(self.path)
        path = parsed_url.path
        if path in {"/", "/index.html"}:
            self._send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if path == "/api/jobs":
            authorized, auth_context = self._authorized_context_for_permission("jobs:read")
            if not authorized:
                return
            role = auth_context["role"] if auth_context is not None else None
            self._handle_list_jobs(parsed_url.query, role=role)
            return
        if path == "/api/review-events":
            if not self._require_permission("review_events:read"):
                return
            self._handle_list_review_events(parsed_url.query)
            return
        if path == "/api/job-events":
            if not self._require_permission("job_events:read"):
                return
            self._handle_list_job_events(parsed_url.query)
            return
        if path == "/api/templates":
            if not self._require_permission("templates:read"):
                return
            self._handle_list_templates()
            return
        if path.startswith("/api/templates/"):
            if not self._require_permission("templates:read"):
                return
            self._handle_get_template(path.removeprefix("/api/templates/"))
            return
        if path.startswith("/api/jobs/"):
            authorized, auth_context = self._authorized_context_for_permission("jobs:read")
            if not authorized:
                return
            role = auth_context["role"] if auth_context is not None else None
            job_path = path.removeprefix("/api/jobs/")
            if job_path.endswith("/result"):
                self._handle_job_result_download(
                    job_path.removesuffix("/result"),
                    auth_context=auth_context,
                )
                return
            job_id = job_path
            job_queue = self._job_queue()
            try:
                job = job_queue.get_job(job_id)
            except KeyError:
                self._send_json({"error": "job_not_found"}, status=404)
                return
            self._send_json({"job": _job_response(job, job_queue, role=role)})
            return
        self._send_json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/jobs":
            authorized, auth_context = self._authorized_context_for_permission("jobs:create")
            if not authorized:
                return
            role = auth_context["role"] if auth_context is not None else None
            self._handle_create_job(role=role, auth_context=auth_context)
            return
        if path == "/api/job-events":
            self._handle_job_event()
            return
        if path == "/api/review-events":
            self._handle_review_event()
            return
        if path == "/api/templates":
            authenticated, auth_context = self._authenticated_context()
            if not authenticated:
                return
            role = auth_context["role"] if auth_context is not None else None
            if not self._role_has_permission(role, "templates:manage"):
                return
            self._handle_register_template(auth_context=auth_context)
            return
        if path != "/api/convert":
            self._send_json({"error": "not_found"}, status=404)
            return
        authorized, role = self._authorized_role_for_permission("convert")
        if not authorized:
            return
        try:
            request = self._read_json_request()
            filename = str(request.get("filename") or "upload.txt")
            content = _decode_request_content(request)
            if len(content) > MAX_UPLOAD_BYTES:
                self._send_json({"error": "upload_too_large"}, status=413)
                return
            result = convert_uploaded_document(filename=filename, content=content)
        except PocServerDependencyError as exc:
            self._send_json(
                {"error": "server_dependency_unavailable", "message": str(exc)},
                status=500,
            )
            return
        except (json.JSONDecodeError, ValueError) as exc:
            if str(exc) == "content_length_required":
                self._send_json({"error": "content_length_required"}, status=411)
                return
            if str(exc) == "upload_too_large":
                self._send_json({"error": "upload_too_large"}, status=413)
                return
            self._send_json({"error": "invalid_upload", "message": str(exc)}, status=400)
            return
        self._send_json(_http_result(result, role=role))

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_create_job(
        self,
        *,
        role: str | None = None,
        auth_context: dict[str, str | None] | None = None,
    ) -> None:
        try:
            request = self._read_json_request()
            filename = str(request.get("filename") or "").strip()
            mode = str(request.get("mode") or "standard")
            idempotency_key = str(
                request.get("idempotency_key") or self.headers.get("Idempotency-Key") or ""
            )
            source = _job_source_from_request(request, filename=filename)
            desktop_upload_audit = _desktop_upload_audit_requested(request)
            requested_template = self._job_template_binding(request.get("template_id"))
            job_queue = self._job_queue()
            upload_audit_event = None
            if not desktop_upload_audit:
                job, created_job = job_queue.get_or_create_job(
                    idempotency_key=idempotency_key,
                    filename=filename,
                    mode=mode,
                    source=source,
                    template=requested_template,
                    create_template=lambda: self._job_template_snapshot(request.get("template_id")),
                )
            else:
                job, created_job = job_queue.get_or_create_job(
                    idempotency_key=idempotency_key,
                    filename=filename,
                    mode=mode,
                    source=source,
                    template=requested_template,
                    create_template=lambda: self._job_template_snapshot(
                        request.get("template_id")
                    ),
                    enqueue=False,
                    publish=False,
                    include_unpublished=True,
                )
                job_event_store = self._job_event_store()
                try:
                    if not isinstance(job.source, dict):
                        raise ValueError("desktop_upload requires stored job source")
                    job_event_store.require_integrity()
                    upload_audit_event = _job_event_with_auth_context(
                        _desktop_upload_audit_event(job),
                        auth_context,
                    )
                    upload_actor = upload_audit_event.get("actor")
                    upload_actor_id = (
                        upload_actor.get("id") if isinstance(upload_actor, dict) else None
                    )
                    upload_audit_event["actor_id"] = upload_actor_id
                    upload_dedupe = {
                        "job_id": job.job_id,
                        "action": "desktop_upload",
                        "actor_id": upload_actor_id,
                    }
                    existing_upload_audit = job_event_store.find_once(dedupe=upload_dedupe)
                    if not created_job:
                        if existing_upload_audit is None:
                            existing_job_upload_audit = job_event_store.find_once(
                                dedupe={
                                    "job_id": job.job_id,
                                    "action": "desktop_upload",
                                }
                            )
                            if (
                                existing_job_upload_audit is None
                                and job_queue.is_unpublished(job.job_id)
                            ):
                                published_job = job_queue.wait_until_published(job.job_id)
                                if published_job is not None:
                                    job = published_job
                                    existing_upload_audit = job_event_store.find_once(
                                        dedupe=upload_dedupe
                                    )
                            if existing_upload_audit is None:
                                raise ValueError(
                                    "desktop_upload audit cannot be added after idempotent job creation"
                                )
                        upload_audit_event = existing_upload_audit
                        if job_queue.is_unpublished(job.job_id):
                            job = job_queue.publish_job(job.job_id, enqueue=True)
                    else:
                        upload_audit_event = job_event_store.record_once(
                            upload_audit_event,
                            dedupe=upload_dedupe,
                        )
                except Exception:
                    if created_job:
                        job_queue.discard_queued_job(job.job_id)
                    raise
                if created_job:
                    job = job_queue.publish_job(job.job_id, enqueue=True)
        except RuntimeError as exc:
            self._send_json({"error": "job_conflict", "message": str(exc)}, status=409)
            return
        except ValueError as exc:
            if str(exc) == "content_length_required":
                self._send_json({"error": "content_length_required"}, status=411)
                return
            if str(exc) == "upload_too_large":
                self._send_json({"error": "upload_too_large"}, status=413)
                return
            self._send_json({"error": "invalid_job_request", "message": str(exc)}, status=400)
            return
        response = {"job": _job_response(job, self._job_queue(), role=role)}
        if upload_audit_event is not None:
            response["audit_event"] = upload_audit_event
        self._send_json(response, status=202)

    def _handle_register_template(
        self,
        *,
        auth_context: dict[str, str | None] | None = None,
    ) -> None:
        try:
            request = self._read_json_request()
            request = _template_request_with_auth_context(request, auth_context)
            template = self._template_store().register_template(request)
        except ValueError as exc:
            if str(exc) == "content_length_required":
                self._send_json({"error": "content_length_required"}, status=411)
                return
            if str(exc) == "upload_too_large":
                self._send_json({"error": "upload_too_large"}, status=413)
                return
            self._send_json({"error": "invalid_template_request", "message": str(exc)}, status=400)
            return
        self._send_json({"template": template}, status=201)

    def _handle_list_templates(self) -> None:
        self._send_json({"templates": self._template_store().list_templates()})

    def _handle_get_template(self, template_id: str) -> None:
        try:
            template = self._template_store().get_template(_validate_template_id(template_id))
        except KeyError:
            self._send_json({"error": "template_not_found"}, status=404)
            return
        except ValueError as exc:
            self._send_json({"error": "invalid_template_request", "message": str(exc)}, status=400)
            return
        self._send_json({"template": template})

    def _handle_list_jobs(self, query: str, *, role: str | None = None) -> None:
        parameters = parse_qs(query, keep_blank_values=True)
        status_values = parameters.get("status", [])
        if len(status_values) > 1:
            self._send_json(
                {"error": "invalid_job_filter", "message": "status filter must be singular"},
                status=400,
            )
            return
        status = status_values[0] if status_values else None
        try:
            job_queue = self._job_queue()
            jobs = job_queue.list_jobs(status=status or None)
        except ValueError as exc:
            self._send_json({"error": "invalid_job_filter", "message": str(exc)}, status=400)
            return
        self._send_json({"jobs": [_job_response(job, job_queue, role=role) for job in jobs]})

    def _handle_job_event(self) -> None:
        authenticated, auth_context = self._authenticated_context()
        if not authenticated:
            return
        role = auth_context["role"] if auth_context is not None else None
        try:
            request = self._read_json_request()
            job_id = str(request.get("job_id") or "")
            action = str(request.get("action") or "")
            if action == "retry_conversion":
                permission = "jobs:retry"
            elif action == "desktop_upload":
                permission = "jobs:create"
            else:
                permission = "jobs:read"
            if not self._role_has_permission(role, permission):
                return
            audit_event = request.get("audit_event")
            job_queue = self._job_queue()
            job = job_queue.get_job(job_id)
            job_event_store = self._job_event_store()
            updated_job = job
            if action == "desktop_result_download":
                job_event_store.require_integrity()
                accepted_event = _validate_desktop_result_download_audit_event(job, audit_event)
            elif action == "desktop_upload":
                job_event_store.require_integrity()
                accepted_event = _reject_direct_desktop_upload_audit_event(job, audit_event)
            else:
                accepted_event = _validate_job_event(job, action, audit_event, job_queue)
            if action == "retry_conversion":
                job_event_store.require_integrity()
                updated_job = job_queue.retry_failed_job(job_id)
            event = _job_event_with_auth_context(accepted_event, auth_context)
            stored_event = job_event_store.record(event)
        except KeyError:
            self._send_json({"error": "job_not_found"}, status=404)
            return
        except RuntimeError as exc:
            self._send_json({"error": "job_conflict", "message": str(exc)}, status=409)
            return
        except ValueError as exc:
            if str(exc) == "content_length_required":
                self._send_json({"error": "content_length_required"}, status=411)
                return
            if str(exc) == "upload_too_large":
                self._send_json({"error": "upload_too_large"}, status=413)
                return
            self._send_json({"error": "invalid_job_event", "message": str(exc)}, status=400)
            return
        self._send_json(
            {
                "accepted": True,
                "audit_event": stored_event,
                "job": _job_response(updated_job, job_queue, role=role),
            },
            status=202,
        )

    def _handle_review_event(self) -> None:
        authenticated, auth_context = self._authenticated_context()
        if not authenticated:
            return
        role = auth_context["role"] if auth_context is not None else None
        if not self._role_has_permission(role, "review_events:edit"):
            return
        try:
            request = self._read_json_request(max_request_bytes=MAX_REVIEW_EVENT_REQUEST_BYTES)
            raw_audit_event = request.get("audit_event")
            raw_action = raw_audit_event.get("action") if isinstance(raw_audit_event, dict) else None
            permission = (
                "review_events:approve"
                if raw_action == "approve"
                else "review_events:edit"
            )
            if not self._role_has_permission(role, permission):
                return
            accepted_event = _validate_review_event(raw_audit_event)
            stored_event = _review_event_with_auth_context(accepted_event, auth_context)
            event_store = self._review_event_store()
            if stored_event["action"] == "approve":
                stored_event = event_store.record_validated(
                    stored_event,
                    _validate_review_workflow_event,
                )
            else:
                stored_event = event_store.record(stored_event)
        except RuntimeError as exc:
            self._send_json({"error": "review_conflict", "message": str(exc)}, status=409)
            return
        except ValueError as exc:
            if str(exc) == "content_length_required":
                self._send_json({"error": "content_length_required"}, status=411)
                return
            if str(exc) == "upload_too_large":
                self._send_json({"error": "upload_too_large"}, status=413)
                return
            self._send_json({"error": "invalid_review_event", "message": str(exc)}, status=400)
            return
        self._send_json(
            {
                "accepted": True,
                "audit_event": stored_event,
            },
            status=202,
        )

    def _handle_list_review_events(self, query: str) -> None:
        try:
            filters = _review_event_filters(query)
        except ValueError as exc:
            self._send_json(
                {"error": "invalid_review_event_filter", "message": str(exc)},
                status=400,
            )
            return
        review_events = self._review_event_store().list_events(filters=filters)
        self._send_json({"review_events": review_events})

    def _handle_list_job_events(self, query: str) -> None:
        try:
            filters = _job_event_filters(query)
        except ValueError as exc:
            self._send_json(
                {"error": "invalid_job_event_filter", "message": str(exc)},
                status=400,
            )
            return
        job_events = self._job_event_store().list_events(filters=filters)
        self._send_json({"job_events": job_events})

    def _handle_job_result_download(
        self,
        job_id: str,
        *,
        auth_context: dict[str, str | None] | None = None,
    ) -> None:
        try:
            job = self._job_queue().get_job(job_id)
            job_event_store = self._job_event_store()
            job_event_store.require_integrity()
            download = _job_download(job)
            content_type = _download_content_type(download["content_type"])
            filename = _download_filename(download["filename"])
        except KeyError:
            self._send_json({"error": "job_not_found"}, status=404)
            return
        except RuntimeError as exc:
            self._send_json(
                {"error": "job_result_integrity_mismatch", "message": str(exc)},
                status=409,
            )
            return
        except ValueError as exc:
            self._send_json({"error": "job_result_unavailable", "message": str(exc)}, status=400)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{filename}"',
        )
        self.send_header("Content-Length", str(len(download["content"])))
        self.end_headers()
        self.wfile.write(download["content"])

    def _read_json_request(self, *, max_request_bytes: int = MAX_UPLOAD_REQUEST_BYTES) -> dict[str, Any]:
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            raise ValueError("content_length_required")
        byte_count = int(length)
        if byte_count > max_request_bytes:
            raise ValueError("upload_too_large")
        request = json.loads(self.rfile.read(byte_count).decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("request JSON root must be an object")
        return request

    def _job_queue(self) -> JobQueue:
        return getattr(self.server, "job_queue", DEFAULT_JOB_QUEUE)

    def _review_event_store(self) -> ReviewAuditEventStore:
        return getattr(self.server, "review_event_store", DEFAULT_REVIEW_AUDIT_EVENTS)

    def _job_event_store(self) -> JobAuditEventStore:
        return getattr(self.server, "job_event_store", DEFAULT_JOB_AUDIT_EVENTS)

    def _template_store(self) -> TemplateStore:
        return getattr(self.server, "template_store", DEFAULT_TEMPLATE_STORE)

    def _job_template_snapshot(self, raw_template_id: Any) -> dict[str, Any] | None:
        if raw_template_id is None:
            return None
        template_id = _validate_template_id(raw_template_id)
        try:
            return self._template_store().latest_job_snapshot(template_id)
        except KeyError as exc:
            raise ValueError("template_id is unknown") from exc

    def _job_template_binding(self, raw_template_id: Any) -> dict[str, str] | None:
        if raw_template_id is None:
            return None
        return {"template_id": _validate_template_id(raw_template_id)}

    def _require_permission(self, permission: str) -> bool:
        authorized, _role = self._authorized_role_for_permission(permission)
        return authorized

    def _authorized_role_for_permission(self, permission: str) -> tuple[bool, str | None]:
        authorized, auth_context = self._authorized_context_for_permission(permission)
        role = auth_context["role"] if auth_context is not None else None
        if not authorized:
            return False, role
        return True, role

    def _authorized_context_for_permission(
        self,
        permission: str,
    ) -> tuple[bool, dict[str, str | None] | None]:
        authenticated, auth_context = self._authenticated_context()
        if not authenticated:
            return False, None
        role = auth_context["role"] if auth_context is not None else None
        if not self._role_has_permission(role, permission):
            return False, auth_context
        return True, auth_context

    def _authenticated_role(self) -> tuple[bool, str | None]:
        authenticated, auth_context = self._authenticated_context()
        if not authenticated:
            return False, None
        return True, auth_context["role"] if auth_context is not None else None

    def _authenticated_context(self) -> tuple[bool, dict[str, str | None] | None]:
        auth_tokens = _local_auth_tokens(self.server)
        if auth_tokens is None:
            return True, {"role": None, "actor_id": None}
        authorization = self.headers.get("Authorization") or ""
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            self._send_json(
                {
                    "error": "auth_required",
                    "message": "Authorization bearer token is required",
                },
                status=401,
            )
            return False, None
        token = authorization.removeprefix(prefix).strip()
        role = auth_tokens.get(token)
        if role is None:
            self._send_json(
                {"error": "auth_required", "message": "Authorization bearer token is invalid"},
                status=401,
            )
            return False, None
        return True, {"role": role["role"], "actor_id": _local_actor_id(role)}

    def _role_has_permission(self, role: str | None, permission: str) -> bool:
        if role is None:
            if permission == "review_events:approve":
                self._send_json(
                    {
                        "error": "forbidden",
                        "message": "review approval requires authenticated actor identity",
                    },
                    status=403,
                )
                return False
            return True
        if permission not in ROLE_PERMISSIONS[role]:
            self._send_json(
                {
                    "error": "forbidden",
                    "message": f"role {role} cannot perform {_permission_label(permission)}",
                },
                status=403,
            )
            return False
        return True

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._send_json({"error": "web_asset_missing"}, status=500)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = _strict_json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _local_auth_tokens(server: Any) -> dict[str, dict[str, str | None]] | None:
    configured = getattr(server, "local_auth_tokens", None)
    if configured is None:
        configured = _local_auth_tokens_from_env(os.environ.get(LOCAL_AUTH_TOKENS_ENV, ""))
    if configured is None:
        return None
    if not isinstance(configured, dict):
        return {}
    tokens: dict[str, dict[str, str | None]] = {}
    sorted_credentials = sorted(configured.items(), key=lambda item: str(item[0]))
    for index, (token, credential) in enumerate(sorted_credentials, start=1):
        token_text = str(token).strip()
        if isinstance(credential, dict):
            role_text = str(credential.get("role") or "").strip()
            principal_id = _local_principal_id(credential.get("principal_id"))
        else:
            role_text = str(credential).strip()
            principal_id = None
        if token_text and role_text in ROLES and principal_id is not None:
            tokens[token_text] = {
                "role": role_text,
                "principal_id": principal_id,
                "token_id": f"token-{index}",
            }
    return tokens


def _local_auth_tokens_from_env(value: str) -> dict[str, dict[str, str | None]] | None:
    if not value.strip():
        return None
    tokens: dict[str, dict[str, str | None]] = {}
    for entry in value.split(","):
        identity, separator, token = entry.partition("=")
        if separator != "=":
            continue
        role_text, principal_id = _local_role_and_principal(identity)
        token_text = token.strip()
        if role_text in ROLES and principal_id is not None and token_text:
            tokens[token_text] = {"role": role_text, "principal_id": principal_id}
    return tokens


def _local_role_and_principal(identity: str) -> tuple[str, str | None]:
    role, separator, principal = identity.strip().partition(":")
    if separator != ":":
        return role.strip(), None
    return role.strip(), _local_principal_id(principal)


def _local_principal_id(value: Any) -> str | None:
    if value is None:
        return None
    principal_id = str(value).strip()
    return principal_id or None


def _validate_template_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("template_id is required")
    template_id = value.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,62}", template_id):
        raise ValueError("template_id must use lowercase letters, numbers, hyphens, or underscores")
    return template_id


def _validate_template_text_field(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    text = value.strip()
    if len(text) > 4096:
        raise ValueError(f"{field_name} is too long")
    return text


def _validate_template_fields(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("fields must be a non-empty list")
    fields: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"fields[{index}] must be an object")
        name = item.get("field_id", item.get("name"))
        if not isinstance(name, str):
            raise ValueError(f"fields[{index}].field_id is required")
        field_name = name.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", field_name):
            raise ValueError(f"fields[{index}].field_id is invalid")
        if field_name in seen_names:
            raise ValueError(f"fields[{index}].field_id duplicates an earlier field")
        seen_names.add(field_name)
        label = _validate_template_text_field(item.get("label"), f"fields[{index}].label")
        required = item.get("required", False)
        if not isinstance(required, bool):
            raise ValueError(f"fields[{index}].required must be boolean")
        field = deepcopy(item)
        field["field_id"] = field_name
        if "field_id" in item:
            field.pop("name", None)
        else:
            field["name"] = field_name
        field["label"] = label
        field["required"] = required
        fields.append(field)
    return fields


def _validate_template_json_list(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return deepcopy(value)


def _validate_template_json_object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return deepcopy(value)


def _validate_template_actor(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} is required")
    principal_id = _validate_template_text_field(value.get("principal_id"), f"{field_name}.principal_id")
    role = _validate_template_text_field(value.get("role"), f"{field_name}.role")
    return {"principal_id": principal_id, "role": role}


def _template_change_approval(value: Any) -> dict[str, Any]:
    if value is None:
        return {"status": "unapproved", "approved_by": None}
    return {
        "status": "approved",
        "approved_by": _validate_template_actor(value, "approved_by"),
    }


def _validate_template_status(value: Any) -> str:
    if value is None:
        return "active"
    if not isinstance(value, str):
        raise ValueError("status must be active or inactive")
    status = value.strip()
    if status not in {"active", "inactive"}:
        raise ValueError("status must be active or inactive")
    return status


def _template_change_action(existing: dict[str, Any] | None, status: str) -> str:
    if existing is None:
        return "created"
    if status == "inactive" and existing.get("status", "active") != "inactive":
        return "disabled"
    if status == "active" and existing.get("status", "active") == "inactive":
        return "enabled"
    return "versioned"


def _template_request_with_auth_context(
    request: dict[str, Any],
    auth_context: dict[str, str | None] | None,
) -> dict[str, Any]:
    trusted_actor = _template_actor_from_auth_context(auth_context)
    if trusted_actor is None:
        return request
    trusted_request = deepcopy(request)
    trusted_request["actor"] = trusted_actor
    if trusted_request.get("approved_by") is not None:
        _validate_template_actor(trusted_request["approved_by"], "approved_by")
        trusted_request["approved_by"] = trusted_actor
    return trusted_request


def _template_actor_from_auth_context(
    auth_context: dict[str, str | None] | None,
) -> dict[str, str] | None:
    if auth_context is None:
        return None
    actor_id = auth_context.get("actor_id")
    role = auth_context.get("role")
    if actor_id is None and role is None:
        return None
    if not actor_id or not role:
        raise ValueError("authenticated template actor context is incomplete")
    return {"principal_id": actor_id, "role": role}


def _template_summary(record: dict[str, Any]) -> dict[str, Any]:
    latest_version = record["versions"][-1]
    return {
        "template_id": record["template_id"],
        "name": record["name"],
        "category": record["category"],
        "document_type": record["document_type"],
        "status": record.get("status", latest_version.get("status", "active")),
        "current_version": record["current_version"],
        "field_count": len(latest_version["fields"]),
        "version_count": len(record["versions"]),
        "updated_at": record["updated_at"],
    }


def _representative_templates() -> list[dict[str, Any]]:
    return [
        {
            "template_id": "batch-record",
            "name": "Batch Record",
            "category": "manufacturing",
            "fields": [
                {"name": "lot_number", "label": "Lot number", "required": True},
                {"name": "operator", "label": "Operator", "required": True},
            ],
            "content": "Lot {{lot_number}} reviewed by {{operator}}",
            "change_reason": "Seed representative batch record template",
            "actor": {"principal_id": "system-seed", "role": "admin"},
        },
        {
            "template_id": "deviation-report",
            "name": "Deviation Report",
            "category": "quality",
            "fields": [
                {"name": "deviation_id", "label": "Deviation ID", "required": True},
                {"name": "impact", "label": "Impact summary", "required": True},
            ],
            "content": "Deviation {{deviation_id}}: {{impact}}",
            "change_reason": "Seed representative deviation report template",
            "actor": {"principal_id": "system-seed", "role": "admin"},
        },
        {
            "template_id": "coa-summary",
            "name": "CoA Summary",
            "category": "release",
            "fields": [
                {"name": "product_name", "label": "Product", "required": True},
                {"name": "specification", "label": "Specification", "required": True},
            ],
            "content": "{{product_name}} conforms to {{specification}}",
            "change_reason": "Seed representative CoA summary template",
            "actor": {"principal_id": "system-seed", "role": "admin"},
        },
        {
            "template_id": "validation-checklist",
            "name": "Validation Checklist",
            "category": "validation",
            "fields": [
                {"name": "protocol_id", "label": "Protocol ID", "required": True},
                {"name": "reviewer", "label": "Reviewer", "required": True},
            ],
            "content": "Protocol {{protocol_id}} reviewed by {{reviewer}}",
            "change_reason": "Seed representative validation checklist template",
            "actor": {"principal_id": "system-seed", "role": "admin"},
        },
    ]


DEFAULT_TEMPLATE_STORE = TemplateStore.with_representative_defaults()


def _review_event_filters(query: str) -> dict[str, str]:
    parameters = parse_qs(query, keep_blank_values=True)
    allowed_filters = {"document_id", "block_id", "conversion_id", "action"}
    unexpected_filters = sorted(set(parameters) - allowed_filters)
    if unexpected_filters:
        raise ValueError(f"{unexpected_filters[0]} filter is unsupported")
    filters: dict[str, str] = {}
    for name, values in parameters.items():
        if len(values) > 1:
            raise ValueError(f"{name} filter must be singular")
        value = values[0].strip()
        if not value:
            raise ValueError(f"{name} filter must be non-empty")
        filters[name] = value
    return filters


def _review_event_matches_filters(event: dict[str, Any], filters: dict[str, str]) -> bool:
    return all(event.get(name) == value for name, value in filters.items())


def _job_event_filters(query: str) -> dict[str, str]:
    parameters = parse_qs(query, keep_blank_values=True)
    allowed_filters = {"job_id", "action", "job_status"}
    unexpected_filters = sorted(set(parameters) - allowed_filters)
    if unexpected_filters:
        raise ValueError(f"{unexpected_filters[0]} filter is unsupported")
    filters: dict[str, str] = {}
    for name, values in parameters.items():
        if len(values) > 1:
            raise ValueError(f"{name} filter must be singular")
        value = values[0].strip()
        if not value:
            raise ValueError(f"{name} filter must be non-empty")
        filters[name] = value
    return filters


def _job_event_matches_filters(event: dict[str, Any], filters: dict[str, str]) -> bool:
    return all(event.get(name) == value for name, value in filters.items())


def _permission_label(permission: str) -> str:
    labels = {
        "job_events:read": "job_events_read",
        "review_events:approve": "review_approve",
        "review_events:edit": "review_edit",
    }
    if permission in labels:
        return labels[permission]
    return permission.replace(":", "_")


def _job_response(
    job: JobRecord,
    job_queue: JobQueue | None = None,
    *,
    role: str | None = None,
) -> dict[str, Any]:
    hashes = _job_hashes(job)
    hash_verification = _job_hash_verification(job)
    return {
        "job_id": job.job_id,
        "idempotency_key": job.idempotency_key,
        "filename": job.filename,
        "mode": job.mode,
        "status": job.status,
        "display_status": _job_display_status(job),
        "progress_percent": _job_progress_percent(job),
        "warning_count": _job_warning_count(job),
        "attempts": job.attempts,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "hashes": hashes,
        "hash_verification": hash_verification,
        "has_result": _job_has_download(job),
        "available_actions": _job_actions(job, job_queue, role=role),
        "template": deepcopy(job.template),
    }


def _job_actions(
    job: JobRecord,
    job_queue: JobQueue | None = None,
    *,
    role: str | None = None,
) -> list[dict[str, Any]]:
    actions = [
        _job_action(job, "open_detail", "Open details"),
    ]
    if _job_has_download(job):
        actions.append(_job_action(job, "download_result", "Download result"))
    if (
        (role is None or "jobs:retry" in ROLE_PERMISSIONS[role])
        and job.status == "failed"
        and not _retry_blocked_by_active_high_quality(job, job_queue)
    ):
        actions.append(_job_action(job, "retry_conversion", "Retry conversion"))
    return actions


def _job_has_download(job: JobRecord) -> bool:
    try:
        _job_download(job)
    except (RuntimeError, ValueError):
        return False
    return True


def _job_display_status(job: JobRecord) -> str:
    if job.status == "succeeded":
        result_status = _job_result_status(job)
        if result_status == "requires_review":
            return "review_required"
        if result_status == "blocked":
            return "blocked"
        return "completed"
    return job.status


def _job_progress_percent(job: JobRecord) -> int:
    if job.status == "queued":
        return 0
    if job.status == "running":
        return 50
    return 100


def _job_warning_count(job: JobRecord) -> int:
    if not isinstance(job.result, dict):
        return 0
    warnings = job.result.get("warnings", [])
    if not isinstance(warnings, list):
        return 0
    return len(warnings)


def _job_result_status(job: JobRecord) -> str | None:
    if not isinstance(job.result, dict):
        return None
    status = job.result.get("status")
    if not isinstance(status, str):
        return None
    return status


def _retry_blocked_by_active_high_quality(
    job: JobRecord, job_queue: JobQueue | None = None
) -> bool:
    if job.mode != "high_quality":
        return False
    if job_queue is None:
        return True
    return any(
        other.job_id != job.job_id
        and other.mode == "high_quality"
        and other.status in {"queued", "running"}
        for other in job_queue.list_jobs()
    )


def _job_action(job: JobRecord, action: str, label: str) -> dict[str, Any]:
    return {
        "action": action,
        "label": label,
        "enabled": True,
        "audit_event": _job_audit_event(job, action),
    }


def _job_audit_event(job: JobRecord, action: str) -> dict[str, Any]:
    return {
        "event_type": "conversion_job.action_requested",
        "job_id": job.job_id,
        "job_status": job.status,
        "action": action,
    }


def _desktop_upload_audit_event(job: JobRecord) -> dict[str, Any]:
    source = job.source if isinstance(job.source, dict) else {}
    source_filename = source.get("filename")
    filename = _safe_filename(source_filename if isinstance(source_filename, str) else job.filename)
    return {
        "event_type": "desktop.job_operation",
        "job_id": job.job_id,
        "job_status": job.status,
        "action": "desktop_upload",
        "filename": filename,
        "mode": job.mode,
        "source_sha256": _sha256_value(source.get("sha256")),
        "size_bytes": source.get("size_bytes") if isinstance(source.get("size_bytes"), int) else None,
        "content_type": source.get("content_type") if isinstance(source.get("content_type"), str) else None,
    }


def _desktop_result_download_audit_event(job: JobRecord) -> dict[str, Any]:
    download = _job_download(job)
    hashes = _job_hashes(job)
    output_sha256 = _desktop_result_output_sha256(download, hashes)
    return {
        "event_type": "desktop.job_operation",
        "job_id": job.job_id,
        "job_status": job.status,
        "action": "desktop_result_download",
        "filename": _download_filename(job.filename),
        "download_filename": _download_filename(download["filename"]),
        "source_sha256": hashes["source_sha256"],
        "output_sha256": output_sha256,
    }


def _desktop_result_output_sha256(
    download: dict[str, Any],
    hashes: dict[str, str | None],
) -> str:
    stored_hash = hashes["output_sha256"]
    if stored_hash is not None:
        return stored_hash
    return hashlib.sha256(download["content"]).hexdigest()


def _validate_job_event(
    job: JobRecord,
    action: str,
    audit_event: Any,
    job_queue: JobQueue | None = None,
) -> dict[str, Any]:
    actions = {item["action"]: item for item in _job_actions(job, job_queue)}
    selected = actions.get(action)
    if selected is None:
        raise ValueError("action is not available for job status")
    expected_event = selected["audit_event"]
    if not isinstance(audit_event, dict):
        raise ValueError("audit_event is required")
    if audit_event != expected_event:
        raise ValueError("audit_event does not match job action")
    return expected_event


def _validate_desktop_result_download_audit_event(
    job: JobRecord,
    audit_event: Any,
) -> dict[str, Any]:
    expected_event = _desktop_result_download_audit_event(job)
    if not isinstance(audit_event, dict):
        raise ValueError("audit_event is required")
    for field_name in ("event_type", "job_id", "action", "download_filename"):
        if audit_event.get(field_name) != expected_event.get(field_name):
            raise ValueError(f"audit_event.{field_name} does not match downloaded result")
    if audit_event.get("output_sha256") != expected_event["output_sha256"]:
        raise ValueError("audit_event.output_sha256 does not match downloaded result content")
    accepted_event = dict(expected_event)
    if "saved_filename" in audit_event:
        saved_filename = audit_event.get("saved_filename")
        if not isinstance(saved_filename, str) or _download_filename(saved_filename) != saved_filename:
            raise ValueError("audit_event.saved_filename is invalid")
        accepted_event["saved_filename"] = saved_filename
    return accepted_event


def _reject_direct_desktop_upload_audit_event(
    job: JobRecord,
    audit_event: Any,
) -> dict[str, Any]:
    if not isinstance(job.source, dict):
        raise ValueError("desktop_upload requires stored job source")
    if job.status != "queued" or job.attempts > 0:
        raise ValueError("desktop_upload audit must be recorded before job starts")
    if not isinstance(audit_event, dict):
        raise ValueError("audit_event is required")
    raise ValueError("desktop_upload audit must be recorded through the job create request")


def _job_event_with_auth_context(
    audit_event: dict[str, Any],
    auth_context: dict[str, str | None] | None,
) -> dict[str, Any]:
    actor_id = None
    actor_role = None
    if auth_context is not None:
        actor_id = auth_context.get("actor_id")
        actor_role = auth_context.get("role")
    return {
        **audit_event,
        "actor": {
            "id": actor_id,
            "role": actor_role,
        },
        "occurred_at": _utc_now_iso(),
    }


def _validate_review_event(audit_event: Any) -> dict[str, Any]:
    if not isinstance(audit_event, dict):
        raise ValueError("audit_event is required")
    if audit_event.get("event_type") != "conversion_review.action_requested":
        raise ValueError("audit_event.event_type is unsupported")
    action = audit_event.get("action")
    if not isinstance(action, str) or action not in {"edit", "approve"}:
        raise ValueError("audit_event.action is unsupported")
    document_id = audit_event.get("document_id")
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("audit_event.document_id is required")
    document_id = document_id.strip()
    conversion_id = audit_event.get("conversion_id")
    if conversion_id is not None:
        if not isinstance(conversion_id, str) or not conversion_id.strip():
            raise ValueError("audit_event.conversion_id must be a non-empty string")
        conversion_id = conversion_id.strip()
    block_id = audit_event.get("block_id")
    if not isinstance(block_id, str) or not block_id.strip():
        raise ValueError("audit_event.block_id is required")
    block_id = block_id.strip()
    source_page = audit_event.get("source_page")
    if not isinstance(source_page, int) or isinstance(source_page, bool) or source_page < 1:
        raise ValueError("audit_event.source_page must be a positive integer")
    source_bbox = audit_event.get("source_bbox")
    if source_bbox is not None:
        source_bbox = _validate_review_event_bbox(source_bbox)
    original_text = audit_event.get("original_text")
    if not isinstance(original_text, str):
        raise ValueError("audit_event.original_text is required")
    _validate_review_event_text("original_text", original_text)
    revised_text = audit_event.get("revised_text")
    if action == "approve" and revised_text is None:
        revised_text = original_text
    if not isinstance(revised_text, str):
        raise ValueError("audit_event.revised_text is required")
    _validate_review_event_text("revised_text", revised_text)
    warnings = audit_event.get("warnings", [])
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise ValueError("audit_event.warnings must be strings")
    return {
        "event_type": "conversion_review.action_requested",
        "action": action,
        "conversion_id": conversion_id,
        "document_id": document_id,
        "block_id": block_id,
        "source_page": source_page,
        "source_bbox": source_bbox,
        "original_text": original_text,
        "revised_text": revised_text,
        "warnings": warnings,
    }


def _review_event_with_auth_context(
    audit_event: dict[str, Any],
    auth_context: dict[str, str | None] | None,
) -> dict[str, Any]:
    actor_id = None
    actor_role = None
    if auth_context is not None:
        actor_id = auth_context.get("actor_id")
        actor_role = auth_context.get("role")
    return {
        **audit_event,
        "actor": {
            "id": actor_id,
            "role": actor_role,
        },
        "occurred_at": _utc_now_iso(),
    }


def _validate_review_workflow_event(
    audit_event: dict[str, Any],
    stored_events: list[dict[str, Any]],
) -> None:
    if audit_event["action"] != "approve":
        return
    actor = audit_event.get("actor")
    actor_id = actor.get("id") if isinstance(actor, dict) else None
    audit_conversion_id = audit_event.get("conversion_id")
    latest_edit_revised_text = None
    matched_audit_conversion_edit = False
    deferred_conflicting_conversion_events: list[dict[str, Any]] = []
    for stored_event in reversed(stored_events):
        if not _same_review_workflow_target_base(stored_event, audit_event):
            continue
        if _has_conflicting_review_conversion_id(stored_event, audit_event):
            if matched_audit_conversion_edit:
                continue
            deferred_conflicting_conversion_events.append(stored_event)
            continue
        stored_conversion_id = stored_event.get("conversion_id")
        if audit_conversion_id and stored_conversion_id == audit_conversion_id:
            matched_audit_conversion_edit = True
        if latest_edit_revised_text is None:
            latest_edit_revised_text = stored_event.get("revised_text")
        stored_actor = stored_event.get("actor")
        stored_actor_id = stored_actor.get("id") if isinstance(stored_actor, dict) else None
        if isinstance(actor_id, str) and actor_id and stored_actor_id == actor_id:
            raise RuntimeError("review approval must be performed by a different actor")
    if not matched_audit_conversion_edit:
        for stored_event in deferred_conflicting_conversion_events:
            _reject_cross_conversion_review_reuse(stored_event, audit_event, actor_id)
    expected_revised_text = (
        latest_edit_revised_text
        if latest_edit_revised_text is not None
        else audit_event["original_text"]
    )
    if audit_event["revised_text"] != expected_revised_text:
        _reject_stale_review_approval()


def _has_conflicting_review_conversion_id(
    stored_event: dict[str, Any],
    audit_event: dict[str, Any],
) -> bool:
    stored_conversion_id = stored_event.get("conversion_id")
    audit_conversion_id = audit_event.get("conversion_id")
    return bool(
        stored_conversion_id
        and audit_conversion_id
        and stored_conversion_id != audit_conversion_id
    )


def _reject_cross_conversion_review_reuse(
    stored_event: dict[str, Any],
    audit_event: dict[str, Any],
    actor_id: str | None,
) -> None:
    if stored_event.get("revised_text") != audit_event["revised_text"]:
        return
    stored_actor = stored_event.get("actor")
    stored_actor_id = stored_actor.get("id") if isinstance(stored_actor, dict) else None
    if isinstance(actor_id, str) and actor_id and stored_actor_id == actor_id:
        raise RuntimeError("review approval must be performed by a different actor")
    if audit_event.get("original_text") != audit_event["revised_text"]:
        _reject_stale_review_approval()


def _reject_stale_review_approval() -> None:
    raise RuntimeError("review approval must target latest edited text")


def _same_review_workflow_target_base(
    stored_event: dict[str, Any],
    audit_event: dict[str, Any],
) -> bool:
    if stored_event.get("action") != "edit":
        return False
    if stored_event.get("document_id") != audit_event["document_id"]:
        return False
    if stored_event.get("block_id") != audit_event["block_id"]:
        return False
    return True


def _local_actor_id(credential: dict[str, str | None]) -> str | None:
    principal_id = credential.get("principal_id")
    if principal_id:
        return f"local-principal:{principal_id}"
    return None


def _conversion_id() -> str:
    return f"conversion-{uuid4().hex}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_review_event_text(field_name: str, value: str) -> None:
    if len(value.encode("utf-8")) > MAX_REVIEW_EVENT_TEXT_BYTES:
        raise ValueError(f"audit_event.{field_name} exceeds review text limit")


def _validate_review_event_bbox(source_bbox: Any) -> dict[str, Any]:
    if not isinstance(source_bbox, dict):
        raise ValueError("audit_event.source_bbox must be an object")
    for key in ("x", "y", "width", "height"):
        value = source_bbox.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"audit_event.source_bbox.{key} must be finite")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"audit_event.source_bbox.{key} must be finite")
    unit = str(source_bbox.get("unit") or "").strip()
    origin = str(source_bbox.get("origin") or "").strip()
    if not unit:
        raise ValueError("audit_event.source_bbox.unit is required")
    if unit not in UNITS:
        supported = ", ".join(sorted(UNITS))
        raise ValueError(f"audit_event.source_bbox.unit must be one of {supported}")
    if origin != "top-left":
        raise ValueError("audit_event.source_bbox.origin must be top-left")
    if source_bbox["x"] < 0 or source_bbox["y"] < 0:
        raise ValueError("audit_event.source_bbox origin coordinates must be non-negative")
    if source_bbox["width"] <= 0 or source_bbox["height"] <= 0:
        raise ValueError("audit_event.source_bbox size must be positive")
    return {
        "x": source_bbox["x"],
        "y": source_bbox["y"],
        "width": source_bbox["width"],
        "height": source_bbox["height"],
        "unit": unit,
        "origin": origin,
    }


def _job_download(job: JobRecord) -> dict[str, Any]:
    if job.status != "succeeded":
        raise ValueError("job has no downloadable result")
    result = job.result
    if not isinstance(result, dict):
        raise ValueError("job result is missing")
    download = result.get("download")
    if not isinstance(download, dict):
        raise ValueError("job result download is missing")
    filename = str(download.get("filename") or "").strip()
    content_type = str(download.get("content_type") or "").strip()
    content = download.get("content")
    if not filename or not isinstance(content, bytes):
        raise ValueError("job result download is invalid")
    verification = _job_hash_verification(job)
    if verification["source"]["status"] == "mismatch":
        raise RuntimeError("job result source hash does not match uploaded source")
    if verification["output"]["status"] == "mismatch":
        raise RuntimeError("job result output hash does not match stored content")
    return {
        "filename": filename,
        "content_type": _download_content_type(content_type),
        "content": content,
    }


def _job_hashes(job: JobRecord) -> dict[str, str | None]:
    result = job.result if isinstance(job.result, dict) else {}
    raw_hashes = result.get("hashes") if isinstance(result, dict) else None
    hashes = raw_hashes if isinstance(raw_hashes, dict) else {}
    source = job.source if isinstance(job.source, dict) else {}
    return {
        "source_sha256": _sha256_value(source.get("sha256"))
        or _sha256_value(hashes.get("source_sha256")),
        "output_sha256": _sha256_value(hashes.get("output_sha256")),
    }


def _job_hash_verification(job: JobRecord) -> dict[str, Any]:
    hashes = _job_hashes(job)
    return {
        "source": _source_hash_verification(job),
        "output": _output_hash_verification(job, hashes["output_sha256"]),
    }


def _source_hash_verification(job: JobRecord) -> dict[str, Any]:
    result = job.result if isinstance(job.result, dict) else {}
    raw_hashes = result.get("hashes") if isinstance(result, dict) else None
    hashes = raw_hashes if isinstance(raw_hashes, dict) else {}
    has_result_source_hash = "source_sha256" in hashes
    result_source_sha256 = (
        _sha256_value(hashes.get("source_sha256")) if has_result_source_hash else None
    )
    source = job.source if isinstance(job.source, dict) else {}
    uploaded_sha256 = _sha256_value(source.get("sha256"))
    if uploaded_sha256 is None:
        if result_source_sha256 is None:
            return {"status": "missing"}
        return {"status": "recorded", "sha256": result_source_sha256}
    if not has_result_source_hash or result_source_sha256 == uploaded_sha256:
        return {"status": "recorded", "sha256": uploaded_sha256}
    return {
        "status": "mismatch",
        "expected_sha256": uploaded_sha256,
        "actual_sha256": result_source_sha256,
    }


def _request_string_field(request: dict[str, Any], field_name: str) -> str:
    value = request[field_name]
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    return value


def _output_hash_verification(job: JobRecord, expected_sha256: str | None) -> dict[str, Any]:
    if expected_sha256 is None:
        return {"status": "missing"}
    result = job.result if isinstance(job.result, dict) else {}
    download = result.get("download") if isinstance(result, dict) else None
    content = download.get("content") if isinstance(download, dict) else None
    if not isinstance(content, bytes):
        return {"status": "missing_content", "expected_sha256": expected_sha256}
    actual_sha256 = _sha256_hex(content)
    status = "match" if actual_sha256 == expected_sha256 else "mismatch"
    return {
        "status": status,
        "expected_sha256": expected_sha256,
        "actual_sha256": actual_sha256,
    }


def _sha256_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", candidate):
        return candidate
    return None


def _sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _download_content_type(content_type: str) -> str:
    if (
        not content_type
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in content_type)
        or not HTTP_CONTENT_TYPE.fullmatch(content_type)
    ):
        raise ValueError("job result download content type is invalid")
    return content_type


def _download_filename(filename: str) -> str:
    basename = re.split(r"[\\/]+", filename)[-1].strip()
    safe = re.sub(r'[\x00-\x1f\x7f"\\]', "", basename)
    safe = "".join(char for char in safe if 0x20 <= ord(char) <= 0x7E).strip()
    return safe or "veridoc-result.json"


def _parser_output_from_upload(filename: str, content: bytes) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    source_type = _source_type_from_path(filename)
    if source_type in KNOWN_SOURCE_TYPES:
        return _parser_output_from_binary_upload(filename, content, source_type), warnings

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "unsupported binary upload; use .pdf, .docx, .xlsx, or UTF-8 JSON/text"
        ) from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        warnings.append("upload was treated as plain text; parser confidence requires review")
        return _plain_text_parser_output(text), warnings
    if not isinstance(parsed, dict):
        warnings.append("JSON upload root is not an object; content requires review")
        return _plain_text_parser_output(text), warnings
    return parsed, warnings


def _parser_output_from_binary_upload(
    filename: str, content: bytes, source_type: str
) -> dict[str, Any]:
    with TemporaryDirectory(prefix="veridoc-poc-upload-") as temp_dir:
        upload_path = Path(temp_dir) / filename
        try:
            upload_path.write_bytes(content)
            if source_type == "docx":
                return extract_docx_structure(upload_path).to_dict()
            if source_type == "xlsx":
                return extract_xlsx_structure(upload_path).to_dict()
            if source_type == "pdf":
                return adapt_document_ir_v0_blocks(
                    parse_text_pdf_to_document_ir(
                        upload_path,
                        document_id=_document_id(filename),
                    )
                )
        except MissingPdfExtractorDependency as exc:
            raise PocServerDependencyError(
                "PDF parser dependency is unavailable; install requirements-pdf-eval.txt"
            ) from exc
        except (BadZipFile, KeyError, OSError, TypeError, ValueError, XmlParseError) as exc:
            raise ValueError(
                f"{source_type.upper()} parser failed; upload requires a valid {source_type.upper()} file"
            ) from exc
    raise ValueError("unsupported binary upload")


def _plain_text_parser_output(text: str) -> dict[str, Any]:
    normalized = text.strip() or "Uploaded document contained no readable text."
    return {
        "pages": [
            {
                "page_number": 1,
                "width": 612,
                "height": 792,
                "unit": "pt",
                "fragments": [
                    {
                        "text": normalized,
                        "bbox": {"x": 72, "y": 72, "width": 468, "height": 24, "unit": "pt"},
                        "confidence": 0.5,
                        "low_confidence": True,
                    }
                ],
            }
        ]
    }


def _review_items(document_ir: DocumentIRV1) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    pages_by_number = {page.page_number: page for page in document_ir.pages}
    for block_index, block in enumerate(document_ir.blocks):
        if not block.review.requires_review and not block.review.warnings:
            continue
        item = {
            "document_id": document_ir.document.id,
            "block_id": block.id,
            "source_id": f"{document_ir.document.id}:{block.id}",
            "source_page": block.source_page,
            "text": block.text,
            "warnings": list(block.review.warnings),
        }
        if _block_llm_involved(block):
            item["llm_involved"] = True
        page = pages_by_number.get(block.source_page)
        source_bbox = _review_source_bbox(block.bbox, page)
        if source_bbox is not None:
            item["source_bbox"] = source_bbox
            item["source_page_geometry"] = asdict(page)
        else:
            item["warnings"] = [
                *item["warnings"],
                f"blocks[{block_index}].source metadata incomplete; original jump unavailable",
            ]
        items.append(item)
    return items


def _block_llm_involved(block: Any) -> bool:
    extractor_name = _normalize_extractor_name(
        getattr(getattr(block, "extractor", None), "name", "") or ""
    )
    if any(token in extractor_name for token in LLM_EXTRACTOR_NAME_TOKENS):
        return True
    return extractor_name in _configured_llm_extractor_names()


def _normalize_extractor_name(value: Any) -> str:
    return str(value or "").strip().casefold()


@lru_cache(maxsize=1)
def _configured_llm_extractor_names() -> frozenset[str]:
    try:
        profiles_config = json.loads(INFERENCE_PROFILES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return frozenset()
    profiles = profiles_config.get("profiles")
    if not isinstance(profiles, list):
        return frozenset()
    names: set[str] = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        for field_name in LLM_INFERENCE_PROFILE_FIELDS:
            value = profile.get(field_name)
            if isinstance(value, str):
                normalized = _normalize_extractor_name(value)
                if normalized:
                    names.add(normalized)
    return frozenset(names)


def _review_source_bbox(bbox: Any, page: Any) -> dict[str, Any] | None:
    if page is None:
        return None
    values = (bbox.x, bbox.y, bbox.width, bbox.height, page.width, page.height)
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values):
        return None
    if page.width <= 0 or page.height <= 0:
        return None
    if bbox.origin != "top-left" or bbox.unit != page.unit:
        return None
    if bbox.unit not in UNITS or page.unit not in UNITS:
        return None
    if bbox.x < 0 or bbox.y < 0 or bbox.width <= 0 or bbox.height <= 0:
        return None
    if bbox.x + bbox.width > page.width or bbox.y + bbox.height > page.height:
        return None
    return asdict(bbox)


def _review_actions(role: str | None) -> list[str]:
    permissions = (
        {"review_events:edit"}
        if role is None
        else ROLE_PERMISSIONS[role]
    )
    actions: list[str] = []
    if "review_events:edit" in permissions:
        actions.append("edit")
    if "review_events:approve" in permissions:
        actions.append("approve")
    return actions


def _http_result(result: dict[str, Any], *, role: str | None = None) -> dict[str, Any]:
    download = dict(result["download"])
    content = download.pop("content")
    return {
        **{key: value for key, value in result.items() if key != "download"},
        "available_review_actions": _review_actions(role),
        "download": {
            **download,
            "content_text": content.decode("utf-8"),
        },
    }


def _decode_request_content(request: dict[str, Any]) -> bytes:
    if "content_base64" in request:
        encoded = _request_string_field(request, "content_base64")
        try:
            return base64.b64decode(encoded, validate=True)
        except binascii.Error as exc:
            raise ValueError("content_base64 must be valid base64") from exc
    if "content" in request:
        return _request_string_field(request, "content").encode("utf-8")
    raise ValueError("content or content_base64 is required")


def _job_source_from_request(request: dict[str, Any], *, filename: str) -> dict[str, Any] | None:
    if "content_base64" not in request and "content" not in request:
        return None
    content = _decode_request_content(request)
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("upload_too_large")
    declared_size = request.get("size_bytes")
    if declared_size is not None:
        if isinstance(declared_size, bool) or not isinstance(declared_size, int):
            raise ValueError("size_bytes must be an integer")
        if declared_size != len(content):
            raise ValueError("size_bytes does not match uploaded content")
    actual_sha256 = _sha256_hex(content)
    declared_sha256 = _sha256_value(request.get("source_sha256"))
    if request.get("source_sha256") is not None and declared_sha256 is None:
        raise ValueError("source_sha256 must be a lowercase sha256 hex digest")
    if declared_sha256 is not None and declared_sha256 != actual_sha256:
        raise ValueError("source_sha256 does not match uploaded content")
    content_type = str(request.get("content_type") or "").strip()
    if content_type and (
        any(ord(char) < 0x20 or ord(char) == 0x7F for char in content_type)
        or not HTTP_CONTENT_TYPE.fullmatch(content_type)
    ):
        raise ValueError("content_type is invalid")
    return {
        "filename": _safe_filename(filename),
        "content_type": content_type or None,
        "size_bytes": len(content),
        "sha256": actual_sha256,
        "content": content,
    }


def _desktop_upload_audit_requested(request: dict[str, Any]) -> bool:
    requested = request.get("desktop_upload_audit", False)
    if isinstance(requested, bool):
        return requested
    raise ValueError("desktop_upload_audit must be boolean")


def _source_type(filename: str, parser_output: dict[str, Any] | None = None) -> str:
    filename_source_type = _source_type_from_path(filename)
    if filename_source_type != "unknown":
        return filename_source_type

    data = parser_output if isinstance(parser_output, dict) else {}
    explicit_source_type = str(data.get("source_type") or "")
    if explicit_source_type:
        return explicit_source_type

    document = data.get("document")
    if isinstance(document, dict):
        document_source_type = str(document.get("source_type") or "")
        if document_source_type:
            return document_source_type

    source_path_type = _source_type_from_path(str(data.get("source_path") or ""))
    if source_path_type != "unknown":
        return source_path_type

    if isinstance(data.get("blocks"), list):
        return "docx"
    if isinstance(data.get("sheets"), list):
        return "xlsx"
    if isinstance(data.get("candidates"), list):
        return "pdf"
    return "unknown"


def _source_type_from_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    if suffix == ".xlsx":
        return "xlsx"
    return "unknown"


def _safe_filename(filename: str) -> str:
    basename = re.split(r"[\\/]+", filename)[-1].strip()
    candidate = re.sub(r'[\x00-\x1f\x7f"\\]', "", basename).strip()
    return candidate or "upload.txt"


def _document_id(filename: str) -> str:
    stem = Path(filename).stem.lower()
    document_id = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
    return document_id or "upload"


def _document_id_from_parser_output(filename: str, parser_output: dict[str, Any]) -> str:
    document = parser_output.get("document")
    if isinstance(document, dict):
        document_id = str(document.get("id") or "").strip()
        if document_id:
            return document_id
    return _document_id(filename)


def _document_title_from_parser_output(filename: str, parser_output: dict[str, Any]) -> str:
    document = parser_output.get("document")
    if isinstance(document, dict):
        title = str(document.get("title") or "").strip()
        if title:
            return title
    return filename


def _status(ok: bool, requires_review: bool) -> str:
    if not ok:
        return "blocked"
    if requires_review:
        return "requires_review"
    return "converted"


def _strict_json_bytes(payload: Any, *, indent: int | None = None) -> bytes:
    return json.dumps(
        _json_safe(payload),
        allow_nan=False,
        ensure_ascii=False,
        indent=indent,
        sort_keys=True,
    ).encode("utf-8")


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
