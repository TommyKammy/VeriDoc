from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "change-management-reevaluation.md"


class ChangeManagementReevaluationDocsTest(unittest.TestCase):
    def test_docs_define_gmp06_change_management_and_reevaluation_flow(self) -> None:
        self.assertTrue(
            DOC_PATH.is_file(),
            msg=f"missing change-management re-evaluation docs: {DOC_PATH.relative_to(REPO_ROOT)}",
        )

        docs = DOC_PATH.read_text(encoding="utf-8")
        docs_flat = " ".join(docs.split())

        for required_heading in (
            "# Change Management And Re-Evaluation Flow",
            "## Target Changes",
            "## Required Re-Evaluation Gates",
            "## Approval And Audit Records",
            "## Rollback And Difference Explanation",
            "## PR Checklist",
        ):
            self.assertIn(required_heading, docs)

        for required_text in (
            "model",
            "prompt",
            "logic",
            "template",
            "renderer",
            "evaluation gate data",
            "datasets/gold/high_risk_labels_v0.json",
            "datasets/gold/evaluation_cases_v0.json",
            "datasets/gold/poc_mode_comparison_v1.json",
            "datasets/gold/llm_stability_runs_v0.json",
            "datasets/gold/template_regression_v0.json",
            "LLM stability run record",
            "fresh public synthetic outputs",
            "model, prompt, logic, template, or renderer changes",
            "stale comparison",
            "python3 scripts/evaluate_dataset.py --poc-comparison datasets/gold/poc_mode_comparison_v1.json",
            "python3 scripts/evaluate_dataset.py --llm-stability-runs datasets/gold/llm_stability_runs_v0.json",
            "fresh same-input N-run capture",
            "classification",
            "requires_review",
            "high_risk_false_auto_confirmed_count",
            "high_risk_false_auto_confirmed_target",
            "0",
            "python3 scripts/ci/repo_hygiene.py",
            "docs/local-inference-setup.md",
            "adr/ADR-001-local-llm-standard-model.md",
            "datasets/README.md",
            "docs/template-change-history.md",
            "approval",
            "audit",
            "rollback",
            "difference explanation",
        ):
            self.assertIn(required_text, docs)

        for required_text in (
            "two ordered evidence stages",
            "first capture current-branch public synthetic outputs",
            "before running the comparison harness",
            "fresh capture source",
        ):
            self.assertIn(required_text, docs_flat)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, docs)


if __name__ == "__main__":
    unittest.main()
