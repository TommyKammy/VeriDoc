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
        actor="system",
        action="job.queued",
        scope_type="job_event",
        scope_id=job_event.event_id,
        payload={"event_type": "job.queued"},
    )

    assert repository.get_document("doc-1") == document
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
        ("audit-scope-mismatch", {"scope_type": "job", "scope_id": job.job_id}),
        ("audit-chain-smuggled", {"sequence": 10}),
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
        action="document.uploaded",
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
            action="document.uploaded",
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
        action="document.uploaded",
        scope_type="document",
        scope_id=document.document_id,
        payload={
            "actor": {"id": "担当者-1", "role": "審査"},
            "event_type": "document.uploaded",
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
        action="document.uploaded",
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
        action="document.uploaded",
        scope_type="document",
        scope_id=document.document_id,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("DROP TRIGGER audit_events_no_update")
        connection.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps({"tampered": True}), audit_event.event_id),
        )

    with pytest.raises(ValueError, match="audit event chain integrity"):
        repository.get_audit_event(audit_event.event_id)


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
        action="document.uploaded",
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
        action="document.uploaded",
        scope_type="document",
        scope_id=document_a.document_id,
    )
    second = repository.create_audit_event(
        event_id="audit-job-b",
        job_id=job_b.job_id,
        document_id=document_b.document_id,
        actor="operator-2",
        action="document.uploaded",
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
