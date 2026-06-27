from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import binascii
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
        "jobs:read",
        "review_events:read",
    },
    "reviewer": {
        "convert",
        "jobs:create",
        "jobs:read",
        "review_events:edit",
        "review_events:read",
    },
    "approver": {
        "convert",
        "jobs:create",
        "jobs:read",
        "review_events:approve",
        "review_events:edit",
        "review_events:read",
    },
    "admin": {
        "convert",
        "jobs:create",
        "jobs:read",
        "jobs:retry",
        "review_events:approve",
        "review_events:edit",
        "review_events:read",
    },
}
HTTP_CONTENT_TYPE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+"
    r"(?:[ \t]*;[ \t]*[A-Za-z0-9!#$&^_.+-]+=[A-Za-z0-9!#$&^_.+-]+)*$"
)
DEFAULT_JOB_QUEUE = JobQueue()


class PocServerDependencyError(RuntimeError):
    """Raised when the PoC server is missing an optional parser dependency."""


class ReviewAuditEventStore:
    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._lock = Lock()

    def record(self, audit_event: dict[str, Any]) -> dict[str, Any]:
        event = deepcopy(audit_event)
        with self._lock:
            self._events.append(event)
        return deepcopy(event)

    def record_validated(
        self,
        audit_event: dict[str, Any],
        validate: Callable[[dict[str, Any], list[dict[str, Any]]], None],
    ) -> dict[str, Any]:
        event = deepcopy(audit_event)
        with self._lock:
            validate(event, [_review_workflow_event_view(item) for item in self._events])
            self._events.append(event)
        return deepcopy(event)

    def list_events(self) -> list[dict[str, Any]]:
        with self._lock:
            return deepcopy(self._events)


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
LLM_EXTRACTOR_NAME_TOKENS = ("llm", "gpt", "openai")
LLM_INFERENCE_PROFILE_FIELDS = ("id", "label", "provider", "model_family", "recommended_model")


def convert_uploaded_document(*, filename: str, content: bytes) -> dict[str, Any]:
    """Convert one uploaded PoC document into IR, review details, and download bytes."""
    safe_filename = _safe_filename(filename)
    conversion_id = _conversion_id()
    parser_output, input_warnings = _parser_output_from_upload(safe_filename, content)
    document_ir = from_parser_output(
        parser_output,
        document_id=_document_id_from_parser_output(safe_filename, parser_output),
        title=_document_title_from_parser_output(safe_filename, parser_output),
        source_type=_source_type(safe_filename, parser_output),
    )
    validation = validate_document_ir_v1(document_ir)
    review_items = _review_items(document_ir)
    payload = {
        "document_ir": document_ir.to_dict(),
        "conversion_id": conversion_id,
        "validation": asdict(validation),
        "review_items": review_items,
        "warnings": [*input_warnings, *validation.warnings],
    }
    download_content = _strict_json_bytes(payload, indent=2)
    return {
        "status": _status(validation.ok, validation.requires_review),
        **payload,
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
            authorized, role = self._authorized_role_for_permission("jobs:read")
            if not authorized:
                return
            self._handle_list_jobs(parsed_url.query, role=role)
            return
        if path == "/api/review-events":
            if not self._require_permission("review_events:read"):
                return
            self._handle_list_review_events()
            return
        if path.startswith("/api/jobs/"):
            authorized, role = self._authorized_role_for_permission("jobs:read")
            if not authorized:
                return
            job_path = path.removeprefix("/api/jobs/")
            if job_path.endswith("/result"):
                self._handle_job_result_download(job_path.removesuffix("/result"))
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
            authorized, role = self._authorized_role_for_permission("jobs:create")
            if not authorized:
                return
            self._handle_create_job(role=role)
            return
        if path == "/api/job-events":
            self._handle_job_event()
            return
        if path == "/api/review-events":
            self._handle_review_event()
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

    def _handle_create_job(self, *, role: str | None = None) -> None:
        try:
            request = self._read_json_request()
            filename = str(request.get("filename") or "").strip()
            mode = str(request.get("mode") or "standard")
            idempotency_key = str(
                request.get("idempotency_key") or self.headers.get("Idempotency-Key") or ""
            )
            job = self._job_queue().create_job(
                idempotency_key=idempotency_key,
                filename=filename,
                mode=mode,
            )
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
        self._send_json({"job": _job_response(job, self._job_queue(), role=role)}, status=202)

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
        authenticated, role = self._authenticated_role()
        if not authenticated:
            return
        try:
            request = self._read_json_request()
            job_id = str(request.get("job_id") or "")
            action = str(request.get("action") or "")
            permission = "jobs:retry" if action == "retry_conversion" else "jobs:read"
            if not self._role_has_permission(role, permission):
                return
            audit_event = request.get("audit_event")
            job_queue = self._job_queue()
            job = job_queue.get_job(job_id)
            accepted_event = _validate_job_event(job, action, audit_event, job_queue)
            updated_job = job
            if action == "retry_conversion":
                updated_job = job_queue.retry_failed_job(job_id)
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
                "audit_event": accepted_event,
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

    def _handle_list_review_events(self) -> None:
        self._send_json({"review_events": self._review_event_store().list_events()})

    def _handle_job_result_download(self, job_id: str) -> None:
        try:
            job = self._job_queue().get_job(job_id)
            download = _job_download(job)
            content_type = _download_content_type(download["content_type"])
            filename = _download_filename(download["filename"])
        except KeyError:
            self._send_json({"error": "job_not_found"}, status=404)
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

    def _require_permission(self, permission: str) -> bool:
        authorized, _role = self._authorized_role_for_permission(permission)
        return authorized

    def _authorized_role_for_permission(self, permission: str) -> tuple[bool, str | None]:
        authenticated, role = self._authenticated_role()
        if not authenticated:
            return False, None
        if not self._role_has_permission(role, permission):
            return False, role
        return True, role

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


def _permission_label(permission: str) -> str:
    labels = {
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
    return {
        "job_id": job.job_id,
        "idempotency_key": job.idempotency_key,
        "filename": job.filename,
        "mode": job.mode,
        "status": job.status,
        "attempts": job.attempts,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "has_result": _job_has_download(job),
        "available_actions": _job_actions(job, job_queue, role=role),
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
    except ValueError:
        return False
    return True


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
    latest_edit_revised_text = None
    for stored_event in reversed(stored_events):
        if not _same_review_workflow_target(stored_event, audit_event):
            continue
        if latest_edit_revised_text is None:
            latest_edit_revised_text = stored_event.get("revised_text")
        stored_actor = stored_event.get("actor")
        stored_actor_id = stored_actor.get("id") if isinstance(stored_actor, dict) else None
        if isinstance(actor_id, str) and actor_id and stored_actor_id == actor_id:
            raise RuntimeError("review approval must be performed by a different actor")
    if (
        latest_edit_revised_text is not None
        and audit_event["revised_text"] != latest_edit_revised_text
    ):
        raise RuntimeError("review approval must target latest edited text")


def _same_review_workflow_target(
    stored_event: dict[str, Any],
    audit_event: dict[str, Any],
) -> bool:
    if stored_event.get("action") != "edit":
        return False
    if stored_event.get("document_id") != audit_event["document_id"]:
        return False
    if stored_event.get("block_id") != audit_event["block_id"]:
        return False
    stored_conversion_id = stored_event.get("conversion_id")
    audit_conversion_id = audit_event.get("conversion_id")
    if stored_conversion_id and audit_conversion_id:
        return stored_conversion_id == audit_conversion_id
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
    return {
        "filename": filename,
        "content_type": _download_content_type(content_type),
        "content": content,
    }


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
    for block in document_ir.blocks:
        if not block.review.requires_review and not block.review.warnings:
            continue
        item = {
            "document_id": document_ir.document.id,
            "block_id": block.id,
            "source_page": block.source_page,
            "text": block.text,
            "warnings": block.review.warnings,
        }
        if _block_llm_involved(block):
            item["llm_involved"] = True
        page = pages_by_number.get(block.source_page)
        source_bbox = _review_source_bbox(block.bbox, page)
        if source_bbox is not None:
            item["source_bbox"] = source_bbox
            item["source_page_geometry"] = asdict(page)
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
        {"review_events:approve", "review_events:edit"}
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
        try:
            return base64.b64decode(str(request["content_base64"]), validate=True)
        except binascii.Error as exc:
            raise ValueError("content_base64 must be valid base64") from exc
    if "content" in request:
        return str(request["content"]).encode("utf-8")
    raise ValueError("content or content_base64 is required")


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
    candidate = Path(filename).name.strip()
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
