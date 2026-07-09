from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ADR_PATH = REPO_ROOT / "adr" / "ADR-005-phase11-persistence-strategy.md"


def test_phase11_persistence_strategy_adr_records_mvp_storage_decision() -> None:
    assert ADR_PATH.is_file(), (
        "missing Phase11 persistence ADR: "
        f"{ADR_PATH.relative_to(REPO_ROOT)}"
    )

    adr = ADR_PATH.read_text(encoding="utf-8")
    adr_flat = " ".join(adr.split())

    for required_heading in (
        "# ADR-005: Phase11 Persistence Strategy",
        "## Context",
        "## Candidate Comparison",
        "## Decision",
        "## Local Development And Test Operation",
        "## Entity List And Responsibility Boundary",
        "## Migration And Backup Posture",
        "## Follow-Up Implementation Boundary",
        "## Verification",
    ):
        assert required_heading in adr

    for required_text in (
        "SQLite",
        "PostgreSQL",
        "file store",
        "Selected: SQLite-backed metadata store plus artifact file store",
        "MVP default",
        "PostgreSQL-compatible repository boundary",
        "VERIDOC_DB_PATH",
        "VERIDOC_ARTIFACT_STORE_ROOT",
        "var/veridoc/dev.sqlite3",
        "var/veridoc/artifacts",
        "python3 -m services.api.persistence init-db",
        "jobs",
        "job_events",
        "source_documents",
        "generated_artifacts",
        "review_decisions",
        "audit_events",
        "schema_migrations",
        "services/api/persistence.py",
        "services/api/artifact_store.py",
        "services/api/poc_web.py",
        "TemporaryFileStore",
        "transaction open across network hops",
        "single committed snapshot",
        "all-or-nothing",
        "P11-02",
    ):
        assert required_text in adr

    for required_text in (
        "File store remains responsible for binary artifact bytes",
        "SQLite remains responsible for authoritative metadata",
        "PostgreSQL is not the Phase11 MVP default",
    ):
        assert required_text in adr_flat

    for forbidden_fragment in ("/" + "Users" + "/", "C:" + "\\Users" + "\\"):
        assert forbidden_fragment not in adr
