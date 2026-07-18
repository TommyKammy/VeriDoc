from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.api.persistence_contracts import (
    _AUDIT_ACTION_CONTRACT_BY_ALIAS,
    _AUDIT_ACTION_CONTRACTS,
    _AUDIT_SCOPE_PAYLOAD_BINDINGS,
    _AuditActionContract,
    _require_audit_action_scope,
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
from services.api.persistence_repository import (
    SQLitePersistenceRepository,
    _audit_event_hash,
    _canonical_json,
    _utc_now,
    default_database_path,
    initialize_database,
    reset_database,
)
from services.api.persistence_schema import (
    _expected_schema_definitions,
    _normalize_schema_sql,
    _RESET_SQL,
    _SCHEMA_SQL,
    _schema_definitions,
    _validate_managed_schema,
)
from services.api.review_decision_workflow import (
    AuthoritativeReviewDecision,
    record_authoritative_review_decision,
)


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


__all__ = [
    "Artifact",
    "AuthoritativeReviewDecision",
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
    "record_authoritative_review_decision",
    "reset_database",
]


if __name__ == "__main__":
    raise SystemExit(main())
