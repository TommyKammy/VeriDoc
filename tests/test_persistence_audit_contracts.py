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
        ("audit-nested-actor-without-id", {"actor": {"name": "mallory"}}),
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
            "desktop_upload",
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
        with pytest.raises(ValueError, match="payload event_type|selected scope type"):
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
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'succeeded' WHERE job_id = ?",
            (job.job_id,),
        )
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
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'succeeded' WHERE job_id = ?",
            (job.job_id,),
        )
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
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'succeeded' WHERE job_id = ?",
            (job.job_id,),
        )
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
        ("UPDATE jobs SET mode = ? WHERE job_id = ?", ("other", job.job_id)),
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
                match="audit scope rows|append-only|job identity",
            ):
                connection.execute(sql, params)

        connection.execute(
            "UPDATE jobs SET status = ?, attempts = ? WHERE job_id = ?",
            ("succeeded", 99, job.job_id),
        )

    assert repository.get_document(document.document_id) == document
    updated_job = repository.get_conversion_job(job.job_id)
    assert updated_job is not None
    assert updated_job.status == "succeeded"
    assert updated_job.attempts == 99
    assert updated_job.mode == job.mode
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

def test_document_and_job_identity_are_frozen_by_any_audit_event_reference(
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
                "UPDATE source_documents SET content_hash = ? WHERE document_id = ?",
                ("d" * 64, document.document_id),
            )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="job identity|audit scope rows",
        ):
            connection.execute(
                "UPDATE jobs SET mode = ? WHERE job_id = ?",
                ("high_quality", job.job_id),
            )
        connection.execute(
            "UPDATE jobs SET status = ? WHERE job_id = ?",
            ("succeeded", job.job_id),
        )

    assert repository.get_document(document.document_id) == document
    updated_job = repository.get_conversion_job(job.job_id)
    assert updated_job is not None
    assert updated_job.status == "succeeded"
    assert updated_job.mode == job.mode
    assert repository.get_audit_event(audit_event.event_id) == audit_event

def test_source_document_status_can_advance_after_audit(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    audit_event = repository.create_audit_event(
        event_id="audit-document-uploaded",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="document.uploaded",
        scope_type="document",
        scope_id=document.document_id,
        payload={
            "event_type": "document.uploaded",
            "document_status": document.status,
        },
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE source_documents SET status = ?, updated_at = ? WHERE document_id = ?",
            ("validated", "2026-07-10T00:00:00+00:00", document.document_id),
        )

    updated_document = repository.get_document(document.document_id)
    assert updated_document is not None
    assert updated_document.status == "validated"
    assert repository.get_audit_event(audit_event.event_id) == audit_event

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

    for event_id, scope_type, scope_id, actor, action, alias_field, alias_value in (
        (
            "audit-review-decision-alias",
            "review_decision",
            review_decision.decision_id,
            "qa-approver",
            "review.approved",
            "review_decision_id",
            "decision-2",
        ),
        (
            "audit-review-decision-natural-alias",
            "review_decision",
            review_decision.decision_id,
            "qa-approver",
            "review.approved",
            "decision_id",
            "decision-2",
        ),
        (
            "audit-artifact-alias",
            "artifact",
            artifact.artifact_id,
            "operator-1",
            "artifact.generated",
            "artifact_id",
            "artifact-2",
        ),
        (
            "audit-job-event-alias",
            "conversion_result",
            result.result_id,
            "operator-1",
            "conversion.completed",
            "conversion_result_id",
            "result-2",
        ),
    ):
        with pytest.raises(ValueError, match=f"payload {alias_field}"):
            repository.create_audit_event(
                event_id=event_id,
                job_id=job.job_id,
                document_id=document.document_id,
                actor=actor,
                action=action,
                scope_type=scope_type,
                scope_id=scope_id,
                payload={"event_type": action, alias_field: alias_value},
            )
        assert repository.get_audit_event(event_id) is None

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

def test_document_scoped_desktop_audits_bind_canonical_job_snapshots(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    upload_document = _create_document(repository, "doc-upload")
    upload_job = _create_job(repository, upload_document, "job-upload")
    upload_payload = {
        "event_type": "desktop.job_operation",
        "job_id": upload_job.job_id,
        "job_status": upload_job.status,
        "action": "desktop_upload",
        "filename": upload_document.original_filename,
        "mode": upload_job.mode,
        "source_sha256": upload_document.content_hash,
        "size_bytes": 123,
        "content_type": "application/pdf",
    }
    upload_audit = repository.create_audit_event(
        event_id="audit-document-desktop-upload",
        job_id=upload_job.job_id,
        document_id=upload_document.document_id,
        actor="operator-1",
        action="desktop_upload",
        scope_type="document",
        scope_id=upload_document.document_id,
        payload=upload_payload,
    )
    assert repository.get_audit_event(upload_audit.event_id) == upload_audit

    with pytest.raises(ValueError, match="mode"):
        repository.create_audit_event(
            event_id="audit-document-upload-wrong-mode",
            job_id=upload_job.job_id,
            document_id=upload_document.document_id,
            actor="operator-1",
            action="desktop_upload",
            scope_type="document",
            scope_id=upload_document.document_id,
            payload={**upload_payload, "mode": "high_quality"},
        )

    download_document = _create_document(repository, "doc-download")
    download_job = repository.create_conversion_job(
        job_id="job-download",
        document_id=download_document.document_id,
        idempotency_key="upload-job-download",
        mode="standard",
        status="succeeded",
    )
    result = _create_result(repository, download_job, "result-download")
    artifact = _create_artifact(repository, result, "artifact-download")
    download_audit = repository.create_audit_event(
        event_id="audit-document-desktop-download",
        job_id=download_job.job_id,
        document_id=download_document.document_id,
        actor="operator-1",
        action="desktop_result_download",
        scope_type="document",
        scope_id=download_document.document_id,
        payload={
            "event_type": "desktop.job_operation",
            "job_id": download_job.job_id,
            "job_status": download_job.status,
            "action": "desktop_result_download",
            "filename": download_document.original_filename,
            "download_filename": artifact.display_filename,
            "source_sha256": download_document.content_hash,
            "output_sha256": artifact.content_hash,
        },
    )
    assert repository.get_audit_event(download_audit.event_id) == download_audit

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
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
    download_document = _create_document(repository, "doc-download")
    download_job = repository.create_conversion_job(
        job_id="job-download",
        document_id=download_document.document_id,
        idempotency_key="upload-job-download",
        mode="standard",
        status="succeeded",
    )
    download_result = _create_result(repository, download_job, "result-download")
    download_artifact = _create_artifact(
        repository,
        download_result,
        "artifact-download",
    )
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
            "audit-wrong-artifact-sha256",
            "operator-1",
            "artifact.generated",
            "artifact",
            artifact.artifact_id,
            {"event_type": "artifact.generated", "sha256": "0" * 64},
            "sha256",
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
            "sha256": artifact.content_hash,
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
        job_id=download_job.job_id,
        document_id=download_document.document_id,
        actor="operator-1",
        action="desktop_result_download",
        scope_type="job",
        scope_id=download_job.job_id,
        payload={
            "event_type": "desktop.job_operation",
            "job_id": download_job.job_id,
            "job_status": download_job.status,
            "action": "desktop_result_download",
            "filename": download_document.original_filename,
            "download_filename": "artifact-download.docx",
            "source_sha256": download_document.content_hash,
            "output_sha256": download_artifact.content_hash,
        },
    )
    assert (
        repository.get_audit_event(valid_desktop_download.event_id)
        == valid_desktop_download
    )

def test_result_scoped_lifecycle_audit_requires_matching_job_status(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")

    with pytest.raises(ValueError, match="current job status"):
        repository.create_audit_event(
            event_id="audit-result-before-job-completed",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action="conversion.completed",
            scope_type="conversion_result",
            scope_id=result.result_id,
            payload={"event_type": "conversion.completed"},
        )

@pytest.mark.parametrize("action", ("approve", "review.approved"))
def test_known_review_actions_reject_unrelated_scope_without_event_type(
    tmp_path,
    action,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    with pytest.raises(ValueError, match="selected scope type"):
        repository.create_audit_event(
            event_id=f"audit-wrong-scope-{action}",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action=action,
            scope_type="job",
            scope_id=job.job_id,
        )

def test_audit_action_contract_aliases_are_unique() -> None:
    declared_alias_count = sum(
        len(contract.aliases) for contract in persistence._AUDIT_ACTION_CONTRACTS
    )
    assert declared_alias_count == len(persistence._AUDIT_ACTION_CONTRACT_BY_ALIAS)

def test_audit_action_contract_matrix_is_fail_closed_and_domain_complete() -> None:
    scope_types = frozenset(persistence._AUDIT_SCOPE_PAYLOAD_BINDINGS)
    for contract in persistence._AUDIT_ACTION_CONTRACTS:
        assert contract.aliases
        assert contract.scope_types
        assert contract.scope_types.issubset(scope_types)
        if "conversion_result" in contract.scope_types:
            assert contract.result_statuses
        else:
            assert contract.result_statuses is None
        if contract.evidence_type is not None:
            assert contract.job_statuses
            assert "job_event" in contract.scope_types
        for alias in contract.aliases:
            for scope_type in scope_types:
                if scope_type in contract.scope_types:
                    assert persistence._require_audit_action_scope(alias, scope_type) == contract
                else:
                    with pytest.raises(ValueError, match="selected scope type"):
                        persistence._require_audit_action_scope(alias, scope_type)

    for scope_type in scope_types:
        with pytest.raises(ValueError, match="declared contract"):
            persistence._require_audit_action_scope("source_uplod", scope_type)

def test_unknown_audit_actions_are_rejected_before_storage(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    with pytest.raises(ValueError, match="declared contract"):
        repository.create_audit_event(
            event_id="audit-unknown-action",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action="source_uplod",
            scope_type="document",
            scope_id=document.document_id,
        )
    assert repository.get_audit_event("audit-unknown-action") is None

    created_at = "2026-07-10T00:00:00+00:00"
    payload_json = persistence._canonical_json(
        {
            "action": "source_uplod",
            "actor": "operator-1",
            "scope_id": document.document_id,
            "scope_type": "document",
        }
    )
    event_hash = persistence._audit_event_hash(
        event_id="audit-unknown-direct",
        job_id=job.job_id,
        document_id=document.document_id,
        sequence=1,
        integrity_algorithm=AUDIT_INTEGRITY_ALGORITHM,
        actor="operator-1",
        action="source_uplod",
        scope_type="document",
        scope_id=document.document_id,
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
                "audit-unknown-direct",
                job.job_id,
                document.document_id,
                1,
                AUDIT_INTEGRITY_ALGORITHM,
                "operator-1",
                "source_uplod",
                "document",
                document.document_id,
                event_hash,
                None,
                payload_json,
                created_at,
            ),
        )

    with pytest.raises(ValueError, match="declared contract"):
        repository.get_audit_event("audit-unknown-direct")

@pytest.mark.parametrize(
    ("action", "scope_type"),
    (
        ("job.completed", "document"),
        ("conversion.failed", "document"),
        ("document.uploaded", "artifact"),
        ("uploaded", "generated_artifact"),
        ("artifact.generated", "job"),
        ("review.opened", "document"),
        ("document.inspected", "job"),
    ),
)
def test_action_contract_rejects_lifecycle_and_upload_actions_in_unrelated_scopes(
    tmp_path,
    action,
    scope_type,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    result = _create_result(repository, job, "result-1")
    artifact = _create_artifact(repository, result, "artifact-1")
    scope_ids = {
        "artifact": artifact.artifact_id,
        "document": document.document_id,
        "generated_artifact": artifact.artifact_id,
        "job": job.job_id,
    }
    scope_id = scope_ids[scope_type]

    with pytest.raises(ValueError, match="selected scope type"):
        repository.create_audit_event(
            event_id=f"audit-wrong-scope-{action}",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="mallory",
            action=action,
            scope_type=scope_type,
            scope_id=scope_id,
        )

def test_audit_evidence_trigger_binds_download_aliases_to_the_artifact(
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
    artifact_a = _create_artifact(repository, result, "artifact-a")
    artifact_b = repository.create_artifact(
        artifact_id="artifact-b",
        result_id=result.result_id,
        job_id=job.job_id,
        document_id=document.document_id,
        category="generated",
        format="docx",
        display_filename="artifact-b.docx",
        storage_key="artifacts/artifact-b.docx",
        content_hash="d" * 64,
    )
    created_at = "2026-07-10T00:00:00+00:00"
    payload_json = persistence._canonical_json(
        {
            "action": "desktop_result_download",
            "actor": "operator-1",
            "download_filename": artifact_a.display_filename,
            "evidence": {
                "artifact_ids": [artifact_b.artifact_id],
                "type": "download_artifact",
            },
            "event_type": "desktop.job_operation",
            "output_sha256": artifact_a.content_hash,
            "scope_id": job.job_id,
            "scope_type": "job",
        }
    )
    event_hash = persistence._audit_event_hash(
        event_id="audit-alias-mismatch",
        job_id=job.job_id,
        document_id=document.document_id,
        sequence=1,
        integrity_algorithm=AUDIT_INTEGRITY_ALGORITHM,
        actor="operator-1",
        action="desktop_result_download",
        scope_type="job",
        scope_id=job.job_id,
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
                "audit-alias-mismatch",
                job.job_id,
                document.document_id,
                1,
                AUDIT_INTEGRITY_ALGORITHM,
                "operator-1",
                "desktop_result_download",
                "job",
                job.job_id,
                event_hash,
                None,
                payload_json,
                created_at,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="audit evidence"):
            connection.execute(
                """
                INSERT INTO audit_event_evidence(event_id, artifact_id, evidence_type)
                VALUES (?, ?, ?)
                """,
                (
                    "audit-alias-mismatch",
                    artifact_b.artifact_id,
                    "download_artifact",
                ),
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
