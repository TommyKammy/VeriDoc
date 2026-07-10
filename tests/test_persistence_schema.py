from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager

import pytest

from services.api import persistence
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
        storage_key=f"artifacts/{artifact_id}.docx",
        content_hash="c" * 64,
    )


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


def test_persistence_repository_preserves_idempotent_job_replays(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document_a = _create_document(repository, "doc-a")
    document_b = _create_document(repository, "doc-b")

    created = repository.create_or_get_conversion_job(
        job_id="job-original",
        document_id=document_a.document_id,
        idempotency_key="upload-replay",
        mode="standard",
        status="queued",
    )
    replayed = repository.create_or_get_conversion_job(
        job_id="job-retry",
        document_id=document_a.document_id,
        idempotency_key="upload-replay",
        mode="standard",
        status="queued",
    )

    assert replayed == created
    assert repository.get_conversion_job("job-retry") is None
    assert repository.get_conversion_job_by_idempotency_key("upload-replay") == created

    with pytest.raises(ValueError, match="idempotency_key already bound"):
        repository.create_or_get_conversion_job(
            job_id="job-wrong-document",
            document_id=document_b.document_id,
            idempotency_key="upload-replay",
            mode="standard",
            status="queued",
        )
    with pytest.raises(ValueError, match="idempotency_key already bound"):
        repository.create_or_get_conversion_job(
            job_id="job-wrong-mode",
            document_id=document_a.document_id,
            idempotency_key="upload-replay",
            mode="high-quality",
            status="queued",
        )

    assert repository.get_conversion_job("job-wrong-document") is None
    assert repository.get_conversion_job("job-wrong-mode") is None


def test_persistence_repository_ignores_internal_columns_when_hydrating_rows(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")

    loaded = repository._get_one(
        Document,
        """
        SELECT source_documents.*, 'internal-only' AS schema_internal_marker
        FROM source_documents
        WHERE document_id = ?
        """,
        (document.document_id,),
    )

    assert loaded == document


def test_source_documents_require_bound_source_artifacts_at_database_boundary(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="source artifact"):
            connection.execute(
                """
                INSERT INTO source_documents(
                    document_id, source_type, original_filename, source_artifact_id,
                    source_storage_key, content_hash, status, uploaded_by, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "doc-orphan-source-artifact",
                    "pdf",
                    "orphan.pdf",
                    "source-artifact-orphan",
                    "uploads/orphan.pdf",
                    VALID_HASH,
                    "uploaded",
                    "operator-1",
                    "2026-07-09T00:00:00+00:00",
                    "2026-07-09T00:00:00+00:00",
                ),
            )

    assert repository.get_document("doc-orphan-source-artifact") is None


def test_source_artifacts_are_valid_audit_scopes_for_upload_provenance(
    tmp_path,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document_a = _create_document(repository, "doc-a")
    document_b = _create_document(repository, "doc-b")
    job_a = _create_job(repository, document_a, "job-a")
    job_b = _create_job(repository, document_b, "job-b")

    audit_event = repository.create_audit_event(
        event_id="audit-source-artifact",
        job_id=job_a.job_id,
        document_id=document_a.document_id,
        actor="operator-1",
        action="document.uploaded",
        scope_type="source_artifact",
        scope_id=document_a.source_artifact_id,
        payload={
            "event_type": "document.uploaded",
            "source_artifact_id": document_a.source_artifact_id,
        },
    )

    with pytest.raises(ValueError, match="audit scope must match"):
        repository.create_audit_event(
            event_id="audit-source-artifact-mixed",
            job_id=job_b.job_id,
            document_id=document_b.document_id,
            actor="operator-1",
            action="document.uploaded",
            scope_type="source_artifact",
            scope_id=document_a.source_artifact_id,
            payload={"event_type": "document.uploaded"},
        )

    assert repository.get_audit_event(audit_event.event_id) == audit_event


def test_source_artifacts_reject_orphan_documents_at_database_boundary(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()

    with pytest.raises(sqlite3.IntegrityError):
        with sqlite3.connect(db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT INTO source_artifacts(
                    artifact_id, document_id, storage_key, content_hash, source_type,
                    original_filename, uploaded_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "source-artifact-orphan",
                    "missing-document",
                    "uploads/orphan.pdf",
                    VALID_HASH,
                    "pdf",
                    "orphan.pdf",
                    "operator-1",
                    "2026-07-09T00:00:00+00:00",
                ),
            )

    assert repository.get_source_artifact("source-artifact-orphan") is None


def test_source_document_and_artifact_provenance_must_match_everywhere(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    created_at = "2026-07-10T00:00:00+00:00"

    with pytest.raises(sqlite3.IntegrityError, match="source artifact"):
        with sqlite3.connect(db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT INTO source_artifacts(
                    artifact_id, document_id, storage_key, content_hash, source_type,
                    original_filename, uploaded_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "source-artifact-mismatch",
                    "doc-mismatch",
                    "uploads/mismatch.pdf",
                    VALID_HASH,
                    "pdf",
                    "mismatch.pdf",
                    "alice",
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO source_documents(
                    document_id, source_type, original_filename, source_artifact_id,
                    source_storage_key, content_hash, status, uploaded_by, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "doc-mismatch",
                    "pdf",
                    "mismatch.pdf",
                    "source-artifact-mismatch",
                    "uploads/mismatch.pdf",
                    VALID_HASH,
                    "uploaded",
                    "bob",
                    created_at,
                    created_at,
                ),
            )

    document = _create_document(repository, "doc-1")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "DROP TRIGGER source_documents_source_artifact_reference_update"
        )
        connection.execute(
            "UPDATE source_documents SET uploaded_by = ? WHERE document_id = ?",
            ("mallory", document.document_id),
        )

    with pytest.raises(ValueError, match="bound source artifact"):
        repository.get_document(document.document_id)
    with pytest.raises(ValueError, match="bound source document"):
        repository.get_source_artifact(document.source_artifact_id)


def test_persistence_repository_rejects_cross_scope_relationships(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()

    document_a = _create_document(repository, "doc-a")
    document_b = _create_document(repository, "doc-b")
    job_a = _create_job(repository, document_a, "job-a")
    job_b = _create_job(repository, document_b, "job-b")
    result_a = _create_result(repository, job_a, "result-a")
    result_b = _create_result(repository, job_b, "result-b")
    artifact_a = _create_artifact(repository, result_a, "artifact-a")
    artifact_b = _create_artifact(repository, result_b, "artifact-b")
    review_item_a = repository.create_review_item(
        review_item_id="review-item-a",
        document_id=document_a.document_id,
        job_id=job_a.job_id,
        target_path="sections[0]",
        status="open",
        severity="medium",
    )

    with pytest.raises(ValueError, match="same document"):
        repository.create_conversion_result(
            result_id="result-mixed",
            job_id=job_a.job_id,
            document_id=document_b.document_id,
            status="succeeded",
            content_hash="b" * 64,
        )

    with pytest.raises(ValueError, match="same conversion"):
        repository.create_artifact(
            artifact_id="artifact-mixed",
            result_id=result_a.result_id,
            job_id=job_b.job_id,
            document_id=document_b.document_id,
            category="generated",
            format="docx",
            storage_key="artifacts/mixed.docx",
            content_hash="c" * 64,
        )

    with pytest.raises(ValueError, match="same conversion"):
        repository.create_review_decision(
            decision_id="decision-mixed",
            review_item_id=review_item_a.review_item_id,
            artifact_id=artifact_b.artifact_id,
            actor="qa-approver",
            role="approver",
            decision="approved",
        )

    with pytest.raises(ValueError, match="audit scope_id"):
        repository.create_audit_event(
            event_id="audit-missing-scope",
            job_id=job_a.job_id,
            document_id=document_a.document_id,
            actor="qa-approver",
            action="review.approved",
            scope_type="review_decision",
            scope_id="missing-decision",
            payload={"event_type": "review.approved"},
        )

    with pytest.raises(ValueError, match="audit scope must match"):
        repository.create_audit_event(
            event_id="audit-wrong-scope",
            job_id=job_a.job_id,
            document_id=document_a.document_id,
            actor="qa-approver",
            action="artifact.generated",
            scope_type="artifact",
            scope_id=artifact_b.artifact_id,
            payload={"event_type": "artifact.generated"},
        )

    assert repository.get_conversion_result("result-mixed") is None
    assert repository.get_artifact("artifact-mixed") is None
    assert repository.get_review_decision("decision-mixed") is None
    assert repository.get_audit_event("audit-missing-scope") is None
    assert repository.get_audit_event("audit-wrong-scope") is None
    assert artifact_a.document_id == document_a.document_id


def test_parent_bindings_are_enforced_without_foreign_key_pragma(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document_a = _create_document(repository, "doc-a")
    document_b = _create_document(repository, "doc-b")
    job_a = _create_job(repository, document_a, "job-a")
    job_b = _create_job(repository, document_b, "job-b")
    result_a = _create_result(repository, job_a, "result-a")
    result_b = _create_result(repository, job_b, "result-b")
    artifact_b = _create_artifact(repository, result_b, "artifact-b")
    review_item_a = repository.create_review_item(
        review_item_id="review-item-a",
        document_id=document_a.document_id,
        job_id=job_a.job_id,
        target_path="sections[0]",
        status="open",
        severity="medium",
    )
    created_at = "2026-07-09T00:00:00+00:00"
    audit_payload_json = persistence._canonical_json(
        {
            "action": "document.uploaded",
            "actor": "operator-1",
            "scope_id": document_a.document_id,
            "scope_type": "document",
        }
    )
    audit_event_hash = persistence._audit_event_hash(
        event_id="audit-missing-job",
        job_id="missing-job",
        document_id=document_a.document_id,
        sequence=1,
        integrity_algorithm=AUDIT_INTEGRITY_ALGORITHM,
        actor="operator-1",
        action="document.uploaded",
        scope_type="document",
        scope_id=document_a.document_id,
        prev_event_hash=None,
        payload_json=audit_payload_json,
        created_at=created_at,
    )

    cases = (
        (
            """
            INSERT INTO jobs(
                job_id, document_id, idempotency_key, mode, status, attempts,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-missing-document",
                "missing-document",
                "upload-missing-document",
                "standard",
                "queued",
                0,
                created_at,
                created_at,
            ),
        ),
        (
            """
            INSERT INTO job_events(
                event_id, job_id, sequence, event_type, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-event-missing-job",
                "missing-job",
                1,
                "job.queued",
                "operator-1",
                "{}",
                created_at,
            ),
        ),
        (
            """
            INSERT INTO conversion_results(
                result_id, job_id, document_id, status, content_hash,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "result-mixed",
                job_a.job_id,
                document_b.document_id,
                "succeeded",
                "b" * 64,
                created_at,
                created_at,
            ),
        ),
        (
            """
            INSERT INTO generated_artifacts(
                artifact_id, result_id, job_id, document_id, category, format,
                storage_key, content_hash, retention_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "artifact-mixed",
                result_a.result_id,
                job_b.job_id,
                document_b.document_id,
                "generated",
                "docx",
                "artifacts/mixed.docx",
                "c" * 64,
                "active",
                created_at,
                created_at,
            ),
        ),
        (
            """
            INSERT INTO review_items(
                review_item_id, document_id, job_id, target_path, status,
                severity, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "review-item-mixed",
                document_b.document_id,
                job_a.job_id,
                "sections[0]",
                "open",
                "medium",
                created_at,
                created_at,
            ),
        ),
        (
            """
            INSERT INTO review_decisions(
                decision_id, review_item_id, artifact_id, job_id, document_id,
                actor, role, decision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "decision-mixed",
                review_item_a.review_item_id,
                artifact_b.artifact_id,
                job_a.job_id,
                document_a.document_id,
                "qa-approver",
                "approver",
                "approved",
                created_at,
                created_at,
            ),
        ),
        (
            """
            INSERT INTO audit_events(
                event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                action, scope_type, scope_id, event_hash, prev_event_hash,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "audit-missing-job",
                "missing-job",
                document_a.document_id,
                1,
                AUDIT_INTEGRITY_ALGORITHM,
                "operator-1",
                "document.uploaded",
                "document",
                document_a.document_id,
                audit_event_hash,
                None,
                audit_payload_json,
                created_at,
            ),
        ),
    )

    with sqlite3.connect(db_path) as connection:
        for sql, params in cases:
            with pytest.raises(sqlite3.IntegrityError, match="matching parent"):
                connection.execute(sql, params)


def test_parent_bindings_remain_guarded_after_insert_with_foreign_keys_off(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
    review_item = repository.create_review_item(
        review_item_id="review-item-1",
        document_id=document.document_id,
        job_id=job.job_id,
        target_path="sections[0]",
        status="open",
        severity="medium",
    )
    repository.create_review_decision(
        decision_id="decision-1",
        review_item_id=review_item.review_item_id,
        artifact_id=artifact.artifact_id,
        actor="qa-approver",
        role="approver",
        decision="approved",
    )

    statements = (
        ("UPDATE jobs SET document_id = 'missing' WHERE job_id = ?", job.job_id),
        ("DELETE FROM source_documents WHERE document_id = ?", document.document_id),
        ("UPDATE conversion_results SET job_id = 'missing' WHERE result_id = ?", result.result_id),
        ("DELETE FROM jobs WHERE job_id = ?", job.job_id),
        ("UPDATE generated_artifacts SET result_id = 'missing' WHERE artifact_id = ?", artifact.artifact_id),
        ("DELETE FROM conversion_results WHERE result_id = ?", result.result_id),
        ("UPDATE review_items SET job_id = 'missing' WHERE review_item_id = ?", review_item.review_item_id),
        ("DELETE FROM generated_artifacts WHERE artifact_id = ?", artifact.artifact_id),
        ("DELETE FROM review_items WHERE review_item_id = ?", review_item.review_item_id),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        for sql, identifier in statements:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, (identifier,))


def test_review_decision_evidence_is_immutable_before_audit(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
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

    statements = (
        (
            "UPDATE generated_artifacts SET storage_key = ? WHERE artifact_id = ?",
            ("artifacts/rewritten.docx", artifact.artifact_id),
        ),
        (
            "UPDATE generated_artifacts SET content_hash = ? WHERE artifact_id = ?",
            ("d" * 64, artifact.artifact_id),
        ),
        (
            "UPDATE review_items SET target_path = ? WHERE review_item_id = ?",
            ("sections[1]", review_item.review_item_id),
        ),
        (
            "UPDATE review_items SET severity = ? WHERE review_item_id = ?",
            ("critical", review_item.review_item_id),
        ),
        (
            "UPDATE review_decisions SET actor = ?, role = ?, decision = ? "
            "WHERE decision_id = ?",
            ("mallory", "viewer", "rejected", decision.decision_id),
        ),
        (
            "DELETE FROM review_decisions WHERE decision_id = ?",
            (decision.decision_id,),
        ),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        for sql, params in statements:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="review decision evidence|append-only",
            ):
                connection.execute(sql, params)

    assert repository.get_artifact(artifact.artifact_id) == artifact
    assert repository.get_review_item(review_item.review_item_id) == review_item
    assert repository.get_review_decision(decision.decision_id) == decision


def test_persistence_repository_rejects_conflicting_job_event_payload_fields(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document_a = _create_document(repository, "doc-a")
    document_b = _create_document(repository, "doc-b")
    job_a = _create_job(repository, document_a, "job-a")
    job_b = _create_job(repository, document_b, "job-b")

    for event_id, payload in (
        ("event-type-mismatch", {"event_type": "job.failed"}),
        ("event-job-mismatch", {"event_type": "job.queued", "job_id": job_b.job_id}),
        ("event-actor-mismatch", {"event_type": "job.queued", "actor": "other-actor"}),
        ("event-actor-id-mismatch", {"event_type": "job.queued", "actor_id": "other-actor"}),
        ("event-job-status-mismatch", {"event_type": "job.queued", "job_status": "failed"}),
        ("event-status-mismatch", {"event_type": "job.queued", "status": "failed"}),
        ("event-sequence-smuggled", {"event_type": "job.queued", "sequence": 10}),
        (
            "event-created-at-smuggled",
            {"event_type": "job.queued", "created_at": "2026-07-09T00:00:00+00:00"},
        ),
        (
            "event-occurred-at-smuggled",
            {"event_type": "job.queued", "occurred_at": "2026-07-09T00:00:00+00:00"},
        ),
    ):
        with pytest.raises(ValueError, match="payload"):
            repository.create_job_event(
                event_id=event_id,
                job_id=job_a.job_id,
                event_type="job.queued",
                actor="operator-1",
                payload=payload,
            )
        assert repository.get_job_event(event_id) is None


def test_persistence_repository_allows_nested_job_event_actor_payload(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    event = repository.create_job_event(
        event_id="event-authenticated",
        job_id=job.job_id,
        event_type="job.queued",
        actor="operator-1",
        payload={
            "event_type": "job.queued",
            "actor": {"id": "operator-1", "role": "reviewer"},
        },
    )

    assert event.actor == "operator-1"
    assert json.loads(event.payload_json) == {
        "actor": {"id": "operator-1", "role": "reviewer"},
        "event_type": "job.queued",
    }

    with pytest.raises(ValueError, match="payload actor.id"):
        repository.create_job_event(
            event_id="event-wrong-actor",
            job_id=job.job_id,
            event_type="job.queued",
            actor="operator-1",
            payload={
                "event_type": "job.queued",
                "actor": {"id": "operator-2", "role": "reviewer"},
            },
        )
    assert repository.get_job_event("event-wrong-actor") is None


def test_job_events_preserve_append_order_when_timestamps_match(
    tmp_path,
    monkeypatch,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    monkeypatch.setattr(
        persistence,
        "_utc_now",
        lambda: "2026-07-09T00:00:00+00:00",
    )

    first = repository.create_job_event(
        event_id="event-z",
        job_id=job.job_id,
        event_type="job.queued",
        actor="operator-1",
    )
    second = repository.create_job_event(
        event_id="event-a",
        job_id=job.job_id,
        event_type="job.running",
        actor="operator-1",
    )

    assert [event.event_id for event in repository.list_job_events(job.job_id)] == [
        first.event_id,
        second.event_id,
    ]


def test_job_events_are_append_only_at_the_database_boundary(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    job_event = repository.create_job_event(
        event_id="job-event-append-only",
        job_id=job.job_id,
        event_type="job.queued",
        actor="operator-1",
    )

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE job_events SET event_type = ? WHERE event_id = ?",
                ("job.failed", job_event.event_id),
            )

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM job_events WHERE event_id = ?",
                (job_event.event_id,),
            )

    assert repository.get_job_event(job_event.event_id) == job_event


def test_job_event_sequences_are_contiguous_at_the_database_boundary(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="contiguous"):
            connection.execute(
                """
                INSERT INTO job_events(
                    event_id, job_id, sequence, event_type, actor, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "job-event-gap",
                    job.job_id,
                    99,
                    "job.queued",
                    "operator-1",
                    "{}",
                    "2026-07-09T00:00:00+00:00",
                ),
            )

    first = repository.create_job_event(
        event_id="job-event-1",
        job_id=job.job_id,
        event_type="job.queued",
        actor="operator-1",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="contiguous"):
            connection.execute(
                """
                INSERT INTO job_events(
                    event_id, job_id, sequence, event_type, actor, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "job-event-gap-2",
                    job.job_id,
                    3,
                    "job.running",
                    "operator-1",
                    "{}",
                    "2026-07-09T00:00:01+00:00",
                ),
            )

    assert repository.list_job_events(job.job_id) == [first]


def test_repository_rejects_corrupt_job_event_history_before_append(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER job_events_contiguous_sequence_insert")
        connection.execute(
            """
            INSERT INTO job_events(
                event_id, job_id, sequence, event_type, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-event-corrupt",
                job.job_id,
                99,
                "job.queued",
                "operator-1",
                '{"event_type":"job.queued"}',
                "2026-07-10T00:00:00+00:00",
            ),
        )

    with pytest.raises(ValueError, match="contiguous sequences"):
        repository.create_job_event(
            event_id="job-event-after-corrupt",
            job_id=job.job_id,
            event_type="job.running",
            actor="operator-1",
        )
    with pytest.raises(ValueError, match="contiguous sequences"):
        repository.get_job_event("job-event-corrupt")
    with pytest.raises(ValueError, match="contiguous sequences"):
        repository.list_job_events(job.job_id)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM job_events WHERE event_id = ?",
            ("job-event-after-corrupt",),
        ).fetchone()[0] == 0


def test_source_and_generated_artifacts_share_global_identity_keys(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")

    with pytest.raises(sqlite3.IntegrityError):
        repository.create_artifact(
            artifact_id=document.source_artifact_id,
            result_id=result.result_id,
            job_id=job.job_id,
            document_id=document.document_id,
            category="generated",
            format="docx",
            storage_key="artifacts/generated.docx",
            content_hash="c" * 64,
        )

    with pytest.raises(sqlite3.IntegrityError):
        repository.create_artifact(
            artifact_id="artifact-unique",
            result_id=result.result_id,
            job_id=job.job_id,
            document_id=document.document_id,
            category="generated",
            format="docx",
            storage_key=document.source_storage_key,
            content_hash="c" * 64,
        )

    artifact = _create_artifact(repository, result, "artifact-existing")
    with pytest.raises(sqlite3.IntegrityError):
        repository.create_document(
            document_id="doc-collides-with-generated-artifact-id",
            source_type="pdf",
            original_filename="artifact-id-collision.pdf",
            source_artifact_id=artifact.artifact_id,
            source_storage_key="uploads/artifact-id-collision.pdf",
            content_hash=VALID_HASH,
            status="uploaded",
            uploaded_by="operator-1",
        )

    with pytest.raises(sqlite3.IntegrityError):
        _create_document(
            repository,
            "doc-collides-with-generated-artifact",
            storage_key=artifact.storage_key,
        )

    assert repository.get_artifact(document.source_artifact_id) is None
    assert repository.get_artifact("artifact-unique") is None
    assert repository.get_document("doc-collides-with-generated-artifact-id") is None
    assert repository.get_document("doc-collides-with-generated-artifact") is None


def test_artifact_identity_global_keys_cannot_collide_on_update(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document_a = _create_document(repository, "doc-a")
    document_b = _create_document(repository, "doc-b")
    job_a = _create_job(repository, document_a, "job-a")
    result_a = _create_result(repository, job_a, "result-a")
    artifact_a = _create_artifact(repository, result_a, "artifact-a")

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="globally unique"):
            connection.execute(
                """
                UPDATE source_documents
                SET source_artifact_id = ?
                WHERE document_id = ?
                """,
                (artifact_a.artifact_id, document_b.document_id),
            )

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="globally unique"):
            connection.execute(
                """
                UPDATE source_documents
                SET source_storage_key = ?
                WHERE document_id = ?
                """,
                (artifact_a.storage_key, document_b.document_id),
            )

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="globally unique"):
            connection.execute(
                """
                UPDATE generated_artifacts
                SET artifact_id = ?
                WHERE artifact_id = ?
                """,
                (document_a.source_artifact_id, artifact_a.artifact_id),
            )

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="globally unique"):
            connection.execute(
                """
                UPDATE generated_artifacts
                SET storage_key = ?
                WHERE artifact_id = ?
                """,
                (document_a.source_storage_key, artifact_a.artifact_id),
            )

    assert repository.get_document(document_b.document_id) == document_b
    assert repository.get_artifact(artifact_a.artifact_id) == artifact_a


def test_persistence_repository_rejects_caller_supplied_audit_chain_fields(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    for field_name, value in (
        ("sequence", 10),
        ("event_hash", "d" * 64),
        ("prev_event_hash", "e" * 64),
    ):
        kwargs = {
            "event_id": f"audit-forged-{field_name}",
            "job_id": job.job_id,
            "document_id": document.document_id,
            "actor": "qa-approver",
            "action": "document.uploaded",
            "scope_type": "document",
            "scope_id": document.document_id,
            field_name: value,
        }
        with pytest.raises(ValueError, match=f"{field_name} is derived"):
            repository.create_audit_event(**kwargs)
        assert repository.get_audit_event(f"audit-forged-{field_name}") is None


def test_persistence_repository_rejects_conflicting_audit_payload_fields(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    for event_id, payload in (
        ("audit-job-mismatch", {"job_id": "job-other"}),
        ("audit-action-mismatch", {"action": "job.failed"}),
        ("audit-event-type-mismatch", {"event_type": "job.failed"}),
        ("audit-scope-mismatch", {"scope_type": "job", "scope_id": job.job_id}),
        ("audit-chain-smuggled", {"sequence": 10}),
        ("audit-created-at-smuggled", {"created_at": "2026-07-08T00:00:00+00:00"}),
        ("audit-occurred-at-smuggled", {"occurred_at": "2026-07-08T00:00:00+00:00"}),
        (
            "audit-event-timestamp-smuggled",
            {"event_timestamp": "2026-07-08T00:00:00+00:00"},
        ),
        ("audit-actor-id-mismatch", {"actor_id": "other-actor"}),
    ):
        with pytest.raises(ValueError, match="payload"):
            repository.create_audit_event(
                event_id=event_id,
                job_id=job.job_id,
                document_id=document.document_id,
                actor="qa-approver",
                action="document.uploaded",
                scope_type="document",
                scope_id=document.document_id,
                payload=payload,
            )
        assert repository.get_audit_event(event_id) is None


def test_persistence_repository_accepts_canonical_audit_event_type_categories(
    tmp_path,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    review_item = repository.create_review_item(
        review_item_id="review-item-1",
        document_id=document.document_id,
        job_id=job.job_id,
        target_path="sections[0]",
        status="open",
        severity="medium",
    )

    for event_id, action, scope_type, scope_id, event_type in (
        (
            "audit-job-action",
            "open_detail",
            "conversion_job",
            job.job_id,
            "conversion_job.action_requested",
        ),
        (
            "audit-desktop-action",
            "desktop_result_download",
            "conversion_job",
            job.job_id,
            "desktop.job_operation",
        ),
        (
            "audit-review-action",
            "approve",
            "review_item",
            review_item.review_item_id,
            "conversion_review.action_requested",
        ),
        (
            "audit-job-lifecycle",
            "conversion_queued",
            "job",
            job.job_id,
            "job.lifecycle",
        ),
    ):
        audit_event = repository.create_audit_event(
            event_id=event_id,
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action=action,
            scope_type=scope_type,
            scope_id=scope_id,
            payload={"event_type": event_type, "action": action},
        )
        assert json.loads(audit_event.payload_json)["event_type"] == event_type

    for event_id, action, scope_type, scope_id, event_type in (
        (
            "audit-job-category-wrong-action",
            "document.uploaded",
            "conversion_job",
            job.job_id,
            "conversion_job.action_requested",
        ),
        (
            "audit-review-category-wrong-scope",
            "approve",
            "conversion_job",
            job.job_id,
            "conversion_review.action_requested",
        ),
    ):
        with pytest.raises(ValueError, match="payload event_type"):
            repository.create_audit_event(
                event_id=event_id,
                job_id=job.job_id,
                document_id=document.document_id,
                actor="operator-1",
                action=action,
                scope_type=scope_type,
                scope_id=scope_id,
                payload={"event_type": event_type, "action": action},
            )
        assert repository.get_audit_event(event_id) is None


def test_persistence_repository_uses_existing_audit_integrity_algorithm(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    audit_event = repository.create_audit_event(
        event_id="audit-1",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="qa-approver",
        action="document.inspected",
        scope_type="document",
        scope_id=document.document_id,
    )

    assert audit_event.integrity_algorithm == AUDIT_INTEGRITY_ALGORITHM
    with pytest.raises(ValueError, match="integrity_algorithm"):
        repository.create_audit_event(
            event_id="audit-old-algorithm",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="qa-approver",
            action="document.inspected",
            scope_type="document",
            scope_id=document.document_id,
            integrity_algorithm="sha256",
        )
    assert repository.get_audit_event("audit-old-algorithm") is None


def test_persistence_repository_uses_utf8_canonical_json_for_audit_hashes(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    audit_event = repository.create_audit_event(
        event_id="audit-non-ascii",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="担当者-1",
        action="document.inspected",
        scope_type="document",
        scope_id=document.document_id,
        payload={
            "actor": {"id": "担当者-1", "role": "審査"},
            "event_type": "document.inspected",
        },
    )
    hash_input = {
        "action": audit_event.action,
        "actor": audit_event.actor,
        "created_at": audit_event.created_at,
        "document_id": audit_event.document_id,
        "event_id": audit_event.event_id,
        "integrity_algorithm": audit_event.integrity_algorithm,
        "job_id": audit_event.job_id,
        "payload_json": audit_event.payload_json,
        "prev_event_hash": audit_event.prev_event_hash,
        "scope_id": audit_event.scope_id,
        "scope_type": audit_event.scope_type,
        "sequence": audit_event.sequence,
    }
    canonical = json.dumps(
        hash_input,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert "\\u" not in audit_event.payload_json
    assert audit_event.event_hash == hashlib.sha256(canonical).hexdigest()


def test_persistence_repository_rejects_nonstandard_canonical_json(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    with pytest.raises(ValueError, match="JSON compliant"):
        repository.create_job_event(
            event_id="event-nan",
            job_id=job.job_id,
            event_type="job.queued",
            actor="operator-1",
            payload={"event_type": "job.queued", "measurement": float("nan")},
        )

    with pytest.raises(ValueError, match="JSON compliant"):
        repository.create_audit_event(
            event_id="audit-infinity",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="qa-approver",
            action="document.inspected",
            scope_type="document",
            scope_id=document.document_id,
            payload={"event_type": "document.inspected", "measurement": float("inf")},
        )

    assert repository.get_job_event("event-nan") is None
    assert repository.get_audit_event("audit-infinity") is None


def test_audit_events_are_append_only_at_the_database_boundary(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    audit_event = repository.create_audit_event(
        event_id="audit-append-only",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="qa-approver",
        action="document.inspected",
        scope_type="document",
        scope_id=document.document_id,
    )

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE audit_events SET action = ? WHERE event_id = ?",
                ("document.changed", audit_event.event_id),
            )

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM audit_events WHERE event_id = ?",
                (audit_event.event_id,),
            )

    assert repository.get_audit_event(audit_event.event_id) == audit_event


def test_audit_event_scope_is_enforced_by_database_constraints(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="audit scope"):
            connection.execute(
                """
                INSERT INTO audit_events(
                    event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                    action, scope_type, scope_id, event_hash, prev_event_hash, payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "audit-missing-scope-db",
                    job.job_id,
                    document.document_id,
                    1,
                    AUDIT_INTEGRITY_ALGORITHM,
                    "qa-approver",
                    "review.approved",
                    "review_decision",
                    "decision-missing",
                    "d" * 64,
                    None,
                    "{}",
                    "2026-07-09T00:00:00+00:00",
                ),
            )

    result = _create_result(repository, job, "result-1")
    audit_event = repository.create_audit_event(
        event_id="audit-result-scope",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="qa-approver",
        action="conversion.completed",
        scope_type="conversion_result",
        scope_id=result.result_id,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="audit scope rows"):
            connection.execute(
                "DELETE FROM conversion_results WHERE result_id = ?",
                (result.result_id,),
            )

    assert repository.get_conversion_result(result.result_id) == result
    assert repository.get_audit_event(audit_event.event_id) == audit_event


def test_audit_events_reject_blank_actors_and_sequence_gaps_at_schema_boundary(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    created_at = "2026-07-09T00:00:00+00:00"

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO audit_events(
                    event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                    action, scope_type, scope_id, event_hash, prev_event_hash,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "audit-blank-actor",
                    job.job_id,
                    document.document_id,
                    1,
                    AUDIT_INTEGRITY_ALGORITHM,
                    "   ",
                    "document.uploaded",
                    "document",
                    document.document_id,
                    "0" * 64,
                    None,
                    "{}",
                    created_at,
                ),
            )

        with pytest.raises(sqlite3.IntegrityError, match="contiguous"):
            connection.execute(
                """
                INSERT INTO audit_events(
                    event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                    action, scope_type, scope_id, event_hash, prev_event_hash,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "audit-sequence-gap",
                    job.job_id,
                    document.document_id,
                    2,
                    AUDIT_INTEGRITY_ALGORITHM,
                    "operator-1",
                    "document.uploaded",
                    "document",
                    document.document_id,
                    "0" * 64,
                    None,
                    "{}",
                    created_at,
                ),
            )

    assert repository.get_audit_event("audit-blank-actor") is None
    assert repository.get_audit_event("audit-sequence-gap") is None


def test_document_and_job_audit_scopes_cannot_be_deleted_with_foreign_keys_off(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    document_audit_event = repository.create_audit_event(
        event_id="audit-document-scope",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="document.uploaded",
        scope_type="document",
        scope_id=document.document_id,
    )
    job_audit_event = repository.create_audit_event(
        event_id="audit-job-scope",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="job.queued",
        scope_type="job",
        scope_id=job.job_id,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="audit scope rows"):
            connection.execute(
                "DELETE FROM jobs WHERE job_id = ?",
                (job.job_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="audit scope rows"):
            connection.execute(
                "DELETE FROM source_documents WHERE document_id = ?",
                (document.document_id,),
            )

    assert repository.get_document(document.document_id) == document
    assert repository.get_conversion_job(job.job_id) == job
    assert repository.get_audit_event(document_audit_event.event_id) == document_audit_event
    assert repository.get_audit_event(job_audit_event.event_id) == job_audit_event


def test_audit_scope_rows_cannot_be_rekeyed_after_events_reference_them(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
    review_item = repository.create_review_item(
        review_item_id="review-item-1",
        document_id=document.document_id,
        job_id=job.job_id,
        target_path="sections[0]",
        status="open",
        severity="medium",
    )
    review_decision = repository.create_review_decision(
        decision_id="decision-1",
        review_item_id=review_item.review_item_id,
        artifact_id=artifact.artifact_id,
        actor="qa-approver",
        role="approver",
        decision="approved",
    )

    audit_events = [
        repository.create_audit_event(
            event_id="audit-result-scope",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="qa-approver",
            action="conversion.completed",
            scope_type="conversion_result",
            scope_id=result.result_id,
        ),
        repository.create_audit_event(
            event_id="audit-artifact-scope",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="qa-approver",
            action="artifact.generated",
            scope_type="artifact",
            scope_id=artifact.artifact_id,
        ),
        repository.create_audit_event(
            event_id="audit-review-item-scope",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="qa-approver",
            action="review.opened",
            scope_type="review_item",
            scope_id=review_item.review_item_id,
        ),
        repository.create_audit_event(
            event_id="audit-review-decision-scope",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="qa-approver",
            action="review.approved",
            scope_type="review_decision",
            scope_id=review_decision.decision_id,
        ),
    ]

    update_statements = (
        (
            "UPDATE conversion_results SET result_id = ? WHERE result_id = ?",
            ("result-2", result.result_id),
        ),
        (
            "UPDATE generated_artifacts SET job_id = ? WHERE artifact_id = ?",
            ("job-other", artifact.artifact_id),
        ),
        (
            "UPDATE review_items SET document_id = ? WHERE review_item_id = ?",
            ("doc-other", review_item.review_item_id),
        ),
        (
            "UPDATE review_decisions SET decision_id = ? WHERE decision_id = ?",
            ("decision-2", review_decision.decision_id),
        ),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for sql, params in update_statements:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="audit scope rows|append-only",
            ):
                connection.execute(sql, params)

    for audit_event in audit_events:
        assert repository.get_audit_event(audit_event.event_id) == audit_event


def test_audited_conversion_result_contents_are_immutable_at_database_boundary(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")
    audit_event = repository.create_audit_event(
        event_id="audit-result-scope",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="qa-approver",
        action="conversion.completed",
        scope_type="conversion_result",
        scope_id=result.result_id,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for sql, params in (
            (
                "UPDATE conversion_results SET status = ? WHERE result_id = ?",
                ("failed", result.result_id),
            ),
            (
                "UPDATE conversion_results SET content_hash = ? WHERE result_id = ?",
                ("d" * 64, result.result_id),
            ),
        ):
            with pytest.raises(
                sqlite3.IntegrityError,
                match="audit scope rows|append-only",
            ):
                connection.execute(sql, params)

    assert repository.get_conversion_result(result.result_id) == result
    assert repository.get_audit_event(audit_event.event_id) == audit_event


def test_results_backing_audited_artifacts_are_immutable_with_foreign_keys_off(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
    audit_event = repository.create_audit_event(
        event_id="audit-artifact-scope",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="qa-approver",
        action="artifact.generated",
        scope_type="artifact",
        scope_id=artifact.artifact_id,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(sqlite3.IntegrityError, match="audit scope rows"):
            connection.execute(
                "DELETE FROM conversion_results WHERE result_id = ?",
                (result.result_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="audit scope rows"):
            connection.execute(
                "UPDATE conversion_results SET content_hash = ? WHERE result_id = ?",
                ("d" * 64, result.result_id),
            )

    assert repository.get_conversion_result(result.result_id) == result
    assert repository.get_artifact(artifact.artifact_id) == artifact
    assert repository.get_audit_event(audit_event.event_id) == audit_event


def test_audited_scope_row_contents_are_immutable_at_database_boundary(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
    review_item = repository.create_review_item(
        review_item_id="review-item-1",
        document_id=document.document_id,
        job_id=job.job_id,
        target_path="sections[0]",
        status="open",
        severity="medium",
    )
    review_decision = repository.create_review_decision(
        decision_id="decision-1",
        review_item_id=review_item.review_item_id,
        artifact_id=artifact.artifact_id,
        actor="qa-approver",
        role="approver",
        decision="approved",
    )
    audit_events = [
        repository.create_audit_event(
            event_id="audit-document-scope",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action="document.uploaded",
            scope_type="document",
            scope_id=document.document_id,
        ),
        repository.create_audit_event(
            event_id="audit-job-scope",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action="job.queued",
            scope_type="job",
            scope_id=job.job_id,
        ),
        repository.create_audit_event(
            event_id="audit-artifact-scope",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="qa-approver",
            action="artifact.generated",
            scope_type="artifact",
            scope_id=artifact.artifact_id,
        ),
        repository.create_audit_event(
            event_id="audit-review-item-scope",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="qa-approver",
            action="review.opened",
            scope_type="review_item",
            scope_id=review_item.review_item_id,
        ),
        repository.create_audit_event(
            event_id="audit-review-decision-scope",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="qa-approver",
            action="review.approved",
            scope_type="review_decision",
            scope_id=review_decision.decision_id,
        ),
    ]

    update_statements = (
        (
            "UPDATE source_documents SET content_hash = ? WHERE document_id = ?",
            ("d" * 64, document.document_id),
        ),
        (
            "UPDATE source_documents SET source_storage_key = ? WHERE document_id = ?",
            ("uploads/rewritten.pdf", document.document_id),
        ),
        (
            "UPDATE jobs SET status = ?, attempts = ?, mode = ? WHERE job_id = ?",
            ("succeeded", 99, "other", job.job_id),
        ),
        (
            "UPDATE generated_artifacts SET storage_key = ? WHERE artifact_id = ?",
            ("artifacts/rewritten.docx", artifact.artifact_id),
        ),
        (
            "UPDATE generated_artifacts SET content_hash = ? WHERE artifact_id = ?",
            ("e" * 64, artifact.artifact_id),
        ),
        (
            "UPDATE review_items SET target_path = ? WHERE review_item_id = ?",
            ("sections[1]", review_item.review_item_id),
        ),
        (
            "UPDATE review_items SET severity = ? WHERE review_item_id = ?",
            ("critical", review_item.review_item_id),
        ),
        (
            "UPDATE review_decisions SET actor = ?, role = ?, decision = ? "
            "WHERE decision_id = ?",
            ("other-approver", "reviewer", "rejected", review_decision.decision_id),
        ),
        (
            "UPDATE review_decisions SET artifact_id = ? WHERE decision_id = ?",
            ("artifact-other", review_decision.decision_id),
        ),
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for sql, params in update_statements:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="audit scope rows|append-only",
            ):
                connection.execute(sql, params)

    assert repository.get_document(document.document_id) == document
    assert repository.get_conversion_job(job.job_id) == job
    assert repository.get_artifact(artifact.artifact_id) == artifact
    assert repository.get_review_item(review_item.review_item_id) == review_item
    assert repository.get_review_decision(review_decision.decision_id) == review_decision
    for audit_event in audit_events:
        assert repository.get_audit_event(audit_event.event_id) == audit_event


def test_audited_review_decision_freezes_approved_item_and_artifact(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
    review_item = repository.create_review_item(
        review_item_id="review-item-1",
        document_id=document.document_id,
        job_id=job.job_id,
        target_path="sections[0]",
        status="open",
        severity="medium",
    )
    review_decision = repository.create_review_decision(
        decision_id="decision-1",
        review_item_id=review_item.review_item_id,
        artifact_id=artifact.artifact_id,
        actor="qa-approver",
        role="approver",
        decision="approved",
    )
    audit_event = repository.create_audit_event(
        event_id="audit-review-decision-scope",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="qa-approver",
        action="review.approved",
        scope_type="review_decision",
        scope_id=review_decision.decision_id,
    )

    update_statements = (
        (
            "UPDATE review_items SET target_path = ? WHERE review_item_id = ?",
            ("sections[1]", review_item.review_item_id),
        ),
        (
            "UPDATE generated_artifacts SET content_hash = ? WHERE artifact_id = ?",
            ("d" * 64, artifact.artifact_id),
        ),
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for sql, params in update_statements:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="audit scope rows|review decision evidence",
            ):
                connection.execute(sql, params)

    assert repository.get_review_item(review_item.review_item_id) == review_item
    assert repository.get_artifact(artifact.artifact_id) == artifact
    assert repository.get_review_decision(review_decision.decision_id) == review_decision
    assert repository.get_audit_event(audit_event.event_id) == audit_event


def test_document_and_job_rows_are_frozen_by_any_audit_event_reference(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
    audit_event = repository.create_audit_event(
        event_id="audit-artifact-scope",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="qa-approver",
        action="artifact.generated",
        scope_type="artifact",
        scope_id=artifact.artifact_id,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        for sql, params in (
            (
                "UPDATE source_documents SET content_hash = ? WHERE document_id = ?",
                ("d" * 64, document.document_id),
            ),
            (
                "UPDATE jobs SET status = ? WHERE job_id = ?",
                ("succeeded", job.job_id),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="audit scope rows"):
                connection.execute(sql, params)

    assert repository.get_document(document.document_id) == document
    assert repository.get_conversion_job(job.job_id) == job
    assert repository.get_audit_event(audit_event.event_id) == audit_event


def test_lifecycle_payload_json_is_validated_at_schema_and_read_boundary(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    created_at = "2026-07-09T00:00:00+00:00"
    invalid_payload_json = "{not json}"

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO job_events(
                    event_id, job_id, sequence, event_type, actor, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "job-event-invalid-payload",
                    job.job_id,
                    1,
                    "job.queued",
                    "operator-1",
                    invalid_payload_json,
                    created_at,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO audit_events(
                    event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                    action, scope_type, scope_id, event_hash, prev_event_hash,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "audit-invalid-payload",
                    job.job_id,
                    document.document_id,
                    1,
                    AUDIT_INTEGRITY_ALGORITHM,
                    "operator-1",
                    "document.uploaded",
                    "document",
                    document.document_id,
                    "0" * 64,
                    None,
                    invalid_payload_json,
                    created_at,
                ),
            )

        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            """
            INSERT INTO job_events(
                event_id, job_id, sequence, event_type, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-event-bypassed-payload",
                job.job_id,
                1,
                "job.queued",
                "operator-1",
                invalid_payload_json,
                created_at,
            ),
        )
        event_hash = persistence._audit_event_hash(
            event_id="audit-bypassed-payload",
            job_id=job.job_id,
            document_id=document.document_id,
            sequence=1,
            integrity_algorithm=AUDIT_INTEGRITY_ALGORITHM,
            actor="operator-1",
            action="document.uploaded",
            scope_type="document",
            scope_id=document.document_id,
            prev_event_hash=None,
            payload_json=invalid_payload_json,
            created_at=created_at,
        )
        connection.execute(
            """
            INSERT INTO audit_events(
                event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                action, scope_type, scope_id, event_hash, prev_event_hash, payload_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "audit-bypassed-payload",
                job.job_id,
                document.document_id,
                1,
                AUDIT_INTEGRITY_ALGORITHM,
                "operator-1",
                "document.uploaded",
                "document",
                document.document_id,
                event_hash,
                None,
                invalid_payload_json,
                created_at,
            ),
        )

    with pytest.raises(ValueError, match="job event payload_json must be valid JSON"):
        repository.get_job_event("job-event-bypassed-payload")
    with pytest.raises(ValueError, match="audit event payload_json must be valid JSON"):
        repository.get_audit_event("audit-bypassed-payload")


def test_lifecycle_payload_json_must_be_objects_at_schema_and_read_boundary(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    created_at = "2026-07-09T00:00:00+00:00"
    non_object_payload_json = "[]"

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO job_events(
                    event_id, job_id, sequence, event_type, actor, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "job-event-array-payload",
                    job.job_id,
                    1,
                    "job.queued",
                    "operator-1",
                    non_object_payload_json,
                    created_at,
                ),
            )
        event_hash = persistence._audit_event_hash(
            event_id="audit-array-payload",
            job_id=job.job_id,
            document_id=document.document_id,
            sequence=1,
            integrity_algorithm=AUDIT_INTEGRITY_ALGORITHM,
            actor="operator-1",
            action="document.uploaded",
            scope_type="document",
            scope_id=document.document_id,
            prev_event_hash=None,
            payload_json=non_object_payload_json,
            created_at=created_at,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO audit_events(
                    event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                    action, scope_type, scope_id, event_hash, prev_event_hash,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "audit-array-payload",
                    job.job_id,
                    document.document_id,
                    1,
                    AUDIT_INTEGRITY_ALGORITHM,
                    "operator-1",
                    "document.uploaded",
                    "document",
                    document.document_id,
                    event_hash,
                    None,
                    non_object_payload_json,
                    created_at,
                ),
            )

        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            """
            INSERT INTO job_events(
                event_id, job_id, sequence, event_type, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-event-bypassed-array-payload",
                job.job_id,
                1,
                "job.queued",
                "operator-1",
                non_object_payload_json,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO audit_events(
                event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                action, scope_type, scope_id, event_hash, prev_event_hash,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "audit-bypassed-array-payload",
                job.job_id,
                document.document_id,
                1,
                AUDIT_INTEGRITY_ALGORITHM,
                "operator-1",
                "document.uploaded",
                "document",
                document.document_id,
                event_hash,
                None,
                non_object_payload_json,
                created_at,
            ),
        )

    with pytest.raises(ValueError, match="job event payload_json must be a JSON object"):
        repository.get_job_event("job-event-bypassed-array-payload")
    with pytest.raises(ValueError, match="audit event payload_json must be a JSON object"):
        repository.get_audit_event("audit-bypassed-array-payload")


def test_job_event_payload_fields_are_validated_at_read_boundary(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO job_events(
                event_id, job_id, sequence, event_type, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-event-conflicting-payload",
                job.job_id,
                1,
                "job.queued",
                "operator-1",
                json.dumps({"event_type": "job.failed"}, sort_keys=True),
                "2026-07-09T00:00:00+00:00",
            ),
        )

    with pytest.raises(ValueError, match="payload event_type"):
        repository.get_job_event("job-event-conflicting-payload")
    with pytest.raises(ValueError, match="payload event_type"):
        repository.list_job_events(job.job_id)


def test_job_event_status_is_validated_at_read_boundary(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    repository.create_job_event(
        event_id="job-event-queued",
        job_id=job.job_id,
        event_type="job.queued",
        actor="operator-1",
        payload={"event_type": "job.queued", "job_status": "queued"},
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO job_events(
                event_id, job_id, sequence, event_type, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-event-running-conflict",
                job.job_id,
                2,
                "job.running",
                "operator-1",
                json.dumps(
                    {"event_type": "job.running", "job_status": "failed"},
                    sort_keys=True,
                ),
                "2026-07-10T00:00:00+00:00",
            ),
        )

    with pytest.raises(ValueError, match="job_status"):
        repository.get_job_event("job-event-running-conflict")
    with pytest.raises(ValueError, match="job_status"):
        repository.list_job_events(job.job_id)


def test_audit_chain_verification_rejects_missing_scope_rows(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    audit_event = repository.create_audit_event(
        event_id="audit-document",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="document.uploaded",
        scope_type="document",
        scope_id=document.document_id,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TRIGGER source_documents_audit_scope_no_delete")
        connection.execute("DROP TRIGGER source_documents_parent_no_delete")
        connection.execute(
            "DELETE FROM source_documents WHERE document_id = ?",
            (document.document_id,),
        )

    with pytest.raises(ValueError, match="audit scope_id"):
        repository.get_audit_event(audit_event.event_id)


def test_source_document_provenance_fields_are_non_empty_at_schema_boundary(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    created_at = "2026-07-09T00:00:00+00:00"

    for field_name in ("document_id", "source_artifact_id", "source_storage_key"):
        values = {
            "document_id": f"doc-{field_name}",
            "source_type": "pdf",
            "original_filename": "source.pdf",
            "source_artifact_id": f"source-artifact-{field_name}",
            "source_storage_key": f"uploads/{field_name}.pdf",
            "content_hash": VALID_HASH,
            "status": "uploaded",
            "uploaded_by": "operator-1",
            "created_at": created_at,
            "updated_at": created_at,
        }
        values[field_name] = ""
        with sqlite3.connect(db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO source_documents(
                        document_id, source_type, original_filename, source_artifact_id,
                        source_storage_key, content_hash, status, uploaded_by,
                        created_at, updated_at
                    ) VALUES (
                        :document_id, :source_type, :original_filename,
                        :source_artifact_id, :source_storage_key, :content_hash,
                        :status, :uploaded_by, :created_at, :updated_at
                    )
                    """,
                    values,
                )


def test_lifecycle_and_artifact_text_contracts_are_enforced_at_schema_boundary(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")
    created_at = "2026-07-09T00:00:00+00:00"

    cases = (
        (
            """
            INSERT INTO jobs(
                job_id, document_id, idempotency_key, mode, status, attempts,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-blank-mode",
                document.document_id,
                "upload-blank-mode",
                "",
                "queued",
                0,
                created_at,
                created_at,
            ),
        ),
        (
            """
            INSERT INTO generated_artifacts(
                artifact_id, result_id, job_id, document_id, category, format,
                storage_key, content_hash, retention_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "artifact-blank-storage",
                result.result_id,
                job.job_id,
                document.document_id,
                "generated",
                "docx",
                "",
                "c" * 64,
                "active",
                created_at,
                created_at,
            ),
        ),
        (
            """
            INSERT INTO job_events(
                event_id, job_id, sequence, event_type, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-event-blob-payload",
                job.job_id,
                1,
                "job.queued",
                "operator-1",
                sqlite3.Binary(b"{}"),
                created_at,
            ),
        ),
        (
            """
            INSERT INTO conversion_results(
                result_id, job_id, document_id, status, content_hash,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "result-blob-hash",
                job.job_id,
                document.document_id,
                "succeeded",
                sqlite3.Binary(b"b" * 64),
                created_at,
                created_at,
            ),
        ),
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for sql, params in cases:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, params)


def test_audit_event_sequences_must_be_stored_as_integers(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    created_at = "2026-07-09T00:00:00+00:00"
    payload_json = persistence._canonical_json(
        {
            "action": "document.uploaded",
            "actor": "operator-1",
            "scope_id": document.document_id,
            "scope_type": "document",
        }
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO audit_events(
                    event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                    action, scope_type, scope_id, event_hash, prev_event_hash,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "audit-real-sequence",
                    job.job_id,
                    document.document_id,
                    1.5,
                    AUDIT_INTEGRITY_ALGORITHM,
                    "operator-1",
                    "document.uploaded",
                    "document",
                    document.document_id,
                    "0" * 64,
                    None,
                    payload_json,
                    created_at,
                ),
        )

        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("DROP TRIGGER audit_events_contiguous_sequence_insert")
        event_hash = persistence._audit_event_hash(
            event_id="audit-bypassed-real-sequence",
            job_id=job.job_id,
            document_id=document.document_id,
            sequence=1,
            integrity_algorithm=AUDIT_INTEGRITY_ALGORITHM,
            actor="operator-1",
            action="document.uploaded",
            scope_type="document",
            scope_id=document.document_id,
            prev_event_hash=None,
            payload_json=payload_json,
            created_at=created_at,
        )
        connection.execute(
            """
            INSERT INTO audit_events(
                event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                action, scope_type, scope_id, event_hash, prev_event_hash, payload_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "audit-bypassed-real-sequence",
                job.job_id,
                document.document_id,
                1.5,
                AUDIT_INTEGRITY_ALGORITHM,
                "operator-1",
                "document.uploaded",
                "document",
                document.document_id,
                event_hash,
                None,
                payload_json,
                created_at,
            ),
        )

    with pytest.raises(ValueError, match="audit event chain integrity"):
        repository.get_audit_event("audit-bypassed-real-sequence")


def test_audit_event_reads_verify_the_stored_hash_chain(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    audit_event = repository.create_audit_event(
        event_id="audit-readable",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="qa-approver",
        action="document.inspected",
        scope_type="document",
        scope_id=document.document_id,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER audit_events_no_update")
        connection.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = ?",
            (persistence._canonical_json({"tampered": True}), audit_event.event_id),
        )

    with pytest.raises(ValueError, match="audit event chain integrity"):
        repository.get_audit_event(audit_event.event_id)


def test_audit_event_reads_reject_hash_consistent_payload_mismatches(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    created_at = "2026-07-09T00:00:00+00:00"
    payload_json = persistence._canonical_json(
        {
            "actor_id": "other-operator",
            "event_type": "job.failed",
        }
    )
    event_hash = persistence._audit_event_hash(
        event_id="audit-conflicting-payload",
        job_id=job.job_id,
        document_id=document.document_id,
        sequence=1,
        integrity_algorithm=AUDIT_INTEGRITY_ALGORITHM,
        actor="operator-1",
        action="document.uploaded",
        scope_type="document",
        scope_id=document.document_id,
        prev_event_hash=None,
        payload_json=payload_json,
        created_at=created_at,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO audit_events(
                event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                action, scope_type, scope_id, event_hash, prev_event_hash,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "audit-conflicting-payload",
                job.job_id,
                document.document_id,
                1,
                AUDIT_INTEGRITY_ALGORITHM,
                "operator-1",
                "document.uploaded",
                "document",
                document.document_id,
                event_hash,
                None,
                payload_json,
                created_at,
            ),
        )

    with pytest.raises(ValueError, match="payload"):
        repository.get_audit_event("audit-conflicting-payload")


def test_persistence_repository_rejects_scoped_audit_payload_alias_mismatches(
    tmp_path,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
    review_item = repository.create_review_item(
        review_item_id="review-item-1",
        document_id=document.document_id,
        job_id=job.job_id,
        target_path="sections[0]",
        status="open",
        severity="medium",
    )
    review_decision = repository.create_review_decision(
        decision_id="decision-1",
        review_item_id=review_item.review_item_id,
        artifact_id=artifact.artifact_id,
        actor="qa-approver",
        role="approver",
        decision="approved",
    )

    for event_id, scope_type, scope_id, alias_field, alias_value in (
        (
            "audit-review-decision-alias",
            "review_decision",
            review_decision.decision_id,
            "review_decision_id",
            "decision-2",
        ),
        (
            "audit-artifact-alias",
            "artifact",
            artifact.artifact_id,
            "artifact_id",
            "artifact-2",
        ),
        (
            "audit-job-event-alias",
            "conversion_result",
            result.result_id,
            "conversion_result_id",
            "result-2",
        ),
    ):
        with pytest.raises(ValueError, match=f"payload {alias_field}"):
            repository.create_audit_event(
                event_id=event_id,
                job_id=job.job_id,
                document_id=document.document_id,
                actor="qa-approver",
                action="review.approved",
                scope_type=scope_type,
                scope_id=scope_id,
                payload={"event_type": "review.approved", alias_field: alias_value},
            )
        assert repository.get_audit_event(event_id) is None


def test_audit_event_reads_reject_hash_consistent_noncanonical_payload_json(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    created_at = "2026-07-09T00:00:00+00:00"
    payload = {
        "scope_type": "document",
        "actor": "operator-1",
        "scope_id": document.document_id,
        "action": "document.uploaded",
    }
    payload_json = json.dumps(payload, indent=2)
    assert payload_json != persistence._canonical_json(payload)
    event_hash = persistence._audit_event_hash(
        event_id="audit-noncanonical-payload",
        job_id=job.job_id,
        document_id=document.document_id,
        sequence=1,
        integrity_algorithm=AUDIT_INTEGRITY_ALGORITHM,
        actor="operator-1",
        action="document.uploaded",
        scope_type="document",
        scope_id=document.document_id,
        prev_event_hash=None,
        payload_json=payload_json,
        created_at=created_at,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO audit_events(
                event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                action, scope_type, scope_id, event_hash, prev_event_hash,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "audit-noncanonical-payload",
                job.job_id,
                document.document_id,
                1,
                AUDIT_INTEGRITY_ALGORITHM,
                "operator-1",
                "document.uploaded",
                "document",
                document.document_id,
                event_hash,
                None,
                payload_json,
                created_at,
            ),
        )

    with pytest.raises(ValueError, match="canonical JSON"):
        repository.get_audit_event("audit-noncanonical-payload")


def test_audit_event_timestamps_are_sampled_after_the_write_lock(
    tmp_path,
    monkeypatch,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    lock_state = {"immediate_scope_open": False}
    original_connection_scope = SQLitePersistenceRepository._connection_scope

    @contextmanager
    def tracked_connection_scope(
        self: SQLitePersistenceRepository,
        *,
        immediate: bool = False,
    ):
        with original_connection_scope(self, immediate=immediate) as connection:
            previous = lock_state["immediate_scope_open"]
            if immediate:
                lock_state["immediate_scope_open"] = True
            try:
                yield connection
            finally:
                lock_state["immediate_scope_open"] = previous

    def locked_timestamp() -> str:
        assert lock_state["immediate_scope_open"]
        return "2026-07-09T00:00:00+00:00"

    monkeypatch.setattr(SQLitePersistenceRepository, "_connection_scope", tracked_connection_scope)
    monkeypatch.setattr(persistence, "_utc_now", locked_timestamp)

    audit_event = repository.create_audit_event(
        event_id="audit-after-lock",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="qa-approver",
        action="document.inspected",
        scope_type="document",
        scope_id=document.document_id,
    )

    assert audit_event.created_at == "2026-07-09T00:00:00+00:00"


def test_audit_hash_chain_is_global_across_jobs(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document_a = _create_document(repository, "doc-a")
    document_b = _create_document(repository, "doc-b")
    job_a = _create_job(repository, document_a, "job-a")
    job_b = _create_job(repository, document_b, "job-b")

    first = repository.create_audit_event(
        event_id="audit-job-a",
        job_id=job_a.job_id,
        document_id=document_a.document_id,
        actor="operator-1",
        action="document.inspected",
        scope_type="document",
        scope_id=document_a.document_id,
    )
    second = repository.create_audit_event(
        event_id="audit-job-b",
        job_id=job_b.job_id,
        document_id=document_b.document_id,
        actor="operator-2",
        action="document.inspected",
        scope_type="document",
        scope_id=document_b.document_id,
    )

    assert second.sequence == first.sequence + 1
    assert second.prev_event_hash == first.event_hash
    assert repository.get_audit_event(first.event_id) == first
    assert repository.get_audit_event(second.event_id) == second


def test_review_decision_scope_is_enforced_by_database_constraints(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()

    document_a = _create_document(repository, "doc-a")
    document_b = _create_document(repository, "doc-b")
    job_a = _create_job(repository, document_a, "job-a")
    job_b = _create_job(repository, document_b, "job-b")
    result_a = _create_result(repository, job_a, "result-a")
    result_b = _create_result(repository, job_b, "result-b")
    artifact_b = _create_artifact(repository, result_b, "artifact-b")
    review_item_a = repository.create_review_item(
        review_item_id="review-item-a",
        document_id=document_a.document_id,
        job_id=job_a.job_id,
        target_path="sections[0]",
        status="open",
        severity="medium",
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO review_decisions(
                    decision_id, review_item_id, artifact_id, job_id, document_id,
                    actor, role, decision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "decision-mixed-db",
                    review_item_a.review_item_id,
                    artifact_b.artifact_id,
                    job_a.job_id,
                    document_a.document_id,
                    "qa-approver",
                    "approver",
                    "approved",
                    "2026-07-09T00:00:00+00:00",
                    "2026-07-09T00:00:00+00:00",
                ),
            )

    assert repository.get_conversion_result(result_a.result_id) == result_a
    assert repository.get_review_decision("decision-mixed-db") is None


def test_review_decision_audit_scope_binds_actor_and_action(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
    review_item = repository.create_review_item(
        review_item_id="review-item-1",
        document_id=document.document_id,
        job_id=job.job_id,
        target_path="sections[0]",
        status="open",
        severity="medium",
    )
    review_decision = repository.create_review_decision(
        decision_id="decision-1",
        review_item_id=review_item.review_item_id,
        artifact_id=artifact.artifact_id,
        actor="qa-approver",
        role="approver",
        decision="approved",
    )

    for event_id, actor, action, message in (
        ("audit-wrong-actor", "mallory", "review.approved", "actor"),
        ("audit-wrong-action", "qa-approver", "review.rejected", "action"),
    ):
        with pytest.raises(ValueError, match=message):
            repository.create_audit_event(
                event_id=event_id,
                job_id=job.job_id,
                document_id=document.document_id,
                actor=actor,
                action=action,
                scope_type="review_decision",
                scope_id=review_decision.decision_id,
                payload={"event_type": action},
            )

    with pytest.raises(ValueError, match="role"):
        repository.create_audit_event(
            event_id="audit-wrong-role",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="qa-approver",
            action="review.approved",
            scope_type="review_decision",
            scope_id=review_decision.decision_id,
            payload={
                "actor": {"id": "qa-approver", "role": "viewer"},
                "event_type": "review.approved",
            },
        )

    for alias in ("decision", "review_decision", "outcome"):
        with pytest.raises(ValueError, match=alias):
            repository.create_audit_event(
                event_id=f"audit-wrong-{alias}",
                job_id=job.job_id,
                document_id=document.document_id,
                actor="qa-approver",
                action="review.approved",
                scope_type="review_decision",
                scope_id=review_decision.decision_id,
                payload={"event_type": "review.approved", alias: "rejected"},
            )


def test_audit_scopes_bind_authoritative_lifecycle_semantics(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    job_event = repository.create_job_event(
        event_id="job-event-1",
        job_id=job.job_id,
        event_type="job.queued",
        actor="operator-1",
        payload={"event_type": "job.queued"},
    )
    retry_job_event = repository.create_job_event(
        event_id="job-event-retry",
        job_id=job.job_id,
        event_type="retry_conversion",
        actor="operator-1",
        payload={"event_type": "retry_conversion"},
    )
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
    review_item = repository.create_review_item(
        review_item_id="review-item-1",
        document_id=document.document_id,
        job_id=job.job_id,
        target_path="sections[0]",
        status="open",
        severity="medium",
    )

    cases = (
        (
            "audit-wrong-uploader",
            "mallory",
            "document.uploaded",
            "source_artifact",
            document.source_artifact_id,
            None,
            "uploader",
        ),
        (
            "audit-wrong-job-event-actor",
            "mallory",
            "job.queued",
            "job_event",
            job_event.event_id,
            {"event_type": "job.queued"},
            "job event actor",
        ),
        (
            "audit-wrong-job-event-type",
            "operator-1",
            "job.failed",
            "job_event",
            job_event.event_id,
            {"event_type": "job.failed"},
            "job event type",
        ),
        (
            "audit-retry-queued-job",
            "operator-1",
            "retry_conversion",
            "job",
            job.job_id,
            None,
            "job status",
        ),
        (
            "audit-retry-queued-job-event",
            "operator-1",
            "retry_conversion",
            "job_event",
            retry_job_event.event_id,
            {"event_type": "conversion_job.action_requested"},
            "job status",
        ),
        (
            "audit-job-upload-wrong-actor",
            "mallory",
            "desktop_upload",
            "job",
            job.job_id,
            {
                "event_type": "desktop.job_operation",
                "filename": document.original_filename,
                "source_sha256": document.content_hash,
            },
            "uploader",
        ),
        (
            "audit-job-upload-wrong-filename",
            "operator-1",
            "desktop_upload",
            "job",
            job.job_id,
            {
                "event_type": "desktop.job_operation",
                "filename": "wrong.pdf",
                "source_sha256": document.content_hash,
            },
            "filename",
        ),
        (
            "audit-document-upload-wrong-source-hash",
            "operator-1",
            "desktop_upload",
            "document",
            document.document_id,
            {
                "event_type": "desktop.job_operation",
                "source_sha256": "0" * 64,
            },
            "source_sha256",
        ),
        (
            "audit-wrong-job-status-payload",
            "operator-1",
            "job.queued",
            "job",
            job.job_id,
            {"event_type": "job.queued", "job_status": "failed"},
            "job_status",
        ),
        (
            "audit-wrong-result-action",
            "operator-1",
            "conversion.failed",
            "conversion_result",
            result.result_id,
            None,
            "result status",
        ),
        (
            "audit-wrong-result-status-payload",
            "operator-1",
            "conversion.completed",
            "conversion_result",
            result.result_id,
            {"event_type": "conversion.completed", "result_status": "failed"},
            "result_status",
        ),
        (
            "audit-wrong-result-hash",
            "operator-1",
            "conversion.completed",
            "conversion_result",
            result.result_id,
            {"event_type": "conversion.completed", "content_hash": "0" * 64},
            "content_hash",
        ),
        (
            "audit-wrong-artifact-storage",
            "operator-1",
            "artifact.generated",
            "artifact",
            artifact.artifact_id,
            {
                "event_type": "artifact.generated",
                "storage_key": "artifacts/wrong.docx",
            },
            "storage_key",
        ),
        (
            "audit-wrong-artifact-hash",
            "operator-1",
            "artifact.generated",
            "generated_artifact",
            artifact.artifact_id,
            {"event_type": "artifact.generated", "content_hash": "0" * 64},
            "content_hash",
        ),
        (
            "audit-wrong-review-target",
            "operator-1",
            "review.opened",
            "review_item",
            review_item.review_item_id,
            {"event_type": "review.opened", "target_path": "sections[9]"},
            "target_path",
        ),
        (
            "audit-wrong-review-status",
            "operator-1",
            "review.opened",
            "review_item",
            review_item.review_item_id,
            {"event_type": "review.opened", "status": "closed"},
            "status",
        ),
        (
            "audit-cross-scope-evidence",
            "operator-1",
            "job.queued",
            "job",
            job.job_id,
            {"event_type": "job.queued", "content_hash": result.content_hash},
            "content_hash",
        ),
    )
    for event_id, actor, action, scope_type, scope_id, payload, message in cases:
        with pytest.raises(ValueError, match=message):
            repository.create_audit_event(
                event_id=event_id,
                job_id=job.job_id,
                document_id=document.document_id,
                actor=actor,
                action=action,
                scope_type=scope_type,
                scope_id=scope_id,
                payload=payload,
            )
        assert repository.get_audit_event(event_id) is None

    valid_artifact_audit = repository.create_audit_event(
        event_id="audit-valid-artifact-contract",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="artifact.generated",
        scope_type="artifact",
        scope_id=artifact.artifact_id,
        payload={
            "event_type": "artifact.generated",
            "result_id": artifact.result_id,
            "storage_key": artifact.storage_key,
            "content_hash": artifact.content_hash,
            "category": artifact.category,
            "format": artifact.format,
            "retention_state": artifact.retention_state,
        },
    )
    assert repository.get_audit_event(valid_artifact_audit.event_id) == valid_artifact_audit

    valid_desktop_audit = repository.create_audit_event(
        event_id="audit-valid-desktop-job-contract",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="desktop_upload",
        scope_type="job",
        scope_id=job.job_id,
        payload={
            "event_type": "desktop.job_operation",
            "job_id": job.job_id,
            "job_status": job.status,
            "action": "desktop_upload",
            "filename": document.original_filename,
            "mode": job.mode,
            "source_sha256": document.content_hash,
            "size_bytes": 123,
            "content_type": "application/pdf",
        },
    )
    assert repository.get_audit_event(valid_desktop_audit.event_id) == valid_desktop_audit

    valid_desktop_download = repository.create_audit_event(
        event_id="audit-valid-desktop-download-contract",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="desktop_result_download",
        scope_type="job",
        scope_id=job.job_id,
        payload={
            "event_type": "desktop.job_operation",
            "job_id": job.job_id,
            "job_status": job.status,
            "action": "desktop_result_download",
            "filename": document.original_filename,
            "download_filename": "artifact-1.docx",
            "source_sha256": document.content_hash,
            "output_sha256": artifact.content_hash,
        },
    )
    assert (
        repository.get_audit_event(valid_desktop_download.event_id)
        == valid_desktop_download
    )


def test_all_declared_audit_scope_payload_aliases_are_enforced(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    job_event = repository.create_job_event(
        event_id="job-event-1",
        job_id=job.job_id,
        event_type="job.queued",
        actor="operator-1",
    )
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
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
    scopes = {
        "document": (document.document_id, "operator-1", "document.uploaded"),
        "source_document": (document.document_id, "operator-1", "document.uploaded"),
        "source_artifact": (
            document.source_artifact_id,
            "operator-1",
            "document.uploaded",
        ),
        "job": (job.job_id, "operator-1", "job.queued"),
        "conversion_job": (job.job_id, "operator-1", "job.queued"),
        "job_event": (job_event.event_id, "operator-1", "job.queued"),
        "conversion_result": (result.result_id, "operator-1", "conversion.completed"),
        "artifact": (artifact.artifact_id, "operator-1", "artifact.generated"),
        "generated_artifact": (
            artifact.artifact_id,
            "operator-1",
            "artifact.generated",
        ),
        "review_item": (review_item.review_item_id, "operator-1", "review.opened"),
        "review_decision": (
            decision.decision_id,
            "qa-approver",
            "review.approved",
        ),
    }

    for scope_type, bindings in persistence._AUDIT_SCOPE_PAYLOAD_BINDINGS.items():
        scope_id, actor, action = scopes[scope_type]
        for aliases, _, _ in bindings:
            for alias in aliases:
                event_id = f"audit-{scope_type}-{alias}"
                with pytest.raises(ValueError, match=alias):
                    repository.create_audit_event(
                        event_id=event_id,
                        job_id=job.job_id,
                        document_id=document.document_id,
                        actor=actor,
                        action=action,
                        scope_type=scope_type,
                        scope_id=scope_id,
                        payload={"event_type": action, alias: "definitely-wrong"},
                    )
                assert repository.get_audit_event(event_id) is None


def test_create_audit_event_rejects_existing_corrupt_chain_before_append(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    created_at = "2026-07-09T00:00:00+00:00"
    payload_json = persistence._canonical_json(
        {
            "action": "document.uploaded",
            "actor": "operator-1",
            "scope_id": document.document_id,
            "scope_type": "document",
        }
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO audit_events(
                event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                action, scope_type, scope_id, event_hash, prev_event_hash,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "audit-corrupt",
                job.job_id,
                document.document_id,
                1,
                AUDIT_INTEGRITY_ALGORITHM,
                "operator-1",
                "document.uploaded",
                "document",
                document.document_id,
                "0" * 64,
                None,
                payload_json,
                created_at,
            ),
        )

    with pytest.raises(ValueError, match="audit event chain integrity"):
        repository.create_audit_event(
            event_id="audit-after-corrupt",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action="document.uploaded",
            scope_type="document",
            scope_id=document.document_id,
        )

    assert repository.get_audit_event("audit-after-corrupt") is None


def test_audit_event_reads_revalidate_scope_semantics(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    created_at = "2026-07-10T00:00:00+00:00"
    payload_json = persistence._canonical_json(
        {
            "action": "document.uploaded",
            "actor": "mallory",
            "scope_id": document.source_artifact_id,
            "scope_type": "source_artifact",
        }
    )
    event_hash = persistence._audit_event_hash(
        event_id="audit-wrong-uploader-direct",
        job_id=job.job_id,
        document_id=document.document_id,
        sequence=1,
        integrity_algorithm=AUDIT_INTEGRITY_ALGORITHM,
        actor="mallory",
        action="document.uploaded",
        scope_type="source_artifact",
        scope_id=document.source_artifact_id,
        prev_event_hash=None,
        payload_json=payload_json,
        created_at=created_at,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO audit_events(
                event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                action, scope_type, scope_id, event_hash, prev_event_hash,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "audit-wrong-uploader-direct",
                job.job_id,
                document.document_id,
                1,
                AUDIT_INTEGRITY_ALGORITHM,
                "mallory",
                "document.uploaded",
                "source_artifact",
                document.source_artifact_id,
                event_hash,
                None,
                payload_json,
                created_at,
            ),
        )

    with pytest.raises(ValueError, match="uploader"):
        repository.get_audit_event("audit-wrong-uploader-direct")


def test_audit_prev_hash_is_enforced_at_database_append_boundary(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    repository.create_audit_event(
        event_id="audit-1",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="document.inspected",
        scope_type="document",
        scope_id=document.document_id,
    )
    created_at = "2026-07-10T00:00:00+00:00"
    wrong_prev_hash = "0" * 64
    payload_json = persistence._canonical_json(
        {
            "action": "document.inspected",
            "actor": "operator-1",
            "scope_id": document.document_id,
            "scope_type": "document",
        }
    )
    event_hash = persistence._audit_event_hash(
        event_id="audit-2",
        job_id=job.job_id,
        document_id=document.document_id,
        sequence=2,
        integrity_algorithm=AUDIT_INTEGRITY_ALGORITHM,
        actor="operator-1",
        action="document.inspected",
        scope_type="document",
        scope_id=document.document_id,
        prev_event_hash=wrong_prev_hash,
        payload_json=payload_json,
        created_at=created_at,
    )

    with sqlite3.connect(db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="prev hash"):
            connection.execute(
                """
                INSERT INTO audit_events(
                    event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                    action, scope_type, scope_id, event_hash, prev_event_hash,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "audit-2",
                    job.job_id,
                    document.document_id,
                    2,
                    AUDIT_INTEGRITY_ALGORITHM,
                    "operator-1",
                    "document.inspected",
                    "document",
                    document.document_id,
                    event_hash,
                    wrong_prev_hash,
                    payload_json,
                    created_at,
                ),
            )


def test_relative_database_path_must_stay_under_repo_root(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    escaped_name = f"outside-{tmp_path.name}.sqlite3"
    repository = SQLitePersistenceRepository(f"../../{escaped_name}")

    with pytest.raises(ValueError, match="repository root"):
        repository.initialize()

    assert not (tmp_path.parent.parent / escaped_name).exists()


def test_database_path_is_validated_for_reads_after_construction(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    escaped_name = f"outside-read-{tmp_path.name}.sqlite3"
    repository = SQLitePersistenceRepository(f"../../{escaped_name}")

    with pytest.raises(ValueError, match="repository root"):
        repository.get_document("doc-1")

    assert not (tmp_path.parent.parent / escaped_name).exists()


def test_persistence_repository_transaction_rolls_back_partial_writes(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    existing_document = _create_document(repository, "doc-existing")
    _create_job(
        repository,
        existing_document,
        "job-existing",
        idempotency_key="duplicate-upload",
    )

    with pytest.raises(sqlite3.IntegrityError):
        with repository.transaction() as transaction:
            rollback_document = _create_document(transaction, "doc-rollback")
            transaction.create_conversion_job(
                job_id="job-rollback",
                document_id=rollback_document.document_id,
                idempotency_key="duplicate-upload",
                mode="standard",
                status="queued",
            )

    assert repository.get_document("doc-rollback") is None
    assert repository.get_conversion_job("job-rollback") is None


def test_persistence_repository_transaction_handle_is_invalid_after_exit(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()

    with repository.transaction() as transaction:
        _create_document(transaction, "doc-in-transaction")

    with pytest.raises(RuntimeError, match="transaction is closed"):
        _create_document(transaction, "doc-after-exit")

    assert repository.get_document("doc-in-transaction") is not None
    assert repository.get_document("doc-after-exit") is None


def test_persistence_repository_reset_reinitializes_schema(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    repository.create_document(
        document_id="doc-1",
        source_type="pdf",
        original_filename="batch-record.pdf",
        source_artifact_id="source-artifact-1",
        source_storage_key="uploads/batch-record.pdf",
        content_hash=VALID_HASH,
        status="uploaded",
        uploaded_by="operator-1",
    )

    repository.reset()

    assert repository.get_document("doc-1") is None
