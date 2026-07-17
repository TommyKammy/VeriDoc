from __future__ import annotations

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "mvp-acceptance-traceability.md"
GAP_REGISTER_PATH = REPO_ROOT / "docs" / "mvp-acceptance-gap-register.md"

EXPECTED_ITEM_IDS = (
    "AC-UI",
    "AC-TEMPLATE",
    "AC-QUALITY",
    "AC-PROVENANCE",
    "AC-REVIEW",
    "AC-EFFICIENCY",
    "AC-PERFORMANCE",
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

        for item_id in EXPECTED_ITEM_IDS:
            rows = re.findall(rf"^\| {re.escape(item_id)} \|", docs, flags=re.MULTILINE)
            self.assertEqual(1, len(rows), msg=f"expected one traceability row for {item_id}")

        for required_text in (
            "15.3_MVP受入基準",
            "#275",
            "#289",
            "as its run input",
            "predates and does not contain the gap register",
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

    def test_gap_register_matches_report_scope_and_records_current_failures(self) -> None:
        self.assertTrue(
            GAP_REGISTER_PATH.is_file(),
            msg=f"missing MVP acceptance gap register: "
            f"{GAP_REGISTER_PATH.relative_to(REPO_ROOT)}",
        )

        register = GAP_REGISTER_PATH.read_text(encoding="utf-8")
        register_ids = re.findall(
            r"^\| ((?:AC|FC|EM|OD)-[A-Z0-9-]+) \|",
            register,
            flags=re.MULTILINE,
        )
        self.assertEqual(list(EXPECTED_ITEM_IDS), register_ids)

        for required_text in (
            "9981ffb9f3e633faedf5bc5c2bd3d5a4845424b7",
            "datasets/mvp_evaluation_manifest_v1.json",
            "python3 -m pip install -r requirements-pdf-eval.txt",
            "Without the prerequisite, the PDF",
            "implementation_gap",
            "e2e_gap",
            "human_evidence_gap",
            "decision_gap",
            "mvp-word-001",
            "mvp-excel-001",
            "mvp-text-pdf-001",
            "mvp-scanned-pdf-001",
            "mvp-record-pdf-001",
            "no authoritative reviewer decision was recorded",
            "P12G-02",
            "P12G-13",
        ):
            self.assertIn(required_text, register)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, register)


if __name__ == "__main__":
    unittest.main()
