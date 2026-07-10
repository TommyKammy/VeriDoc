from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar


SCHEMA_VERSION = "20260710_14_fail_closed_action_contract"
AUDIT_INTEGRITY_ALGORITHM = "sha256-canonical-json-chain-v1"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "var" / "veridoc" / "dev.sqlite3"
_SOURCE_ARTIFACT_INSERT_BINDING: ContextVar[tuple[str, ...] | None] = ContextVar(
    "veridoc_source_artifact_insert_binding",
    default=None,
)


def _source_artifact_insert_allowed(*binding: str) -> int:
    return int(_SOURCE_ARTIFACT_INSERT_BINDING.get() == binding)


@dataclass(frozen=True)
class Document:
    document_id: str
    source_type: str
    original_filename: str
    source_artifact_id: str
    source_storage_key: str
    content_hash: str
    status: str
    uploaded_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SourceArtifact:
    artifact_id: str
    document_id: str
    storage_key: str
    content_hash: str
    source_type: str
    original_filename: str
    uploaded_by: str
    created_at: str


@dataclass(frozen=True)
class ConversionJob:
    job_id: str
    document_id: str
    idempotency_key: str
    mode: str
    status: str
    attempts: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class JobEvent:
    event_id: str
    job_id: str
    sequence: int
    event_type: str
    actor: str
    payload_json: str
    created_at: str


@dataclass(frozen=True)
class ConversionResult:
    result_id: str
    job_id: str
    document_id: str
    status: str
    content_hash: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    result_id: str
    job_id: str
    document_id: str
    category: str
    format: str
    display_filename: str
    storage_key: str
    content_hash: str
    retention_state: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReviewItem:
    review_item_id: str
    document_id: str
    job_id: str
    target_path: str
    status: str
    severity: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReviewDecision:
    decision_id: str
    review_item_id: str
    artifact_id: str
    job_id: str
    document_id: str
    actor: str
    role: str
    decision: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    job_id: str
    document_id: str
    sequence: int
    integrity_algorithm: str
    actor: str
    action: str
    scope_type: str
    scope_id: str
    event_hash: str
    prev_event_hash: str | None
    payload_json: str
    created_at: str


RecordT = TypeVar("RecordT")


class SQLitePersistenceRepository:
    def __init__(
        self,
        db_path: str | os.PathLike[str] | None = None,
        *,
        _connection: sqlite3.Connection | None = None,
        _closed: bool = False,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_database_path()
        self._connection = _connection
        self._closed = _closed

    def initialize(self) -> None:
        self.db_path = _validate_database_path(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            with connection:
                _validate_managed_schema(connection, allow_missing=True)
                connection.executescript(_SCHEMA_SQL)
                _validate_managed_schema(connection, allow_missing=False)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO schema_migrations(migration_id, applied_at)
                    VALUES (?, ?)
                    """,
                    (SCHEMA_VERSION, _utc_now()),
                )
        finally:
            connection.close()

    def reset(self) -> None:
        self.db_path = _validate_database_path(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            with connection:
                connection.executescript(_RESET_SQL)
        finally:
            connection.close()
        self.initialize()

    def create_document(
        self,
        *,
        document_id: str,
        source_type: str,
        original_filename: str,
        source_artifact_id: str,
        source_storage_key: str,
        content_hash: str,
        status: str,
        uploaded_by: str,
    ) -> Document:
        _require_non_empty(
            document_id=document_id,
            source_type=source_type,
            original_filename=original_filename,
            source_artifact_id=source_artifact_id,
            source_storage_key=source_storage_key,
            status=status,
            uploaded_by=uploaded_by,
        )
        _require_sha256(content_hash, field_name="content_hash")
        now = _utc_now()
        with self._connection_scope(immediate=True) as connection:
            source_binding = (
                source_artifact_id,
                document_id,
                source_storage_key,
                content_hash,
                source_type,
                original_filename,
                uploaded_by,
            )
            guard_token = _SOURCE_ARTIFACT_INSERT_BINDING.set(source_binding)
            try:
                connection.execute(
                    """
                    INSERT INTO source_artifacts(
                        artifact_id, document_id, storage_key, content_hash, source_type,
                        original_filename, uploaded_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*source_binding, now),
                )
                document = self._insert_and_get_from_connection(
                    connection,
                    Document,
                    """
                    INSERT INTO source_documents(
                        document_id, source_type, original_filename, source_artifact_id,
                        source_storage_key, content_hash, status, uploaded_by, created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        source_type,
                        original_filename,
                        source_artifact_id,
                        source_storage_key,
                        content_hash,
                        status,
                        uploaded_by,
                        now,
                        now,
                    ),
                    "SELECT * FROM source_documents WHERE document_id = ?",
                    (document_id,),
                )
            finally:
                _SOURCE_ARTIFACT_INSERT_BINDING.reset(guard_token)
            return document

    def get_document(self, document_id: str) -> Document | None:
        with self._connection_scope() as connection:
            row = connection.execute(
                "SELECT * FROM source_documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            if row is None:
                return None
            self._require_document_source_artifact(connection, row)
        return _row_to_dataclass(Document, row)

    def get_source_artifact(self, artifact_id: str) -> SourceArtifact | None:
        with self._connection_scope() as connection:
            row = connection.execute(
                "SELECT * FROM source_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                return None
            self._require_source_artifact_document(connection, row)
        return _row_to_dataclass(SourceArtifact, row)

    def create_conversion_job(
        self,
        *,
        job_id: str,
        document_id: str,
        idempotency_key: str,
        mode: str,
        status: str,
        attempts: int = 0,
    ) -> ConversionJob:
        _require_non_empty(
            job_id=job_id,
            document_id=document_id,
            idempotency_key=idempotency_key,
            mode=mode,
            status=status,
        )
        if attempts < 0:
            raise ValueError("attempts must not be negative")
        now = _utc_now()
        return self._insert_and_get(
            ConversionJob,
            """
            INSERT INTO jobs(
                job_id, document_id, idempotency_key, mode, status, attempts,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, document_id, idempotency_key, mode, status, attempts, now, now),
            "SELECT * FROM jobs WHERE job_id = ?",
            (job_id,),
        )

    def create_or_get_conversion_job(
        self,
        *,
        job_id: str,
        document_id: str,
        idempotency_key: str,
        mode: str,
        status: str,
        attempts: int = 0,
    ) -> ConversionJob:
        _require_non_empty(
            job_id=job_id,
            document_id=document_id,
            idempotency_key=idempotency_key,
            mode=mode,
            status=status,
        )
        if attempts < 0:
            raise ValueError("attempts must not be negative")
        now = _utc_now()
        with self._connection_scope(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["document_id"] != document_id or existing["mode"] != mode:
                    raise ValueError(
                        "idempotency_key already bound to different job parameters"
                    )
                return _row_to_dataclass(ConversionJob, existing)
            return self._insert_and_get_from_connection(
                connection,
                ConversionJob,
                """
                INSERT INTO jobs(
                    job_id, document_id, idempotency_key, mode, status, attempts,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, document_id, idempotency_key, mode, status, attempts, now, now),
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            )

    def get_conversion_job(self, job_id: str) -> ConversionJob | None:
        return self._get_one(ConversionJob, "SELECT * FROM jobs WHERE job_id = ?", (job_id,))

    def get_conversion_job_by_idempotency_key(self, idempotency_key: str) -> ConversionJob | None:
        _require_non_empty(idempotency_key=idempotency_key)
        return self._get_one(
            ConversionJob,
            "SELECT * FROM jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        )

    def create_job_event(
        self,
        *,
        event_id: str,
        job_id: str,
        event_type: str,
        actor: str,
        payload: Mapping[str, Any] | None = None,
    ) -> JobEvent:
        _require_non_empty(
            event_id=event_id,
            job_id=job_id,
            event_type=event_type,
            actor=actor,
        )
        with self._connection_scope(immediate=True) as connection:
            self._verify_job_event_history(connection, job_id)
            job = connection.execute(
                """
                SELECT jobs.job_id, jobs.document_id, jobs.idempotency_key,
                       jobs.mode, jobs.status, jobs.attempts,
                       source_documents.original_filename,
                       source_documents.content_hash AS source_content_hash,
                       source_documents.uploaded_by
                FROM jobs
                JOIN source_documents
                  ON source_documents.document_id = jobs.document_id
                WHERE jobs.job_id = ?
                """,
                (job_id,),
            ).fetchone()
            if job is None:
                raise ValueError("job_id must reference an existing job")
            sequence = self._next_job_event_sequence(connection, job_id)
            now = _utc_now()
            event_payload = payload or {"event_type": event_type}
            effective_action = _require_job_event_payload_matches(
                event_payload,
                event_id=event_id,
                job_id=job_id,
                event_type=event_type,
                actor=actor,
            )
            contract = _require_audit_action_contract(effective_action)
            if "job_event" not in contract.scope_types:
                raise ValueError("job event action is not valid for job-event history")
            _require_job_action_available(
                job["status"],
                effective_action,
                attempts=job["attempts"],
            )
            _require_declared_audit_scope_payload(
                "job",
                job,
                event_payload,
                allowed_aliases=_evidence_aliases_for_contract(contract),
            )
            if contract.requires_uploader and job["uploaded_by"] != actor:
                raise ValueError("job event actor must match the recorded uploader")
            evidence_artifact_ids: tuple[str, ...] = ()
            if contract.evidence_type == "download_artifact":
                evidence_artifact_ids = self._require_download_evidence(
                    connection,
                    job_id=job_id,
                    document_id=job["document_id"],
                    action=effective_action,
                    payload=event_payload,
                    evidence_event_id=None,
                )
                event_payload = {
                    **event_payload,
                    "evidence": {
                        "artifact_ids": sorted(evidence_artifact_ids),
                        "type": contract.evidence_type,
                    },
                }
            payload_json = _canonical_json(event_payload)
            job_event = self._insert_and_get_from_connection(
                connection,
                JobEvent,
                """
                INSERT INTO job_events(
                    event_id, job_id, sequence, event_type, actor, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, job_id, sequence, event_type, actor, payload_json, now),
                "SELECT * FROM job_events WHERE event_id = ?",
                (event_id,),
            )
            if contract.evidence_type is not None:
                connection.executemany(
                    """
                    INSERT INTO job_event_evidence(event_id, artifact_id, evidence_type)
                    VALUES (?, ?, ?)
                    """,
                    (
                        (event_id, artifact_id, contract.evidence_type)
                        for artifact_id in evidence_artifact_ids
                    ),
                )
            return job_event

    def get_job_event(self, event_id: str) -> JobEvent | None:
        with self._connection_scope() as connection:
            row = connection.execute(
                "SELECT * FROM job_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is not None:
                self._verify_job_event_history(connection, row["job_id"])
        if row is None:
            return None
        return _row_to_dataclass(JobEvent, row)

    def list_job_events(self, job_id: str) -> list[JobEvent]:
        _require_non_empty(job_id=job_id)
        with self._connection_scope() as connection:
            rows = self._verify_job_event_history(connection, job_id)
        return [_row_to_dataclass(JobEvent, row) for row in rows]

    def create_conversion_result(
        self,
        *,
        result_id: str,
        job_id: str,
        document_id: str,
        status: str,
        content_hash: str,
    ) -> ConversionResult:
        _require_non_empty(
            result_id=result_id,
            job_id=job_id,
            document_id=document_id,
            status=status,
        )
        _require_sha256(content_hash, field_name="content_hash")
        now = _utc_now()
        with self._connection_scope() as connection:
            self._require_job_document(connection, job_id, document_id)
            return self._insert_and_get_from_connection(
                connection,
                ConversionResult,
                """
                INSERT INTO conversion_results(
                    result_id, job_id, document_id, status, content_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (result_id, job_id, document_id, status, content_hash, now, now),
                "SELECT * FROM conversion_results WHERE result_id = ?",
                (result_id,),
            )

    def get_conversion_result(self, result_id: str) -> ConversionResult | None:
        return self._get_one(
            ConversionResult,
            "SELECT * FROM conversion_results WHERE result_id = ?",
            (result_id,),
        )

    def create_artifact(
        self,
        *,
        artifact_id: str,
        result_id: str,
        job_id: str,
        document_id: str,
        category: str,
        format: str,
        display_filename: str,
        storage_key: str,
        content_hash: str,
        retention_state: str = "active",
    ) -> Artifact:
        _require_non_empty(
            artifact_id=artifact_id,
            result_id=result_id,
            job_id=job_id,
            document_id=document_id,
            category=category,
            format=format,
            display_filename=display_filename,
            storage_key=storage_key,
            retention_state=retention_state,
        )
        _require_sha256(content_hash, field_name="content_hash")
        now = _utc_now()
        with self._connection_scope() as connection:
            self._require_result_scope(connection, result_id, job_id, document_id)
            return self._insert_and_get_from_connection(
                connection,
                Artifact,
                """
                INSERT INTO generated_artifacts(
                    artifact_id, result_id, job_id, document_id, category, format,
                    display_filename, storage_key, content_hash, retention_state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    result_id,
                    job_id,
                    document_id,
                    category,
                    format,
                    display_filename,
                    storage_key,
                    content_hash,
                    retention_state,
                    now,
                    now,
                ),
                "SELECT * FROM generated_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            )

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        return self._get_one(
            Artifact,
            "SELECT * FROM generated_artifacts WHERE artifact_id = ?",
            (artifact_id,),
        )

    def create_review_item(
        self,
        *,
        review_item_id: str,
        document_id: str,
        job_id: str,
        target_path: str,
        status: str,
        severity: str,
    ) -> ReviewItem:
        _require_non_empty(
            review_item_id=review_item_id,
            document_id=document_id,
            job_id=job_id,
            target_path=target_path,
            status=status,
            severity=severity,
        )
        now = _utc_now()
        with self._connection_scope() as connection:
            self._require_job_document(connection, job_id, document_id)
            return self._insert_and_get_from_connection(
                connection,
                ReviewItem,
                """
                INSERT INTO review_items(
                    review_item_id, document_id, job_id, target_path, status, severity,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (review_item_id, document_id, job_id, target_path, status, severity, now, now),
                "SELECT * FROM review_items WHERE review_item_id = ?",
                (review_item_id,),
            )

    def get_review_item(self, review_item_id: str) -> ReviewItem | None:
        return self._get_one(
            ReviewItem,
            "SELECT * FROM review_items WHERE review_item_id = ?",
            (review_item_id,),
        )

    def create_review_decision(
        self,
        *,
        decision_id: str,
        review_item_id: str,
        artifact_id: str,
        actor: str,
        role: str,
        decision: str,
    ) -> ReviewDecision:
        _require_non_empty(
            decision_id=decision_id,
            review_item_id=review_item_id,
            artifact_id=artifact_id,
            actor=actor,
            role=role,
            decision=decision,
        )
        now = _utc_now()
        with self._connection_scope() as connection:
            job_id, document_id = self._require_review_item_matches_artifact(
                connection,
                review_item_id,
                artifact_id,
            )
            return self._insert_and_get_from_connection(
                connection,
                ReviewDecision,
                """
                INSERT INTO review_decisions(
                    decision_id, review_item_id, artifact_id, job_id, document_id,
                    actor, role, decision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    review_item_id,
                    artifact_id,
                    job_id,
                    document_id,
                    actor,
                    role,
                    decision,
                    now,
                    now,
                ),
                "SELECT * FROM review_decisions WHERE decision_id = ?",
                (decision_id,),
            )

    def get_review_decision(self, decision_id: str) -> ReviewDecision | None:
        return self._get_one(
            ReviewDecision,
            "SELECT * FROM review_decisions WHERE decision_id = ?",
            (decision_id,),
        )

    def create_audit_event(
        self,
        *,
        event_id: str,
        job_id: str,
        document_id: str,
        actor: str,
        action: str,
        scope_type: str,
        scope_id: str,
        integrity_algorithm: str = AUDIT_INTEGRITY_ALGORITHM,
        sequence: int | None = None,
        event_hash: str | None = None,
        prev_event_hash: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        _require_non_empty(
            event_id=event_id,
            job_id=job_id,
            document_id=document_id,
            integrity_algorithm=integrity_algorithm,
            actor=actor,
            action=action,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        if integrity_algorithm != AUDIT_INTEGRITY_ALGORITHM:
            raise ValueError(f"integrity_algorithm must be {AUDIT_INTEGRITY_ALGORITHM}")
        if sequence is not None:
            raise ValueError("sequence is derived from the audit chain")
        if event_hash is not None:
            raise ValueError("event_hash is derived from the audit chain")
        if prev_event_hash is not None:
            raise ValueError("prev_event_hash is derived from the audit chain")
        _require_audit_event_payload_matches(
            payload,
            event_id=event_id,
            job_id=job_id,
            document_id=document_id,
            integrity_algorithm=integrity_algorithm,
            actor=actor,
            action=action,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        input_payload = payload or {
            "action": action,
            "actor": actor,
            "scope_id": scope_id,
            "scope_type": scope_type,
        }
        audit_payload = _load_json_object(
            _canonical_json(input_payload),
            field_name="audit event payload_json",
        )
        with self._connection_scope(immediate=True) as connection:
            now = _utc_now()
            self._require_job_document(connection, job_id, document_id)
            evidence_artifact_ids = self._require_audit_scope(
                connection,
                scope_type,
                scope_id,
                job_id,
                document_id,
                actor,
                action,
                audit_payload,
            )
            contract = _require_audit_action_contract(action)
            if contract.evidence_type is not None:
                audit_payload = {
                    **audit_payload,
                    "evidence": {
                        "artifact_ids": sorted(evidence_artifact_ids),
                        "type": contract.evidence_type,
                    },
                }
            payload_json = _canonical_json(audit_payload)
            self._verify_audit_chain(connection)
            sequence, prev_event_hash = self._next_audit_chain_fields(connection)
            event_hash = _audit_event_hash(
                event_id=event_id,
                job_id=job_id,
                document_id=document_id,
                sequence=sequence,
                integrity_algorithm=integrity_algorithm,
                actor=actor,
                action=action,
                scope_type=scope_type,
                scope_id=scope_id,
                prev_event_hash=prev_event_hash,
                payload_json=payload_json,
                created_at=now,
            )
            audit_event = self._insert_and_get_from_connection(
                connection,
                AuditEvent,
                """
                INSERT INTO audit_events(
                    event_id, job_id, document_id, sequence, integrity_algorithm, actor,
                    action, scope_type, scope_id, event_hash, prev_event_hash, payload_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    job_id,
                    document_id,
                    sequence,
                    integrity_algorithm,
                    actor,
                    action,
                    scope_type,
                    scope_id,
                    event_hash,
                    prev_event_hash,
                    payload_json,
                    now,
                ),
                "SELECT * FROM audit_events WHERE event_id = ?",
                (event_id,),
            )
            if contract.evidence_type is not None:
                connection.executemany(
                    """
                    INSERT INTO audit_event_evidence(event_id, artifact_id, evidence_type)
                    VALUES (?, ?, ?)
                    """,
                    (
                        (event_id, artifact_id, contract.evidence_type)
                        for artifact_id in evidence_artifact_ids
                    ),
                )
            return audit_event

    def get_audit_event(self, event_id: str) -> AuditEvent | None:
        with self._connection_scope() as connection:
            row = connection.execute(
                "SELECT * FROM audit_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                return None
            self._verify_audit_chain(connection)
        return _row_to_dataclass(AuditEvent, row)

    @contextmanager
    def transaction(self) -> Iterator["SQLitePersistenceRepository"]:
        if self._connection is not None:
            if self._closed:
                raise RuntimeError("transaction is closed")
            yield self
            return

        connection = self._connect()
        transaction_repository = SQLitePersistenceRepository(self.db_path, _connection=connection)
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield transaction_repository
        finally:
            transaction_repository._closed = True
            connection.close()

    def _ensure_transaction_open(self) -> None:
        if self._closed:
            raise RuntimeError("transaction is closed")

    def _insert_and_get(
        self,
        record_type: type[RecordT],
        insert_sql: str,
        insert_params: Iterable[Any],
        select_sql: str,
        select_params: Iterable[Any],
    ) -> RecordT:
        with self._connection_scope() as connection:
            return self._insert_and_get_from_connection(
                connection,
                record_type,
                insert_sql,
                insert_params,
                select_sql,
                select_params,
            )

    def _insert_and_get_from_connection(
        self,
        connection: sqlite3.Connection,
        record_type: type[RecordT],
        insert_sql: str,
        insert_params: Iterable[Any],
        select_sql: str,
        select_params: Iterable[Any],
    ) -> RecordT:
        connection.execute(insert_sql, tuple(insert_params))
        row = connection.execute(select_sql, tuple(select_params)).fetchone()
        if row is None:
            raise RuntimeError("insert succeeded without a readable row")
        return _row_to_dataclass(record_type, row)

    def _get_one(
        self,
        record_type: type[RecordT],
        sql: str,
        params: Iterable[Any],
    ) -> RecordT | None:
        with self._connection_scope() as connection:
            row = connection.execute(sql, tuple(params)).fetchone()
        if row is None:
            return None
        return _row_to_dataclass(record_type, row)

    def _get_many(
        self,
        record_type: type[RecordT],
        sql: str,
        params: Iterable[Any],
    ) -> list[RecordT]:
        with self._connection_scope() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [_row_to_dataclass(record_type, row) for row in rows]

    @contextmanager
    def _connection_scope(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        self._ensure_transaction_open()
        if self._connection is not None:
            yield self._connection
            return

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        self.db_path = _validate_database_path(self.db_path)
        connection = sqlite3.connect(self.db_path)
        connection.create_function(
            "veridoc_source_artifact_insert_allowed",
            7,
            _source_artifact_insert_allowed,
            deterministic=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _require_job_document(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        document_id: str,
    ) -> None:
        row = connection.execute(
            "SELECT document_id FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError("job_id must reference an existing job")
        if row["document_id"] != document_id:
            raise ValueError("job_id and document_id must refer to the same document")

    def _require_result_scope(
        self,
        connection: sqlite3.Connection,
        result_id: str,
        job_id: str,
        document_id: str,
    ) -> None:
        row = connection.execute(
            "SELECT job_id, document_id FROM conversion_results WHERE result_id = ?",
            (result_id,),
        ).fetchone()
        if row is None:
            raise ValueError("result_id must reference an existing conversion result")
        if row["job_id"] != job_id or row["document_id"] != document_id:
            raise ValueError("result_id, job_id, and document_id must refer to the same conversion")

    def _require_source_artifact_document(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> None:
        document = connection.execute(
            """
            SELECT source_artifact_id, source_storage_key, content_hash,
                   source_type, original_filename, uploaded_by
            FROM source_documents
            WHERE document_id = ?
            """,
            (row["document_id"],),
        ).fetchone()
        if document is None:
            raise ValueError("source_artifact document_id must reference an existing document")
        if (
            document["source_artifact_id"] != row["artifact_id"]
            or document["source_storage_key"] != row["storage_key"]
            or document["content_hash"] != row["content_hash"]
            or document["source_type"] != row["source_type"]
            or document["original_filename"] != row["original_filename"]
            or document["uploaded_by"] != row["uploaded_by"]
        ):
            raise ValueError("source_artifact must match the bound source document")

    def _require_document_source_artifact(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> None:
        artifact = connection.execute(
            """
            SELECT artifact_id, storage_key, content_hash, source_type,
                   original_filename, uploaded_by
            FROM source_artifacts
            WHERE document_id = ?
            """,
            (row["document_id"],),
        ).fetchone()
        if artifact is None:
            raise ValueError("source document must reference an existing source artifact")
        if (
            artifact["artifact_id"] != row["source_artifact_id"]
            or artifact["storage_key"] != row["source_storage_key"]
            or artifact["content_hash"] != row["content_hash"]
            or artifact["source_type"] != row["source_type"]
            or artifact["original_filename"] != row["original_filename"]
            or artifact["uploaded_by"] != row["uploaded_by"]
        ):
            raise ValueError("source document must match the bound source artifact")

    def _next_job_event_sequence(
        self,
        connection: sqlite3.Connection,
        job_id: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT sequence
            FROM job_events
            WHERE job_id = ?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return 1
        return int(row["sequence"]) + 1

    def _verify_job_event_history(
        self,
        connection: sqlite3.Connection,
        job_id: str,
    ) -> list[sqlite3.Row]:
        rows = connection.execute(
            """
            SELECT * FROM job_events
            WHERE job_id = ?
            ORDER BY sequence ASC
            """,
            (job_id,),
        ).fetchall()
        for expected_sequence, row in enumerate(rows, start=1):
            sequence = row["sequence"]
            if not isinstance(sequence, int) or sequence != expected_sequence:
                raise ValueError("job event history must have contiguous sequences")
            payload, effective_action = _require_job_event_row_payload_matches(row)
            self._require_job_event_durable_evidence(
                connection,
                row,
                payload,
                effective_action,
            )
        return rows

    def _require_job_event_durable_evidence(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        payload: Mapping[str, Any],
        effective_action: str,
    ) -> None:
        job = connection.execute(
            """
            SELECT jobs.job_id, jobs.document_id, jobs.idempotency_key,
                   jobs.mode, jobs.status, jobs.attempts,
                   source_documents.original_filename,
                   source_documents.content_hash AS source_content_hash,
                   source_documents.uploaded_by
            FROM jobs
            JOIN source_documents
              ON source_documents.document_id = jobs.document_id
            WHERE jobs.job_id = ?
            """,
            (row["job_id"],),
        ).fetchone()
        if job is None:
            raise ValueError("job event must reference an existing job and source document")
        contract = _require_audit_action_contract(effective_action)
        _require_historical_job_event_payload(job, payload, contract)
        if contract.requires_uploader:
            if job["uploaded_by"] != row["actor"]:
                raise ValueError("job event actor must match the recorded uploader")
        if contract.evidence_type == "download_artifact":
            self._require_download_evidence(
                connection,
                job_id=job["job_id"],
                document_id=job["document_id"],
                action=effective_action,
                payload=payload,
                evidence_event_id=row["event_id"],
                evidence_table="job_event_evidence",
                historical_job_event=True,
            )

    def _require_review_item_matches_artifact(
        self,
        connection: sqlite3.Connection,
        review_item_id: str,
        artifact_id: str,
    ) -> tuple[str, str]:
        review_item = connection.execute(
            "SELECT job_id, document_id FROM review_items WHERE review_item_id = ?",
            (review_item_id,),
        ).fetchone()
        if review_item is None:
            raise ValueError("review_item_id must reference an existing review item")

        artifact = connection.execute(
            "SELECT job_id, document_id FROM generated_artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        if artifact is None:
            raise ValueError("artifact_id must reference an existing artifact")

        if (
            review_item["job_id"] != artifact["job_id"]
            or review_item["document_id"] != artifact["document_id"]
        ):
            raise ValueError("review_item_id and artifact_id must refer to the same conversion")
        return review_item["job_id"], review_item["document_id"]

    def _next_audit_chain_fields(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[int, str | None]:
        row = connection.execute(
            """
            SELECT sequence, event_hash
            FROM audit_events
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return 1, None
        return int(row["sequence"]) + 1, row["event_hash"]

    def _require_audit_scope(
        self,
        connection: sqlite3.Connection,
        scope_type: str,
        scope_id: str,
        job_id: str,
        document_id: str,
        actor: str,
        action: str,
        payload: Mapping[str, Any],
        *,
        evidence_event_id: str | None = None,
    ) -> tuple[str, ...]:
        if scope_type in {"document", "source_document"}:
            row = connection.execute(
                """
                SELECT document_id, source_artifact_id, uploaded_by,
                       source_storage_key, content_hash, source_type,
                       original_filename, status
                FROM source_documents
                WHERE document_id = ?
                """,
                (scope_id,),
            ).fetchone()
            expected = {"document_id": document_id}
        elif scope_type == "source_artifact":
            row = connection.execute(
                """
                SELECT artifact_id, document_id, uploaded_by, storage_key,
                       content_hash, source_type, original_filename
                FROM source_artifacts
                WHERE artifact_id = ?
                """,
                (scope_id,),
            ).fetchone()
            expected = {"document_id": document_id}
        elif scope_type in {"job", "conversion_job"}:
            row = connection.execute(
                """
                SELECT jobs.job_id, jobs.document_id, jobs.idempotency_key,
                       jobs.mode, jobs.status, jobs.attempts,
                       source_documents.original_filename,
                       source_documents.content_hash AS source_content_hash,
                       source_documents.uploaded_by
                FROM jobs
                JOIN source_documents
                  ON source_documents.document_id = jobs.document_id
                WHERE jobs.job_id = ?
                """,
                (scope_id,),
            ).fetchone()
            expected = {"job_id": job_id, "document_id": document_id}
        elif scope_type == "job_event":
            row = connection.execute(
                """
                SELECT job_events.event_id, job_events.job_id, jobs.document_id,
                       job_events.sequence, job_events.event_type, job_events.actor,
                       job_events.payload_json,
                       jobs.idempotency_key, jobs.mode, jobs.status, jobs.attempts,
                       source_documents.original_filename,
                       source_documents.content_hash AS source_content_hash,
                       source_documents.uploaded_by
                FROM job_events
                JOIN jobs ON jobs.job_id = job_events.job_id
                JOIN source_documents
                  ON source_documents.document_id = jobs.document_id
                WHERE job_events.event_id = ?
                """,
                (scope_id,),
            ).fetchone()
            expected = {"job_id": job_id, "document_id": document_id}
        elif scope_type == "conversion_result":
            row = connection.execute(
                """
                SELECT result_id, job_id, document_id, status, content_hash
                FROM conversion_results
                WHERE result_id = ?
                """,
                (scope_id,),
            ).fetchone()
            expected = {"job_id": job_id, "document_id": document_id}
        elif scope_type in {"artifact", "generated_artifact"}:
            row = connection.execute(
                """
                SELECT artifact_id, result_id, job_id, document_id, storage_key,
                       display_filename, content_hash, category, format,
                       retention_state
                FROM generated_artifacts
                WHERE artifact_id = ?
                """,
                (scope_id,),
            ).fetchone()
            expected = {"job_id": job_id, "document_id": document_id}
        elif scope_type == "review_item":
            row = connection.execute(
                """
                SELECT review_item_id, job_id, document_id, target_path, status,
                       severity
                FROM review_items
                WHERE review_item_id = ?
                """,
                (scope_id,),
            ).fetchone()
            expected = {"job_id": job_id, "document_id": document_id}
        elif scope_type == "review_decision":
            row = connection.execute(
                """
                SELECT decision_id, review_item_id, artifact_id, job_id,
                       document_id, actor, role, decision
                FROM review_decisions
                WHERE decision_id = ?
                """,
                (scope_id,),
            ).fetchone()
            expected = {"job_id": job_id, "document_id": document_id}
        else:
            raise ValueError(f"unsupported audit scope_type: {scope_type}")

        if row is None:
            raise ValueError("audit scope_id must reference an existing scope row")
        for field_name, expected_value in expected.items():
            if row[field_name] != expected_value:
                raise ValueError("audit scope must match the event job and document")
        if scope_type == "job_event":
            self._verify_job_event_history(connection, row["job_id"])
        evidence_artifact_ids: tuple[str, ...] = ()
        contract = _require_audit_action_contract(action)
        desktop_job_row = None
        if (
            scope_type == "document"
            and contract.name in {"desktop_upload", "desktop_result_download"}
        ):
            desktop_job_row = connection.execute(
                """
                SELECT jobs.job_id, jobs.document_id, jobs.idempotency_key,
                       jobs.mode, jobs.status, jobs.attempts,
                       source_documents.original_filename,
                       source_documents.content_hash AS source_content_hash,
                       source_documents.uploaded_by
                FROM jobs
                JOIN source_documents
                  ON source_documents.document_id = jobs.document_id
                WHERE jobs.job_id = ? AND jobs.document_id = ?
                """,
                (job_id, document_id),
            ).fetchone()
            if desktop_job_row is None:
                raise ValueError("desktop audit must reference an existing document job")
        if contract.evidence_type == "download_artifact":
            required_artifact_ids = None
            if scope_type == "job_event":
                required_artifact_ids = _event_evidence_artifact_ids(
                    connection,
                    evidence_table="job_event_evidence",
                    event_id=row["event_id"],
                )
            evidence_artifact_ids = self._require_download_evidence(
                connection,
                job_id=job_id,
                document_id=document_id,
                action=action,
                payload=payload,
                evidence_event_id=evidence_event_id,
                required_artifact_ids=required_artifact_ids,
                historical_job_event=(
                    scope_type == "job_event" or evidence_event_id is not None
                ),
            )
        _require_audit_scope_semantics(
            scope_type=scope_type,
            row=row,
            actor=actor,
            action=action,
            payload=payload,
            historical=evidence_event_id is not None,
        )
        if desktop_job_row is not None:
            _require_desktop_document_job_payload(
                desktop_job_row,
                payload,
                contract,
                historical=evidence_event_id is not None,
            )
        if scope_type != "job_event" and evidence_event_id is None:
            job_state = connection.execute(
                "SELECT status, attempts FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if job_state is None:
                raise ValueError("job_id must reference an existing job")
            _require_job_action_available(
                job_state["status"],
                action,
                attempts=job_state["attempts"],
            )
        if scope_type == "review_decision":
            if row["actor"] != actor:
                raise ValueError("audit actor must match the review decision actor")
            if not _review_decision_action_matches(row["decision"], action):
                raise ValueError("audit action must match the review decision")
        return evidence_artifact_ids

    def _require_download_evidence(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        document_id: str,
        action: str,
        payload: Mapping[str, Any],
        evidence_event_id: str | None,
        evidence_table: str = "audit_event_evidence",
        required_artifact_ids: tuple[str, ...] | None = None,
        historical_job_event: bool = False,
    ) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT generated_artifacts.artifact_id,
                   generated_artifacts.display_filename,
                   generated_artifacts.content_hash,
                   jobs.status AS job_status
            FROM generated_artifacts
            JOIN conversion_results
              ON conversion_results.result_id = generated_artifacts.result_id
             AND conversion_results.job_id = generated_artifacts.job_id
             AND conversion_results.document_id = generated_artifacts.document_id
            JOIN jobs
              ON jobs.job_id = generated_artifacts.job_id
             AND jobs.document_id = generated_artifacts.document_id
            WHERE generated_artifacts.job_id = ?
              AND generated_artifacts.document_id = ?
            """,
            (job_id, document_id),
        ).fetchall()
        contract = _require_audit_action_contract(action)
        allowed_statuses = contract.job_statuses
        rows = [
            row
            for row in rows
            if allowed_statuses is not None
            and (
                historical_job_event or row["job_status"] in allowed_statuses
            )
        ]
        if not rows:
            raise ValueError("download audit requires a stored successful result artifact")
        matching_rows = rows
        has_download_filename = "download_filename" in payload
        has_output_sha256 = "output_sha256" in payload
        if action == "desktop_result_download" or (
            has_download_filename or has_output_sha256
        ):
            output_sha256 = payload.get("output_sha256")
            download_filename = payload.get("download_filename")
            if (
                not isinstance(output_sha256, str)
                or SHA256_HEX.fullmatch(output_sha256) is None
            ):
                raise ValueError(
                    "payload output_sha256 must identify a stored result artifact"
                )
            if not isinstance(download_filename, str) or not download_filename.strip():
                raise ValueError(
                    "payload download_filename must identify a stored result artifact"
                )
            matching_rows = [
                row
                for row in rows
                if row["content_hash"] == output_sha256
                and row["display_filename"] == download_filename
            ]
            if not matching_rows:
                raise ValueError(
                    "download audit evidence must match a stored result artifact"
                )

        matching_artifact_ids = tuple(row["artifact_id"] for row in matching_rows)
        selected_artifact_ids = matching_artifact_ids
        if required_artifact_ids is not None:
            if not required_artifact_ids or not set(required_artifact_ids).issubset(
                matching_artifact_ids
            ):
                raise ValueError(
                    "download audit evidence must match the scoped job event evidence"
                )
            selected_artifact_ids = required_artifact_ids
        if evidence_event_id is None:
            return selected_artifact_ids

        if evidence_table not in {"audit_event_evidence", "job_event_evidence"}:
            raise ValueError("unsupported download evidence table")
        recorded_artifact_ids = _event_evidence_artifact_ids(
            connection,
            evidence_table=evidence_table,
            event_id=evidence_event_id,
        )
        if not recorded_artifact_ids or not set(recorded_artifact_ids).issubset(
            selected_artifact_ids
        ):
            owner = "job event" if evidence_table == "job_event_evidence" else "audit"
            raise ValueError(f"download {owner} evidence link is missing or inconsistent")
        if required_artifact_ids is not None and set(recorded_artifact_ids) != set(
            required_artifact_ids
        ):
            raise ValueError(
                "download audit evidence must match the scoped job event evidence"
            )
        if evidence_table == "job_event_evidence":
            expected_evidence = {
                "artifact_ids": sorted(recorded_artifact_ids),
                "type": "download_artifact",
            }
            if payload.get("evidence") != expected_evidence:
                raise ValueError(
                    "download job event evidence is not bound to the event payload"
                )
            return recorded_artifact_ids
        expected_evidence = {
            "artifact_ids": sorted(recorded_artifact_ids),
            "type": "download_artifact",
        }
        if payload.get("evidence") != expected_evidence:
            raise ValueError("download audit evidence is not bound to the audit payload")
        return recorded_artifact_ids

    def _verify_audit_chain(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT *
            FROM audit_events
            ORDER BY sequence ASC
            """
        ).fetchall()
        previous_hash: str | None = None
        for expected_sequence, row in enumerate(rows, start=1):
            sequence = row["sequence"]
            if not isinstance(sequence, int) or sequence != expected_sequence:
                raise ValueError("audit event chain integrity verification failed")
            _require_valid_json_text(
                row["payload_json"],
                field_name="audit event payload_json",
            )
            payload = _require_audit_event_row_payload_matches(row)
            self._require_job_document(connection, row["job_id"], row["document_id"])
            self._require_audit_scope(
                connection,
                row["scope_type"],
                row["scope_id"],
                row["job_id"],
                row["document_id"],
                row["actor"],
                row["action"],
                payload,
                evidence_event_id=row["event_id"],
            )
            if row["integrity_algorithm"] != AUDIT_INTEGRITY_ALGORITHM:
                raise ValueError("audit event chain integrity verification failed")
            if row["prev_event_hash"] != previous_hash:
                raise ValueError("audit event chain integrity verification failed")
            expected_hash = _audit_event_hash(
                event_id=row["event_id"],
                job_id=row["job_id"],
                document_id=row["document_id"],
                sequence=sequence,
                integrity_algorithm=row["integrity_algorithm"],
                actor=row["actor"],
                action=row["action"],
                scope_type=row["scope_type"],
                scope_id=row["scope_id"],
                prev_event_hash=row["prev_event_hash"],
                payload_json=row["payload_json"],
                created_at=row["created_at"],
            )
            if row["event_hash"] != expected_hash:
                raise ValueError("audit event chain integrity verification failed")
            previous_hash = row["event_hash"]


def default_database_path() -> Path:
    configured = os.environ.get("VERIDOC_DB_PATH")
    if configured:
        return Path(configured)
    return DEFAULT_DB_PATH


def initialize_database(db_path: str | os.PathLike[str] | None = None) -> None:
    SQLitePersistenceRepository(db_path).initialize()


def reset_database(db_path: str | os.PathLike[str] | None = None) -> None:
    SQLitePersistenceRepository(db_path).reset()


def _row_to_dataclass(record_type: type[RecordT], row: sqlite3.Row) -> RecordT:
    row_keys = set(row.keys())
    return record_type(
        **{field.name: row[field.name] for field in fields(record_type) if field.name in row_keys}
    )


def _audit_event_hash(
    *,
    event_id: str,
    job_id: str,
    document_id: str,
    sequence: int,
    integrity_algorithm: str,
    actor: str,
    action: str,
    scope_type: str,
    scope_id: str,
    prev_event_hash: str | None,
    payload_json: str,
    created_at: str,
) -> str:
    payload = {
        "action": action,
        "actor": actor,
        "created_at": created_at,
        "document_id": document_id,
        "event_id": event_id,
        "integrity_algorithm": integrity_algorithm,
        "job_id": job_id,
        "payload_json": payload_json,
        "prev_event_hash": prev_event_hash,
        "scope_id": scope_id,
        "scope_type": scope_type,
        "sequence": sequence,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_non_empty(**values: str) -> None:
    for field_name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} is required")


def _require_sha256(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or SHA256_HEX.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a sha256 hex digest")


def _require_job_event_payload_matches(
    payload: Mapping[str, Any] | None,
    *,
    event_id: str,
    job_id: str,
    event_type: str,
    actor: str,
    allow_derived_evidence: bool = False,
) -> str:
    if payload is None:
        payload = {"event_type": event_type}
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
    _require_authoritative_payload_fields(payload)
    expected = {
        "event_id": event_id,
        "job_id": job_id,
        "event_type": event_type,
        "actor": actor,
    }
    for field_name, expected_value in expected.items():
        if field_name not in payload:
            continue
        payload_value = payload[field_name]
        if field_name == "actor" and isinstance(payload_value, Mapping):
            _require_nested_actor_id(
                payload_value,
                expected_value,
                row_name="job event",
            )
            continue
        if payload_value != expected_value:
            raise ValueError(f"payload {field_name} must match the job event row")
    if "actor_id" in payload and payload["actor_id"] != actor:
        raise ValueError("payload actor_id must match the job event row")
    effective_action = _job_event_effective_action(event_type, payload)
    contract = _require_audit_action_contract(effective_action)
    if "job_event" not in contract.scope_types:
        raise ValueError("job event action is not valid for job-event history")
    if "evidence" in payload:
        if not allow_derived_evidence:
            raise ValueError("payload evidence is derived from the job event evidence set")
        if contract.evidence_type is None:
            raise ValueError("payload evidence is not valid for the job event action")
        if not isinstance(payload["evidence"], Mapping):
            raise ValueError("payload evidence must be a mapping")
    _require_job_attempt_aliases(payload, contract)
    _require_job_event_payload_status(payload, effective_action)
    for field_name in ("sequence", "created_at", "occurred_at"):
        if field_name in payload:
            raise ValueError(f"payload {field_name} is derived from the job event row")
    return effective_action


def _require_job_event_payload_status(
    payload: Mapping[str, Any],
    effective_action: str,
) -> None:
    contract = _require_audit_action_contract(effective_action)
    _require_job_status_aliases(payload, contract)


def _job_event_effective_action(
    event_type: str,
    payload: Mapping[str, Any],
) -> str:
    payload_action = payload.get("action")
    if payload_action is None:
        if event_type in _CONTRACT_EVENT_TYPES:
            raise ValueError("payload action is required for a generic job event type")
        return event_type
    if not isinstance(payload_action, str) or not payload_action.strip():
        raise ValueError("payload action must be a non-empty string")
    if not _job_event_action_matches(event_type, payload_action):
        raise ValueError("payload action must match the job event row")
    return payload_action


def _require_job_event_row_payload_matches(
    row: sqlite3.Row,
) -> tuple[Mapping[str, Any], str]:
    payload = _load_json_object(
        row["payload_json"],
        field_name="job event payload_json",
    )
    effective_action = _require_job_event_payload_matches(
        payload,
        event_id=row["event_id"],
        job_id=row["job_id"],
        event_type=row["event_type"],
        actor=row["actor"],
        allow_derived_evidence=True,
    )
    return payload, effective_action


def _require_audit_event_row_payload_matches(row: sqlite3.Row) -> Mapping[str, Any]:
    payload = _load_json_object(
        row["payload_json"],
        field_name="audit event payload_json",
    )
    if row["payload_json"] != _canonical_json(payload):
        raise ValueError("audit event payload_json must be canonical JSON")
    _require_audit_event_payload_matches(
        payload,
        event_id=row["event_id"],
        job_id=row["job_id"],
        document_id=row["document_id"],
        integrity_algorithm=row["integrity_algorithm"],
        actor=row["actor"],
        action=row["action"],
        scope_type=row["scope_type"],
        scope_id=row["scope_id"],
        allow_derived_evidence=True,
    )
    return payload


def _require_audit_event_payload_matches(
    payload: Mapping[str, Any] | None,
    *,
    event_id: str,
    job_id: str,
    document_id: str,
    integrity_algorithm: str,
    actor: str,
    action: str,
    scope_type: str,
    scope_id: str,
    allow_derived_evidence: bool = False,
) -> None:
    contract = _require_audit_action_scope(action, scope_type)
    if payload is None:
        return
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
    _require_authoritative_payload_fields(payload)
    if "evidence" in payload:
        if not allow_derived_evidence:
            raise ValueError("payload evidence is derived from the audit evidence set")
        if contract.evidence_type is None:
            raise ValueError("payload evidence is not valid for the audit action")
        if not isinstance(payload["evidence"], Mapping):
            raise ValueError("payload evidence must be a mapping")
    expected = {
        "event_id": event_id,
        "job_id": job_id,
        "document_id": document_id,
        "integrity_algorithm": integrity_algorithm,
        "actor": actor,
        "action": action,
        "scope_type": scope_type,
        "scope_id": scope_id,
    }
    for field_name, expected_value in expected.items():
        if field_name not in payload:
            continue
        payload_value = payload[field_name]
        if field_name == "actor" and isinstance(payload_value, Mapping):
            _require_nested_actor_id(
                payload_value,
                expected_value,
                row_name="audit event",
            )
            continue
        if payload_value != expected_value:
            raise ValueError(f"payload {field_name} must match the audit event row")
    if "actor_id" in payload and payload["actor_id"] != actor:
        raise ValueError("payload actor_id must match the audit event row")
    if "event_type" in payload and not _audit_event_type_matches_action(
        payload["event_type"],
        action=action,
        scope_type=scope_type,
    ):
        raise ValueError("payload event_type must match the audit event row")
    for alias_field in _AUDIT_SCOPE_ID_ALIASES.get(scope_type, frozenset()):
        if alias_field in payload and payload[alias_field] != scope_id:
            raise ValueError(f"payload {alias_field} must match the audit event scope")
    for field_name in (
        "sequence",
        "event_hash",
        "prev_event_hash",
        "created_at",
        "occurred_at",
        "event_timestamp",
    ):
        if field_name in payload:
            raise ValueError(f"payload {field_name} is derived from the audit chain")


def _require_authoritative_payload_fields(payload: Mapping[str, Any]) -> None:
    for field_name in _EPHEMERAL_DESKTOP_PAYLOAD_FIELDS:
        if field_name in payload:
            raise ValueError(
                f"payload {field_name} is not stored in authoritative audit history"
            )


def _require_nested_actor_id(
    actor_payload: Mapping[str, Any],
    expected_actor: str,
    *,
    row_name: str,
) -> None:
    if "id" not in actor_payload:
        raise ValueError(f"payload actor.id is required for the {row_name} row")
    if actor_payload["id"] != expected_actor:
        raise ValueError(f"payload actor.id must match the {row_name} row")


@dataclass(frozen=True)
class _AuditActionContract:
    name: str
    aliases: frozenset[str]
    scope_types: frozenset[str]
    event_types: frozenset[str] = frozenset()
    job_statuses: frozenset[str] | None = None
    result_statuses: frozenset[str] | None = None
    lifecycle_state: str | None = None
    evidence_type: str | None = None
    requires_uploader: bool = False
    requires_unattempted_job: bool = False


_JOB_AUDIT_SCOPES = frozenset({"conversion_job", "job", "job_event"})
_RESULT_LIFECYCLE_SCOPES = _JOB_AUDIT_SCOPES | frozenset({"conversion_result"})
_DESKTOP_AUDIT_SCOPES = _JOB_AUDIT_SCOPES | frozenset({"document"})
_UPLOAD_AUDIT_SCOPES = _JOB_AUDIT_SCOPES | frozenset(
    {"document", "source_artifact", "source_document"}
)
_REVIEW_REQUEST_SCOPES = frozenset(
    {"document", "review_decision", "review_item"}
)
_QUEUED_JOB_STATUSES = frozenset({"queued"})
_RUNNING_JOB_STATUSES = frozenset({"processing", "running", "started"})
_FAILED_JOB_STATUSES = frozenset({"failed"})
_SUCCEEDED_JOB_STATUSES = frozenset({"completed", "succeeded", "success"})
_FAILED_RESULT_STATUSES = frozenset({"failed"})
_SUCCEEDED_RESULT_STATUSES = frozenset(
    {"blocked", "completed", "converted", "requires_review", "succeeded", "success"}
)

_AUDIT_ACTION_CONTRACTS = (
    _AuditActionContract(
        name="source_upload",
        aliases=frozenset({"document.uploaded", "upload", "uploaded"}),
        scope_types=_UPLOAD_AUDIT_SCOPES,
        requires_uploader=True,
    ),
    _AuditActionContract(
        name="desktop_upload",
        aliases=frozenset({"desktop_upload"}),
        scope_types=_DESKTOP_AUDIT_SCOPES,
        event_types=frozenset({"desktop.job_operation"}),
        job_statuses=_QUEUED_JOB_STATUSES,
        requires_uploader=True,
        requires_unattempted_job=True,
    ),
    _AuditActionContract(
        name="document_inspected",
        aliases=frozenset({"document.inspected"}),
        scope_types=frozenset({"document", "source_document"}),
    ),
    _AuditActionContract(
        name="artifact_generated",
        aliases=frozenset({"artifact.generated"}),
        scope_types=frozenset({"artifact", "generated_artifact"}),
    ),
    _AuditActionContract(
        name="review_opened",
        aliases=frozenset({"review.opened"}),
        scope_types=frozenset({"review_item"}),
    ),
    _AuditActionContract(
        name="job_queued",
        aliases=frozenset(
            {"conversion.queued", "conversion_queued", "job.queued", "job_queued"}
        ),
        scope_types=_JOB_AUDIT_SCOPES,
        event_types=frozenset({"job.lifecycle"}),
        job_statuses=_QUEUED_JOB_STATUSES,
        lifecycle_state="queued",
    ),
    _AuditActionContract(
        name="job_running",
        aliases=frozenset(
            {
                "conversion.running",
                "conversion.started",
                "conversion_running",
                "conversion_started",
                "job.running",
                "job.started",
                "job_running",
                "job_started",
            }
        ),
        scope_types=_JOB_AUDIT_SCOPES,
        event_types=frozenset({"job.lifecycle"}),
        job_statuses=_RUNNING_JOB_STATUSES,
        lifecycle_state="running",
    ),
    _AuditActionContract(
        name="job_failed",
        aliases=frozenset(
            {"conversion.failed", "conversion_failed", "job.failed", "job_failed"}
        ),
        scope_types=_RESULT_LIFECYCLE_SCOPES,
        event_types=frozenset({"job.lifecycle"}),
        job_statuses=_FAILED_JOB_STATUSES,
        result_statuses=_FAILED_RESULT_STATUSES,
        lifecycle_state="failed",
    ),
    _AuditActionContract(
        name="job_succeeded",
        aliases=frozenset(
            {
                "conversion.completed",
                "conversion.succeeded",
                "conversion_completed",
                "conversion_succeeded",
                "job.completed",
                "job.succeeded",
                "job_completed",
                "job_succeeded",
            }
        ),
        scope_types=_RESULT_LIFECYCLE_SCOPES,
        event_types=frozenset({"job.lifecycle"}),
        job_statuses=_SUCCEEDED_JOB_STATUSES,
        result_statuses=_SUCCEEDED_RESULT_STATUSES,
        lifecycle_state="succeeded",
    ),
    _AuditActionContract(
        name="job_retry",
        aliases=frozenset(
            {
                "conversion.retry_requested",
                "conversion_retry_requested",
                "job.retry_requested",
                "job_retry_requested",
                "retry_conversion",
            }
        ),
        scope_types=_JOB_AUDIT_SCOPES,
        event_types=frozenset(
            {"conversion_job.action_requested", "job.lifecycle"}
        ),
        job_statuses=_FAILED_JOB_STATUSES,
        lifecycle_state="retry",
    ),
    _AuditActionContract(
        name="download_result",
        aliases=frozenset({"download_result"}),
        scope_types=_JOB_AUDIT_SCOPES,
        event_types=frozenset({"conversion_job.action_requested"}),
        job_statuses=_SUCCEEDED_JOB_STATUSES,
        evidence_type="download_artifact",
    ),
    _AuditActionContract(
        name="desktop_result_download",
        aliases=frozenset({"desktop_result_download"}),
        scope_types=_DESKTOP_AUDIT_SCOPES,
        event_types=frozenset({"desktop.job_operation"}),
        job_statuses=_SUCCEEDED_JOB_STATUSES,
        evidence_type="download_artifact",
    ),
    _AuditActionContract(
        name="open_detail",
        aliases=frozenset({"open_detail"}),
        scope_types=_JOB_AUDIT_SCOPES,
        event_types=frozenset({"conversion_job.action_requested"}),
    ),
    _AuditActionContract(
        name="review_approve_request",
        aliases=frozenset({"approve"}),
        scope_types=_REVIEW_REQUEST_SCOPES,
        event_types=frozenset({"conversion_review.action_requested"}),
    ),
    _AuditActionContract(
        name="review_edit_request",
        aliases=frozenset({"edit"}),
        scope_types=_REVIEW_REQUEST_SCOPES,
        event_types=frozenset({"conversion_review.action_requested"}),
    ),
    _AuditActionContract(
        name="review_outcome",
        aliases=frozenset(
            {
                "approved",
                "edited",
                "rejected",
                "review.approve",
                "review.approved",
                "review.edit",
                "review.edited",
                "review.reject",
                "review.rejected",
            }
        ),
        scope_types=frozenset({"review_decision"}),
    ),
)

_AUDIT_ACTION_CONTRACT_BY_ALIAS = {
    alias: contract
    for contract in _AUDIT_ACTION_CONTRACTS
    for alias in contract.aliases
}
_CONTRACT_EVENT_TYPES = frozenset(
    event_type
    for contract in _AUDIT_ACTION_CONTRACTS
    for event_type in contract.event_types
)


def _audit_action_contract(action: str) -> _AuditActionContract | None:
    return _AUDIT_ACTION_CONTRACT_BY_ALIAS.get(action)


def _require_audit_action_contract(action: str) -> _AuditActionContract:
    contract = _audit_action_contract(action)
    if contract is None:
        raise ValueError("audit action must have a declared contract")
    return contract


_DOWNLOAD_EVIDENCE_ALIASES = frozenset({"download_filename", "output_sha256"})
_EPHEMERAL_DESKTOP_PAYLOAD_FIELDS = frozenset(
    {"download_proof", "saved_filename"}
)


def _evidence_aliases_for_contract(
    contract: _AuditActionContract,
) -> frozenset[str]:
    if contract.evidence_type == "download_artifact":
        return _DOWNLOAD_EVIDENCE_ALIASES
    return frozenset()


def _require_audit_action_scope(
    action: str,
    scope_type: str,
) -> _AuditActionContract:
    contract = _require_audit_action_contract(action)
    if scope_type not in contract.scope_types:
        raise ValueError("audit action is not valid for the selected scope type")
    return contract


_AUDIT_SCOPE_ID_ALIASES = {
    "document": frozenset({"document_id", "source_document_id"}),
    "source_document": frozenset({"document_id", "source_document_id"}),
    "source_artifact": frozenset({"artifact_id", "source_artifact_id"}),
    "job": frozenset({"job_id", "conversion_job_id"}),
    "conversion_job": frozenset({"job_id", "conversion_job_id"}),
    "job_event": frozenset({"job_event_id"}),
    "conversion_result": frozenset({"result_id", "conversion_result_id"}),
    "artifact": frozenset({"artifact_id", "generated_artifact_id"}),
    "generated_artifact": frozenset({"artifact_id", "generated_artifact_id"}),
    "review_item": frozenset({"review_item_id"}),
    "review_decision": frozenset({"decision_id", "review_decision_id"}),
}


def _audit_event_type_matches_action(
    event_type: Any,
    *,
    action: str,
    scope_type: str,
) -> bool:
    if event_type == action:
        return True
    if not isinstance(event_type, str):
        return False
    contract = _require_audit_action_contract(action)
    return (
        event_type in contract.event_types
        and scope_type in contract.scope_types
    )


def _review_decision_action_matches(decision: str, action: str) -> bool:
    normalized = decision.strip().lower()
    aliases = {
        "approved": frozenset({"approved", "approve", "review.approved", "review.approve"}),
        "rejected": frozenset({"rejected", "reject", "review.rejected", "review.reject"}),
        "edited": frozenset({"edited", "edit", "review.edited", "review.edit"}),
    }
    return action in aliases.get(normalized, frozenset({normalized, f"review.{normalized}"}))


_SOURCE_DOCUMENT_PAYLOAD_BINDINGS = (
    (("source_artifact_id", "artifact_id"), "source_artifact_id", "source artifact id"),
    (("storage_key", "source_storage_key"), "source_storage_key", "source storage key"),
    (
        ("content_hash", "source_content_hash", "source_sha256"),
        "content_hash",
        "source content hash",
    ),
    (("source_type",), "source_type", "source type"),
    (("original_filename", "filename"), "original_filename", "source filename"),
    (("document_status", "status"), "status", "source document status"),
    (("uploaded_by", "uploader_id"), "uploaded_by", "recorded uploader"),
)

_SOURCE_ARTIFACT_PAYLOAD_BINDINGS = (
    (("storage_key", "source_storage_key"), "storage_key", "source artifact storage key"),
    (
        ("content_hash", "source_content_hash", "source_sha256"),
        "content_hash",
        "source artifact content hash",
    ),
    (("source_type",), "source_type", "source artifact type"),
    (("original_filename", "filename"), "original_filename", "source artifact filename"),
    (("uploaded_by", "uploader_id"), "uploaded_by", "recorded uploader"),
)

_JOB_PAYLOAD_BINDINGS = (
    (("idempotency_key",), "idempotency_key", "job idempotency key"),
    (("mode", "job_mode"), "mode", "job mode"),
    (("job_status", "status"), "status", "job status"),
    (("attempts", "job_attempts"), "attempts", "job attempts"),
    (("filename", "source_filename"), "original_filename", "job source filename"),
    (("source_sha256",), "source_content_hash", "job source hash"),
)
_DESKTOP_DOCUMENT_JOB_PAYLOAD_BINDINGS = (
    (("idempotency_key",), "idempotency_key", "job idempotency key"),
    (("mode", "job_mode"), "mode", "job mode"),
    (("job_status",), "status", "job status"),
    (("attempts", "job_attempts"), "attempts", "job attempts"),
)
_DESKTOP_DOCUMENT_JOB_PAYLOAD_ALIASES = frozenset(
    alias
    for aliases, _, _ in _DESKTOP_DOCUMENT_JOB_PAYLOAD_BINDINGS
    for alias in aliases
)

_JOB_HISTORY_MUTABLE_ALIASES = frozenset(
    {"job_status", "status", "attempts", "job_attempts"}
)
_JOB_IMMUTABLE_PAYLOAD_BINDINGS = tuple(
    binding
    for binding in _JOB_PAYLOAD_BINDINGS
    if not set(binding[0]).intersection(_JOB_HISTORY_MUTABLE_ALIASES)
)
_JOB_EVENT_PAYLOAD_BINDINGS = _JOB_PAYLOAD_BINDINGS + (
    (("job_event_sequence",), "sequence", "job event sequence"),
    (("event_actor",), "actor", "job event actor"),
)
_JOB_EVENT_IMMUTABLE_PAYLOAD_BINDINGS = _JOB_IMMUTABLE_PAYLOAD_BINDINGS + (
    (("job_event_sequence",), "sequence", "job event sequence"),
    (("event_actor",), "actor", "job event actor"),
)

_RESULT_PAYLOAD_BINDINGS = (
    (("result_status", "conversion_status", "status"), "status", "conversion result status"),
    (("content_hash", "result_content_hash"), "content_hash", "conversion result hash"),
)

_ARTIFACT_PAYLOAD_BINDINGS = (
    (("result_id", "conversion_result_id"), "result_id", "artifact result id"),
    (("display_filename", "artifact_filename"), "display_filename", "artifact display filename"),
    (("storage_key", "artifact_storage_key"), "storage_key", "artifact storage key"),
    (
        ("content_hash", "artifact_content_hash", "sha256"),
        "content_hash",
        "artifact content hash",
    ),
    (("category", "artifact_category"), "category", "artifact category"),
    (("format", "artifact_format"), "format", "artifact format"),
    (("retention_state",), "retention_state", "artifact retention state"),
)

_REVIEW_ITEM_PAYLOAD_BINDINGS = (
    (("target_path", "review_target_path"), "target_path", "review item target path"),
    (("review_status", "status"), "status", "review item status"),
    (("severity", "review_severity"), "severity", "review item severity"),
)

_REVIEW_DECISION_PAYLOAD_BINDINGS = (
    (("review_item_id",), "review_item_id", "review item id"),
    (("artifact_id", "generated_artifact_id"), "artifact_id", "review artifact id"),
)

_AUDIT_SCOPE_PAYLOAD_BINDINGS = {
    "document": _SOURCE_DOCUMENT_PAYLOAD_BINDINGS,
    "source_document": _SOURCE_DOCUMENT_PAYLOAD_BINDINGS,
    "source_artifact": _SOURCE_ARTIFACT_PAYLOAD_BINDINGS,
    "job": _JOB_PAYLOAD_BINDINGS,
    "conversion_job": _JOB_PAYLOAD_BINDINGS,
    "job_event": _JOB_EVENT_PAYLOAD_BINDINGS,
    "conversion_result": _RESULT_PAYLOAD_BINDINGS,
    "artifact": _ARTIFACT_PAYLOAD_BINDINGS,
    "generated_artifact": _ARTIFACT_PAYLOAD_BINDINGS,
    "review_item": _REVIEW_ITEM_PAYLOAD_BINDINGS,
    "review_decision": _REVIEW_DECISION_PAYLOAD_BINDINGS,
}

_AUDIT_SCOPE_SPECIAL_PAYLOAD_FIELDS = {
    "review_decision": frozenset(
        {"actor_role", "role", "decision", "review_decision", "outcome"}
    ),
}

_AUDIT_GLOBAL_PAYLOAD_FIELDS = frozenset(
    {
        "event_id",
        "job_id",
        "document_id",
        "integrity_algorithm",
        "actor",
        "actor_id",
        "action",
        "event_type",
        "scope_type",
        "scope_id",
    }
)

_AUDIT_RESERVED_EVIDENCE_FIELDS = frozenset(
    alias
    for bindings in _AUDIT_SCOPE_PAYLOAD_BINDINGS.values()
    for aliases, _, _ in bindings
    for alias in aliases
).union(
    alias
    for aliases in _AUDIT_SCOPE_ID_ALIASES.values()
    for alias in aliases
).union(
    field
    for fields in _AUDIT_SCOPE_SPECIAL_PAYLOAD_FIELDS.values()
    for field in fields
).union(_DOWNLOAD_EVIDENCE_ALIASES)


def _require_audit_scope_semantics(
    *,
    scope_type: str,
    row: sqlite3.Row,
    actor: str,
    action: str,
    payload: Mapping[str, Any],
    historical: bool,
) -> None:
    contract = _require_audit_action_contract(action)
    if scope_type == "job_event":
        event_payload = _load_json_object(
            row["payload_json"],
            field_name="job event payload_json",
        )
        event_action = _job_event_effective_action(row["event_type"], event_payload)
        event_contract = _require_audit_action_contract(event_action)
        _require_historical_job_event_payload(
            row,
            payload,
            event_contract,
            scope_type="job_event",
        )
        _require_job_event_snapshot_aliases_match(payload, event_payload)
    elif scope_type in {"job", "conversion_job"} and historical:
        _require_historical_job_event_payload(row, payload, contract)
    else:
        allowed_aliases = _evidence_aliases_for_contract(contract)
        if (
            scope_type == "document"
            and contract.name in {"desktop_upload", "desktop_result_download"}
        ):
            allowed_aliases |= _DESKTOP_DOCUMENT_JOB_PAYLOAD_ALIASES
        _require_declared_audit_scope_payload(
            scope_type,
            row,
            payload,
            allowed_aliases=allowed_aliases,
        )
        if scope_type in {"job", "conversion_job"}:
            _require_job_attempt_aliases(payload, contract)

    if scope_type in {"document", "source_document", "source_artifact"}:
        if _is_upload_action(action) and row["uploaded_by"] != actor:
            raise ValueError("audit actor must match the recorded uploader")
        return

    if scope_type in {"job", "conversion_job"}:
        if _is_upload_action(action) and row["uploaded_by"] != actor:
            raise ValueError("audit actor must match the recorded uploader")
        return

    if scope_type == "job_event":
        if row["actor"] != actor:
            raise ValueError("audit actor must match the scoped job event actor")
        if not _job_event_action_matches(event_action, action):
            raise ValueError("audit action must match the scoped job event type")
        if _is_upload_action(action) and row["uploaded_by"] != actor:
            raise ValueError("audit actor must match the recorded uploader")
        return

    if scope_type == "conversion_result":
        expected_statuses = contract.result_statuses
        if expected_statuses is not None and row["status"] not in expected_statuses:
            raise ValueError("audit action must match the conversion result status")
        return

    if scope_type == "review_decision":
        _require_payload_actor_role(payload, row["role"])
        _require_payload_review_outcome(payload, row["decision"])


def _require_job_action_available(
    status: str,
    action: str,
    *,
    attempts: int,
) -> None:
    contract = _require_audit_action_contract(action)
    expected_statuses = contract.job_statuses
    if expected_statuses is not None and status not in expected_statuses:
        raise ValueError("audit action is not valid for the current job status")
    if contract.requires_unattempted_job and attempts != 0:
        raise ValueError("audit action is not valid after a job attempt")


def _require_declared_audit_scope_payload(
    scope_type: str,
    row: sqlite3.Row,
    payload: Mapping[str, Any],
    *,
    bindings: Iterable[tuple[Iterable[str], str, str]] | None = None,
    allowed_aliases: Iterable[str] = (),
) -> None:
    declared_bindings = tuple(
        _AUDIT_SCOPE_PAYLOAD_BINDINGS.get(scope_type, ())
        if bindings is None
        else bindings
    )
    allowed_fields = set(_AUDIT_GLOBAL_PAYLOAD_FIELDS)
    allowed_fields.update(_AUDIT_SCOPE_ID_ALIASES.get(scope_type, ()))
    allowed_fields.update(_AUDIT_SCOPE_SPECIAL_PAYLOAD_FIELDS.get(scope_type, ()))
    allowed_fields.update(allowed_aliases)
    for aliases, _, _ in declared_bindings:
        allowed_fields.update(aliases)
    _require_payload_bindings_match(row, payload, declared_bindings)
    for field_name in payload:
        if (
            field_name in _AUDIT_RESERVED_EVIDENCE_FIELDS
            and field_name not in allowed_fields
        ):
            raise ValueError(
                f"payload {field_name} is not valid for audit scope {scope_type}"
            )


def _require_payload_bindings_match(
    row: sqlite3.Row,
    payload: Mapping[str, Any],
    bindings: Iterable[tuple[Iterable[str], str, str]],
) -> None:
    for aliases, column, field_name in bindings:
        _require_payload_aliases_match(
            payload,
            aliases,
            row[column],
            field_name=field_name,
        )


def _require_historical_job_event_payload(
    row: sqlite3.Row,
    payload: Mapping[str, Any],
    contract: _AuditActionContract,
    *,
    scope_type: str = "job",
) -> None:
    bindings = (
        _JOB_EVENT_IMMUTABLE_PAYLOAD_BINDINGS
        if scope_type == "job_event"
        else _JOB_IMMUTABLE_PAYLOAD_BINDINGS
    )
    _require_declared_audit_scope_payload(
        scope_type,
        row,
        payload,
        bindings=bindings,
        allowed_aliases=(
            _JOB_HISTORY_MUTABLE_ALIASES
            | _evidence_aliases_for_contract(contract)
        ),
    )
    _require_job_status_aliases(payload, contract)
    _require_job_attempt_aliases(payload, contract)


def _require_desktop_document_job_payload(
    job_row: sqlite3.Row,
    payload: Mapping[str, Any],
    contract: _AuditActionContract,
    *,
    historical: bool,
) -> None:
    bindings = (
        tuple(
            binding
            for binding in _DESKTOP_DOCUMENT_JOB_PAYLOAD_BINDINGS
            if "job_status" not in binding[0]
            and not set(binding[0]).intersection({"attempts", "job_attempts"})
        )
        if historical
        else _DESKTOP_DOCUMENT_JOB_PAYLOAD_BINDINGS
    )
    _require_payload_bindings_match(job_row, payload, bindings)
    if historical:
        _require_job_status_aliases(payload, contract, aliases=("job_status",))
    _require_job_attempt_aliases(payload, contract)


def _require_job_status_aliases(
    payload: Mapping[str, Any],
    contract: _AuditActionContract,
    *,
    aliases: Iterable[str] = ("job_status", "status"),
) -> None:
    allowed_statuses = contract.job_statuses
    status_values = []
    for alias in aliases:
        if alias not in payload:
            continue
        value = payload[alias]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"payload {alias} must be a non-empty job status string")
        status_values.append(value)
        if allowed_statuses is not None and value not in allowed_statuses:
            raise ValueError(
                f"payload {alias} must match the job event lifecycle state"
            )
    if len(set(status_values)) > 1:
        raise ValueError("payload job status aliases must match")


def _require_job_attempt_aliases(
    payload: Mapping[str, Any],
    contract: _AuditActionContract,
) -> None:
    attempt_values = []
    for alias in ("attempts", "job_attempts"):
        if alias not in payload:
            continue
        value = payload[alias]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"payload {alias} must be a non-negative integer")
        attempt_values.append(value)
    if len(set(attempt_values)) > 1:
        raise ValueError("payload job attempt aliases must match")
    if (
        attempt_values
        and contract.requires_unattempted_job
        and attempt_values[0] != 0
    ):
        raise ValueError("payload job attempts must record an unattempted job")


def _require_job_event_snapshot_aliases_match(
    payload: Mapping[str, Any],
    event_payload: Mapping[str, Any],
) -> None:
    for aliases, field_name in (
        (("job_status", "status"), "job status"),
        (("attempts", "job_attempts"), "job attempts"),
    ):
        event_values = {event_payload[alias] for alias in aliases if alias in event_payload}
        for alias in aliases:
            if alias not in payload:
                continue
            if not event_values or payload[alias] not in event_values:
                raise ValueError(
                    f"payload {alias} must match the scoped job event {field_name}"
                )


def _require_payload_aliases_match(
    payload: Mapping[str, Any],
    aliases: Iterable[str],
    expected: Any,
    *,
    field_name: str,
) -> None:
    for alias in aliases:
        if alias in payload and payload[alias] != expected:
            raise ValueError(f"payload {alias} must match the scoped {field_name}")


def _event_evidence_artifact_ids(
    connection: sqlite3.Connection,
    *,
    evidence_table: str,
    event_id: str,
) -> tuple[str, ...]:
    if evidence_table == "audit_event_evidence":
        rows = connection.execute(
            """
            SELECT artifact_id
            FROM audit_event_evidence
            WHERE event_id = ? AND evidence_type = 'download_artifact'
            ORDER BY artifact_id
            """,
            (event_id,),
        ).fetchall()
    elif evidence_table == "job_event_evidence":
        rows = connection.execute(
            """
            SELECT artifact_id
            FROM job_event_evidence
            WHERE event_id = ? AND evidence_type = 'download_artifact'
            ORDER BY artifact_id
            """,
            (event_id,),
        ).fetchall()
    else:
        raise ValueError("unsupported download evidence table")
    return tuple(row["artifact_id"] for row in rows)


def _require_payload_actor_role(payload: Mapping[str, Any], expected_role: str) -> None:
    actor_payload = payload.get("actor")
    if isinstance(actor_payload, Mapping) and "role" in actor_payload:
        if actor_payload["role"] != expected_role:
            raise ValueError("payload actor.role must match the review decision role")
    _require_payload_aliases_match(
        payload,
        ("actor_role", "role"),
        expected_role,
        field_name="review decision role",
    )


def _require_payload_review_outcome(
    payload: Mapping[str, Any],
    expected_decision: str,
) -> None:
    for alias in ("decision", "review_decision", "outcome"):
        if alias not in payload:
            continue
        value = payload[alias]
        if not isinstance(value, str) or not _review_decision_action_matches(
            expected_decision,
            value,
        ):
            raise ValueError(
                f"payload {alias} must match the scoped review decision outcome"
            )


def _is_upload_action(action: str) -> bool:
    return _require_audit_action_contract(action).requires_uploader


def _job_event_action_matches(event_type: str, action: str) -> bool:
    if event_type == action:
        return True
    contract = _audit_action_contract(action)
    if contract is not None and event_type in contract.event_types:
        return True
    event_state = _job_lifecycle_state(event_type)
    action_state = _job_lifecycle_state(action)
    return event_state is not None and event_state == action_state


def _job_lifecycle_state(value: str) -> str | None:
    contract = _audit_action_contract(value)
    return contract.lifecycle_state if contract is not None else None


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_json_object(payload_json: str, *, field_name: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{field_name} must be a JSON object")
    return payload


def _require_valid_json_text(payload_json: str, *, field_name: str) -> None:
    try:
        json.loads(payload_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{field_name} must be valid JSON") from exc


def _validate_managed_schema(
    connection: sqlite3.Connection,
    *,
    allow_missing: bool,
) -> None:
    expected = _expected_schema_definitions()
    existing = _schema_definitions(connection)
    for name, expected_definition in expected.items():
        existing_definition = existing.get(name)
        if existing_definition is None:
            if allow_missing:
                continue
            raise ValueError(f"managed database object is missing or incompatible: {name}")
        if existing_definition != expected_definition:
            raise ValueError(f"managed database object is missing or incompatible: {name}")


def _schema_definitions(connection: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    rows = connection.execute(
        """
        SELECT type, name, sql
        FROM sqlite_master
        WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    return {
        row["name"]: (row["type"], _normalize_schema_sql(row["sql"]))
        for row in rows
    }


def _expected_schema_definitions() -> dict[str, tuple[str, str]]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_SCHEMA_SQL)
        return _schema_definitions(connection)
    finally:
        connection.close()


def _normalize_schema_sql(sql: str) -> str:
    normalized: list[str] = []
    pending_space = False
    in_string = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if in_string:
            normalized.append(char)
            if char == "'":
                next_index = index + 1
                if next_index < len(sql) and sql[next_index] == "'":
                    normalized.append(sql[next_index])
                    index = next_index
                else:
                    in_string = False
            index += 1
            continue

        if char.isspace():
            pending_space = True
            index += 1
            continue
        if pending_space and normalized:
            normalized.append(" ")
            pending_space = False
        if char == "'":
            in_string = True
            normalized.append(char)
        else:
            normalized.append(char.lower())
        index += 1
    return "".join(normalized).strip()


def _validate_database_path(db_path: Path) -> Path:
    if str(db_path).strip() == "":
        raise ValueError("database path is required")
    if not db_path.is_absolute():
        repo_root = REPO_ROOT.resolve()
        resolved_path = (repo_root / db_path).resolve()
        if resolved_path != repo_root and repo_root not in resolved_path.parents:
            raise ValueError("relative database path must stay within the repository root")
        db_path = resolved_path
    if db_path.exists() and db_path.is_dir():
        raise ValueError("database path must be a file")
    return db_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage VeriDoc Phase11 persistence")
    parser.add_argument(
        "command",
        choices=("init-db", "reset-db"),
        help="database operation to run",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="SQLite database path; defaults to VERIDOC_DB_PATH or repo local var path",
    )
    args = parser.parse_args(argv)

    repository = SQLitePersistenceRepository(args.db_path)
    if args.command == "init-db":
        repository.initialize()
    else:
        repository.reset()
    return 0


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT NOT NULL PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_artifacts (
    artifact_id TEXT NOT NULL PRIMARY KEY CHECK(
        typeof(artifact_id) = 'text' AND length(trim(artifact_id)) > 0
    ),
    document_id TEXT NOT NULL UNIQUE CHECK(
        typeof(document_id) = 'text' AND length(trim(document_id)) > 0
    ) REFERENCES source_documents(document_id) ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED,
    storage_key TEXT NOT NULL UNIQUE CHECK(
        typeof(storage_key) = 'text' AND length(trim(storage_key)) > 0
    ),
    content_hash TEXT NOT NULL CHECK(
        typeof(content_hash) = 'text'
        AND
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    source_type TEXT NOT NULL CHECK(typeof(source_type) = 'text' AND length(trim(source_type)) > 0),
    original_filename TEXT NOT NULL CHECK(
        typeof(original_filename) = 'text' AND length(trim(original_filename)) > 0
    ),
    uploaded_by TEXT NOT NULL CHECK(typeof(uploaded_by) = 'text' AND length(trim(uploaded_by)) > 0),
    created_at TEXT NOT NULL CHECK(typeof(created_at) = 'text' AND length(trim(created_at)) > 0),
    UNIQUE(
        artifact_id, document_id, storage_key, content_hash, source_type,
        original_filename, uploaded_by
    )
);

CREATE TABLE IF NOT EXISTS source_documents (
    document_id TEXT NOT NULL PRIMARY KEY CHECK(
        typeof(document_id) = 'text' AND length(trim(document_id)) > 0
    ),
    source_type TEXT NOT NULL CHECK(typeof(source_type) = 'text' AND length(trim(source_type)) > 0),
    original_filename TEXT NOT NULL CHECK(
        typeof(original_filename) = 'text' AND length(trim(original_filename)) > 0
    ),
    source_artifact_id TEXT NOT NULL UNIQUE CHECK(
        typeof(source_artifact_id) = 'text' AND length(trim(source_artifact_id)) > 0
    ),
    source_storage_key TEXT NOT NULL UNIQUE CHECK(
        typeof(source_storage_key) = 'text' AND length(trim(source_storage_key)) > 0
    ),
    content_hash TEXT NOT NULL CHECK(
        typeof(content_hash) = 'text'
        AND
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    status TEXT NOT NULL CHECK(typeof(status) = 'text' AND length(trim(status)) > 0),
    uploaded_by TEXT NOT NULL CHECK(typeof(uploaded_by) = 'text' AND length(trim(uploaded_by)) > 0),
    created_at TEXT NOT NULL CHECK(typeof(created_at) = 'text' AND length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK(typeof(updated_at) = 'text' AND length(trim(updated_at)) > 0),
    FOREIGN KEY(
        source_artifact_id, document_id, source_storage_key, content_hash,
        source_type, original_filename, uploaded_by
    ) REFERENCES source_artifacts(
        artifact_id, document_id, storage_key, content_hash, source_type,
        original_filename, uploaded_by
    )
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT NOT NULL PRIMARY KEY CHECK(typeof(job_id) = 'text' AND length(trim(job_id)) > 0),
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE CHECK(
        typeof(idempotency_key) = 'text' AND length(trim(idempotency_key)) > 0
    ),
    mode TEXT NOT NULL CHECK(typeof(mode) = 'text' AND length(trim(mode)) > 0),
    status TEXT NOT NULL CHECK(typeof(status) = 'text' AND length(trim(status)) > 0),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(
        typeof(attempts) = 'integer'
        AND attempts >= 0
    ),
    created_at TEXT NOT NULL CHECK(typeof(created_at) = 'text' AND length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK(typeof(updated_at) = 'text' AND length(trim(updated_at)) > 0),
    UNIQUE(job_id, document_id)
);

CREATE TABLE IF NOT EXISTS job_events (
    event_id TEXT NOT NULL PRIMARY KEY CHECK(
        typeof(event_id) = 'text' AND length(trim(event_id)) > 0
    ),
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK(
        typeof(sequence) = 'integer'
        AND sequence > 0
    ),
    event_type TEXT NOT NULL CHECK(typeof(event_type) = 'text' AND length(trim(event_type)) > 0),
    actor TEXT NOT NULL CHECK(typeof(actor) = 'text' AND length(trim(actor)) > 0),
    payload_json TEXT NOT NULL CHECK(
        typeof(payload_json) = 'text'
        AND CASE
            WHEN json_valid(payload_json) THEN json_type(payload_json) = 'object'
            ELSE 0
        END
    ),
    created_at TEXT NOT NULL CHECK(typeof(created_at) = 'text' AND length(trim(created_at)) > 0),
    UNIQUE(job_id, sequence)
);

CREATE TABLE IF NOT EXISTS conversion_results (
    result_id TEXT NOT NULL PRIMARY KEY CHECK(
        typeof(result_id) = 'text' AND length(trim(result_id)) > 0
    ),
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK(typeof(status) = 'text' AND length(trim(status)) > 0),
    content_hash TEXT NOT NULL CHECK(
        typeof(content_hash) = 'text'
        AND
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL CHECK(typeof(created_at) = 'text' AND length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK(typeof(updated_at) = 'text' AND length(trim(updated_at)) > 0),
    UNIQUE(result_id, job_id, document_id),
    FOREIGN KEY(job_id, document_id) REFERENCES jobs(job_id, document_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS generated_artifacts (
    artifact_id TEXT NOT NULL PRIMARY KEY CHECK(
        typeof(artifact_id) = 'text' AND length(trim(artifact_id)) > 0
    ),
    result_id TEXT NOT NULL REFERENCES conversion_results(result_id) ON DELETE RESTRICT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    category TEXT NOT NULL CHECK(typeof(category) = 'text' AND length(trim(category)) > 0),
    format TEXT NOT NULL CHECK(typeof(format) = 'text' AND length(trim(format)) > 0),
    display_filename TEXT NOT NULL CHECK(
        typeof(display_filename) = 'text' AND length(trim(display_filename)) > 0
    ),
    storage_key TEXT NOT NULL UNIQUE CHECK(
        typeof(storage_key) = 'text' AND length(trim(storage_key)) > 0
    ),
    content_hash TEXT NOT NULL CHECK(
        typeof(content_hash) = 'text'
        AND
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    retention_state TEXT NOT NULL CHECK(
        typeof(retention_state) = 'text' AND length(trim(retention_state)) > 0
    ),
    created_at TEXT NOT NULL CHECK(typeof(created_at) = 'text' AND length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK(typeof(updated_at) = 'text' AND length(trim(updated_at)) > 0),
    UNIQUE(artifact_id, job_id, document_id),
    FOREIGN KEY(result_id, job_id, document_id)
        REFERENCES conversion_results(result_id, job_id, document_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS review_items (
    review_item_id TEXT NOT NULL PRIMARY KEY CHECK(
        typeof(review_item_id) = 'text' AND length(trim(review_item_id)) > 0
    ),
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    target_path TEXT NOT NULL CHECK(typeof(target_path) = 'text' AND length(trim(target_path)) > 0),
    status TEXT NOT NULL CHECK(typeof(status) = 'text' AND length(trim(status)) > 0),
    severity TEXT NOT NULL CHECK(typeof(severity) = 'text' AND length(trim(severity)) > 0),
    created_at TEXT NOT NULL CHECK(typeof(created_at) = 'text' AND length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK(typeof(updated_at) = 'text' AND length(trim(updated_at)) > 0),
    UNIQUE(review_item_id, job_id, document_id),
    FOREIGN KEY(job_id, document_id) REFERENCES jobs(job_id, document_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS review_decisions (
    decision_id TEXT NOT NULL PRIMARY KEY CHECK(
        typeof(decision_id) = 'text' AND length(trim(decision_id)) > 0
    ),
    review_item_id TEXT NOT NULL REFERENCES review_items(review_item_id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL REFERENCES generated_artifacts(artifact_id) ON DELETE RESTRICT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    actor TEXT NOT NULL CHECK(typeof(actor) = 'text' AND length(trim(actor)) > 0),
    role TEXT NOT NULL CHECK(typeof(role) = 'text' AND length(trim(role)) > 0),
    decision TEXT NOT NULL CHECK(typeof(decision) = 'text' AND length(trim(decision)) > 0),
    created_at TEXT NOT NULL CHECK(typeof(created_at) = 'text' AND length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK(typeof(updated_at) = 'text' AND length(trim(updated_at)) > 0),
    FOREIGN KEY(review_item_id, job_id, document_id)
        REFERENCES review_items(review_item_id, job_id, document_id) ON DELETE RESTRICT,
    FOREIGN KEY(artifact_id, job_id, document_id)
        REFERENCES generated_artifacts(artifact_id, job_id, document_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT NOT NULL PRIMARY KEY CHECK(
        typeof(event_id) = 'text' AND length(trim(event_id)) > 0
    ),
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK(
        typeof(sequence) = 'integer'
        AND sequence > 0
    ),
    integrity_algorithm TEXT NOT NULL CHECK(
        typeof(integrity_algorithm) = 'text'
        AND integrity_algorithm = 'sha256-canonical-json-chain-v1'
    ),
    actor TEXT NOT NULL CHECK(typeof(actor) = 'text' AND length(trim(actor)) > 0),
    action TEXT NOT NULL CHECK(typeof(action) = 'text' AND length(trim(action)) > 0),
    scope_type TEXT NOT NULL CHECK(typeof(scope_type) = 'text' AND length(trim(scope_type)) > 0),
    scope_id TEXT NOT NULL CHECK(typeof(scope_id) = 'text' AND length(trim(scope_id)) > 0),
    event_hash TEXT NOT NULL CHECK(
        typeof(event_hash) = 'text'
        AND
        length(event_hash) = 64
        AND event_hash NOT GLOB '*[^0-9a-f]*'
    ),
    prev_event_hash TEXT CHECK(
        prev_event_hash IS NULL
        OR (
            typeof(prev_event_hash) = 'text'
            AND
            length(prev_event_hash) = 64
            AND prev_event_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    payload_json TEXT NOT NULL CHECK(
        typeof(payload_json) = 'text'
        AND CASE
            WHEN json_valid(payload_json) THEN json_type(payload_json) = 'object'
            ELSE 0
        END
    ),
    created_at TEXT NOT NULL CHECK(typeof(created_at) = 'text' AND length(trim(created_at)) > 0),
    UNIQUE(sequence),
    FOREIGN KEY(job_id, document_id) REFERENCES jobs(job_id, document_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS audit_event_evidence (
    event_id TEXT NOT NULL REFERENCES audit_events(event_id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL REFERENCES generated_artifacts(artifact_id) ON DELETE RESTRICT,
    evidence_type TEXT NOT NULL CHECK(
        typeof(evidence_type) = 'text' AND length(trim(evidence_type)) > 0
    ),
    PRIMARY KEY(event_id, artifact_id)
);

CREATE TABLE IF NOT EXISTS job_event_evidence (
    event_id TEXT NOT NULL REFERENCES job_events(event_id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL REFERENCES generated_artifacts(artifact_id) ON DELETE RESTRICT,
    evidence_type TEXT NOT NULL CHECK(
        typeof(evidence_type) = 'text' AND length(trim(evidence_type)) > 0
    ),
    PRIMARY KEY(event_id, artifact_id)
);

CREATE TRIGGER IF NOT EXISTS source_artifacts_parent_reference_insert
BEFORE INSERT ON source_artifacts
WHEN NOT EXISTS (
    SELECT 1 FROM source_documents
    WHERE source_documents.document_id = NEW.document_id
      AND source_documents.source_artifact_id = NEW.artifact_id
      AND source_documents.source_storage_key = NEW.storage_key
      AND source_documents.content_hash = NEW.content_hash
      AND source_documents.source_type = NEW.source_type
      AND source_documents.original_filename = NEW.original_filename
      AND source_documents.uploaded_by = NEW.uploaded_by
)
AND veridoc_source_artifact_insert_allowed(
    NEW.artifact_id,
    NEW.document_id,
    NEW.storage_key,
    NEW.content_hash,
    NEW.source_type,
    NEW.original_filename,
    NEW.uploaded_by
) != 1
BEGIN
    SELECT RAISE(ABORT, 'source artifact insert requires a repository document binding');
END;

CREATE TRIGGER IF NOT EXISTS jobs_parent_reference_insert
BEFORE INSERT ON jobs
WHEN NOT EXISTS (
    SELECT 1 FROM source_documents
    WHERE source_documents.document_id = NEW.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'record must reference matching parent rows');
END;

CREATE TRIGGER IF NOT EXISTS jobs_parent_reference_update
BEFORE UPDATE OF document_id ON jobs
WHEN NOT EXISTS (
    SELECT 1 FROM source_documents
    WHERE source_documents.document_id = NEW.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'record must reference matching parent rows');
END;

CREATE TRIGGER IF NOT EXISTS source_documents_source_artifact_reference_insert
BEFORE INSERT ON source_documents
WHEN NOT EXISTS (
    SELECT 1 FROM source_artifacts
    WHERE source_artifacts.artifact_id = NEW.source_artifact_id
      AND source_artifacts.document_id = NEW.document_id
      AND source_artifacts.storage_key = NEW.source_storage_key
      AND source_artifacts.content_hash = NEW.content_hash
      AND source_artifacts.source_type = NEW.source_type
      AND source_artifacts.original_filename = NEW.original_filename
      AND source_artifacts.uploaded_by = NEW.uploaded_by
)
BEGIN
    SELECT RAISE(ABORT, 'source document must reference matching source artifact');
END;

CREATE TRIGGER IF NOT EXISTS source_documents_source_artifact_reference_update
BEFORE UPDATE OF source_artifact_id, document_id, source_storage_key, content_hash,
                 source_type, original_filename, uploaded_by
ON source_documents
WHEN NOT EXISTS (
    SELECT 1 FROM source_artifacts
    WHERE source_artifacts.artifact_id = NEW.source_artifact_id
      AND source_artifacts.document_id = NEW.document_id
      AND source_artifacts.storage_key = NEW.source_storage_key
      AND source_artifacts.content_hash = NEW.content_hash
      AND source_artifacts.source_type = NEW.source_type
      AND source_artifacts.original_filename = NEW.original_filename
      AND source_artifacts.uploaded_by = NEW.uploaded_by
)
BEGIN
    SELECT RAISE(ABORT, 'source document must reference matching source artifact');
END;

CREATE TRIGGER IF NOT EXISTS job_events_parent_reference_insert
BEFORE INSERT ON job_events
WHEN NOT EXISTS (
    SELECT 1 FROM jobs
    WHERE jobs.job_id = NEW.job_id
)
BEGIN
    SELECT RAISE(ABORT, 'record must reference matching parent rows');
END;

CREATE TRIGGER IF NOT EXISTS conversion_results_parent_reference_insert
BEFORE INSERT ON conversion_results
WHEN NOT EXISTS (
    SELECT 1 FROM jobs
    WHERE jobs.job_id = NEW.job_id
      AND jobs.document_id = NEW.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'record must reference matching parent rows');
END;

CREATE TRIGGER IF NOT EXISTS conversion_results_parent_reference_update
BEFORE UPDATE OF job_id, document_id ON conversion_results
WHEN NOT EXISTS (
    SELECT 1 FROM jobs
    WHERE jobs.job_id = NEW.job_id
      AND jobs.document_id = NEW.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'record must reference matching parent rows');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_parent_reference_insert
BEFORE INSERT ON generated_artifacts
WHEN NOT EXISTS (
    SELECT 1 FROM conversion_results
    WHERE conversion_results.result_id = NEW.result_id
      AND conversion_results.job_id = NEW.job_id
      AND conversion_results.document_id = NEW.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'record must reference matching parent rows');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_parent_reference_update
BEFORE UPDATE OF result_id, job_id, document_id ON generated_artifacts
WHEN NOT EXISTS (
    SELECT 1 FROM conversion_results
    WHERE conversion_results.result_id = NEW.result_id
      AND conversion_results.job_id = NEW.job_id
      AND conversion_results.document_id = NEW.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'record must reference matching parent rows');
END;

CREATE TRIGGER IF NOT EXISTS review_items_parent_reference_insert
BEFORE INSERT ON review_items
WHEN NOT EXISTS (
    SELECT 1 FROM jobs
    WHERE jobs.job_id = NEW.job_id
      AND jobs.document_id = NEW.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'record must reference matching parent rows');
END;

CREATE TRIGGER IF NOT EXISTS review_items_parent_reference_update
BEFORE UPDATE OF job_id, document_id ON review_items
WHEN NOT EXISTS (
    SELECT 1 FROM jobs
    WHERE jobs.job_id = NEW.job_id
      AND jobs.document_id = NEW.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'record must reference matching parent rows');
END;

CREATE TRIGGER IF NOT EXISTS review_decisions_parent_reference_insert
BEFORE INSERT ON review_decisions
WHEN NOT EXISTS (
    SELECT 1 FROM review_items
    WHERE review_items.review_item_id = NEW.review_item_id
      AND review_items.job_id = NEW.job_id
      AND review_items.document_id = NEW.document_id
)
OR NOT EXISTS (
    SELECT 1 FROM generated_artifacts
    WHERE generated_artifacts.artifact_id = NEW.artifact_id
      AND generated_artifacts.job_id = NEW.job_id
      AND generated_artifacts.document_id = NEW.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'record must reference matching parent rows');
END;

CREATE TRIGGER IF NOT EXISTS review_decisions_parent_reference_update
BEFORE UPDATE OF review_item_id, artifact_id, job_id, document_id ON review_decisions
WHEN NOT EXISTS (
    SELECT 1 FROM review_items
    WHERE review_items.review_item_id = NEW.review_item_id
      AND review_items.job_id = NEW.job_id
      AND review_items.document_id = NEW.document_id
)
OR NOT EXISTS (
    SELECT 1 FROM generated_artifacts
    WHERE generated_artifacts.artifact_id = NEW.artifact_id
      AND generated_artifacts.job_id = NEW.job_id
      AND generated_artifacts.document_id = NEW.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'record must reference matching parent rows');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_parent_reference_insert
BEFORE INSERT ON audit_events
WHEN NOT EXISTS (
    SELECT 1 FROM jobs
    WHERE jobs.job_id = NEW.job_id
      AND jobs.document_id = NEW.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'record must reference matching parent rows');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_scope_reference_insert
BEFORE INSERT ON audit_events
WHEN NOT (
    (
        NEW.scope_type IN ('document', 'source_document')
        AND EXISTS (
            SELECT 1 FROM source_documents
            WHERE source_documents.document_id = NEW.scope_id
              AND source_documents.document_id = NEW.document_id
        )
    )
    OR (
        NEW.scope_type = 'source_artifact'
        AND EXISTS (
            SELECT 1
            FROM source_artifacts
            JOIN jobs ON jobs.job_id = NEW.job_id
            WHERE source_artifacts.artifact_id = NEW.scope_id
              AND source_artifacts.document_id = NEW.document_id
              AND jobs.document_id = source_artifacts.document_id
        )
    )
    OR (
        NEW.scope_type IN ('job', 'conversion_job')
        AND EXISTS (
            SELECT 1 FROM jobs
            WHERE jobs.job_id = NEW.scope_id
              AND jobs.job_id = NEW.job_id
              AND jobs.document_id = NEW.document_id
        )
    )
    OR (
        NEW.scope_type = 'job_event'
        AND EXISTS (
            SELECT 1
            FROM job_events
            JOIN jobs ON jobs.job_id = job_events.job_id
            WHERE job_events.event_id = NEW.scope_id
              AND job_events.job_id = NEW.job_id
              AND jobs.document_id = NEW.document_id
        )
    )
    OR (
        NEW.scope_type = 'conversion_result'
        AND EXISTS (
            SELECT 1 FROM conversion_results
            WHERE conversion_results.result_id = NEW.scope_id
              AND conversion_results.job_id = NEW.job_id
              AND conversion_results.document_id = NEW.document_id
        )
    )
    OR (
        NEW.scope_type IN ('artifact', 'generated_artifact')
        AND EXISTS (
            SELECT 1 FROM generated_artifacts
            WHERE generated_artifacts.artifact_id = NEW.scope_id
              AND generated_artifacts.job_id = NEW.job_id
              AND generated_artifacts.document_id = NEW.document_id
        )
    )
    OR (
        NEW.scope_type = 'review_item'
        AND EXISTS (
            SELECT 1 FROM review_items
            WHERE review_items.review_item_id = NEW.scope_id
              AND review_items.job_id = NEW.job_id
              AND review_items.document_id = NEW.document_id
        )
    )
    OR (
        NEW.scope_type = 'review_decision'
        AND EXISTS (
            SELECT 1 FROM review_decisions
            WHERE review_decisions.decision_id = NEW.scope_id
              AND review_decisions.job_id = NEW.job_id
              AND review_decisions.document_id = NEW.document_id
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope must reference an existing matching row');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_contiguous_sequence_insert
BEFORE INSERT ON audit_events
WHEN NEW.sequence != COALESCE((
    SELECT MAX(audit_events.sequence) + 1
    FROM audit_events
), 1)
BEGIN
    SELECT RAISE(ABORT, 'audit events must be contiguous');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_prev_hash_insert
BEFORE INSERT ON audit_events
WHEN NEW.prev_event_hash IS NOT (
    SELECT audit_events.event_hash
    FROM audit_events
    ORDER BY audit_events.sequence DESC
    LIMIT 1
)
BEGIN
    SELECT RAISE(ABORT, 'audit event prev hash must match the current chain tail');
END;

CREATE TRIGGER IF NOT EXISTS job_events_contiguous_sequence_insert
BEFORE INSERT ON job_events
WHEN NEW.sequence != COALESCE((
    SELECT MAX(job_events.sequence) + 1
    FROM job_events
    WHERE job_events.job_id = NEW.job_id
), 1)
BEGIN
    SELECT RAISE(ABORT, 'job events must be contiguous');
END;

CREATE TRIGGER IF NOT EXISTS job_events_no_update
BEFORE UPDATE ON job_events
BEGIN
    SELECT RAISE(ABORT, 'job events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS job_events_no_delete
BEFORE DELETE ON job_events
BEGIN
    SELECT RAISE(ABORT, 'job events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_event_evidence_reference_insert
BEFORE INSERT ON audit_event_evidence
WHEN NEW.evidence_type != 'download_artifact'
OR NOT EXISTS (
    SELECT 1
    FROM audit_events
    JOIN generated_artifacts
      ON generated_artifacts.artifact_id = NEW.artifact_id
    WHERE audit_events.event_id = NEW.event_id
      AND audit_events.action IN ('desktop_result_download', 'download_result')
      AND audit_events.job_id = generated_artifacts.job_id
      AND audit_events.document_id = generated_artifacts.document_id
      AND json_extract(audit_events.payload_json, '$.evidence.type') = NEW.evidence_type
      AND EXISTS (
          SELECT 1
          FROM json_each(
              audit_events.payload_json,
              '$.evidence.artifact_ids'
          ) AS evidence_artifact
          WHERE evidence_artifact.type = 'text'
            AND evidence_artifact.value = NEW.artifact_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'audit evidence must match its event and artifact');
END;

CREATE TRIGGER IF NOT EXISTS audit_event_evidence_no_update
BEFORE UPDATE ON audit_event_evidence
BEGIN
    SELECT RAISE(ABORT, 'audit evidence links are append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_event_evidence_no_delete
BEFORE DELETE ON audit_event_evidence
BEGIN
    SELECT RAISE(ABORT, 'audit evidence links are append-only');
END;

CREATE TRIGGER IF NOT EXISTS job_event_evidence_reference_insert
BEFORE INSERT ON job_event_evidence
WHEN NEW.evidence_type != 'download_artifact'
OR NOT EXISTS (
    SELECT 1
    FROM job_events
    JOIN generated_artifacts
      ON generated_artifacts.artifact_id = NEW.artifact_id
    WHERE job_events.event_id = NEW.event_id
      AND job_events.job_id = generated_artifacts.job_id
      AND json_extract(job_events.payload_json, '$.evidence.type') = NEW.evidence_type
      AND EXISTS (
          SELECT 1
          FROM json_each(
              job_events.payload_json,
              '$.evidence.artifact_ids'
          ) AS evidence_artifact
          WHERE evidence_artifact.type = 'text'
            AND evidence_artifact.value = NEW.artifact_id
      )
      AND (
          job_events.event_type IN ('desktop_result_download', 'download_result')
          OR json_extract(job_events.payload_json, '$.action')
             IN ('desktop_result_download', 'download_result')
      )
      AND (
          json_extract(job_events.payload_json, '$.download_filename') IS NULL
          OR json_extract(job_events.payload_json, '$.download_filename')
             = generated_artifacts.display_filename
      )
      AND (
          json_extract(job_events.payload_json, '$.output_sha256') IS NULL
          OR json_extract(job_events.payload_json, '$.output_sha256')
             = generated_artifacts.content_hash
      )
)
BEGIN
    SELECT RAISE(ABORT, 'job event evidence must match its event and artifact');
END;

CREATE TRIGGER IF NOT EXISTS job_event_evidence_no_update
BEFORE UPDATE ON job_event_evidence
BEGIN
    SELECT RAISE(ABORT, 'job event evidence links are append-only');
END;

CREATE TRIGGER IF NOT EXISTS job_event_evidence_no_delete
BEFORE DELETE ON job_event_evidence
BEGIN
    SELECT RAISE(ABORT, 'job event evidence links are append-only');
END;

CREATE TRIGGER IF NOT EXISTS source_artifacts_no_update
BEFORE UPDATE ON source_artifacts
BEGIN
    SELECT RAISE(ABORT, 'source artifacts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS source_artifacts_no_delete
BEFORE DELETE ON source_artifacts
WHEN EXISTS (
    SELECT 1 FROM source_documents
    WHERE source_documents.source_artifact_id = OLD.artifact_id
)
OR EXISTS (
    SELECT 1 FROM audit_events
    WHERE audit_events.scope_type = 'source_artifact'
      AND audit_events.scope_id = OLD.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'source artifacts referenced by provenance cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS source_documents_parent_key_no_update
BEFORE UPDATE OF document_id ON source_documents
WHEN EXISTS (
    SELECT 1 FROM source_artifacts
    WHERE source_artifacts.document_id = OLD.document_id
)
OR EXISTS (
    SELECT 1 FROM jobs
    WHERE jobs.document_id = OLD.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'parent keys referenced by lifecycle rows cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS source_documents_parent_no_delete
BEFORE DELETE ON source_documents
WHEN EXISTS (
    SELECT 1 FROM source_artifacts
    WHERE source_artifacts.document_id = OLD.document_id
)
OR EXISTS (
    SELECT 1 FROM jobs
    WHERE jobs.document_id = OLD.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'parent rows referenced by lifecycle rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS jobs_parent_key_no_update
BEFORE UPDATE OF job_id, document_id, idempotency_key, mode ON jobs
WHEN NEW.job_id != OLD.job_id
  OR NEW.document_id != OLD.document_id
  OR NEW.idempotency_key != OLD.idempotency_key
  OR NEW.mode != OLD.mode
BEGIN
    SELECT RAISE(ABORT, 'job identity and source binding cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS jobs_parent_no_delete
BEFORE DELETE ON jobs
WHEN EXISTS (SELECT 1 FROM job_events WHERE job_events.job_id = OLD.job_id)
OR EXISTS (SELECT 1 FROM conversion_results WHERE conversion_results.job_id = OLD.job_id)
OR EXISTS (SELECT 1 FROM review_items WHERE review_items.job_id = OLD.job_id)
OR EXISTS (SELECT 1 FROM audit_events WHERE audit_events.job_id = OLD.job_id)
BEGIN
    SELECT RAISE(ABORT, 'parent rows referenced by lifecycle rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS conversion_results_parent_key_no_update
BEFORE UPDATE OF result_id, job_id, document_id ON conversion_results
WHEN (
    NEW.result_id != OLD.result_id
    OR NEW.job_id != OLD.job_id
    OR NEW.document_id != OLD.document_id
)
AND EXISTS (
    SELECT 1 FROM generated_artifacts
    WHERE generated_artifacts.result_id = OLD.result_id
)
BEGIN
    SELECT RAISE(ABORT, 'parent keys referenced by lifecycle rows cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS conversion_results_parent_no_delete
BEFORE DELETE ON conversion_results
WHEN EXISTS (
    SELECT 1 FROM generated_artifacts
    WHERE generated_artifacts.result_id = OLD.result_id
)
BEGIN
    SELECT RAISE(ABORT, 'parent rows referenced by lifecycle rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS conversion_results_audit_evidence_no_update
BEFORE UPDATE ON conversion_results
WHEN EXISTS (
    SELECT 1
    FROM audit_event_evidence
    JOIN generated_artifacts
      ON generated_artifacts.artifact_id = audit_event_evidence.artifact_id
    WHERE generated_artifacts.result_id = OLD.result_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit evidence rows cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS conversion_results_audit_evidence_no_delete
BEFORE DELETE ON conversion_results
WHEN EXISTS (
    SELECT 1
    FROM audit_event_evidence
    JOIN generated_artifacts
      ON generated_artifacts.artifact_id = audit_event_evidence.artifact_id
    WHERE generated_artifacts.result_id = OLD.result_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit evidence rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS conversion_results_job_event_evidence_no_update
BEFORE UPDATE ON conversion_results
WHEN EXISTS (
    SELECT 1
    FROM job_event_evidence
    JOIN generated_artifacts
      ON generated_artifacts.artifact_id = job_event_evidence.artifact_id
    WHERE generated_artifacts.result_id = OLD.result_id
)
BEGIN
    SELECT RAISE(ABORT, 'job event evidence rows cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS conversion_results_job_event_evidence_no_delete
BEFORE DELETE ON conversion_results
WHEN EXISTS (
    SELECT 1
    FROM job_event_evidence
    JOIN generated_artifacts
      ON generated_artifacts.artifact_id = job_event_evidence.artifact_id
    WHERE generated_artifacts.result_id = OLD.result_id
)
BEGIN
    SELECT RAISE(ABORT, 'job event evidence rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_review_decision_no_update
BEFORE UPDATE ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM review_decisions
    WHERE review_decisions.artifact_id = OLD.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'review decision evidence cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_audit_evidence_no_update
BEFORE UPDATE ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM audit_event_evidence
    WHERE audit_event_evidence.artifact_id = OLD.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit evidence rows cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_audit_evidence_no_delete
BEFORE DELETE ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM audit_event_evidence
    WHERE audit_event_evidence.artifact_id = OLD.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit evidence rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_job_event_evidence_no_update
BEFORE UPDATE ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM job_event_evidence
    WHERE job_event_evidence.artifact_id = OLD.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'job event evidence rows cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_job_event_evidence_no_delete
BEFORE DELETE ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM job_event_evidence
    WHERE job_event_evidence.artifact_id = OLD.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'job event evidence rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_review_decision_no_delete
BEFORE DELETE ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM review_decisions
    WHERE review_decisions.artifact_id = OLD.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'review decision evidence cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS review_items_review_decision_no_update
BEFORE UPDATE ON review_items
WHEN EXISTS (
    SELECT 1 FROM review_decisions
    WHERE review_decisions.review_item_id = OLD.review_item_id
)
BEGIN
    SELECT RAISE(ABORT, 'review decision evidence cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS review_items_review_decision_no_delete
BEFORE DELETE ON review_items
WHEN EXISTS (
    SELECT 1 FROM review_decisions
    WHERE review_decisions.review_item_id = OLD.review_item_id
)
BEGIN
    SELECT RAISE(ABORT, 'review decision evidence cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS source_documents_audit_scope_no_delete
BEFORE DELETE ON source_documents
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE audit_events.document_id = OLD.document_id
       OR (
           audit_events.scope_type IN ('document', 'source_document')
           AND audit_events.scope_id = OLD.document_id
       )
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS jobs_audit_scope_no_delete
BEFORE DELETE ON jobs
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE audit_events.job_id = OLD.job_id
       OR (
           audit_events.scope_type IN ('job', 'conversion_job')
           AND audit_events.scope_id = OLD.job_id
       )
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS job_events_audit_scope_no_delete
BEFORE DELETE ON job_events
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE audit_events.scope_type = 'job_event'
      AND audit_events.scope_id = OLD.event_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS conversion_results_audit_scope_no_delete
BEFORE DELETE ON conversion_results
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE (
            audit_events.scope_type = 'conversion_result'
            AND audit_events.scope_id = OLD.result_id
          )
       OR (
            audit_events.scope_type IN ('artifact', 'generated_artifact')
            AND EXISTS (
                SELECT 1 FROM generated_artifacts
                WHERE generated_artifacts.artifact_id = audit_events.scope_id
                  AND generated_artifacts.result_id = OLD.result_id
            )
          )
       OR (
            audit_events.scope_type = 'review_decision'
            AND EXISTS (
                SELECT 1
                FROM review_decisions
                JOIN generated_artifacts
                  ON generated_artifacts.artifact_id = review_decisions.artifact_id
                WHERE review_decisions.decision_id = audit_events.scope_id
                  AND generated_artifacts.result_id = OLD.result_id
            )
          )
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_audit_scope_no_delete
BEFORE DELETE ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE audit_events.scope_type IN ('artifact', 'generated_artifact')
      AND audit_events.scope_id = OLD.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS review_items_audit_scope_no_delete
BEFORE DELETE ON review_items
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE audit_events.scope_type = 'review_item'
      AND audit_events.scope_id = OLD.review_item_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS review_decisions_no_delete
BEFORE DELETE ON review_decisions
BEGIN
    SELECT RAISE(ABORT, 'review decisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS source_documents_audit_scope_no_update
BEFORE UPDATE ON source_documents
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE audit_events.document_id = OLD.document_id
       OR (
           audit_events.scope_type IN ('document', 'source_document')
           AND audit_events.scope_id = OLD.document_id
       )
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS jobs_audit_scope_no_update
BEFORE UPDATE OF job_id, document_id, idempotency_key, mode ON jobs
WHEN (
    NEW.job_id != OLD.job_id
    OR NEW.document_id != OLD.document_id
    OR NEW.idempotency_key != OLD.idempotency_key
    OR NEW.mode != OLD.mode
)
AND EXISTS (
    SELECT 1 FROM audit_events
    WHERE audit_events.job_id = OLD.job_id
       OR (
           audit_events.scope_type IN ('job', 'conversion_job')
           AND audit_events.scope_id = OLD.job_id
       )
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS conversion_results_audit_scope_no_update
BEFORE UPDATE ON conversion_results
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE (
            audit_events.scope_type = 'conversion_result'
            AND audit_events.scope_id = OLD.result_id
          )
       OR (
            audit_events.scope_type IN ('artifact', 'generated_artifact')
            AND EXISTS (
                SELECT 1 FROM generated_artifacts
                WHERE generated_artifacts.artifact_id = audit_events.scope_id
                  AND generated_artifacts.result_id = OLD.result_id
            )
          )
       OR (
            audit_events.scope_type = 'review_decision'
            AND EXISTS (
                SELECT 1
                FROM review_decisions
                JOIN generated_artifacts
                  ON generated_artifacts.artifact_id = review_decisions.artifact_id
                WHERE review_decisions.decision_id = audit_events.scope_id
                  AND generated_artifacts.result_id = OLD.result_id
            )
          )
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_audit_scope_no_update
BEFORE UPDATE ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE audit_events.scope_type IN ('artifact', 'generated_artifact')
      AND audit_events.scope_id = OLD.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS review_items_audit_scope_no_update
BEFORE UPDATE ON review_items
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE audit_events.scope_type = 'review_item'
      AND audit_events.scope_id = OLD.review_item_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS review_decisions_no_update
BEFORE UPDATE ON review_decisions
BEGIN
    SELECT RAISE(ABORT, 'review decisions are append-only');
END;

CREATE TRIGGER IF NOT EXISTS source_documents_artifact_id_global_insert
BEFORE INSERT ON source_documents
WHEN EXISTS (
    SELECT 1 FROM generated_artifacts
    WHERE generated_artifacts.artifact_id = NEW.source_artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'source artifact id must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS source_artifacts_artifact_id_global_insert
BEFORE INSERT ON source_artifacts
WHEN EXISTS (
    SELECT 1 FROM generated_artifacts
    WHERE generated_artifacts.artifact_id = NEW.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'source artifact id must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS source_artifacts_artifact_id_global_update
BEFORE UPDATE OF artifact_id ON source_artifacts
WHEN EXISTS (
    SELECT 1 FROM generated_artifacts
    WHERE generated_artifacts.artifact_id = NEW.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'source artifact id must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS source_documents_artifact_id_global_update
BEFORE UPDATE OF source_artifact_id ON source_documents
WHEN EXISTS (
    SELECT 1 FROM generated_artifacts
    WHERE generated_artifacts.artifact_id = NEW.source_artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'source artifact id must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS source_documents_storage_key_global_insert
BEFORE INSERT ON source_documents
WHEN EXISTS (
    SELECT 1 FROM generated_artifacts
    WHERE generated_artifacts.storage_key = NEW.source_storage_key
)
BEGIN
    SELECT RAISE(ABORT, 'source storage key must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS source_artifacts_storage_key_global_insert
BEFORE INSERT ON source_artifacts
WHEN EXISTS (
    SELECT 1 FROM generated_artifacts
    WHERE generated_artifacts.storage_key = NEW.storage_key
)
BEGIN
    SELECT RAISE(ABORT, 'source storage key must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS source_artifacts_storage_key_global_update
BEFORE UPDATE OF storage_key ON source_artifacts
WHEN EXISTS (
    SELECT 1 FROM generated_artifacts
    WHERE generated_artifacts.storage_key = NEW.storage_key
)
BEGIN
    SELECT RAISE(ABORT, 'source storage key must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS source_documents_storage_key_global_update
BEFORE UPDATE OF source_storage_key ON source_documents
WHEN EXISTS (
    SELECT 1 FROM generated_artifacts
    WHERE generated_artifacts.storage_key = NEW.source_storage_key
)
BEGIN
    SELECT RAISE(ABORT, 'source storage key must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_artifact_id_global_insert
BEFORE INSERT ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM source_artifacts
    WHERE source_artifacts.artifact_id = NEW.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'generated artifact id must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_artifact_id_global_update
BEFORE UPDATE OF artifact_id ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM source_artifacts
    WHERE source_artifacts.artifact_id = NEW.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'generated artifact id must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_storage_key_global_insert
BEFORE INSERT ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM source_artifacts
    WHERE source_artifacts.storage_key = NEW.storage_key
)
BEGIN
    SELECT RAISE(ABORT, 'generated storage key must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_storage_key_global_update
BEFORE UPDATE OF storage_key ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM source_artifacts
    WHERE source_artifacts.storage_key = NEW.storage_key
)
BEGIN
    SELECT RAISE(ABORT, 'generated storage key must be globally unique');
END;
"""

_RESET_SQL = """
DROP TABLE IF EXISTS audit_event_evidence;
DROP TABLE IF EXISTS job_event_evidence;
DROP TABLE IF EXISTS audit_events;
DROP TABLE IF EXISTS review_decisions;
DROP TABLE IF EXISTS review_items;
DROP TABLE IF EXISTS generated_artifacts;
DROP TABLE IF EXISTS conversion_results;
DROP TABLE IF EXISTS job_events;
DROP TABLE IF EXISTS jobs;
DROP TABLE IF EXISTS source_documents;
DROP TABLE IF EXISTS source_artifacts;
DROP TABLE IF EXISTS schema_migrations;
"""


__all__ = [
    "Artifact",
    "AuditEvent",
    "ConversionJob",
    "ConversionResult",
    "Document",
    "JobEvent",
    "ReviewDecision",
    "ReviewItem",
    "SQLitePersistenceRepository",
    "SourceArtifact",
    "default_database_path",
    "initialize_database",
    "reset_database",
]


if __name__ == "__main__":
    raise SystemExit(main())
