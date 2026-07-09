from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar


SCHEMA_VERSION = "20260709_01_minimal_phase11_schema"
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
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_database_path()
        self._connection = _connection

    def initialize(self) -> None:
        _validate_database_path(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA_SQL)
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(migration_id, applied_at)
                VALUES (?, ?)
                """,
                (SCHEMA_VERSION, _utc_now()),
            )

    def reset(self) -> None:
        _validate_database_path(self.db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_RESET_SQL)
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

    def get_conversion_job(self, job_id: str) -> ConversionJob | None:
        return self._get_one(ConversionJob, "SELECT * FROM jobs WHERE job_id = ?", (job_id,))

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
        payload_json = _canonical_json(payload or {"event_type": event_type})
        now = _utc_now()
        return self._insert_and_get(
            JobEvent,
            """
            INSERT INTO job_events(event_id, job_id, event_type, actor, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, job_id, event_type, actor, payload_json, now),
            "SELECT * FROM job_events WHERE event_id = ?",
            (event_id,),
        )

    def get_job_event(self, event_id: str) -> JobEvent | None:
        return self._get_one(JobEvent, "SELECT * FROM job_events WHERE event_id = ?", (event_id,))

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
            self._require_review_item_matches_artifact(connection, review_item_id, artifact_id)
            return self._insert_and_get_from_connection(
                connection,
                ReviewDecision,
                """
                INSERT INTO review_decisions(
                    decision_id, review_item_id, artifact_id, actor, role, decision,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (decision_id, review_item_id, artifact_id, actor, role, decision, now, now),
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
        sequence: int,
        integrity_algorithm: str,
        actor: str,
        action: str,
        scope_type: str,
        scope_id: str,
        event_hash: str,
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
        _require_sha256(event_hash, field_name="event_hash")
        if prev_event_hash is not None:
            _require_sha256(prev_event_hash, field_name="prev_event_hash")
        if sequence < 0:
            raise ValueError("sequence must not be negative")
        payload_json = _canonical_json(
            payload
            or {
                "action": action,
                "actor": actor,
                "scope_id": scope_id,
                "scope_type": scope_type,
            }
        )
        now = _utc_now()
        with self._connection_scope() as connection:
            self._require_job_document(connection, job_id, document_id)
            self._require_audit_scope(connection, scope_type, scope_id, job_id, document_id)
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
        return self._get_one(
            AuditEvent,
            "SELECT * FROM audit_events WHERE event_id = ?",
            (event_id,),
        )

    @contextmanager
    def transaction(self) -> Iterator["SQLitePersistenceRepository"]:
        if self._connection is not None:
            yield self
            return

        with self._connect() as connection:
            connection.execute("BEGIN")
            yield SQLitePersistenceRepository(self.db_path, _connection=connection)

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

    @contextmanager
    def _connection_scope(self) -> Iterator[sqlite3.Connection]:
        if self._connection is not None:
            yield self._connection
            return

        with self._connect() as connection:
            yield connection

    def _connect(self) -> sqlite3.Connection:
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

    def _require_review_item_matches_artifact(
        self,
        connection: sqlite3.Connection,
        review_item_id: str,
        artifact_id: str,
    ) -> None:
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
                """
                SELECT review_items.job_id, review_items.document_id
                FROM review_decisions
                JOIN review_items ON review_items.review_item_id = review_decisions.review_item_id
                WHERE review_decisions.decision_id = ?
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
    return record_type(**{key: row[key] for key in row.keys()})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_non_empty(**values: str) -> None:
    for field_name, value in values.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} is required")


def _require_sha256(value: str, *, field_name: str) -> None:
    if not isinstance(value, str) or SHA256_HEX.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a sha256 hex digest")


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _validate_database_path(db_path: Path) -> None:
    if str(db_path).strip() == "":
        raise ValueError("database path is required")
    if db_path.exists() and db_path.is_dir():
        raise ValueError("database path must be a file")


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
    migration_id TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_documents (
    document_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    source_artifact_id TEXT NOT NULL UNIQUE,
    source_storage_key TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    idempotency_key TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, document_id)
);

CREATE TABLE IF NOT EXISTS job_events (
    event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversion_results (
    result_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    status TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(result_id, job_id, document_id),
    FOREIGN KEY(job_id, document_id) REFERENCES jobs(job_id, document_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS generated_artifacts (
    artifact_id TEXT PRIMARY KEY,
    result_id TEXT NOT NULL REFERENCES conversion_results(result_id) ON DELETE RESTRICT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    category TEXT NOT NULL,
    format TEXT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    retention_state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(artifact_id, job_id, document_id),
    FOREIGN KEY(result_id, job_id, document_id)
        REFERENCES conversion_results(result_id, job_id, document_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS review_items (
    review_item_id TEXT PRIMARY KEY,
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
    decision_id TEXT PRIMARY KEY,
    review_item_id TEXT NOT NULL REFERENCES review_items(review_item_id) ON DELETE RESTRICT,
    artifact_id TEXT NOT NULL REFERENCES generated_artifacts(artifact_id) ON DELETE RESTRICT,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    decision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE RESTRICT,
    document_id TEXT NOT NULL REFERENCES source_documents(document_id) ON DELETE RESTRICT,
    sequence INTEGER NOT NULL,
    integrity_algorithm TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    prev_event_hash TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, sequence),
    FOREIGN KEY(job_id, document_id) REFERENCES jobs(job_id, document_id) ON DELETE RESTRICT
);
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
