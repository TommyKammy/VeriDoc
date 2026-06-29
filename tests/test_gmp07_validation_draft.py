from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "gmp07-validation-draft.md"


class Gmp07ValidationDraftDocsTest(unittest.TestCase):
    def test_docs_define_validation_draft_and_traceability(self) -> None:
        self.assertTrue(
            DOC_PATH.is_file(),
            msg=f"missing GMP-07 validation draft: {DOC_PATH.relative_to(REPO_ROOT)}",
        )

        docs = DOC_PATH.read_text(encoding="utf-8")
        docs_flat = " ".join(docs.split())

        for required_heading in (
            "# GMP-07 Validation Draft",
            "## Scope And CSV Posture",
            "## User Requirements Specification Draft",
            "## Risk Assessment Draft",
            "## Traceability Matrix",
            "## IQ/OQ/PQ-Equivalent Verification Draft",
            "## Open Items And QA Confirmation",
        ):
            self.assertIn(required_heading, docs)

        for required_text in (
            "URS",
            "risk assessment",
            "traceability matrix",
            "IQ",
            "OQ",
            "PQ",
            "15.7_GMP対応受入基準",
            "GMP-01",
            "GMP-02",
            "GMP-03",
            "GMP-04",
            "GMP-05",
            "GMP-06",
            "GMP-07",
            "GMP-08",
            "high-risk items are never auto-confirmed",
            "high-risk auto-confirmed miss count is 0",
            "QA confirmation required",
            "formal CSV owner and scope are not yet approved",
            "python3 scripts/ci/repo_hygiene.py",
        ):
            self.assertIn(required_text, docs)

        for required_text in (
            "draft only and is not an approved formal CSV package",
            "production validation remains QA-led after scope approval",
            "do not infer GMP fitness from this draft alone",
        ):
            self.assertIn(required_text, docs_flat)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, docs)


if __name__ == "__main__":
    unittest.main()
