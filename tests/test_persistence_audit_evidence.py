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
    record_authoritative_review_decision,
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


def _create_review_decision_scope(
    repository: SQLitePersistenceRepository,
) -> tuple[ReviewItem, Artifact]:
    document = _create_document(repository, "review-doc")
    job = _create_job(repository, document, "review-job")
    result = _create_result(repository, job, "review-result")
    artifact = _create_artifact(repository, result, "review-artifact")
    review_item = repository.create_review_item(
        review_item_id="review-item",
        document_id=document.document_id,
        job_id=job.job_id,
        target_path="blocks[0]",
        status="open",
        severity="high",
    )
    return review_item, artifact


def test_authoritative_review_decision_persists_decision_and_audit_snapshot(
    tmp_path,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    review_item, artifact = _create_review_decision_scope(repository)

    recorded = record_authoritative_review_decision(
        repository,
        decision_id="decision-authoritative",
        decision_version=1,
        review_item_id=review_item.review_item_id,
        item_version="item-version-1",
        artifact_id=artifact.artifact_id,
        actor_id="approver-1",
        actor_role="approver",
        decision="approved",
        reason="high-risk value verified against source",
        high_risk=True,
    )

    persisted = repository.get_review_decision(recorded.decision_id)
    audit = repository.get_audit_event(recorded.audit_event_id)
    assert persisted is not None
    assert audit is not None
    assert persisted.actor == "approver-1"
    assert persisted.role == "approver"
    assert persisted.decision == "approved"
    payload = json.loads(audit.payload_json)
    assert payload["decided_at"] == recorded.decided_at
    assert payload["decision_version"] == recorded.version
    assert payload["item_version"] == recorded.item_version
    assert payload["reason"] == recorded.reason
    assert payload["review_item_id"] == review_item.review_item_id
    assert payload["artifact_id"] == artifact.artifact_id


@pytest.mark.parametrize(
    ("actor_role", "decision", "high_risk", "error_type", "message"),
    (
        ("viewer", "approved", False, PermissionError, "cannot record decision"),
        ("reviewer", "approved", False, PermissionError, "cannot record decision"),
        (
            "reviewer",
            "edited",
            True,
            ValueError,
            "high-risk review item requires approver approval",
        ),
    ),
)
def test_authoritative_review_decision_denials_leave_no_durable_state(
    tmp_path,
    actor_role,
    decision,
    high_risk,
    error_type,
    message,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    review_item, artifact = _create_review_decision_scope(repository)

    with pytest.raises(error_type, match=message):
        record_authoritative_review_decision(
            repository,
            decision_id="decision-denied",
            decision_version=1,
            review_item_id=review_item.review_item_id,
            item_version="item-version-1",
            artifact_id=artifact.artifact_id,
            actor_id="denied-actor",
            actor_role=actor_role,
            decision=decision,
            reason="attempted synthetic review",
            high_risk=high_risk,
        )

    assert repository.get_review_decision("decision-denied") is None
    assert repository.get_audit_event("audit-decision-denied") is None


def test_authoritative_review_decision_rolls_back_when_audit_append_fails(
    tmp_path,
    monkeypatch,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    review_item, artifact = _create_review_decision_scope(repository)

    def fail_audit_append(*_args, **_kwargs):
        raise RuntimeError("forced audit append failure")

    monkeypatch.setattr(
        SQLitePersistenceRepository,
        "create_audit_event",
        fail_audit_append,
    )
    with pytest.raises(RuntimeError, match="forced audit append failure"):
        record_authoritative_review_decision(
            repository,
            decision_id="decision-rolled-back",
            decision_version=1,
            review_item_id=review_item.review_item_id,
            item_version="item-version-1",
            artifact_id=artifact.artifact_id,
            actor_id="approver-1",
            actor_role="approver",
            decision="approved",
            reason="review would otherwise be accepted",
            high_risk=False,
        )

    assert repository.get_review_decision("decision-rolled-back") is None
    assert repository.get_audit_event("audit-decision-rolled-back") is None


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

def test_download_audits_require_matching_durable_result_evidence(tmp_path) -> None:
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

    with pytest.raises(ValueError, match="stored successful result artifact"):
        repository.create_audit_event(
            event_id="audit-download-without-result",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action="download_result",
            scope_type="job",
            scope_id=job.job_id,
            payload={"event_type": "conversion_job.action_requested"},
        )
    with pytest.raises(ValueError, match="stored successful result artifact"):
        repository.create_job_event(
            event_id="job-event-download-without-result",
            job_id=job.job_id,
            event_type="conversion_job.action_requested",
            actor="operator-1",
            payload={
                "event_type": "conversion_job.action_requested",
                "action": "download_result",
            },
        )

    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
    for event_id, payload, message in (
        (
            "audit-download-wrong-hash",
            {
                "event_type": "desktop.job_operation",
                "download_filename": "artifact-1.docx",
                "output_sha256": "0" * 64,
            },
            "evidence",
        ),
        (
            "audit-download-wrong-filename",
            {
                "event_type": "desktop.job_operation",
                "download_filename": "different.docx",
                "output_sha256": artifact.content_hash,
            },
            "evidence",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            repository.create_audit_event(
                event_id=event_id,
                job_id=job.job_id,
                document_id=document.document_id,
                actor="operator-1",
                action="desktop_result_download",
                scope_type="job",
                scope_id=job.job_id,
                payload=payload,
            )

    for event_id, payload in (
        (
            "audit-generic-download-wrong-hash",
            {
                "event_type": "conversion_job.action_requested",
                "download_filename": artifact.display_filename,
                "output_sha256": "0" * 64,
            },
        ),
        (
            "audit-generic-download-wrong-filename",
            {
                "event_type": "conversion_job.action_requested",
                "download_filename": "different.docx",
                "output_sha256": artifact.content_hash,
            },
        ),
    ):
        with pytest.raises(ValueError, match="evidence"):
            repository.create_audit_event(
                event_id=event_id,
                job_id=job.job_id,
                document_id=document.document_id,
                actor="operator-1",
                action="download_result",
                scope_type="job",
                scope_id=job.job_id,
                payload=payload,
            )

    event = repository.create_audit_event(
        event_id="audit-download-with-result",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="download_result",
        scope_type="job",
        scope_id=job.job_id,
        payload={"event_type": "conversion_job.action_requested"},
    )
    assert repository.get_audit_event(event.event_id) == event

@pytest.mark.parametrize("result_status", ("converted", "requires_review", "blocked"))
def test_download_and_completion_contracts_use_the_result_status_domain(
    tmp_path,
    result_status,
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
    result = repository.create_conversion_result(
        result_id="result-1",
        job_id=job.job_id,
        document_id=document.document_id,
        status=result_status,
        content_hash="b" * 64,
    )
    artifact = _create_artifact(repository, result, "artifact-1")
    completion_audit = repository.create_audit_event(
        event_id="audit-conversion-completed",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="conversion.completed",
        scope_type="conversion_result",
        scope_id=result.result_id,
        payload={"event_type": "conversion.completed", "result_status": result_status},
    )
    download_audit = repository.create_audit_event(
        event_id="audit-download-result",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="download_result",
        scope_type="job",
        scope_id=job.job_id,
        payload={
            "event_type": "conversion_job.action_requested",
            "download_filename": artifact.display_filename,
            "output_sha256": artifact.content_hash,
        },
    )

    assert repository.get_audit_event(completion_audit.event_id) == completion_audit
    assert repository.get_audit_event(download_audit.event_id) == download_audit

def test_download_evidence_rejects_failed_conversion_results(tmp_path) -> None:
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
    result = repository.create_conversion_result(
        result_id="result-1",
        job_id=job.job_id,
        document_id=document.document_id,
        status="failed",
        content_hash="b" * 64,
    )
    _create_artifact(repository, result, "artifact-1")

    with pytest.raises(ValueError, match="stored successful result artifact"):
        repository.create_audit_event(
            event_id="audit-download-failed-result",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action="download_result",
            scope_type="job",
            scope_id=job.job_id,
            payload={"event_type": "conversion_job.action_requested"},
        )

@pytest.mark.parametrize("alias", ("download_filename", "output_sha256"))
def test_download_evidence_aliases_are_rejected_for_non_download_actions(
    tmp_path,
    alias,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    value = "report.docx" if alias == "download_filename" else "c" * 64
    payload = {"event_type": "job.queued", alias: value}

    with pytest.raises(ValueError, match=alias):
        repository.create_job_event(
            event_id=f"job-event-queued-{alias}",
            job_id=job.job_id,
            event_type="job.queued",
            actor="operator-1",
            payload=payload,
        )
    with pytest.raises(ValueError, match=alias):
        repository.create_audit_event(
            event_id=f"audit-queued-{alias}",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action="job.queued",
            scope_type="job",
            scope_id=job.job_id,
            payload=payload,
        )

def test_download_evidence_uses_display_filename_and_freezes_linked_rows(
    tmp_path,
) -> None:
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
    artifact = repository.create_artifact(
        artifact_id="artifact-1",
        result_id=result.result_id,
        job_id=job.job_id,
        document_id=document.document_id,
        category="generated",
        format="docx",
        display_filename="report.veridoc-standard.docx",
        storage_key="objects/abc123.bin",
        content_hash="c" * 64,
    )
    job_event = repository.create_job_event(
        event_id="job-event-download",
        job_id=job.job_id,
        event_type="desktop.job_operation",
        actor="operator-1",
        payload={
            "event_type": "desktop.job_operation",
            "action": "desktop_result_download",
            "job_status": "succeeded",
            "download_filename": artifact.display_filename,
            "output_sha256": artifact.content_hash,
        },
    )
    audit_event = repository.create_audit_event(
        event_id="audit-download-1",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="desktop_result_download",
        scope_type="job_event",
        scope_id=job_event.event_id,
        payload={
            "event_type": "desktop.job_operation",
            "download_filename": artifact.display_filename,
            "output_sha256": artifact.content_hash,
        },
    )
    assert json.loads(audit_event.payload_json)["evidence"] == {
        "artifact_ids": [artifact.artifact_id],
        "type": "download_artifact",
    }
    later_artifact = repository.create_artifact(
        artifact_id="artifact-2",
        result_id=result.result_id,
        job_id=job.job_id,
        document_id=document.document_id,
        category="generated",
        format="docx",
        display_filename="later.docx",
        storage_key="objects/def456.bin",
        content_hash="d" * 64,
    )

    with sqlite3.connect(db_path) as connection:
        evidence_row = connection.execute(
            """
            SELECT event_id, artifact_id, evidence_type
            FROM audit_event_evidence
            WHERE event_id = ?
            """,
            (audit_event.event_id,),
        ).fetchone()
        assert evidence_row == (
            audit_event.event_id,
            artifact.artifact_id,
            "download_artifact",
        )
        with pytest.raises(sqlite3.IntegrityError, match="audit evidence"):
            connection.execute(
                """
                INSERT INTO audit_event_evidence(event_id, artifact_id, evidence_type)
                VALUES (?, ?, ?)
                """,
                (
                    audit_event.event_id,
                    later_artifact.artifact_id,
                    "download_artifact",
                ),
            )
        for sql, params in (
            (
                "UPDATE generated_artifacts SET content_hash = ? WHERE artifact_id = ?",
                ("d" * 64, artifact.artifact_id),
            ),
            (
                "UPDATE generated_artifacts SET display_filename = ? WHERE artifact_id = ?",
                ("rewritten.docx", artifact.artifact_id),
            ),
            (
                "UPDATE conversion_results SET status = ? WHERE result_id = ?",
                ("failed", result.result_id),
            ),
        ):
            with pytest.raises(
                sqlite3.IntegrityError,
                match="(?:audit|job event) evidence rows",
            ):
                connection.execute(sql, params)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM audit_event_evidence WHERE event_id = ?",
                (audit_event.event_id,),
            )

    assert repository.get_audit_event(audit_event.event_id) == audit_event

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
