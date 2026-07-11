from __future__ import annotations

import sqlite3


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
    JOIN conversion_results
      ON conversion_results.result_id = generated_artifacts.result_id
     AND conversion_results.job_id = generated_artifacts.job_id
     AND conversion_results.document_id = generated_artifacts.document_id
    WHERE audit_events.event_id = NEW.event_id
      AND audit_events.action IN ('desktop_result_download', 'download_result')
      AND audit_events.job_id = generated_artifacts.job_id
      AND audit_events.document_id = generated_artifacts.document_id
      AND conversion_results.status IN (
          'blocked', 'completed', 'converted', 'requires_review', 'succeeded', 'success'
      )
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
      AND (
          json_extract(audit_events.payload_json, '$.download_filename') IS NULL
          OR json_extract(audit_events.payload_json, '$.download_filename')
             = generated_artifacts.display_filename
      )
      AND (
          json_extract(audit_events.payload_json, '$.output_sha256') IS NULL
          OR json_extract(audit_events.payload_json, '$.output_sha256')
             = generated_artifacts.content_hash
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
    JOIN conversion_results
      ON conversion_results.result_id = generated_artifacts.result_id
     AND conversion_results.job_id = generated_artifacts.job_id
     AND conversion_results.document_id = generated_artifacts.document_id
    WHERE job_events.event_id = NEW.event_id
      AND job_events.job_id = generated_artifacts.job_id
      AND conversion_results.status IN (
          'blocked', 'completed', 'converted', 'requires_review', 'succeeded', 'success'
      )
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
BEFORE UPDATE OF
    document_id,
    source_type,
    original_filename,
    source_artifact_id,
    source_storage_key,
    content_hash,
    uploaded_by,
    created_at
ON source_documents
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
DROP TABLE IF EXISTS job_queue_records;
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
