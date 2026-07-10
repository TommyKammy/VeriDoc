from __future__ import annotations

from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class _AuditActionContract:
    name: str
    aliases: frozenset[str]
    scope_types: frozenset[str]
    event_types: frozenset[str] = frozenset()
    job_statuses: frozenset[str] | None = None
    result_statuses: frozenset[str] | None = None
    lifecycle_state: str | None = None
    evidence_type: str | None = None
    requires_uploader: bool = False
    requires_unattempted_job: bool = False


_JOB_AUDIT_SCOPES = frozenset({"conversion_job", "job", "job_event"})
_RESULT_LIFECYCLE_SCOPES = _JOB_AUDIT_SCOPES | frozenset({"conversion_result"})
_DESKTOP_AUDIT_SCOPES = _JOB_AUDIT_SCOPES | frozenset({"document"})
_UPLOAD_AUDIT_SCOPES = _JOB_AUDIT_SCOPES | frozenset(
    {"document", "source_artifact", "source_document"}
)
_REVIEW_REQUEST_SCOPES = frozenset(
    {"document", "review_decision", "review_item"}
)
_QUEUED_JOB_STATUSES = frozenset({"queued"})
_RUNNING_JOB_STATUSES = frozenset({"processing", "running", "started"})
_FAILED_JOB_STATUSES = frozenset({"failed"})
_SUCCEEDED_JOB_STATUSES = frozenset({"completed", "succeeded", "success"})
_FAILED_RESULT_STATUSES = frozenset({"failed"})
_SUCCEEDED_RESULT_STATUSES = frozenset(
    {"blocked", "completed", "converted", "requires_review", "succeeded", "success"}
)

_AUDIT_ACTION_CONTRACTS = (
    _AuditActionContract(
        name="source_upload",
        aliases=frozenset({"document.uploaded", "upload", "uploaded"}),
        scope_types=_UPLOAD_AUDIT_SCOPES,
        requires_uploader=True,
    ),
    _AuditActionContract(
        name="desktop_upload",
        aliases=frozenset({"desktop_upload"}),
        scope_types=_DESKTOP_AUDIT_SCOPES,
        event_types=frozenset({"desktop.job_operation"}),
        job_statuses=_QUEUED_JOB_STATUSES,
        requires_uploader=True,
        requires_unattempted_job=True,
    ),
    _AuditActionContract(
        name="document_inspected",
        aliases=frozenset({"document.inspected"}),
        scope_types=frozenset({"document", "source_document"}),
    ),
    _AuditActionContract(
        name="artifact_generated",
        aliases=frozenset({"artifact.generated"}),
        scope_types=frozenset({"artifact", "generated_artifact"}),
    ),
    _AuditActionContract(
        name="review_opened",
        aliases=frozenset({"review.opened"}),
        scope_types=frozenset({"review_item"}),
    ),
    _AuditActionContract(
        name="job_queued",
        aliases=frozenset(
            {"conversion.queued", "conversion_queued", "job.queued", "job_queued"}
        ),
        scope_types=_JOB_AUDIT_SCOPES,
        event_types=frozenset({"job.lifecycle"}),
        job_statuses=_QUEUED_JOB_STATUSES,
        lifecycle_state="queued",
    ),
    _AuditActionContract(
        name="job_running",
        aliases=frozenset(
            {
                "conversion.running",
                "conversion.started",
                "conversion_running",
                "conversion_started",
                "job.running",
                "job.started",
                "job_running",
                "job_started",
            }
        ),
        scope_types=_JOB_AUDIT_SCOPES,
        event_types=frozenset({"job.lifecycle"}),
        job_statuses=_RUNNING_JOB_STATUSES,
        lifecycle_state="running",
    ),
    _AuditActionContract(
        name="job_failed",
        aliases=frozenset(
            {"conversion.failed", "conversion_failed", "job.failed", "job_failed"}
        ),
        scope_types=_RESULT_LIFECYCLE_SCOPES,
        event_types=frozenset({"job.lifecycle"}),
        job_statuses=_FAILED_JOB_STATUSES,
        result_statuses=_FAILED_RESULT_STATUSES,
        lifecycle_state="failed",
    ),
    _AuditActionContract(
        name="job_succeeded",
        aliases=frozenset(
            {
                "conversion.completed",
                "conversion.succeeded",
                "conversion_completed",
                "conversion_succeeded",
                "job.completed",
                "job.succeeded",
                "job_completed",
                "job_succeeded",
            }
        ),
        scope_types=_RESULT_LIFECYCLE_SCOPES,
        event_types=frozenset({"job.lifecycle"}),
        job_statuses=_SUCCEEDED_JOB_STATUSES,
        result_statuses=_SUCCEEDED_RESULT_STATUSES,
        lifecycle_state="succeeded",
    ),
    _AuditActionContract(
        name="job_retry",
        aliases=frozenset(
            {
                "conversion.retry_requested",
                "conversion_retry_requested",
                "job.retry_requested",
                "job_retry_requested",
                "retry_conversion",
            }
        ),
        scope_types=_JOB_AUDIT_SCOPES,
        event_types=frozenset(
            {"conversion_job.action_requested", "job.lifecycle"}
        ),
        job_statuses=_FAILED_JOB_STATUSES,
        lifecycle_state="retry",
    ),
    _AuditActionContract(
        name="download_result",
        aliases=frozenset({"download_result"}),
        scope_types=_JOB_AUDIT_SCOPES,
        event_types=frozenset({"conversion_job.action_requested"}),
        job_statuses=_SUCCEEDED_JOB_STATUSES,
        evidence_type="download_artifact",
    ),
    _AuditActionContract(
        name="desktop_result_download",
        aliases=frozenset({"desktop_result_download"}),
        scope_types=_DESKTOP_AUDIT_SCOPES,
        event_types=frozenset({"desktop.job_operation"}),
        job_statuses=_SUCCEEDED_JOB_STATUSES,
        evidence_type="download_artifact",
    ),
    _AuditActionContract(
        name="open_detail",
        aliases=frozenset({"open_detail"}),
        scope_types=_JOB_AUDIT_SCOPES,
        event_types=frozenset({"conversion_job.action_requested"}),
    ),
    _AuditActionContract(
        name="review_approve_request",
        aliases=frozenset({"approve"}),
        scope_types=_REVIEW_REQUEST_SCOPES,
        event_types=frozenset({"conversion_review.action_requested"}),
    ),
    _AuditActionContract(
        name="review_edit_request",
        aliases=frozenset({"edit"}),
        scope_types=_REVIEW_REQUEST_SCOPES,
        event_types=frozenset({"conversion_review.action_requested"}),
    ),
    _AuditActionContract(
        name="review_outcome",
        aliases=frozenset(
            {
                "approved",
                "edited",
                "rejected",
                "review.approve",
                "review.approved",
                "review.edit",
                "review.edited",
                "review.reject",
                "review.rejected",
            }
        ),
        scope_types=frozenset({"review_decision"}),
    ),
)

_AUDIT_ACTION_CONTRACT_BY_ALIAS = {
    alias: contract
    for contract in _AUDIT_ACTION_CONTRACTS
    for alias in contract.aliases
}
_CONTRACT_EVENT_TYPES = frozenset(
    event_type
    for contract in _AUDIT_ACTION_CONTRACTS
    for event_type in contract.event_types
)


def _audit_action_contract(action: str) -> _AuditActionContract | None:
    return _AUDIT_ACTION_CONTRACT_BY_ALIAS.get(action)


def _require_audit_action_contract(action: str) -> _AuditActionContract:
    contract = _audit_action_contract(action)
    if contract is None:
        raise ValueError("audit action must have a declared contract")
    return contract


_DOWNLOAD_EVIDENCE_ALIASES = frozenset({"download_filename", "output_sha256"})
_EPHEMERAL_DESKTOP_PAYLOAD_FIELDS = frozenset(
    {"download_proof", "saved_filename"}
)


def _evidence_aliases_for_contract(
    contract: _AuditActionContract,
) -> frozenset[str]:
    if contract.evidence_type == "download_artifact":
        return _DOWNLOAD_EVIDENCE_ALIASES
    return frozenset()


def _require_audit_action_scope(
    action: str,
    scope_type: str,
) -> _AuditActionContract:
    contract = _require_audit_action_contract(action)
    if scope_type not in contract.scope_types:
        raise ValueError("audit action is not valid for the selected scope type")
    return contract


_AUDIT_SCOPE_ID_ALIASES = {
    "document": frozenset({"document_id", "source_document_id"}),
    "source_document": frozenset({"document_id", "source_document_id"}),
    "source_artifact": frozenset({"artifact_id", "source_artifact_id"}),
    "job": frozenset({"job_id", "conversion_job_id"}),
    "conversion_job": frozenset({"job_id", "conversion_job_id"}),
    "job_event": frozenset({"job_event_id"}),
    "conversion_result": frozenset({"result_id", "conversion_result_id"}),
    "artifact": frozenset({"artifact_id", "generated_artifact_id"}),
    "generated_artifact": frozenset({"artifact_id", "generated_artifact_id"}),
    "review_item": frozenset({"review_item_id"}),
    "review_decision": frozenset({"decision_id", "review_decision_id"}),
}


def _audit_event_type_matches_action(
    event_type: Any,
    *,
    action: str,
    scope_type: str,
) -> bool:
    if event_type == action:
        return True
    if not isinstance(event_type, str):
        return False
    contract = _require_audit_action_contract(action)
    return (
        event_type in contract.event_types
        and scope_type in contract.scope_types
    )


def _review_decision_action_matches(decision: str, action: str) -> bool:
    normalized = decision.strip().lower()
    aliases = {
        "approved": frozenset({"approved", "approve", "review.approved", "review.approve"}),
        "rejected": frozenset({"rejected", "reject", "review.rejected", "review.reject"}),
        "edited": frozenset({"edited", "edit", "review.edited", "review.edit"}),
    }
    return action in aliases.get(normalized, frozenset({normalized, f"review.{normalized}"}))


_SOURCE_DOCUMENT_PAYLOAD_BINDINGS = (
    (("source_artifact_id", "artifact_id"), "source_artifact_id", "source artifact id"),
    (("storage_key", "source_storage_key"), "source_storage_key", "source storage key"),
    (
        ("content_hash", "source_content_hash", "source_sha256"),
        "content_hash",
        "source content hash",
    ),
    (("source_type",), "source_type", "source type"),
    (("original_filename", "filename"), "original_filename", "source filename"),
    (("document_status", "status"), "status", "source document status"),
    (("uploaded_by", "uploader_id"), "uploaded_by", "recorded uploader"),
)
_SOURCE_DOCUMENT_STATUS_ALIASES = frozenset({"document_status", "status"})
_SOURCE_DOCUMENT_IMMUTABLE_PAYLOAD_BINDINGS = tuple(
    binding
    for binding in _SOURCE_DOCUMENT_PAYLOAD_BINDINGS
    if not set(binding[0]).intersection(_SOURCE_DOCUMENT_STATUS_ALIASES)
)

_SOURCE_ARTIFACT_PAYLOAD_BINDINGS = (
    (("storage_key", "source_storage_key"), "storage_key", "source artifact storage key"),
    (
        ("content_hash", "source_content_hash", "source_sha256"),
        "content_hash",
        "source artifact content hash",
    ),
    (("source_type",), "source_type", "source artifact type"),
    (("original_filename", "filename"), "original_filename", "source artifact filename"),
    (("uploaded_by", "uploader_id"), "uploaded_by", "recorded uploader"),
)

_JOB_PAYLOAD_BINDINGS = (
    (("idempotency_key",), "idempotency_key", "job idempotency key"),
    (("mode", "job_mode"), "mode", "job mode"),
    (("job_status", "status"), "status", "job status"),
    (("attempts", "job_attempts"), "attempts", "job attempts"),
    (("filename", "source_filename"), "original_filename", "job source filename"),
    (("source_sha256",), "source_content_hash", "job source hash"),
)
_DESKTOP_DOCUMENT_JOB_PAYLOAD_BINDINGS = (
    (("idempotency_key",), "idempotency_key", "job idempotency key"),
    (("mode", "job_mode"), "mode", "job mode"),
    (("job_status",), "status", "job status"),
    (("attempts", "job_attempts"), "attempts", "job attempts"),
)
_DESKTOP_DOCUMENT_JOB_PAYLOAD_ALIASES = frozenset(
    alias
    for aliases, _, _ in _DESKTOP_DOCUMENT_JOB_PAYLOAD_BINDINGS
    for alias in aliases
)

_JOB_HISTORY_MUTABLE_ALIASES = frozenset(
    {"job_status", "status", "attempts", "job_attempts"}
)
_JOB_IMMUTABLE_PAYLOAD_BINDINGS = tuple(
    binding
    for binding in _JOB_PAYLOAD_BINDINGS
    if not set(binding[0]).intersection(_JOB_HISTORY_MUTABLE_ALIASES)
)
_JOB_EVENT_PAYLOAD_BINDINGS = _JOB_PAYLOAD_BINDINGS + (
    (("job_event_sequence",), "sequence", "job event sequence"),
    (("event_actor",), "actor", "job event actor"),
)
_JOB_EVENT_IMMUTABLE_PAYLOAD_BINDINGS = _JOB_IMMUTABLE_PAYLOAD_BINDINGS + (
    (("job_event_sequence",), "sequence", "job event sequence"),
    (("event_actor",), "actor", "job event actor"),
)

_RESULT_PAYLOAD_BINDINGS = (
    (("result_status", "conversion_status", "status"), "status", "conversion result status"),
    (("content_hash", "result_content_hash"), "content_hash", "conversion result hash"),
)

_ARTIFACT_PAYLOAD_BINDINGS = (
    (("result_id", "conversion_result_id"), "result_id", "artifact result id"),
    (("display_filename", "artifact_filename"), "display_filename", "artifact display filename"),
    (("storage_key", "artifact_storage_key"), "storage_key", "artifact storage key"),
    (
        ("content_hash", "artifact_content_hash", "sha256"),
        "content_hash",
        "artifact content hash",
    ),
    (("category", "artifact_category"), "category", "artifact category"),
    (("format", "artifact_format"), "format", "artifact format"),
    (("retention_state",), "retention_state", "artifact retention state"),
)

_REVIEW_ITEM_PAYLOAD_BINDINGS = (
    (("target_path", "review_target_path"), "target_path", "review item target path"),
    (("review_status", "status"), "status", "review item status"),
    (("severity", "review_severity"), "severity", "review item severity"),
)

_REVIEW_DECISION_PAYLOAD_BINDINGS = (
    (("review_item_id",), "review_item_id", "review item id"),
    (("artifact_id", "generated_artifact_id"), "artifact_id", "review artifact id"),
)

_AUDIT_SCOPE_PAYLOAD_BINDINGS = {
    "document": _SOURCE_DOCUMENT_PAYLOAD_BINDINGS,
    "source_document": _SOURCE_DOCUMENT_PAYLOAD_BINDINGS,
    "source_artifact": _SOURCE_ARTIFACT_PAYLOAD_BINDINGS,
    "job": _JOB_PAYLOAD_BINDINGS,
    "conversion_job": _JOB_PAYLOAD_BINDINGS,
    "job_event": _JOB_EVENT_PAYLOAD_BINDINGS,
    "conversion_result": _RESULT_PAYLOAD_BINDINGS,
    "artifact": _ARTIFACT_PAYLOAD_BINDINGS,
    "generated_artifact": _ARTIFACT_PAYLOAD_BINDINGS,
    "review_item": _REVIEW_ITEM_PAYLOAD_BINDINGS,
    "review_decision": _REVIEW_DECISION_PAYLOAD_BINDINGS,
}

_AUDIT_SCOPE_SPECIAL_PAYLOAD_FIELDS = {
    "review_decision": frozenset(
        {"actor_role", "role", "decision", "review_decision", "outcome"}
    ),
}

_AUDIT_GLOBAL_PAYLOAD_FIELDS = frozenset(
    {
        "event_id",
        "job_id",
        "document_id",
        "integrity_algorithm",
        "actor",
        "actor_id",
        "action",
        "event_type",
        "scope_type",
        "scope_id",
    }
)

_AUDIT_RESERVED_EVIDENCE_FIELDS = frozenset(
    alias
    for bindings in _AUDIT_SCOPE_PAYLOAD_BINDINGS.values()
    for aliases, _, _ in bindings
    for alias in aliases
).union(
    alias
    for aliases in _AUDIT_SCOPE_ID_ALIASES.values()
    for alias in aliases
).union(
    field
    for fields in _AUDIT_SCOPE_SPECIAL_PAYLOAD_FIELDS.values()
    for field in fields
).union(_DOWNLOAD_EVIDENCE_ALIASES)
