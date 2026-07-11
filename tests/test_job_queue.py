import sqlite3

import pytest

from services.api.job_queue import JobQueue
from services.api.persistence_repository import reset_database


def test_job_queue_persists_state_transitions_and_idempotent_creation() -> None:
    queue = JobQueue()

    created = queue.create_job(
        idempotency_key="upload-1",
        filename="batch-record.pdf",
        mode="standard",
    )
    duplicate = queue.create_job(
        idempotency_key="upload-1",
        filename="batch-record.pdf",
        mode="standard",
    )

    assert duplicate.job_id == created.job_id
    assert duplicate.status == "queued"

    running = queue.start_next_job()
    assert running is not None
    assert running.job_id == created.job_id
    assert running.status == "running"

    succeeded = queue.mark_succeeded(running.job_id, result={"status": "converted"})
    assert succeeded.status == "succeeded"
    assert succeeded.result == {"status": "converted"}
    assert queue.get_job(created.job_id).status == "succeeded"


def test_job_queue_restores_failed_retry_state_after_reinitialization(tmp_path) -> None:
    database_path = tmp_path / "job-queue.sqlite3"
    queue = JobQueue(max_attempts=2, database_path=database_path)
    created = queue.create_job(
        idempotency_key="restart-retry",
        filename="batch-record.pdf",
        mode="standard",
    )
    running = queue.start_next_job()
    assert running is not None
    retrying = queue.mark_failed(running.job_id, error="parser unavailable")
    assert retrying.status == "queued"
    assert retrying.attempts == 1

    restored_queue = JobQueue(max_attempts=2, database_path=database_path)
    restored = restored_queue.get_job(created.job_id)

    assert restored.status == "queued"
    assert restored.attempts == 1
    assert restored.error == "parser unavailable"
    restored_running = restored_queue.start_next_job()
    assert restored_running is not None
    assert restored_running.job_id == created.job_id


def test_job_queue_restores_terminal_results_and_errors(tmp_path) -> None:
    database_path = tmp_path / "job-queue.sqlite3"
    queue = JobQueue(max_attempts=1, database_path=database_path)
    successful = queue.create_job(
        idempotency_key="restart-success",
        filename="successful.pdf",
        mode="standard",
    )
    successful_running = queue.start_next_job()
    assert successful_running is not None
    queue.mark_succeeded(successful_running.job_id, result={"status": "converted"})
    failed = queue.create_job(
        idempotency_key="restart-failure",
        filename="failed.pdf",
        mode="standard",
    )
    failed_running = queue.start_next_job()
    assert failed_running is not None
    queue.mark_failed(failed_running.job_id, error="parser unavailable")

    restored_queue = JobQueue(max_attempts=1, database_path=database_path)

    restored_success = restored_queue.get_job(successful.job_id)
    assert restored_success.status == "succeeded"
    assert restored_success.result == {"status": "converted"}
    restored_failure = restored_queue.get_job(failed.job_id)
    assert restored_failure.status == "failed"
    assert restored_failure.attempts == 1
    assert restored_failure.error == "parser unavailable"


def test_job_queue_restores_byte_payloads(tmp_path) -> None:
    database_path = tmp_path / "job-queue.sqlite3"
    queue = JobQueue(database_path=database_path)
    created = queue.create_job(
        idempotency_key="restart-bytes",
        filename="batch-record.pdf",
        mode="standard",
        source={"content": b"uploaded document"},
    )
    running = queue.start_next_job()
    assert running is not None
    queue.mark_succeeded(
        running.job_id,
        result={"download": {"content": b"converted document"}},
    )

    restored = JobQueue(database_path=database_path).get_job(created.job_id)

    assert restored.source == {"content": b"uploaded document"}
    assert restored.result == {"download": {"content": b"converted document"}}


def test_job_queue_requeues_running_job_after_reinitialization(tmp_path) -> None:
    database_path = tmp_path / "job-queue.sqlite3"
    queue = JobQueue(database_path=database_path)
    created = queue.create_job(
        idempotency_key="restart-running",
        filename="batch-record.pdf",
        mode="standard",
    )
    running = queue.start_next_job()
    assert running is not None

    restored_queue = JobQueue(database_path=database_path)

    assert restored_queue.get_job(created.job_id).status == "queued"
    recovered = restored_queue.start_next_job()
    assert recovered is not None
    assert recovered.job_id == created.job_id


def test_job_queue_requeues_running_job_before_later_queued_jobs(tmp_path) -> None:
    database_path = tmp_path / "job-queue.sqlite3"
    queue = JobQueue(database_path=database_path)
    older = queue.create_job(
        idempotency_key="restart-running-older",
        filename="older.pdf",
        mode="standard",
    )
    newer = queue.create_job(
        idempotency_key="restart-running-newer",
        filename="newer.pdf",
        mode="standard",
    )
    running = queue.start_next_job()
    assert running is not None
    assert running.job_id == older.job_id

    restored_queue = JobQueue(database_path=database_path)

    recovered = restored_queue.start_next_job()
    assert recovered is not None
    assert recovered.job_id == older.job_id
    restored_queue.mark_succeeded(recovered.job_id, result={})
    next_job = restored_queue.start_next_job()
    assert next_job is not None
    assert next_job.job_id == newer.job_id


def test_job_queue_keeps_job_pending_when_running_persistence_fails(
    tmp_path, monkeypatch
) -> None:
    database_path = tmp_path / "job-queue.sqlite3"
    queue = JobQueue(database_path=database_path)
    created = queue.create_job(
        idempotency_key="running-persistence-failure",
        filename="batch-record.pdf",
        mode="standard",
    )
    persist = queue._persist

    def fail_persistence(*args, **kwargs) -> None:
        raise OSError("simulated persistence failure")

    monkeypatch.setattr(queue, "_persist", fail_persistence)
    with pytest.raises(OSError, match="simulated persistence failure"):
        queue.start_next_job()

    monkeypatch.setattr(queue, "_persist", persist)
    running = queue.start_next_job()
    assert running is not None
    assert running.job_id == created.job_id


def test_job_queue_preserves_deferred_job_after_reinitialization(tmp_path) -> None:
    database_path = tmp_path / "job-queue.sqlite3"
    queue = JobQueue(database_path=database_path)
    created = queue.create_job(
        idempotency_key="restart-deferred",
        filename="batch-record.pdf",
        mode="standard",
        enqueue=False,
    )

    restored_queue = JobQueue(database_path=database_path)

    assert restored_queue.get_job(created.job_id).status == "queued"
    assert restored_queue.start_next_job() is None
    restored_queue.enqueue_job(created.job_id)
    started = restored_queue.start_next_job()
    assert started is not None
    assert started.job_id == created.job_id


def test_job_queue_backfills_legacy_queued_sequences_durably(tmp_path) -> None:
    database_path = tmp_path / "job-queue.sqlite3"
    queue = JobQueue(database_path=database_path)
    created = queue.create_job(
        idempotency_key="legacy-queued",
        filename="batch-record.pdf",
        mode="standard",
    )
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            ALTER TABLE job_queue_records RENAME TO job_queue_records_with_sequence;
            CREATE TABLE job_queue_records (
                job_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL CHECK(attempts >= 0),
                record_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO job_queue_records(
                job_id, idempotency_key, status, attempts, record_json, updated_at
            )
            SELECT job_id, idempotency_key, status, attempts, record_json, updated_at
            FROM job_queue_records_with_sequence;
            DROP TABLE job_queue_records_with_sequence;
            """
        )

    JobQueue(database_path=database_path)
    restored_again = JobQueue(database_path=database_path)

    running = restored_again.start_next_job()
    assert running is not None
    assert running.job_id == created.job_id


def test_persistence_reset_drops_job_queue_records(tmp_path) -> None:
    database_path = tmp_path / "veridoc.sqlite3"
    queue = JobQueue(database_path=database_path)
    created = queue.create_job(
        idempotency_key="reset-queued",
        filename="batch-record.pdf",
        mode="standard",
    )

    reset_database(database_path)
    restored = JobQueue(database_path=database_path)

    with pytest.raises(KeyError, match=created.job_id):
        restored.get_job(created.job_id)
    assert restored.start_next_job() is None


def test_job_queue_preserves_retry_queue_order_after_reinitialization(tmp_path) -> None:
    database_path = tmp_path / "job-queue.sqlite3"
    queue = JobQueue(max_attempts=2, database_path=database_path)
    older = queue.create_job(
        idempotency_key="restart-order-older",
        filename="older.pdf",
        mode="standard",
    )
    newer = queue.create_job(
        idempotency_key="restart-order-newer",
        filename="newer.pdf",
        mode="standard",
    )
    running = queue.start_next_job()
    assert running is not None
    assert running.job_id == older.job_id
    queue.mark_failed(running.job_id, error="temporary failure")

    restored_queue = JobQueue(max_attempts=2, database_path=database_path)

    first = restored_queue.start_next_job()
    assert first is not None
    assert first.job_id == newer.job_id
    restored_queue.mark_succeeded(first.job_id, result={})
    second = restored_queue.start_next_job()
    assert second is not None
    assert second.job_id == older.job_id


def test_job_queue_idempotent_retry_keeps_original_template_version() -> None:
    queue = JobQueue()

    created = queue.create_job(
        idempotency_key="upload-with-template",
        filename="batch-record.pdf",
        mode="standard",
        template={
            "template_id": "batch-record",
            "template_version": 2,
            "name": "Batch Record",
        },
    )
    duplicate = queue.create_job(
        idempotency_key="upload-with-template",
        filename="batch-record.pdf",
        mode="standard",
        template={
            "template_id": "batch-record",
            "template_version": 3,
            "name": "Batch Record",
        },
    )

    assert duplicate.job_id == created.job_id
    assert duplicate.template == {
        "template_id": "batch-record",
        "template_version": 2,
        "name": "Batch Record",
    }


def test_job_queue_idempotent_retry_rejects_different_template_binding() -> None:
    queue = JobQueue()
    queue.create_job(
        idempotency_key="upload-with-template",
        filename="batch-record.pdf",
        mode="standard",
        template={
            "template_id": "batch-record",
            "template_version": 2,
            "name": "Batch Record",
        },
    )

    with pytest.raises(ValueError, match="idempotency_key already bound"):
        queue.create_job(
            idempotency_key="upload-with-template",
            filename="batch-record.pdf",
            mode="standard",
            template={
                "template_id": "coa",
                "template_version": 1,
                "name": "Certificate of Analysis",
            },
        )


def test_job_queue_idempotent_retry_rejects_different_uploaded_source() -> None:
    queue = JobQueue()
    queue.create_job(
        idempotency_key="upload-with-source",
        filename="batch-record.pdf",
        mode="standard",
        source={
            "filename": "batch-record.pdf",
            "content_type": "application/pdf",
            "size_bytes": 10,
            "sha256": "0" * 64,
            "content": b"first file",
        },
    )

    with pytest.raises(ValueError, match="idempotency_key already bound"):
        queue.create_job(
            idempotency_key="upload-with-source",
            filename="batch-record.pdf",
            mode="standard",
            source={
                "filename": "batch-record.pdf",
                "content_type": "application/pdf",
                "size_bytes": 11,
                "sha256": "1" * 64,
                "content": b"second file",
            },
        )


def test_job_queue_retries_failed_job_with_bounded_attempts() -> None:
    queue = JobQueue(max_attempts=2)
    created = queue.create_job(
        idempotency_key="upload-1",
        filename="batch-record.pdf",
        mode="standard",
    )

    first_attempt = queue.start_next_job()
    assert first_attempt is not None
    retry = queue.mark_failed(first_attempt.job_id, error="parser unavailable")

    assert retry.status == "queued"
    assert retry.attempts == 1

    second_attempt = queue.start_next_job()
    assert second_attempt is not None
    terminal = queue.mark_failed(second_attempt.job_id, error="parser unavailable")

    assert terminal.status == "failed"
    assert terminal.attempts == 2
    assert queue.create_job(
        idempotency_key="upload-1",
        filename="batch-record.pdf",
        mode="standard",
    ).job_id == created.job_id


def test_job_queue_requeues_explicit_retry_for_failed_job() -> None:
    queue = JobQueue(max_attempts=1)
    created = queue.create_job(
        idempotency_key="upload-1",
        filename="batch-record.pdf",
        mode="standard",
    )
    running = queue.start_next_job()
    assert running is not None
    failed = queue.mark_failed(running.job_id, error="parser unavailable")

    retried = queue.retry_failed_job(failed.job_id)
    next_job = queue.start_next_job()

    assert retried.status == "queued"
    assert retried.error is None
    assert retried.attempts == 1
    assert next_job is not None
    assert next_job.job_id == created.job_id


def test_high_quality_jobs_are_not_admitted_while_another_is_active() -> None:
    queue = JobQueue()
    queue.create_job(
        idempotency_key="hq-1",
        filename="batch-record.pdf",
        mode="high_quality",
    )
    active = queue.start_next_job()
    assert active is not None
    assert active.mode == "high_quality"

    with pytest.raises(RuntimeError, match="high_quality job already active"):
        queue.create_job(
            idempotency_key="hq-2",
            filename="batch-record.pdf",
            mode="high_quality",
        )

    queue.mark_succeeded(active.job_id, result={"status": "converted"})
    admitted = queue.create_job(
        idempotency_key="hq-2",
        filename="batch-record.pdf",
        mode="high_quality",
    )

    assert admitted.status == "queued"
