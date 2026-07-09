from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar


SCHEMA_VERSION = "20260709_01_minimal_phase11_schema"
AUDIT_INTEGRITY_ALGORITHM = "sha256-canonical-json-chain-v1"
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "var" / "veridoc" / "dev.sqlite3"


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
        return self._insert_and_get(
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

    def get_document(self, document_id: str) -> Document | None:
        return self._get_one(
            Document,
            "SELECT * FROM source_documents WHERE document_id = ?",
            (document_id,),
        )

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
            sequence = self._next_job_event_sequence(connection, job_id)
            now = _utc_now()
            _require_job_event_payload_matches(
                payload,
                event_id=event_id,
                job_id=job_id,
                event_type=event_type,
                actor=actor,
            )
            payload_json = _canonical_json(payload or {"event_type": event_type})
            return self._insert_and_get_from_connection(
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

    def get_job_event(self, event_id: str) -> JobEvent | None:
        with self._connection_scope() as connection:
            row = connection.execute(
                "SELECT * FROM job_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        _require_job_event_row_payload_matches(row)
        return _row_to_dataclass(JobEvent, row)

    def list_job_events(self, job_id: str) -> list[JobEvent]:
        _require_non_empty(job_id=job_id)
        with self._connection_scope() as connection:
            rows = connection.execute(
                """
                SELECT * FROM job_events
                WHERE job_id = ?
                ORDER BY sequence ASC
                """,
                (job_id,),
            ).fetchall()
        for row in rows:
            _require_job_event_row_payload_matches(row)
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
                    storage_key, content_hash, retention_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    result_id,
                    job_id,
                    document_id,
                    category,
                    format,
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
        payload_json = _canonical_json(
            payload
            or {
                "action": action,
                "actor": actor,
                "scope_id": scope_id,
                "scope_type": scope_type,
            }
        )
        with self._connection_scope(immediate=True) as connection:
            now = _utc_now()
            self._require_job_document(connection, job_id, document_id)
            self._require_audit_scope(connection, scope_type, scope_id, job_id, document_id)
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
            return self._insert_and_get_from_connection(
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
    ) -> None:
        if scope_type in {"document", "source_document"}:
            row = connection.execute(
                "SELECT document_id FROM source_documents WHERE document_id = ?",
                (scope_id,),
            ).fetchone()
            expected = {"document_id": document_id}
        elif scope_type in {"job", "conversion_job"}:
            row = connection.execute(
                "SELECT job_id, document_id FROM jobs WHERE job_id = ?",
                (scope_id,),
            ).fetchone()
            expected = {"job_id": job_id, "document_id": document_id}
        elif scope_type == "job_event":
            row = connection.execute(
                """
                SELECT job_events.job_id, jobs.document_id
                FROM job_events
                JOIN jobs ON jobs.job_id = job_events.job_id
                WHERE job_events.event_id = ?
                """,
                (scope_id,),
            ).fetchone()
            expected = {"job_id": job_id, "document_id": document_id}
        elif scope_type == "conversion_result":
            row = connection.execute(
                "SELECT job_id, document_id FROM conversion_results WHERE result_id = ?",
                (scope_id,),
            ).fetchone()
            expected = {"job_id": job_id, "document_id": document_id}
        elif scope_type in {"artifact", "generated_artifact"}:
            row = connection.execute(
                "SELECT job_id, document_id FROM generated_artifacts WHERE artifact_id = ?",
                (scope_id,),
            ).fetchone()
            expected = {"job_id": job_id, "document_id": document_id}
        elif scope_type == "review_item":
            row = connection.execute(
                "SELECT job_id, document_id FROM review_items WHERE review_item_id = ?",
                (scope_id,),
            ).fetchone()
            expected = {"job_id": job_id, "document_id": document_id}
        elif scope_type == "review_decision":
            row = connection.execute(
                "SELECT job_id, document_id FROM review_decisions WHERE decision_id = ?",
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
            _require_audit_event_row_payload_matches(row)
            self._require_job_document(connection, row["job_id"], row["document_id"])
            self._require_audit_scope(
                connection,
                row["scope_type"],
                row["scope_id"],
                row["job_id"],
                row["document_id"],
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
) -> None:
    if payload is None:
        return
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
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
            if "id" in payload_value and payload_value["id"] != expected_value:
                raise ValueError("payload actor.id must match the job event row")
            continue
        if payload_value != expected_value:
            raise ValueError(f"payload {field_name} must match the job event row")
    if "actor_id" in payload and payload["actor_id"] != actor:
        raise ValueError("payload actor_id must match the job event row")
    for field_name in ("sequence", "created_at", "occurred_at"):
        if field_name in payload:
            raise ValueError(f"payload {field_name} is derived from the job event row")


def _require_job_event_row_payload_matches(row: sqlite3.Row) -> None:
    payload = _load_json_object(
        row["payload_json"],
        field_name="job event payload_json",
    )
    _require_job_event_payload_matches(
        payload,
        event_id=row["event_id"],
        job_id=row["job_id"],
        event_type=row["event_type"],
        actor=row["actor"],
    )


def _require_audit_event_row_payload_matches(row: sqlite3.Row) -> None:
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
    )


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
) -> None:
    if payload is None:
        return
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
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
            if "id" in payload_value and payload_value["id"] != expected_value:
                raise ValueError("payload actor.id must match the audit event row")
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


_AUDIT_EVENT_TYPE_CATEGORIES = {
    "conversion_job.action_requested": {
        "actions": frozenset({"download_result", "open_detail", "retry_conversion"}),
        "scope_types": frozenset({"conversion_job", "job", "job_event"}),
    },
    "conversion_review.action_requested": {
        "actions": frozenset({"approve", "edit"}),
        "scope_types": frozenset({"document", "review_decision", "review_item"}),
    },
    "desktop.job_operation": {
        "actions": frozenset({"desktop_result_download", "desktop_upload"}),
        "scope_types": frozenset({"conversion_job", "document", "job", "job_event"}),
    },
    "job.lifecycle": {
        "actions": frozenset(
            {
                "conversion_completed",
                "conversion_failed",
                "conversion_queued",
                "conversion_started",
                "retry_conversion",
            }
        ),
        "scope_types": frozenset({"conversion_job", "job", "job_event"}),
    },
}


_AUDIT_SCOPE_ID_ALIASES = {
    "document": frozenset({"document_id", "source_document_id"}),
    "source_document": frozenset({"document_id", "source_document_id"}),
    "job": frozenset({"job_id", "conversion_job_id"}),
    "conversion_job": frozenset({"job_id", "conversion_job_id"}),
    "job_event": frozenset({"job_event_id"}),
    "conversion_result": frozenset({"result_id", "conversion_result_id"}),
    "artifact": frozenset({"artifact_id", "generated_artifact_id"}),
    "generated_artifact": frozenset({"artifact_id", "generated_artifact_id"}),
    "review_item": frozenset({"review_item_id"}),
    "review_decision": frozenset({"review_decision_id"}),
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
    category = _AUDIT_EVENT_TYPE_CATEGORIES.get(event_type)
    if category is None:
        return False
    return action in category["actions"] and scope_type in category["scope_types"]


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

CREATE TABLE IF NOT EXISTS source_documents (
    document_id TEXT NOT NULL PRIMARY KEY CHECK(length(trim(document_id)) > 0),
    source_type TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    source_artifact_id TEXT NOT NULL UNIQUE CHECK(length(trim(source_artifact_id)) > 0),
    source_storage_key TEXT NOT NULL UNIQUE CHECK(length(trim(source_storage_key)) > 0),
    content_hash TEXT NOT NULL CHECK(
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    status TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT NOT NULL PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(
        typeof(attempts) = 'integer'
        AND attempts >= 0
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, document_id)
);

CREATE TABLE IF NOT EXISTS job_events (
    event_id TEXT NOT NULL PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK(
        typeof(sequence) = 'integer'
        AND sequence > 0
    ),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(
        CASE
            WHEN json_valid(payload_json) THEN json_type(payload_json) = 'object'
            ELSE 0
        END
    ),
    created_at TEXT NOT NULL,
    UNIQUE(job_id, sequence)
);

CREATE TABLE IF NOT EXISTS conversion_results (
    result_id TEXT NOT NULL PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    status TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK(
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(result_id, job_id, document_id),
    FOREIGN KEY(job_id, document_id) REFERENCES jobs(job_id, document_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS generated_artifacts (
    artifact_id TEXT NOT NULL PRIMARY KEY,
    result_id TEXT NOT NULL REFERENCES conversion_results(result_id) ON DELETE RESTRICT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    category TEXT NOT NULL,
    format TEXT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL CHECK(
        length(content_hash) = 64
        AND content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    retention_state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(artifact_id, job_id, document_id),
    FOREIGN KEY(result_id, job_id, document_id)
        REFERENCES conversion_results(result_id, job_id, document_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS review_items (
    review_item_id TEXT NOT NULL PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    target_path TEXT NOT NULL,
    status TEXT NOT NULL,
    severity TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(review_item_id, job_id, document_id),
    FOREIGN KEY(job_id, document_id) REFERENCES jobs(job_id, document_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS review_decisions (
    decision_id TEXT NOT NULL PRIMARY KEY,
    review_item_id TEXT NOT NULL REFERENCES review_items(review_item_id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL REFERENCES generated_artifacts(artifact_id) ON DELETE RESTRICT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    decision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(review_item_id, job_id, document_id)
        REFERENCES review_items(review_item_id, job_id, document_id) ON DELETE RESTRICT,
    FOREIGN KEY(artifact_id, job_id, document_id)
        REFERENCES generated_artifacts(artifact_id, job_id, document_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT NOT NULL PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL CHECK(
        typeof(sequence) = 'integer'
        AND sequence > 0
    ),
    integrity_algorithm TEXT NOT NULL CHECK(
        integrity_algorithm = 'sha256-canonical-json-chain-v1'
    ),
    actor TEXT NOT NULL CHECK(length(trim(actor)) > 0),
    action TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    event_hash TEXT NOT NULL CHECK(
        length(event_hash) = 64
        AND event_hash NOT GLOB '*[^0-9a-f]*'
    ),
    prev_event_hash TEXT CHECK(
        prev_event_hash IS NULL
        OR (
            length(prev_event_hash) = 64
            AND prev_event_hash NOT GLOB '*[^0-9a-f]*'
        )
    ),
    payload_json TEXT NOT NULL CHECK(
        CASE
            WHEN json_valid(payload_json) THEN json_type(payload_json) = 'object'
            ELSE 0
        END
    ),
    created_at TEXT NOT NULL,
    UNIQUE(sequence),
    FOREIGN KEY(job_id, document_id) REFERENCES jobs(job_id, document_id) ON DELETE RESTRICT
);

CREATE TRIGGER IF NOT EXISTS jobs_parent_reference_insert
BEFORE INSERT ON jobs
WHEN NOT EXISTS (
    SELECT 1 FROM source_documents
    WHERE source_documents.document_id = NEW.document_id
)
BEGIN
    SELECT RAISE(ABORT, 'record must reference matching parent rows');
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

CREATE TRIGGER IF NOT EXISTS generated_artifacts_audited_decision_no_delete
BEFORE DELETE ON generated_artifacts
WHEN EXISTS (
    SELECT 1
    FROM audit_events
    JOIN review_decisions
      ON review_decisions.decision_id = audit_events.scope_id
    WHERE audit_events.scope_type = 'review_decision'
      AND review_decisions.artifact_id = OLD.artifact_id
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

CREATE TRIGGER IF NOT EXISTS review_items_audited_decision_no_delete
BEFORE DELETE ON review_items
WHEN EXISTS (
    SELECT 1
    FROM audit_events
    JOIN review_decisions
      ON review_decisions.decision_id = audit_events.scope_id
    WHERE audit_events.scope_type = 'review_decision'
      AND review_decisions.review_item_id = OLD.review_item_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS review_decisions_audit_scope_no_delete
BEFORE DELETE ON review_decisions
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE audit_events.scope_type = 'review_decision'
      AND audit_events.scope_id = OLD.decision_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be deleted');
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
BEFORE UPDATE ON jobs
WHEN EXISTS (
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

CREATE TRIGGER IF NOT EXISTS generated_artifacts_audited_decision_no_update
BEFORE UPDATE ON generated_artifacts
WHEN EXISTS (
    SELECT 1
    FROM audit_events
    JOIN review_decisions
      ON review_decisions.decision_id = audit_events.scope_id
    WHERE audit_events.scope_type = 'review_decision'
      AND review_decisions.artifact_id = OLD.artifact_id
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

CREATE TRIGGER IF NOT EXISTS review_items_audited_decision_no_update
BEFORE UPDATE ON review_items
WHEN EXISTS (
    SELECT 1
    FROM audit_events
    JOIN review_decisions
      ON review_decisions.decision_id = audit_events.scope_id
    WHERE audit_events.scope_type = 'review_decision'
      AND review_decisions.review_item_id = OLD.review_item_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS review_decisions_audit_scope_no_update
BEFORE UPDATE ON review_decisions
WHEN EXISTS (
    SELECT 1 FROM audit_events
    WHERE audit_events.scope_type = 'review_decision'
      AND audit_events.scope_id = OLD.decision_id
)
BEGIN
    SELECT RAISE(ABORT, 'audit scope rows cannot be updated');
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
    SELECT 1 FROM source_documents
    WHERE source_documents.source_artifact_id = NEW.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'generated artifact id must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_artifact_id_global_update
BEFORE UPDATE OF artifact_id ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM source_documents
    WHERE source_documents.source_artifact_id = NEW.artifact_id
)
BEGIN
    SELECT RAISE(ABORT, 'generated artifact id must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_storage_key_global_insert
BEFORE INSERT ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM source_documents
    WHERE source_documents.source_storage_key = NEW.storage_key
)
BEGIN
    SELECT RAISE(ABORT, 'generated storage key must be globally unique');
END;

CREATE TRIGGER IF NOT EXISTS generated_artifacts_storage_key_global_update
BEFORE UPDATE OF storage_key ON generated_artifacts
WHEN EXISTS (
    SELECT 1 FROM source_documents
    WHERE source_documents.source_storage_key = NEW.storage_key
)
BEGIN
    SELECT RAISE(ABORT, 'generated storage key must be globally unique');
END;
"""

_RESET_SQL = """
DROP TABLE IF EXISTS audit_events;
DROP TABLE IF EXISTS review_decisions;
DROP TABLE IF EXISTS review_items;
DROP TABLE IF EXISTS generated_artifacts;
DROP TABLE IF EXISTS conversion_results;
DROP TABLE IF EXISTS job_events;
DROP TABLE IF EXISTS jobs;
DROP TABLE IF EXISTS source_documents;
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
    "default_database_path",
    "initialize_database",
    "reset_database",
]


if __name__ == "__main__":
    raise SystemExit(main())
