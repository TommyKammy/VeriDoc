from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "mvp-operations-runbook.md"


class MvpOperationsRunbookDocsTest(unittest.TestCase):
    def test_runbook_covers_the_mvp_operating_lifecycle(self) -> None:
        self.assertTrue(
            DOC_PATH.is_file(),
            msg=f"missing MVP operations runbook: {DOC_PATH.relative_to(REPO_ROOT)}",
        )

        docs = DOC_PATH.read_text(encoding="utf-8")
        docs_flat = " ".join(docs.split())
        docs_flat_lower = docs_flat.lower()

        for required_heading in (
            "# MVP Operations Runbook",
            "## Scope And Safety Boundary",
            "## Start",
            "## Stop",
            "## Backup",
            "## Restore",
            "## Evaluation",
            "## Troubleshooting",
            "## Data Deletion",
        ):
            self.assertIn(required_heading, docs)

        for required_text in (
            "VERIDOC_DB_PATH",
            "VERIDOC_ARTIFACT_STORE_ROOT",
            "var/veridoc/dev.sqlite3",
            "var/veridoc/artifacts",
            "python3 services/api/poc_web.py --check",
            "python3 services/api/poc_web.py",
            "Ctrl-C",
            "python3 scripts/evaluate_dataset.py --mvp-harness",
            "python3 scripts/evaluate_dataset.py --poc-acceptance-report",
            "overall_status",
            "python3 scripts/ci/repo_hygiene.py",
            "SQLite",
            "audit",
            "artifacts",
            "snapshot-consistent",
            "reset-db",
            "retention",
        ):
            self.assertIn(required_text, docs)

        for required_text in (
            "stop the API before copying or restoring",
            "database and artifact store as one state set",
            "do not treat a partial copy as a valid backup",
            "review events accepted by `/api/review-events` remain process-local",
            "template registrations created or updated through `post /api/templates` are also process-local",
            "database contains an invalid artifact reference",
            "referenced artifact failed verification",
            "backup target must be outside the artifact store",
            "target or parent is a symlink",
            "do not run reset-db by itself as a full deletion procedure",
            "verify that no database, WAL, SHM, or artifact data remains",
        ):
            self.assertIn(required_text.lower(), docs_flat_lower)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, docs)

        self.assertNotIn("SQLite metadata, job, review, and audit records", docs)
        stop_section = docs.split("## Stop", 1)[1].split("## Backup", 1)[0]
        self.assertLess(
            stop_section.index("except HTTPError:"),
            stop_section.index("except URLError as error:"),
        )
        self.assertIn("ConnectionRefusedError", stop_section)
        backup_section = docs.split("## Backup", 1)[1].split("## Restore", 1)[0]
        self.assertLess(
            backup_section.index("source_artifacts.is_symlink()"),
            backup_section.index("backup.mkdir"),
        )
        self.assertLess(
            backup_section.index("backup.resolve().relative_to(source_artifacts.resolve())"),
            backup_section.index("backup.mkdir"),
        )
        self.assertLess(
            backup_section.index("referenced_artifacts ="),
            backup_section.index("shutil.copytree"),
        )
        self.assertLess(
            backup_section.index("artifact.parent.is_symlink()"),
            backup_section.index("content = artifact.read_bytes()"),
        )
        self.assertLess(
            backup_section.index("shutil.copytree(artifact_backup, validation_artifacts)"),
            backup_section.index("JobQueue(database_path=validation_db"),
        )
        self.assertIn("artifact_store_root=validation_artifacts", backup_section)
        self.assertLess(
            backup_section.index("JobQueue(database_path=validation_db"),
            backup_section.index('(backup / "SHA256SUMS").write_text'),
        )
        self.assertLess(
            backup_section.index("JobAuditEventStore(database_path=validation_db)"),
            backup_section.index('(backup / "SHA256SUMS").write_text'),
        )
        restore_section = docs.split("## Restore", 1)[1].split("## Evaluation", 1)[0]
        self.assertIn("if set(manifest_entries) != expected_files:", restore_section)
        self.assertIn("backup manifest does not cover the complete state set", restore_section)
        self.assertIn("${VERIDOC_DB_PATH}-wal", restore_section)
        self.assertIn("${VERIDOC_DB_PATH}-shm", restore_section)
        self.assertIn("confirm no old sidecar remains", restore_section)
        evaluation_section = docs.split("## Evaluation", 1)[1].split(
            "## Troubleshooting", 1
        )[0]
        self.assertIn("acceptance_handoff.overall_status: pass", evaluation_section)
        deletion_section = docs.split("## Data Deletion", 1)[1]
        self.assertLess(
            deletion_section.index("path.is_symlink()"),
            deletion_section.index("return candidate.resolve()"),
        )


if __name__ == "__main__":
    unittest.main()
