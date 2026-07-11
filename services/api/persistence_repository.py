from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

REPO_ROOT = Path(__file__).resolve().parents[2]

from services.api.persistence_schema import (
    _expected_schema_definitions,
    _normalize_schema_sql,
    _RESET_SQL,
    _SCHEMA_SQL,
    _schema_definitions,
    _validate_managed_schema,
)
from services.api.persistence_models import (
    Artifact,
    AuditEvent,
    ConversionJob,
    ConversionResult,
    Document,
    JobEvent,
    ReviewDecision,
    ReviewItem,
    SourceArtifact,
)


SCHEMA_VERSION = "20260710_16_successful_evidence_triggers"
AUDIT_INTEGRITY_ALGORITHM = "sha256-canonical-json-chain-v1"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_DB_PATH = REPO_ROOT / "var" / "veridoc" / "dev.sqlite3"
_SOURCE_ARTIFACT_INSERT_BINDING: ContextVar[tuple[str, ...] | None] = ContextVar(
    "veridoc_source_artifact_insert_binding",
    default=None,
)


def _source_artifact_insert_allowed(*binding: str) -> int:
    return int(_SOURCE_ARTIFACT_INSERT_BINDING.get() == binding)


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
        _require_attempt_count(attempts)
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
        _require_attempt_count(attempts)
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
            event_payload = (
                {"event_type": event_type} if payload is None else payload
            )
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
                   conversion_results.status AS result_status,
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
            and row["result_status"] in _SUCCEEDED_RESULT_STATUSES
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
    database_path = Path(configured) if configured else DEFAULT_DB_PATH
    return _validate_database_path(database_path)


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
    # Preserve the legacy facade's monkeypatch seam while the implementation
    # lives in this module.
    from services.api import persistence

    facade_clock = getattr(persistence, "_utc_now", _utc_now)
    if facade_clock is not _utc_now:
        return facade_clock()
    return datetime.now(timezone.utc).isoformat()


def _require_non_empty(**values: str) -> None:
    for field_name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} is required")


def _require_attempt_count(attempts: Any) -> None:
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
        raise ValueError("attempts must be a non-negative integer")


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


from services.api.persistence_contracts import (
    _AuditActionContract,
    _JOB_AUDIT_SCOPES,
    _RESULT_LIFECYCLE_SCOPES,
    _DESKTOP_AUDIT_SCOPES,
    _UPLOAD_AUDIT_SCOPES,
    _REVIEW_REQUEST_SCOPES,
    _QUEUED_JOB_STATUSES,
    _RUNNING_JOB_STATUSES,
    _FAILED_JOB_STATUSES,
    _SUCCEEDED_JOB_STATUSES,
    _FAILED_RESULT_STATUSES,
    _SUCCEEDED_RESULT_STATUSES,
    _AUDIT_ACTION_CONTRACTS,
    _AUDIT_ACTION_CONTRACT_BY_ALIAS,
    _CONTRACT_EVENT_TYPES,
    _audit_action_contract,
    _require_audit_action_contract,
    _DOWNLOAD_EVIDENCE_ALIASES,
    _EPHEMERAL_DESKTOP_PAYLOAD_FIELDS,
    _evidence_aliases_for_contract,
    _require_audit_action_scope,
    _AUDIT_SCOPE_ID_ALIASES,
    _audit_event_type_matches_action,
    _review_decision_action_matches,
    _SOURCE_DOCUMENT_PAYLOAD_BINDINGS,
    _SOURCE_DOCUMENT_STATUS_ALIASES,
    _SOURCE_DOCUMENT_IMMUTABLE_PAYLOAD_BINDINGS,
    _SOURCE_ARTIFACT_PAYLOAD_BINDINGS,
    _JOB_PAYLOAD_BINDINGS,
    _DESKTOP_DOCUMENT_JOB_PAYLOAD_BINDINGS,
    _DESKTOP_DOCUMENT_JOB_PAYLOAD_ALIASES,
    _JOB_HISTORY_MUTABLE_ALIASES,
    _JOB_IMMUTABLE_PAYLOAD_BINDINGS,
    _JOB_EVENT_PAYLOAD_BINDINGS,
    _JOB_EVENT_IMMUTABLE_PAYLOAD_BINDINGS,
    _RESULT_PAYLOAD_BINDINGS,
    _ARTIFACT_PAYLOAD_BINDINGS,
    _REVIEW_ITEM_PAYLOAD_BINDINGS,
    _REVIEW_DECISION_PAYLOAD_BINDINGS,
    _AUDIT_SCOPE_PAYLOAD_BINDINGS,
    _AUDIT_SCOPE_SPECIAL_PAYLOAD_FIELDS,
    _AUDIT_GLOBAL_PAYLOAD_FIELDS,
    _AUDIT_RESERVED_EVIDENCE_FIELDS,
)


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
    elif scope_type in {"document", "source_document"} and historical:
        allowed_aliases = (
            _SOURCE_DOCUMENT_STATUS_ALIASES
            | _evidence_aliases_for_contract(contract)
        )
        if (
            scope_type == "document"
            and contract.name in {"desktop_upload", "desktop_result_download"}
        ):
            allowed_aliases |= _DESKTOP_DOCUMENT_JOB_PAYLOAD_ALIASES
        _require_declared_audit_scope_payload(
            scope_type,
            row,
            payload,
            bindings=_SOURCE_DOCUMENT_IMMUTABLE_PAYLOAD_BINDINGS,
            allowed_aliases=allowed_aliases,
        )
        _require_source_document_status_aliases(payload)
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


def _require_source_document_status_aliases(payload: Mapping[str, Any]) -> None:
    status_values = []
    for alias in _SOURCE_DOCUMENT_STATUS_ALIASES:
        if alias not in payload:
            continue
        value = payload[alias]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"payload {alias} must be a non-empty document status")
        status_values.append(value)
    if len(set(status_values)) > 1:
        raise ValueError("payload document status aliases must match")


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
