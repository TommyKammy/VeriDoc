from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "mvp-acceptance-traceability.md"


class MvpAcceptanceTraceabilityDocsTest(unittest.TestCase):
    def test_all_15_3_items_have_stable_traceability_rows(self) -> None:
        self.assertTrue(
            DOC_PATH.is_file(),
            msg=f"missing MVP acceptance traceability: {DOC_PATH.relative_to(REPO_ROOT)}",
        )

        docs = DOC_PATH.read_text(encoding="utf-8")

        for required_heading in (
            "# MVP Acceptance Traceability",
            "## Status Rules",
            "## Acceptance Criteria",
            "## Failure Conditions",
            "## Evaluation Methods",
            "## Open Decisions",
            "## Stable MVP Gate",
        ):
            self.assertIn(required_heading, docs)

        expected_ids = (
            "AC-UI",
            "AC-TEMPLATE",
            "AC-QUALITY",
            "AC-PROVENANCE",
            "AC-REVIEW",
            "AC-EFFICIENCY",
            "AC-AUDIT",
            "AC-AUTH",
            "AC-SECURITY",
            "FC-HIGH-RISK",
            "FC-EVIDENCE",
            "FC-EXTERNAL-SEND",
            "FC-REVIEW-UI",
            "FC-REPRODUCIBILITY",
            "EM-USER-REVIEW",
            "EM-E2E",
            "OD-TEMPLATES",
            "OD-EFFICIENCY-SCOPE",
            "OD-SEGREGATION",
        )
        for item_id in expected_ids:
            rows = re.findall(rf"^\| {re.escape(item_id)} \|", docs, flags=re.MULTILINE)
            self.assertEqual(1, len(rows), msg=f"expected one traceability row for {item_id}")

        for required_text in (
            "15.3_MVP受入基準",
            "#275",
            "#289",
            "tests/test_poc_web_api.py",
            "docs/mvp-transition-decision.md",
            "python3 -m unittest tests.test_mvp_acceptance_traceability",
            "python3 scripts/ci/repo_hygiene.py",
            "未達",
            "一部達成",
            "Phase13以降",
        ):
            self.assertIn(required_text, docs)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, docs)


if __name__ == "__main__":
    unittest.main()
