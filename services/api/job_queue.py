from __future__ import annotations

import base64
from collections import deque
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from os import PathLike
from pathlib import Path
import sqlite3
from threading import Condition, Lock
from time import monotonic
from typing import Any, Callable, Literal
from uuid import uuid4

from services.api.artifact_store import ArtifactFileRecord, ArtifactFileStore

JobStatus = Literal["queued", "running", "succeeded", "failed"]
JOB_STATUSES: set[JobStatus] = {"queued", "running", "succeeded", "failed"}
TERMINAL_STATUSES: set[JobStatus] = {"succeeded", "failed"}
_BYTES_MARKER = "__veridoc_job_queue_bytes__"
_DICT_MARKER = "__veridoc_job_queue_dict__"
_ARTIFACT_MARKER = "__veridoc_job_queue_artifact__"


@dataclass(frozen=True)
class _ArtifactReference:
    artifact_id: str


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    idempotency_key: str
    filename: str
    mode: str
    status: JobStatus
    source: dict[str, Any] | None = None
    template: dict[str, Any] | None = None
    attempts: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None
    retryable: bool = True
    created_at: str = field(default_factory=lambda: _utc_now())
    updated_at: str = field(default_factory=lambda: _utc_now())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobQueue:
    def __init__(
        self,
        *,
        max_attempts: int = 3,
        database_path: str | PathLike[str] | None = None,
        artifact_store_root: str | PathLike[str] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._max_attempts = max_attempts
        self._jobs: dict[str, JobRecord] = {}
        self._idempotency_index: dict[str, str] = {}
        self._pending_job_ids: deque[str] = deque()
        self._pending_sequences: dict[str, int] = {}
        self._next_pending_sequence = 0
        self._unpublished_job_ids: set[str] = set()
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._database_path = str(database_path) if database_path is not None else None
        self._artifact_store: ArtifactFileStore | None = None
        if self._database_path is not None:
            artifact_root = (
                Path(artifact_store_root)
                if artifact_store_root is not None
                else Path(self._database_path).parent / "artifacts"
            )
            self._artifact_store = ArtifactFileStore(artifact_root)
            self._initialize_store()
            self._restore_jobs()

    @property
    def database_path(self) -> str | None:
        return self._database_path

    @property
    def artifact_store_root(self) -> Path | None:
        return self._artifact_store.root if self._artifact_store is not None else None

    def create_job(
        self,
        *,
        idempotency_key: str,
        filename: str,
        mode: str,
        source: dict[str, Any] | None = None,
        template: dict[str, Any] | None = None,
        enqueue: bool = True,
    ) -> JobRecord:
        key, filename, mode = _normalize_job_request(
            idempotency_key=idempotency_key,
            filename=filename,
            mode=mode,
        )

        with self._lock:
            existing = self._get_idempotent_job_locked(
                idempotency_key=key,
                filename=filename,
                mode=mode,
                source=source,
                template=template,
            )
            if existing is not None:
                return existing
            return self._create_job_locked(
                idempotency_key=key,
                filename=filename,
                mode=mode,
                source=source,
                template=template,
                enqueue=enqueue,
            )

    def get_or_create_job(
        self,
        *,
        idempotency_key: str,
        filename: str,
        mode: str,
        source: dict[str, Any] | None = None,
        template: dict[str, Any] | None = None,
        create_template: Callable[[], dict[str, Any] | None] | None = None,
        enqueue: bool = True,
        publish: bool = True,
        include_unpublished: bool = False,
    ) -> tuple[JobRecord, bool]:
        key, filename, mode = _normalize_job_request(
            idempotency_key=idempotency_key,
            filename=filename,
            mode=mode,
        )

        with self._lock:
            existing = self._get_idempotent_job_locked(
                idempotency_key=key,
                filename=filename,
                mode=mode,
                source=source,
                template=template,
                include_unpublished=include_unpublished,
            )
            if existing is not None:
                return existing, False
            stored_template = create_template() if create_template is not None else template
            job = self._create_job_locked(
                idempotency_key=key,
                filename=filename,
                mode=mode,
                source=source,
                template=stored_template,
                enqueue=enqueue,
                publish=publish,
            )
            return job, True

    def publish_job(
        self,
        job_id: str,
        *,
        enqueue: bool = True,
        persist_related: Callable[[sqlite3.Connection], None] | None = None,
    ) -> JobRecord:
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(f"unknown job_id: {job_id}") from exc
            was_unpublished = job_id in self._unpublished_job_ids
            pending_sequence = self._pending_sequences.get(job_id)
            should_enqueue = enqueue and job_id not in self._pending_job_ids
            if should_enqueue:
                if job.status != "queued":
                    if not was_unpublished:
                        return job
                    raise RuntimeError("job is already active")
                pending_sequence = self._allocate_pending_sequence()
            self._persist(
                job,
                queue_sequence=pending_sequence,
                persist_related=persist_related,
            )
            self._unpublished_job_ids.discard(job_id)
            if should_enqueue:
                self._append_pending(job_id, pending_sequence)
            self._condition.notify_all()
            return job

    def enqueue_job(self, job_id: str) -> JobRecord:
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(f"unknown job_id: {job_id}") from exc
            if job.status != "queued":
                raise RuntimeError("job is already active")
            pending_sequence = self._pending_sequences.get(job_id)
            should_enqueue = job_id not in self._pending_job_ids
            if should_enqueue:
                pending_sequence = self._allocate_pending_sequence()
            self._persist(job, queue_sequence=pending_sequence)
            self._unpublished_job_ids.discard(job_id)
            if should_enqueue:
                self._append_pending(job_id, pending_sequence)
            self._condition.notify_all()
            return job

    def discard_queued_job(self, job_id: str) -> None:
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError:
                return
            if job.status != "queued":
                raise RuntimeError("job is already active")
            self._delete_persisted(job_id)
            del self._jobs[job_id]
            self._unpublished_job_ids.discard(job_id)
            self._idempotency_index.pop(job.idempotency_key, None)
            try:
                self._pending_job_ids.remove(job_id)
            except ValueError:
                pass
            self._pending_sequences.pop(job_id, None)
            self._condition.notify_all()

    def is_pending(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._pending_job_ids

    def is_unpublished(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._unpublished_job_ids

    def wait_until_published(self, job_id: str, *, timeout: float = 5.0) -> JobRecord | None:
        deadline = monotonic() + timeout
        with self._condition:
            while True:
                job = self._jobs.get(job_id)
                if job is None:
                    return None
                if job_id not in self._unpublished_job_ids:
                    return job
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise RuntimeError("job creation pending")
                self._condition.wait(remaining)

    def get_idempotent_job(
        self,
        *,
        idempotency_key: str,
        filename: str,
        mode: str,
        source: dict[str, Any] | None = None,
        template: dict[str, Any] | None = None,
    ) -> JobRecord | None:
        key, filename, mode = _normalize_job_request(
            idempotency_key=idempotency_key,
            filename=filename,
            mode=mode,
        )
        with self._lock:
            return self._get_idempotent_job_locked(
                idempotency_key=key,
                filename=filename,
                mode=mode,
                source=source,
                template=template,
                include_unpublished=False,
            )

    def _get_idempotent_job_locked(
        self,
        *,
        idempotency_key: str,
        filename: str,
        mode: str,
        source: dict[str, Any] | None,
        template: dict[str, Any] | None,
        include_unpublished: bool = False,
    ) -> JobRecord | None:
        existing_id = self._idempotency_index.get(idempotency_key)
        if existing_id is None:
            return None
        if existing_id in self._unpublished_job_ids and not include_unpublished:
            raise RuntimeError("job creation pending")
        existing = self._jobs[existing_id]
        if (
            existing.filename != filename
            or existing.mode != mode
            or not _same_source_binding(existing.source, source)
            or not _same_template_binding(existing.template, template)
        ):
            raise ValueError("idempotency_key already bound to different job parameters")
        return existing

    def _create_job_locked(
        self,
        *,
        idempotency_key: str,
        filename: str,
        mode: str,
        source: dict[str, Any] | None,
        template: dict[str, Any] | None,
        enqueue: bool,
        publish: bool = True,
    ) -> JobRecord:
        if mode == "high_quality" and self._has_active_high_quality_job():
            raise RuntimeError("high_quality job already active")
        job = JobRecord(
            job_id=f"job-{uuid4().hex}",
            idempotency_key=idempotency_key,
            filename=filename,
            mode=mode,
            status="queued",
            source=deepcopy(source),
            template=deepcopy(template),
        )
        pending_sequence = (
            self._allocate_pending_sequence() if enqueue and publish else None
        )
        if publish:
            self._persist(job, queue_sequence=pending_sequence)
        self._jobs[job.job_id] = job
        self._idempotency_index[idempotency_key] = job.job_id
        if not publish:
            self._unpublished_job_ids.add(job.job_id)
        if enqueue and publish:
            self._append_pending(job.job_id, pending_sequence)
        return job

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(f"unknown job_id: {job_id}") from exc
            if job_id in self._unpublished_job_ids:
                raise KeyError(f"unknown job_id: {job_id}")
            return job

    def list_jobs(self, *, status: str | None = None) -> list[JobRecord]:
        if status is not None and status not in JOB_STATUSES:
            raise ValueError("unsupported job status")
        with self._lock:
            return [
                job
                for job in self._jobs.values()
                if job.job_id not in self._unpublished_job_ids
                and (status is None or job.status == status)
            ]

    def start_next_job(self) -> JobRecord | None:
        with self._lock:
            while self._pending_job_ids:
                job_id = self._pending_job_ids[0]
                job = self._jobs[job_id]
                if job.status != "queued":
                    self._pending_job_ids.popleft()
                    self._pending_sequences.pop(job_id, None)
                    continue
                if job.mode == "high_quality" and self._has_running_high_quality_job():
                    return None
                pending_sequence = self._pending_sequences.get(job_id)
                running = self._replace(
                    job,
                    queue_sequence=pending_sequence,
                    status="running",
                )
                self._pending_job_ids.popleft()
                self._pending_sequences.pop(job_id, None)
                return running
            return None

    def start_job(self, job_id: str) -> JobRecord:
        """Claim one known queued job for an in-process PoC worker."""
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(f"unknown job_id: {job_id}") from exc
            if job_id in self._unpublished_job_ids:
                raise KeyError(f"unknown job_id: {job_id}")
            if job.status != "queued":
                raise ValueError("job must be queued")
            if job.mode == "high_quality" and self._has_running_high_quality_job():
                raise RuntimeError("high_quality job already running")
            pending_sequence = self._pending_sequences.get(job_id)
            running = self._replace(
                job,
                queue_sequence=pending_sequence,
                status="running",
            )
            try:
                self._pending_job_ids.remove(job_id)
            except ValueError:
                pass
            self._pending_sequences.pop(job_id, None)
            return running

    def mark_succeeded(self, job_id: str, *, result: dict[str, Any]) -> JobRecord:
        with self._lock:
            job = self._require_running_job(job_id)
            return self._replace(job, status="succeeded", result=result, error=None)

    def mark_failed(self, job_id: str, *, error: str, retryable: bool = True) -> JobRecord:
        with self._lock:
            job = self._require_running_job(job_id)
            attempts = job.attempts + 1
            if retryable and attempts < self._max_attempts:
                pending_sequence = self._allocate_pending_sequence()
                retried = self._replace(
                    job,
                    queue_sequence=pending_sequence,
                    status="queued",
                    attempts=attempts,
                    error=error,
                )
                self._append_pending(job_id, pending_sequence)
                return retried
            return self._replace(
                job,
                status="failed",
                attempts=attempts,
                error=error,
                retryable=retryable,
            )

    def retry_failed_job(
        self,
        job_id: str,
        *,
        persist_related: Callable[[sqlite3.Connection], None] | None = None,
    ) -> JobRecord:
        with self._lock:
            try:
                job = self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(f"unknown job_id: {job_id}") from exc
            if job.status != "failed":
                raise ValueError("job must be failed")
            if not job.retryable:
                raise ValueError("job is not retryable")
            if job.mode == "high_quality" and self._has_active_high_quality_job():
                raise RuntimeError("high_quality job already active")
            pending_sequence = self._allocate_pending_sequence()
            retried = self._replace(
                job,
                queue_sequence=pending_sequence,
                persist_related=persist_related,
                status="queued",
                error=None,
            )
            self._append_pending(job_id, pending_sequence)
            return retried

    def _require_running_job(self, job_id: str) -> JobRecord:
        try:
            job = self._jobs[job_id]
        except KeyError as exc:
            raise KeyError(f"unknown job_id: {job_id}") from exc
        if job.status != "running":
            raise ValueError("job must be running")
        return job

    def _replace(
        self,
        job: JobRecord,
        *,
        queue_sequence: int | None = None,
        persist_related: Callable[[sqlite3.Connection], None] | None = None,
        **changes: Any,
    ) -> JobRecord:
        values = job.to_dict()
        values.update(changes)
        values["updated_at"] = _utc_now()
        updated = JobRecord(**values)
        self._persist(
            updated,
            queue_sequence=queue_sequence,
            persist_related=persist_related,
        )
        self._jobs[job.job_id] = updated
        return updated

    def _initialize_store(self) -> None:
        Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_queue_records (
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL CHECK(attempts >= 0),
                    queue_sequence INTEGER,
                    record_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            artifact_table_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(job_queue_artifacts)")
            }
            if "content" in artifact_table_columns:
                legacy_artifacts = connection.execute(
                    "SELECT job_id, artifact_id, content FROM job_queue_artifacts"
                ).fetchall()
                migrated_artifacts = [
                    (job_id, artifact_id, self._artifact_store.save(bytes(content)))
                    for job_id, artifact_id, content in legacy_artifacts
                ]
                connection.execute(
                    "ALTER TABLE job_queue_artifacts "
                    "RENAME TO job_queue_artifacts_with_content"
                )
            else:
                migrated_artifacts = []
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS job_queue_artifacts (
                    job_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
                    PRIMARY KEY(job_id, artifact_id),
                    FOREIGN KEY(job_id) REFERENCES job_queue_records(job_id)
                        ON DELETE CASCADE
                )
                """
            )
            if migrated_artifacts:
                connection.executemany(
                    "INSERT INTO job_queue_artifacts("
                    "job_id, artifact_id, content_sha256, size_bytes"
                    ") VALUES (?, ?, ?, ?)",
                    [
                        (
                            job_id,
                            artifact_id,
                            artifact.content_sha256,
                            artifact.size_bytes,
                        )
                        for job_id, artifact_id, artifact in migrated_artifacts
                    ],
                )
            if "content" in artifact_table_columns:
                connection.execute("DROP TABLE job_queue_artifacts_with_content")
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(job_queue_records)")
            }
            if "queue_sequence" not in columns:
                connection.execute(
                    "ALTER TABLE job_queue_records ADD COLUMN queue_sequence INTEGER"
                )
                connection.execute(
                    "UPDATE job_queue_records SET queue_sequence = rowid "
                    "WHERE status = 'queued'"
                )

    def _restore_jobs(self) -> None:
        with sqlite3.connect(self._database_path) as connection:
            rows = connection.execute(
                "SELECT rowid, record_json, queue_sequence "
                "FROM job_queue_records ORDER BY rowid"
            ).fetchall()
            artifact_rows = connection.execute(
                "SELECT job_id, artifact_id, content_sha256, size_bytes "
                "FROM job_queue_artifacts"
            ).fetchall()
        artifacts_by_job: dict[str, dict[str, bytes]] = {}
        for job_id, artifact_id, content_sha256, size_bytes in artifact_rows:
            artifacts_by_job.setdefault(job_id, {})[artifact_id] = (
                self._artifact_store.read(content_sha256, size_bytes=size_bytes)
            )
        pending_jobs: list[tuple[int, int, str]] = []
        running_jobs: list[tuple[int, int | None, JobRecord]] = []
        for rowid, record_json, queue_sequence in rows:
            values = _decode_persisted_value(json.loads(record_json))
            values = _restore_binary_artifacts(
                values,
                artifacts_by_job.get(values.get("job_id"), {}),
            )
            job = JobRecord(**values)
            if job.status not in JOB_STATUSES:
                raise ValueError(f"unsupported persisted job status: {job.status}")
            self._jobs[job.job_id] = job
            self._idempotency_index[job.idempotency_key] = job.job_id
            if job.status == "running":
                running_jobs.append((rowid, queue_sequence, job))
            elif job.status == "queued" and queue_sequence is not None:
                pending_jobs.append((queue_sequence, rowid, job.job_id))

        fallback_sequence = min((item[0] for item in pending_jobs), default=0) - len(
            running_jobs
        )
        for index, (rowid, queue_sequence, job) in enumerate(running_jobs):
            sequence = (
                fallback_sequence + index
                if queue_sequence is None
                else queue_sequence
            )
            values = job.to_dict()
            values.update(status="queued", updated_at=_utc_now())
            recovered = JobRecord(**values)
            self._persist(recovered, queue_sequence=sequence)
            self._jobs[job.job_id] = recovered
            pending_jobs.append((sequence, rowid, job.job_id))

        for sequence, _, job_id in sorted(pending_jobs):
            self._pending_job_ids.append(job_id)
            self._pending_sequences[job_id] = sequence
        if pending_jobs:
            self._next_pending_sequence = max(item[0] for item in pending_jobs) + 1

    def _persist(
        self,
        job: JobRecord,
        *,
        queue_sequence: int | None = None,
        persist_related: Callable[[sqlite3.Connection], None] | None = None,
    ) -> None:
        if self._database_path is None:
            if persist_related is not None:
                raise ValueError("related persistence requires a durable job queue")
            return
        persisted_record, artifacts = _extract_binary_artifacts(job.to_dict())
        stored_artifacts: dict[str, ArtifactFileRecord] = {}
        try:
            for artifact_id, content in artifacts.items():
                stored_artifacts[artifact_id] = self._artifact_store.save(content)
        except Exception:
            self._cleanup_unreferenced_artifacts(
                artifact
                for artifact in stored_artifacts.values()
                if artifact.created
            )
            raise
        previous_hashes: set[str] = set()
        try:
            with sqlite3.connect(self._database_path) as connection:
                previous_hashes = {
                    row[0]
                    for row in connection.execute(
                        "SELECT content_sha256 FROM job_queue_artifacts WHERE job_id = ?",
                        (job.job_id,),
                    )
                }
                connection.execute(
                """
                INSERT INTO job_queue_records(
                    job_id, idempotency_key, status, attempts, queue_sequence,
                    record_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    idempotency_key = excluded.idempotency_key,
                    status = excluded.status,
                    attempts = excluded.attempts,
                    queue_sequence = excluded.queue_sequence,
                    record_json = excluded.record_json,
                    updated_at = excluded.updated_at
                """,
                (
                    job.job_id,
                    job.idempotency_key,
                    job.status,
                    job.attempts,
                    queue_sequence,
                    json.dumps(
                        _encode_persisted_value(persisted_record),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    job.updated_at,
                ),
            )
                connection.execute(
                    "DELETE FROM job_queue_artifacts WHERE job_id = ?", (job.job_id,)
                )
                connection.executemany(
                    "INSERT INTO job_queue_artifacts("
                    "job_id, artifact_id, content_sha256, size_bytes"
                    ") VALUES (?, ?, ?, ?)",
                    [
                        (
                            job.job_id,
                            artifact_id,
                            artifact.content_sha256,
                            artifact.size_bytes,
                        )
                        for artifact_id, artifact in stored_artifacts.items()
                    ],
                )
                if persist_related is not None:
                    persist_related(connection)
        except Exception:
            self._cleanup_unreferenced_artifacts(
                artifact
                for artifact in stored_artifacts.values()
                if artifact.created
            )
            raise
        self._cleanup_unreferenced_artifacts(
            ArtifactFileRecord(content_sha256, 0, False)
            for content_sha256 in previous_hashes
            if content_sha256
            not in {artifact.content_sha256 for artifact in stored_artifacts.values()}
        )

    def _allocate_pending_sequence(self) -> int:
        sequence = self._next_pending_sequence
        self._next_pending_sequence += 1
        return sequence

    def _append_pending(self, job_id: str, sequence: int | None) -> None:
        if sequence is None:
            raise ValueError("pending jobs require a queue sequence")
        self._pending_job_ids.append(job_id)
        self._pending_sequences[job_id] = sequence

    def _delete_persisted(self, job_id: str) -> None:
        if self._database_path is None:
            return
        with sqlite3.connect(self._database_path) as connection:
            artifact_hashes = [
                row[0]
                for row in connection.execute(
                    "SELECT content_sha256 FROM job_queue_artifacts WHERE job_id = ?",
                    (job_id,),
                )
            ]
            connection.execute(
                "DELETE FROM job_queue_artifacts WHERE job_id = ?", (job_id,)
            )
            connection.execute("DELETE FROM job_queue_records WHERE job_id = ?", (job_id,))
        self._cleanup_unreferenced_artifacts(
            ArtifactFileRecord(content_sha256, 0, False)
            for content_sha256 in artifact_hashes
        )

    def _cleanup_unreferenced_artifacts(
        self, artifacts: Iterable[ArtifactFileRecord]
    ) -> None:
        if self._database_path is None or self._artifact_store is None:
            return
        with sqlite3.connect(self._database_path) as connection:
            for artifact in artifacts:
                referenced = connection.execute(
                    "SELECT 1 FROM job_queue_artifacts "
                    "WHERE content_sha256 = ? LIMIT 1",
                    (artifact.content_sha256,),
                ).fetchone()
                if referenced is None:
                    self._artifact_store.delete(artifact.content_sha256)

    def _has_active_high_quality_job(self) -> bool:
        return any(
            job.mode == "high_quality" and job.status not in TERMINAL_STATUSES
            for job in self._jobs.values()
        )

    def _has_running_high_quality_job(self) -> bool:
        return any(
            job.mode == "high_quality" and job.status == "running"
            for job in self._jobs.values()
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_binary_artifacts(value: Any) -> tuple[Any, dict[str, bytes]]:
    artifacts: dict[str, bytes] = {}

    def project(item: Any) -> Any:
        if isinstance(item, bytes):
            artifact_id = str(len(artifacts))
            artifacts[artifact_id] = item
            return _ArtifactReference(artifact_id)
        if isinstance(item, dict):
            return {key: project(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [project(child) for child in item]
        return item

    return project(value), artifacts


def _restore_binary_artifacts(value: Any, artifacts: dict[str, bytes]) -> Any:
    if isinstance(value, _ArtifactReference):
        if value.artifact_id not in artifacts:
            raise ValueError("persisted job artifact is missing")
        return artifacts[value.artifact_id]
    if isinstance(value, dict):
        return {
            key: _restore_binary_artifacts(item, artifacts)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_restore_binary_artifacts(item, artifacts) for item in value]
    return value


def _encode_persisted_value(value: Any) -> Any:
    if isinstance(value, _ArtifactReference):
        return {_ARTIFACT_MARKER: value.artifact_id}
    if isinstance(value, bytes):
        return {_BYTES_MARKER: base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {
            _DICT_MARKER: [
                [key, _encode_persisted_value(item)] for key, item in value.items()
            ]
        }
    if isinstance(value, list):
        return [_encode_persisted_value(item) for item in value]
    if isinstance(value, tuple):
        return [_encode_persisted_value(item) for item in value]
    return value


def _decode_persisted_value(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {_ARTIFACT_MARKER}:
            artifact_id = value[_ARTIFACT_MARKER]
            if not isinstance(artifact_id, str):
                raise ValueError("invalid persisted artifact reference")
            return _ArtifactReference(artifact_id)
        if set(value) == {_BYTES_MARKER}:
            encoded = value[_BYTES_MARKER]
            if not isinstance(encoded, str):
                raise ValueError("invalid persisted byte payload")
            try:
                return base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise ValueError("invalid persisted byte payload") from exc
        if set(value) == {_DICT_MARKER}:
            items = value[_DICT_MARKER]
            if not isinstance(items, list):
                raise ValueError("invalid persisted dictionary payload")
            decoded: dict[str, Any] = {}
            for item in items:
                if (
                    not isinstance(item, list)
                    or len(item) != 2
                    or not isinstance(item[0], str)
                    or item[0] in decoded
                ):
                    raise ValueError("invalid persisted dictionary payload")
                decoded[item[0]] = _decode_persisted_value(item[1])
            return decoded
        return {key: _decode_persisted_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_persisted_value(item) for item in value]
    return value


def _normalize_job_request(
    *,
    idempotency_key: str,
    filename: str,
    mode: str,
) -> tuple[str, str, str]:
    key = idempotency_key.strip()
    if not key:
        raise ValueError("idempotency_key is required")
    filename = filename.strip()
    if not filename:
        raise ValueError("filename is required")
    if mode not in {"standard", "high_quality"}:
        raise ValueError("unsupported job mode")
    return key, filename, mode


def _same_source_binding(
    existing_source: dict[str, Any] | None,
    requested_source: dict[str, Any] | None,
) -> bool:
    if existing_source is None or requested_source is None:
        return existing_source is requested_source
    metadata_matches = (
        existing_source.get("sha256") == requested_source.get("sha256")
        and existing_source.get("size_bytes") == requested_source.get("size_bytes")
        and existing_source.get("content_type") == requested_source.get("content_type")
    )
    if not metadata_matches:
        return False
    existing_content = existing_source.get("content")
    requested_content = requested_source.get("content")
    if existing_content is None and isinstance(requested_content, bytes):
        return existing_source.get("sha256") == sha256(requested_content).hexdigest()
    return existing_content == requested_content


def _same_template_binding(
    existing_template: dict[str, Any] | None,
    requested_template: dict[str, Any] | None,
) -> bool:
    if existing_template is None or requested_template is None:
        return existing_template is requested_template

    existing_template_id = existing_template.get("template_id")
    requested_template_id = requested_template.get("template_id")
    if (
        isinstance(existing_template_id, str)
        and existing_template_id.strip()
        and isinstance(requested_template_id, str)
        and requested_template_id.strip()
    ):
        return existing_template_id == requested_template_id

    return existing_template == requested_template
