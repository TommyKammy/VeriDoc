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

    with pytest.raises(ValueError, match="payload actor.id is required"):
        repository.create_job_event(
            event_id="event-actor-without-id",
            job_id=job.job_id,
            event_type="job.queued",
            actor="operator-1",
            payload={
                "event_type": "job.queued",
                "actor": {"name": "mallory", "role": "reviewer"},
            },
        )

@pytest.mark.parametrize("payload", ([], "", 0, False))
def test_job_event_rejects_falsey_non_mapping_payloads(tmp_path, payload) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    with pytest.raises(ValueError, match="payload must be a mapping"):
        repository.create_job_event(
            event_id=f"job-event-falsey-{type(payload).__name__}",
            job_id=job.job_id,
            event_type="job.queued",
            actor="operator-1",
            payload=payload,
        )

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
        event_type="conversion_queued",
        actor="operator-1",
    )

    assert [event.event_id for event in repository.list_job_events(job.job_id)] == [
        first.event_id,
        second.event_id,
    ]

def test_job_event_history_validates_status_at_the_recorded_lifecycle_point(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    queued = repository.create_job_event(
        event_id="job-event-queued",
        job_id=job.job_id,
        event_type="job.queued",
        actor="operator-1",
        payload={
            "event_type": "job.queued",
            "job_status": "queued",
            "attempts": 0,
        },
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'running', attempts = 1 WHERE job_id = ?",
            (job.job_id,),
        )

    running = repository.create_job_event(
        event_id="job-event-running",
        job_id=job.job_id,
        event_type="job.running",
        actor="operator-1",
        payload={
            "event_type": "job.running",
            "job_status": "running",
            "attempts": 1,
        },
    )

    assert repository.list_job_events(job.job_id) == [queued, running]

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

def test_retry_event_status_contract_is_enforced_on_write_and_read(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    cases = (
        ("retry_conversion", "retry_conversion"),
        ("job.retry_requested", "job.retry_requested"),
        ("conversion_job.action_requested", "retry_conversion"),
    )
    for index, (event_type, action) in enumerate(cases):
        with pytest.raises(ValueError, match="job_status"):
            repository.create_job_event(
                event_id=f"retry-invalid-write-{index}",
                job_id=job.job_id,
                event_type=event_type,
                actor="operator-1",
                payload={
                    "event_type": event_type,
                    "action": action,
                    "job_status": "succeeded",
                },
            )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO job_events(
                event_id, job_id, sequence, event_type, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "retry-invalid-read",
                job.job_id,
                1,
                "conversion_job.action_requested",
                "operator-1",
                persistence._canonical_json(
                    {
                        "event_type": "conversion_job.action_requested",
                        "action": "retry_conversion",
                        "job_status": "succeeded",
                    }
                ),
                job.created_at,
            ),
        )

    with pytest.raises(ValueError, match="job_status"):
        repository.list_job_events(job.job_id)

@pytest.mark.parametrize(
    ("event_type", "payload", "message"),
    (
        ("conversion_job.action_requested", None, "payload action"),
        (
            "conversion_job.action_requested",
            {
                "event_type": "conversion_job.action_requested",
                "action": "retry_conversion",
            },
            "current job status",
        ),
        (
            "review.approved",
            {"event_type": "review.approved"},
            "job-event history",
        ),
        (
            "artifact.generated",
            {"event_type": "artifact.generated"},
            "job-event history",
        ),
        (
            "document.inspected",
            {"event_type": "document.inspected"},
            "job-event history",
        ),
    ),
)
def test_job_event_write_contract_rejects_incomplete_or_non_job_actions(
    tmp_path,
    event_type,
    payload,
    message,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    with pytest.raises(ValueError, match=message):
        repository.create_job_event(
            event_id=f"job-event-invalid-{event_type}",
            job_id=job.job_id,
            event_type=event_type,
            actor="operator-1",
            payload=payload,
        )

@pytest.mark.parametrize(
    ("event_type", "actor", "payload", "message"),
    (
        (
            "conversion_job.action_requested",
            "operator-1",
            {"event_type": "conversion_job.action_requested"},
            "payload action",
        ),
        (
            "review.approved",
            "operator-1",
            {"event_type": "review.approved"},
            "job-event history",
        ),
        (
            "desktop.job_operation",
            "mallory",
            {
                "event_type": "desktop.job_operation",
                "action": "desktop_upload",
            },
            "uploader",
        ),
        (
            "conversion_job.action_requested",
            "operator-1",
            {
                "event_type": "conversion_job.action_requested",
                "action": "download_result",
            },
            "stored successful result artifact",
        ),
    ),
)
def test_job_event_reads_revalidate_action_and_durable_evidence(
    tmp_path,
    event_type,
    actor,
    payload,
    message,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO job_events(
                event_id, job_id, sequence, event_type, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-event-direct-invalid",
                job.job_id,
                1,
                event_type,
                actor,
                persistence._canonical_json(payload),
                job.created_at,
            ),
        )

    with pytest.raises(ValueError, match=message):
        repository.list_job_events(job.job_id)

def test_upload_job_events_bind_actor_and_source_provenance(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    cases = (
        (
            "job-event-upload-wrong-actor",
            "mallory",
            {
                "event_type": "desktop.job_operation",
                "action": "desktop_upload",
                "filename": document.original_filename,
                "source_sha256": document.content_hash,
            },
            "uploader",
        ),
        (
            "job-event-upload-wrong-source",
            "operator-1",
            {
                "event_type": "desktop.job_operation",
                "action": "desktop_upload",
                "filename": document.original_filename,
                "source_sha256": "0" * 64,
            },
            "source_sha256",
        ),
    )
    for event_id, actor, payload, message in cases:
        with pytest.raises(ValueError, match=message):
            repository.create_job_event(
                event_id=event_id,
                job_id=job.job_id,
                event_type="desktop.job_operation",
                actor=actor,
                payload=payload,
            )

def test_upload_audit_preserves_history_while_job_lifecycle_advances(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    upload_audit = repository.create_audit_event(
        event_id="audit-desktop-upload",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="desktop_upload",
        scope_type="job",
        scope_id=job.job_id,
        payload={
            "event_type": "desktop.job_operation",
            "job_status": "queued",
            "attempts": 0,
            "filename": document.original_filename,
            "source_sha256": document.content_hash,
        },
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'running', attempts = 1 WHERE job_id = ?",
            (job.job_id,),
        )

    running_event = repository.create_job_event(
        event_id="job-event-running",
        job_id=job.job_id,
        event_type="job.running",
        actor="operator-1",
        payload={
            "event_type": "job.running",
            "job_status": "running",
            "attempts": 1,
        },
    )

    assert repository.get_audit_event(upload_audit.event_id) == upload_audit
    assert repository.list_job_events(job.job_id) == [running_event]

def test_job_event_payload_action_must_match_the_event_contract(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    with pytest.raises(ValueError, match="payload action"):
        repository.create_job_event(
            event_id="job-event-contradictory-action",
            job_id=job.job_id,
            event_type="job.queued",
            actor="operator-1",
            payload={
                "event_type": "job.queued",
                "action": "retry_conversion",
                "job_status": "queued",
            },
        )

def test_ungated_job_status_snapshots_remain_readable_after_state_changes(
    tmp_path,
) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    payload = {
        "event_type": "conversion_job.action_requested",
        "action": "open_detail",
        "job_status": "queued",
    }
    job_event = repository.create_job_event(
        event_id="job-event-open-detail",
        job_id=job.job_id,
        event_type="conversion_job.action_requested",
        actor="operator-1",
        payload=payload,
    )
    audit_event = repository.create_audit_event(
        event_id="audit-open-detail",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="open_detail",
        scope_type="job",
        scope_id=job.job_id,
        payload=payload,
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE jobs SET status = 'running', attempts = 1 WHERE job_id = ?",
            (job.job_id,),
        )

    assert repository.list_job_events(job.job_id) == [job_event]
    assert repository.get_audit_event(audit_event.event_id) == audit_event

def test_job_status_snapshot_aliases_must_agree_on_write_and_replay(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    payload = {
        "event_type": "conversion_job.action_requested",
        "action": "open_detail",
        "job_status": "queued",
        "status": "failed",
    }

    with pytest.raises(ValueError, match="status aliases"):
        repository.create_job_event(
            event_id="job-event-conflicting-status-write",
            job_id=job.job_id,
            event_type="conversion_job.action_requested",
            actor="operator-1",
            payload=payload,
        )

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO job_events(
                event_id, job_id, sequence, event_type, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-event-conflicting-status-read",
                job.job_id,
                1,
                "conversion_job.action_requested",
                "operator-1",
                persistence._canonical_json(payload),
                job.created_at,
            ),
        )

    with pytest.raises(ValueError, match="status aliases"):
        repository.list_job_events(job.job_id)

@pytest.mark.parametrize(
    ("alias", "value"),
    (("job_status", []), ("job_status", {}), ("status", []), ("status", {})),
)
def test_job_event_status_aliases_require_non_empty_strings(
    tmp_path,
    alias,
    value,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    with pytest.raises(ValueError, match=alias):
        repository.create_job_event(
            event_id=f"job-event-invalid-{alias}-{type(value).__name__}",
            job_id=job.job_id,
            event_type="job.queued",
            actor="operator-1",
            payload={"event_type": "job.queued", alias: value},
        )

@pytest.mark.parametrize(
    ("alias", "value"),
    (("attempts", 0.0), ("attempts", False), ("job_attempts", 0.0), ("job_attempts", False)),
)
def test_job_event_attempt_aliases_require_real_integers(
    tmp_path,
    alias,
    value,
) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")

    with pytest.raises(ValueError, match=alias):
        repository.create_job_event(
            event_id=f"job-event-invalid-{alias}-{type(value).__name__}",
            job_id=job.job_id,
            event_type="job.queued",
            actor="operator-1",
            payload={"event_type": "job.queued", alias: value},
        )
    with pytest.raises(ValueError, match=alias):
        repository.create_audit_event(
            event_id=f"audit-invalid-{alias}-{type(value).__name__}",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action="job.queued",
            scope_type="job",
            scope_id=job.job_id,
            payload={"event_type": "job.queued", alias: value},
        )

def test_job_event_history_rejects_attempted_upload_snapshots(tmp_path) -> None:
    db_path = tmp_path / "veridoc.sqlite3"
    repository = SQLitePersistenceRepository(db_path)
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = _create_job(repository, document, "job-1")
    payload = {
        "event_type": "desktop.job_operation",
        "action": "desktop_upload",
        "attempts": 1,
        "filename": document.original_filename,
        "source_sha256": document.content_hash,
    }

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO job_events(
                event_id, job_id, sequence, event_type, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-event-attempted-upload",
                job.job_id,
                1,
                "desktop.job_operation",
                "operator-1",
                persistence._canonical_json(payload),
                job.created_at,
            ),
        )

    with pytest.raises(ValueError, match="unattempted"):
        repository.list_job_events(job.job_id)

def test_generic_job_event_action_contract_drives_scoped_audit(tmp_path) -> None:
    repository = SQLitePersistenceRepository(tmp_path / "veridoc.sqlite3")
    repository.initialize()
    document = _create_document(repository, "doc-1")
    job = repository.create_conversion_job(
        job_id="job-1",
        document_id=document.document_id,
        idempotency_key="upload-job-1",
        mode="standard",
        status="failed",
    )
    job_event = repository.create_job_event(
        event_id="job-event-retry",
        job_id=job.job_id,
        event_type="conversion_job.action_requested",
        actor="operator-1",
        payload={
            "event_type": "conversion_job.action_requested",
            "action": "retry_conversion",
            "job_status": "failed",
        },
    )

    audit_event = repository.create_audit_event(
        event_id="audit-retry",
        job_id=job.job_id,
        document_id=document.document_id,
        actor="operator-1",
        action="retry_conversion",
        scope_type="job_event",
        scope_id=job_event.event_id,
        payload={"event_type": "conversion_job.action_requested"},
    )
    assert repository.get_audit_event(audit_event.event_id) == audit_event

def test_download_job_events_freeze_their_selected_artifact_evidence(tmp_path) -> None:
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
    later_artifact = _create_artifact(repository, result, "artifact-later")

    with sqlite3.connect(db_path) as connection:
        evidence_row = connection.execute(
            """
            SELECT event_id, artifact_id, evidence_type
            FROM job_event_evidence
            WHERE event_id = ?
            """,
            (job_event.event_id,),
        ).fetchone()
        assert evidence_row == (
            job_event.event_id,
            artifact.artifact_id,
            "download_artifact",
        )
        with pytest.raises(sqlite3.IntegrityError, match="job event evidence"):
            connection.execute(
                """
                INSERT INTO job_event_evidence(event_id, artifact_id, evidence_type)
                VALUES (?, ?, ?)
                """,
                (
                    job_event.event_id,
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
                "UPDATE conversion_results SET status = ? WHERE result_id = ?",
                ("failed", result.result_id),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="job event evidence rows"):
                connection.execute(sql, params)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM job_event_evidence WHERE event_id = ?",
                (job_event.event_id,),
            )

    assert repository.list_job_events(job.job_id) == [job_event]

def test_download_job_event_read_requires_its_frozen_evidence_link(tmp_path) -> None:
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
    payload = {
        "event_type": "desktop.job_operation",
        "action": "desktop_result_download",
        "job_status": "succeeded",
        "download_filename": artifact.display_filename,
        "output_sha256": artifact.content_hash,
    }

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO job_events(
                event_id, job_id, sequence, event_type, actor, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "job-event-without-evidence",
                job.job_id,
                1,
                "desktop.job_operation",
                "operator-1",
                persistence._canonical_json(payload),
                job.created_at,
            ),
        )

    with pytest.raises(ValueError, match="job event evidence link"):
        repository.list_job_events(job.job_id)

def test_job_event_scoped_download_audit_uses_the_event_evidence(tmp_path) -> None:
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
    job_event = repository.create_job_event(
        event_id="job-event-download-a",
        job_id=job.job_id,
        event_type="desktop.job_operation",
        actor="operator-1",
        payload={
            "event_type": "desktop.job_operation",
            "action": "desktop_result_download",
            "download_filename": artifact_a.display_filename,
            "output_sha256": artifact_a.content_hash,
        },
    )

    with pytest.raises(ValueError, match="scoped job event evidence"):
        repository.create_audit_event(
            event_id="audit-download-b-for-event-a",
            job_id=job.job_id,
            document_id=document.document_id,
            actor="operator-1",
            action="desktop_result_download",
            scope_type="job_event",
            scope_id=job_event.event_id,
            payload={
                "event_type": "desktop.job_operation",
                "download_filename": artifact_b.display_filename,
                "output_sha256": artifact_b.content_hash,
            },
        )
