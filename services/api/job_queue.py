from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Literal
from uuid import uuid4

JobStatus = Literal["queued", "running", "succeeded", "failed"]
JOB_STATUSES: set[JobStatus] = {"queued", "running", "succeeded", "failed"}
TERMINAL_STATUSES: set[JobStatus] = {"succeeded", "failed"}


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    idempotency_key: str
    filename: str
    mode: str
    status: JobStatus
    attempts: int = 0
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: _utc_now())
    updated_at: str = field(default_factory=lambda: _utc_now())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobQueue:
    def __init__(self, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._max_attempts = max_attempts
        self._jobs: dict[str, JobRecord] = {}
        self._idempotency_index: dict[str, str] = {}
        self._pending_job_ids: deque[str] = deque()
        self._lock = Lock()

    def create_job(self, *, idempotency_key: str, filename: str, mode: str) -> JobRecord:
        key = idempotency_key.strip()
        if not key:
            raise ValueError("idempotency_key is required")
        filename = filename.strip()
        if not filename:
            raise ValueError("filename is required")
        if mode not in {"standard", "high_quality"}:
            raise ValueError("unsupported job mode")

        with self._lock:
            existing_id = self._idempotency_index.get(key)
            if existing_id is not None:
                existing = self._jobs[existing_id]
                if existing.filename != filename or existing.mode != mode:
                    raise ValueError("idempotency_key already bound to different job parameters")
                return existing
            if mode == "high_quality" and self._has_active_high_quality_job():
                raise RuntimeError("high_quality job already active")
            job = JobRecord(
                job_id=f"job-{uuid4().hex}",
                idempotency_key=key,
                filename=filename,
                mode=mode,
                status="queued",
            )
            self._jobs[job.job_id] = job
            self._idempotency_index[key] = job.job_id
            self._pending_job_ids.append(job.job_id)
            return job

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(f"unknown job_id: {job_id}") from exc

    def list_jobs(self, *, status: str | None = None) -> list[JobRecord]:
        if status is not None and status not in JOB_STATUSES:
            raise ValueError("unsupported job status")
        with self._lock:
            return [
                job
                for job in self._jobs.values()
                if status is None or job.status == status
            ]

    def start_next_job(self) -> JobRecord | None:
        with self._lock:
            while self._pending_job_ids:
                job_id = self._pending_job_ids.popleft()
                job = self._jobs[job_id]
                if job.status != "queued":
                    continue
                if job.mode == "high_quality" and self._has_running_high_quality_job():
                    self._pending_job_ids.appendleft(job_id)
                    return None
                return self._replace(job, status="running")
            return None

    def mark_succeeded(self, job_id: str, *, result: dict[str, Any]) -> JobRecord:
        with self._lock:
            job = self._require_running_job(job_id)
            return self._replace(job, status="succeeded", result=result, error=None)

    def mark_failed(self, job_id: str, *, error: str) -> JobRecord:
        with self._lock:
            job = self._require_running_job(job_id)
            attempts = job.attempts + 1
            if attempts < self._max_attempts:
                retried = self._replace(job, status="queued", attempts=attempts, error=error)
                self._pending_job_ids.append(job_id)
                return retried
            return self._replace(job, status="failed", attempts=attempts, error=error)

    def _require_running_job(self, job_id: str) -> JobRecord:
        try:
            job = self._jobs[job_id]
        except KeyError as exc:
            raise KeyError(f"unknown job_id: {job_id}") from exc
        if job.status != "running":
            raise ValueError("job must be running")
        return job

    def _replace(self, job: JobRecord, **changes: Any) -> JobRecord:
        values = job.to_dict()
        values.update(changes)
        values["updated_at"] = _utc_now()
        updated = JobRecord(**values)
        self._jobs[job.job_id] = updated
        return updated

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
