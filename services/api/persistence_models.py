from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Document:
    document_id: str
    source_type: str
    original_filename: str
    source_artifact_id: str
    source_storage_key: str
    content_hash: str
    status: str
    uploaded_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SourceArtifact:
    artifact_id: str
    document_id: str
    storage_key: str
    content_hash: str
    source_type: str
    original_filename: str
    uploaded_by: str
    created_at: str


@dataclass(frozen=True)
class ConversionJob:
    job_id: str
    document_id: str
    idempotency_key: str
    mode: str
    status: str
    attempts: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class JobEvent:
    event_id: str
    job_id: str
    sequence: int
    event_type: str
    actor: str
    payload_json: str
    created_at: str


@dataclass(frozen=True)
class ConversionResult:
    result_id: str
    job_id: str
    document_id: str
    status: str
    content_hash: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    result_id: str
    job_id: str
    document_id: str
    category: str
    format: str
    display_filename: str
    storage_key: str
    content_hash: str
    retention_state: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReviewItem:
    review_item_id: str
    document_id: str
    job_id: str
    target_path: str
    status: str
    severity: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReviewDecision:
    decision_id: str
    review_item_id: str
    artifact_id: str
    job_id: str
    document_id: str
    actor: str
    role: str
    decision: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    job_id: str
    document_id: str
    sequence: int
    integrity_algorithm: str
    actor: str
    action: str
    scope_type: str
    scope_id: str
    event_hash: str
    prev_event_hash: str | None
    payload_json: str
    created_at: str
