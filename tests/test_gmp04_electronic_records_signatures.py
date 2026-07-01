from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "gmp04-electronic-records-signatures.md"


class Gmp04ElectronicRecordsSignaturesTest(unittest.TestCase):
    def test_gmp04_design_note_documents_scope_boundaries_and_open_items(self) -> None:
        self.assertTrue(
            DOC_PATH.is_file(),
            msg=f"missing GMP-04 design note: {DOC_PATH.relative_to(REPO_ROOT)}",
        )

        docs = DOC_PATH.read_text(encoding="utf-8")

        for required_heading in (
            "# GMP-04 Electronic Records and Electronic Signatures Design Note",
            "## VeriDoc Responsibility Boundary",
            "## Non-Responsibility Boundary",
            "## Relationship to GMP-03",
            "## Audit Log and Electronic Record Posture",
            "## External Signature Integration Option",
            "## QA and GMP SME Acceptance Questions",
            "## Open Items",
        ):
            self.assertIn(required_heading, docs)

        for required_text in (
            "VeriDoc is not the formal electronic signature system of record",
            "external validated signature service",
            "segregation of duties",
            "audit-ready events",
            "explicit job-event submissions",
            "when local auth is enabled, direct result downloads are",
            "protected by job-read authorization",
            "default unauthenticated PoC",
            "permits downloads without making them authenticated GMP evidence",
            "Phase5",
            "desktop result save operations add a separate",
            "`desktop_result_download`",
            "job-action audit event when the desktop client records",
            "completed local",
            "no legal or regulatory compliance conclusion",
            "fail closed",
            "GMP-03",
            "self-contained",
            "and traceable to the current implementation anchors",
            "standalone GMP-03 design artifact",
            "The `reviewer` role has `review_events:edit` but not",
            "`review_events:approve`; reviewer-only approval is rejected",
            "witness or QA approval",
            "ROLE_PERMISSIONS",
            "_validate_review_event",
            "_validate_review_workflow_event",
            "tests/test_poc_web_api.py",
            "when local auth is enabled",
            "default unauthenticated PoC review and job-event submissions",
            "store null",
            "actor and role fields and must not be treated as",
            "authenticated GMP evidence",
            "unauthenticated template mutations use the caller payload",
            "instead of storing",
            "null actor and role fields",
            "when local auth is enabled, missing or invalid actor and role",
            "authentication also fails closed",
            "review and job-event submissions record null",
            "`conversion_id` as an optional review-audit scope field",
            "unchanged approvals do not require an",
            "existing edit for the same conversion",
            "when comparable prior-edit evidence exists",
            "the endpoint rejects approval",
            "text that differs from the latest saved revised text",
            "latest saved revised text",
            "Standalone approvals",
            "without a saved edit can be accepted",
            "can be accepted from caller-supplied original and",
            "missing `revised_text` defaults to",
            "`original_text`",
            "does not compare against the converted document's",
            "independent prior reviewed text",
            "reject approval attempts without authenticated actor identity",
            "before workflow",
            "validation, while no-auth edit capture remains",
            "no-auth edit capture remains",
            "authenticated actor IDs exist",
            "Same-actor rejection",
            "applies",
            "only to enforced paths",
            "comparable prior-review evidence",
            "admin approval events can be accepted by the local PoC API",
            "must not claim conversion-version binding as universal",
            "caller-supplied source context fields",
            "syntactic validation",
            "without treating direct review-event submissions as",
            "verified lookup-backed links",
        ):
            self.assertIn(required_text, docs)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, docs)
        self.assertNotIn("GMP-03 established the role", docs)
        self.assertNotIn("job actions, and related operator decisions", docs)
        self.assertNotIn("document, block, actor, and latest edited text", docs)
        self.assertNotIn(
            "failing closed when required provenance, actor, role, or target binding",
            docs,
        )
        self.assertNotIn(
            "when auth context exists, the approver must be a different actor",
            docs,
        )
        self.assertNotIn("source context directly linked to the reviewed record", docs)
        self.assertNotIn(
            "direct result downloads are protected by job-read authorization but",
            docs,
        )
        self.assertNotIn(
            "default unauthenticated PoC mode stores null actor and role fields",
            docs,
        )
        self.assertNotIn("approval text must match the current reviewed text", docs)
        self.assertNotIn("approval text must match", docs)


if __name__ == "__main__":
    unittest.main()
