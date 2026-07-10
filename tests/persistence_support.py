from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
import pytest
from services.api import persistence, persistence_schema
from services.api.persistence import (
    AuditEvent,
    Artifact,
    ConversionJob,
    ConversionResult,
    Document,
    JobEvent,
    ReviewDecision,
    ReviewItem,
    SQLitePersistenceRepository,
    SourceArtifact,
)

VALID_HASH = "a" * 64

def _create_document(
    repository: SQLitePersistenceRepository,
    document_id: str,
    *,
    storage_key: str | None = None,
) -> Document:
    return repository.create_document(
        document_id=document_id,
        source_type="pdf",
        original_filename=f"{document_id}.pdf",
        source_artifact_id=f"source-artifact-{document_id}",
        source_storage_key=storage_key or f"uploads/{document_id}.pdf",
        content_hash=VALID_HASH,
        status="uploaded",
        uploaded_by="operator-1",
    )

def _create_job(
    repository: SQLitePersistenceRepository,
    document: Document,
    job_id: str,
    *,
    idempotency_key: str | None = None,
) -> ConversionJob:
    return repository.create_conversion_job(
        job_id=job_id,
        document_id=document.document_id,
        idempotency_key=idempotency_key or f"upload-{job_id}",
        mode="standard",
        status="queued",
    )

def _create_result(
    repository: SQLitePersistenceRepository,
    job: ConversionJob,
    result_id: str,
) -> ConversionResult:
    return repository.create_conversion_result(
        result_id=result_id,
        job_id=job.job_id,
        document_id=job.document_id,
        status="succeeded",
        content_hash="b" * 64,
    )

def _create_artifact(
    repository: SQLitePersistenceRepository,
    result: ConversionResult,
    artifact_id: str,
) -> Artifact:
    return repository.create_artifact(
        artifact_id=artifact_id,
        result_id=result.result_id,
        job_id=result.job_id,
        document_id=result.document_id,
        category="generated",
        format="docx",
        display_filename=f"{artifact_id}.docx",
        storage_key=f"artifacts/{artifact_id}.docx",
        content_hash="c" * 64,
    )
