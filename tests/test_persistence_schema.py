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
AUDIT_INTEGRITY_ALGORITHM = "sha256-canonical-json-chain-v1"
REPO_ROOT = Path(__file__).resolve().parents[1]

from tests.persistence_support import (
    _create_artifact,
    _create_document,
    _create_job,
    _create_result,
)
def test_persistence_cli_supports_direct_script_execution(tmp_path) -> None:
    db_path = tmp_path / "direct-script.sqlite3"

    completed = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "services" / "api" / "persistence.py"),
            "init-db",
            "--db-path",
            str(db_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert db_path.is_file()

def test_persistence_repository_initializes_and_reads_minimal_schema(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()

    document = repository.create_document(
        document_id="doc-1",
        source_type="pdf",
        original_filename="batch-record.pdf",
        source_artifact_id="source-artifact-1",
        source_storage_key="uploads/batch-record.pdf",
        content_hash=VALID_HASH,
        status="uploaded",
        uploaded_by="operator-1",
    )
    source_artifact = repository.get_source_artifact(document.source_artifact_id)
    job = repository.create_conversion_job(
        job_id="job-1",
        document_id=document.document_id,
        idempotency_key="upload-1",
        mode="standard",
        status="queued",
    )
    job_event = repository.create_job_event(
        event_id="job-event-1",
        job_id=job.job_id,
        event_type="job.queued",
        actor="operator-1",
        payload={"event_type": "job.queued", "job_status": "queued"},
    )
    result = repository.create_conversion_result(
        result_id="result-1",
        job_id=job.job_id,
        document_id=document.document_id,
        status="succeeded",
        content_hash="b" * 64,
    )
    artifact = repository.create_artifact(
        artifact_id="artifact-1",
        result_id=result.result_id,
        job_id=job.job_id,
        document_id=document.document_id,
        category="generated",
        format="docx",
        display_filename="result-1.docx",
        storage_key="artifacts/result-1.docx",
        content_hash="c" * 64,
    )
    review_item = repository.create_review_item(
        review_item_id="review-item-1",
        document_id=document.document_id,
        job_id=job.job_id,
        target_path="sections[0]",
        status="open",
        severity="medium",
    )
    decision = repository.create_review_decision(
        decision_id="decision-1",
        review_item_id=review_item.review_item_id,
        artifact_id=artifact.artifact_id,
        actor="qa-approver",
        role="approver",
        decision="approved",
    )
    audit_event = repository.create_audit_event(
        event_id="audit-1",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="qa-approver",
        action="review.approved",
        scope_type="review_decision",
        scope_id=decision.decision_id,
        payload={
            "event_type": "review.approved",
            "review_decision_id": decision.decision_id,
            "actor": {"id": "qa-approver", "role": "approver"},
        },
    )
    chained_audit_event = repository.create_audit_event(
        event_id="audit-2",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="job.queued",
        scope_type="job_event",
        scope_id=job_event.event_id,
        payload={"event_type": "job.queued"},
    )
    assert repository.get_document("doc-1") == document
    assert source_artifact == SourceArtifact(
        artifact_id=document.source_artifact_id,
        document_id=document.document_id,
        storage_key=document.source_storage_key,
        content_hash=document.content_hash,
        source_type=document.source_type,
        original_filename=document.original_filename,
        uploaded_by=document.uploaded_by,
        created_at=document.created_at,
    )
    assert repository.get_conversion_job("job-1") == job
    assert repository.get_job_event("job-event-1") == job_event
    assert repository.list_job_events(job.job_id) == [job_event]
    assert repository.get_conversion_result("result-1") == result
    assert repository.get_artifact("artifact-1") == artifact
    assert repository.get_review_item("review-item-1") == review_item
    assert repository.get_review_decision("decision-1") == decision
    assert repository.get_audit_event("audit-1") == audit_event
    assert json.loads(job_event.payload_json) == {
        "event_type": "job.queued",
        "job_status": "queued",
    }
    assert audit_event.sequence == 1
    assert audit_event.integrity_algorithm == AUDIT_INTEGRITY_ALGORITHM
    assert audit_event.prev_event_hash is None
    assert len(audit_event.event_hash) == 64
    assert chained_audit_event.sequence == 2
    assert chained_audit_event.prev_event_hash == audit_event.event_hash
    assert chained_audit_event.event_hash != audit_event.event_hash
    assert json.loads(audit_event.payload_json) == {
        "actor": {"id": "qa-approver", "role": "approver"},
        "event_type": "review.approved",
        "review_decision_id": decision.decision_id,
    }

    for row in (
        document,
        job,
        job_event,
        result,
        artifact,
        review_item,
        decision,
        audit_event,
        chained_audit_event,
    ):
        assert row.created_at.endswith("+00:00")

def test_initialize_rejects_incompatible_existing_managed_table(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE source_documents (document_id TEXT PRIMARY KEY)")

    repository = SQLitePersistenceRepository(db_path)

    with pytest.raises(ValueError, match="source_documents"):
        repository.initialize()

    with sqlite3.connect(db_path) as connection:
        migration_table = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'schema_migrations'
            """
        ).fetchone()

    assert migration_table is None

def test_schema_normalization_preserves_case_inside_string_literals() -> None:
    lowercase_literal = "CHECK(integrity_algorithm = 'sha256-canonical-json-chain-v1')"
    uppercase_literal = "CHECK(integrity_algorithm = 'SHA256-CANONICAL-JSON-CHAIN-V1')"

    assert persistence._normalize_schema_sql(lowercase_literal) != (
        persistence._normalize_schema_sql(uppercase_literal)
    )
    assert persistence._normalize_schema_sql("CREATE  TABLE T (A TEXT)") == (
        "create table t (a text)"
    )

def test_persistence_schema_module_owns_schema_contract() -> None:
    assert persistence._SCHEMA_SQL is persistence_schema._SCHEMA_SQL
    assert persistence._RESET_SQL is persistence_schema._RESET_SQL
    assert persistence._normalize_schema_sql is persistence_schema._normalize_schema_sql
    assert persistence._schema_definitions is persistence_schema._schema_definitions
    assert persistence._expected_schema_definitions is (
        persistence_schema._expected_schema_definitions
    )
    assert persistence._validate_managed_schema is (
        persistence_schema._validate_managed_schema
    )

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(persistence._SCHEMA_SQL)
        assert persistence._schema_definitions(connection) == (
            persistence_schema._expected_schema_definitions()
        )
    finally:
        connection.close()

def test_persistence_repository_rejects_missing_bindings_and_keeps_state_clean(
    tmp_path,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()

    with pytest.raises(ValueError, match="content_hash must be a sha256 hex digest"):
        repository.create_document(
            document_id="doc-bad",
            source_type="pdf",
            original_filename="bad.pdf",
            source_artifact_id="source-artifact-bad",
            source_storage_key="uploads/bad.pdf",
            content_hash="not-a-hash",
            status="uploaded",
            uploaded_by="operator-1",
        )

    with pytest.raises(sqlite3.IntegrityError):
        repository.create_conversion_job(
            job_id="job-orphan",
            document_id="missing-doc",
            idempotency_key="upload-1",
            mode="standard",
            status="queued",
        )

    assert repository.get_document("doc-bad") is None
    assert repository.get_conversion_job("job-orphan") is None

def test_schema_rejects_null_keys_and_malformed_hashes_from_direct_writes(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO source_documents(
                    document_id, source_type, original_filename, source_artifact_id,
                    source_storage_key, content_hash, status, uploaded_by, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    None,
                    "pdf",
                    "null-key.pdf",
                    "source-artifact-null",
                    "uploads/null-key.pdf",
                    VALID_HASH,
                    "uploaded",
                    "operator-1",
                    "2026-07-09T00:00:00+00:00",
                    "2026-07-09T00:00:00+00:00",
                ),
            )

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO source_documents(
                    document_id, source_type, original_filename, source_artifact_id,
                    source_storage_key, content_hash, status, uploaded_by, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "doc-bad-hash",
                    "pdf",
                    "bad-hash.pdf",
                    "source-artifact-bad-hash",
                    "uploads/bad-hash.pdf",
                    "not-a-sha256",
                    "uploaded",
                    "operator-1",
                    "2026-07-09T00:00:00+00:00",
                    "2026-07-09T00:00:00+00:00",
                ),
            )

    document = _create_document(repository, "doc-for-job-attempts")
    for job_id, attempts in (
        ("job-negative-attempts", -1),
        ("job-non-integer-attempts", "not-an-integer"),
    ):
        with sqlite3.connect(db_path) as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, document_id, idempotency_key, mode, status, attempts,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        document.document_id,
                        f"upload-{job_id}",
                        "standard",
                        "queued",
                        attempts,
                        "2026-07-09T00:00:00+00:00",
                        "2026-07-09T00:00:00+00:00",
                    ),
                )

    assert repository.get_document("doc-bad-hash") is None
    assert repository.get_conversion_job("job-negative-attempts") is None
    assert repository.get_conversion_job("job-non-integer-attempts") is None
