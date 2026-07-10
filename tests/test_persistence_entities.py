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
@pytest.mark.parametrize(
    ("method_name", "attempts"),
    (
        ("create_conversion_job", False),
        ("create_conversion_job", True),
        ("create_conversion_job", 1.0),
        ("create_or_get_conversion_job", False),
        ("create_or_get_conversion_job", True),
        ("create_or_get_conversion_job", 1.0),
    ),
)
def test_job_creation_requires_integer_attempt_counts(
    tmp_path,
    method_name,
    attempts,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")

    with pytest.raises(ValueError, match="non-negative integer"):
        getattr(repository, method_name)(
            job_id="job-invalid-attempts",
            document_id=document.document_id,
            idempotency_key="upload-invalid-attempts",
            mode="standard",
            status="queued",
            attempts=attempts,
        )

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

def test_source_artifacts_require_repository_guard_with_foreign_keys_off(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        with pytest.raises(sqlite3.DatabaseError, match="veridoc_source"):
            connection.execute(
                """
                INSERT INTO source_artifacts(
                    artifact_id, document_id, storage_key, content_hash, source_type,
                    original_filename, uploaded_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "source-artifact-orphan",
                    "doc-orphan",
                    "uploads/orphan.pdf",
                    VALID_HASH,
                    "pdf",
                    "orphan.pdf",
                    "operator-1",
                    "2026-07-09T00:00:00+00:00",
                ),
            )

    assert repository.get_source_artifact("source-artifact-orphan") is None
    document = _create_document(repository, "doc-1")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE name = 'source_artifact_insert_intents'
            """
        ).fetchone() is None
    assert repository.get_source_artifact(document.source_artifact_id) is not None

def test_source_artifacts_reject_orphan_documents_at_database_boundary(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()

    with pytest.raises(sqlite3.DatabaseError):
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

    with pytest.raises(sqlite3.DatabaseError):
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
            display_filename="mixed.docx",
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
                display_filename, storage_key, content_hash, retention_state,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "artifact-mixed",
                result_a.result_id,
                job_b.job_id,
                document_b.document_id,
                "generated",
                "docx",
                "mixed.docx",
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

def test_job_identity_and_source_binding_are_immutable_before_child_rows(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document_a = _create_document(repository, "doc-a")
    document_b = _create_document(repository, "doc-b")
    job = _create_job(repository, document_a, "job-1")

    statements = (
        ("UPDATE jobs SET job_id = ? WHERE job_id = ?", ("job-rebound", job.job_id)),
        (
            "UPDATE jobs SET document_id = ? WHERE job_id = ?",
            (document_b.document_id, job.job_id),
        ),
        (
            "UPDATE jobs SET idempotency_key = ? WHERE job_id = ?",
            ("replacement-key", job.job_id),
        ),
        (
            "UPDATE jobs SET mode = ? WHERE job_id = ?",
            ("high_quality", job.job_id),
        ),
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        for sql, params in statements:
            with pytest.raises(sqlite3.IntegrityError, match="job identity"):
                connection.execute(sql, params)

    assert repository.get_conversion_job(job.job_id) == job

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
            display_filename="generated.docx",
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
            display_filename="artifact-unique.docx",
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
                display_filename, storage_key, content_hash, retention_state,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "artifact-blank-storage",
                result.result_id,
                job.job_id,
                document.document_id,
                "generated",
                "docx",
                "artifact-blank-storage.docx",
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

@pytest.mark.parametrize(
    ("status", "attempts", "message"),
    (
        ("succeeded", 0, "current job status"),
        ("queued", 1, "job attempt"),
    ),
)
def test_desktop_upload_contract_requires_an_unattempted_queued_job(
    tmp_path,
    status,
    attempts,
    message,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = repository.create_conversion_job(
        job_id="job-1",
        document_id=document.document_id,
        idempotency_key="upload-job-1",
        mode="standard",
        status=status,
        attempts=attempts,
    )
    payload = {
        "event_type": "desktop.job_operation",
        "action": "desktop_upload",
        "filename": document.original_filename,
        "source_sha256": document.content_hash,
    }

    with pytest.raises(ValueError, match=message):
        repository.create_job_event(
            event_id="job-event-late-upload",
            job_id=job.job_id,
            event_type="desktop.job_operation",
            actor="operator-1",
            payload=payload,
        )
    with pytest.raises(ValueError, match=message):
        repository.create_audit_event(
            event_id="audit-late-upload",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action="desktop_upload",
            scope_type="job",
            scope_id=job.job_id,
            payload=payload,
        )

@pytest.mark.parametrize("field_name", ("saved_filename", "download_proof"))
def test_ephemeral_desktop_fields_never_enter_authoritative_history(
    tmp_path,
    field_name,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = repository.create_conversion_job(
        job_id="job-1",
        document_id=document.document_id,
        idempotency_key="upload-job-1",
        mode="standard",
        status="succeeded",
    )
    payload = {
        "event_type": "desktop.job_operation",
        "action": "desktop_result_download",
        field_name: "../untrusted.docx",
    }

    with pytest.raises(ValueError, match=field_name):
        repository.create_job_event(
            event_id=f"job-event-ephemeral-{field_name}",
            job_id=job.job_id,
            event_type="desktop.job_operation",
            actor="operator-1",
            payload=payload,
        )
    with pytest.raises(ValueError, match=field_name):
        repository.create_audit_event(
            event_id=f"audit-ephemeral-{field_name}",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action="desktop_result_download",
            scope_type="job",
            scope_id=job.job_id,
            payload=payload,
        )

def test_evidence_triggers_reject_failed_result_artifacts(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = repository.create_conversion_job(
        job_id="job-1",
        document_id=document.document_id,
        idempotency_key="upload-job-1",
        mode="standard",
        status="succeeded",
    )
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
    created_at = "2026-07-10T00:00:00+00:00"
    audit_payload_json = persistence._canonical_json(
        {
            "action": "desktop_result_download",
            "actor": "operator-1",
            "download_filename": artifact.display_filename,
            "evidence": {
                "artifact_ids": [artifact.artifact_id],
                "type": "download_artifact",
            },
            "event_type": "desktop.job_operation",
            "output_sha256": artifact.content_hash,
            "scope_id": job.job_id,
            "scope_type": "job",
        }
    )
    job_payload_json = persistence._canonical_json(
        {
            "action": "desktop_result_download",
            "actor": "operator-1",
            "download_filename": artifact.display_filename,
            "evidence": {
                "artifact_ids": [artifact.artifact_id],
                "type": "download_artifact",
            },
            "event_type": "desktop.job_operation",
            "job_status": "succeeded",
            "output_sha256": artifact.content_hash,
        }
    )
    audit_hash = persistence._audit_event_hash(
        event_id="audit-failed-result",
        job_id=job.job_id,
        document_id=document.document_id,
        sequence=1,
        integrity_algorithm=AUDIT_INTEGRITY_ALGORITHM,
        actor="operator-1",
        action="desktop_result_download",
        scope_type="job",
        scope_id=job.job_id,
        prev_event_hash=None,
        payload_json=audit_payload_json,
        created_at=created_at,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE conversion_results SET status = 'failed' WHERE result_id = ?",
            (result.result_id,),
        )
        connection.execute(
            """
            INSERT INTO job_events(
                event_id, job_id, sequence, event_type, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-event-failed-result",
                job.job_id,
                1,
                "desktop.job_operation",
                "operator-1",
                job_payload_json,
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
                "audit-failed-result",
                job.job_id,
                document.document_id,
                1,
                AUDIT_INTEGRITY_ALGORITHM,
                "operator-1",
                "desktop_result_download",
                "job",
                job.job_id,
                audit_hash,
                None,
                audit_payload_json,
                created_at,
            ),
        )
        for sql, event_id, error_match in (
            (
                """
                INSERT INTO job_event_evidence(event_id, artifact_id, evidence_type)
                VALUES (?, ?, ?)
                """,
                "job-event-failed-result",
                "job event evidence",
            ),
            (
                """
                INSERT INTO audit_event_evidence(event_id, artifact_id, evidence_type)
                VALUES (?, ?, ?)
                """,
                "audit-failed-result",
                "audit evidence",
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError, match=error_match):
                connection.execute(
                    sql,
                    (event_id, artifact.artifact_id, "download_artifact"),
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
