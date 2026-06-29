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
            "no legal or regulatory compliance conclusion",
            "fail closed",
            "GMP-03",
            "witness or QA approval",
            "ROLE_PERMISSIONS",
            "_validate_review_event",
            "_validate_review_workflow_event",
            "`conversion_id` as an optional review-audit scope field",
            "admin approval events can be accepted by the local PoC API",
            "must not claim conversion-version binding as universal",
        ):
            self.assertIn(required_text, docs)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, docs)


if __name__ == "__main__":
    unittest.main()
