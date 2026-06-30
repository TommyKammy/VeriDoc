import pytest

from services.api.job_queue import JobQueue


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
