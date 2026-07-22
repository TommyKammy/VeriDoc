from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = REPO_ROOT / "docs" / "mvp-acceptance-traceability.md"
GAP_REGISTER_PATH = REPO_ROOT / "docs" / "mvp-acceptance-gap-register.md"
SCOPE_DECISIONS_PATH = REPO_ROOT / "docs" / "mvp-scope-decisions.md"
MANIFEST_PATH = REPO_ROOT / "datasets" / "mvp_evaluation_manifest_v1.json"
REPORT_SAMPLE_PATH = REPO_ROOT / "reports" / "mvp-acceptance-report.md"

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


def _git_blob_id(path: Path) -> str:
    content = path.read_bytes()
    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content).hexdigest()


def _required_record_value(record: str, label: str) -> str:
    match = re.search(
        rf"^- {re.escape(label)}: `([^`]+)`$",
        record,
        flags=re.MULTILINE,
    )
    if match is None:
        raise AssertionError(f"missing decision record field: {label}")
    return match.group(1)


def _scope_section_sha256(record: str, item_id: str) -> str:
    match = re.search(
        rf"^## {re.escape(item_id)}\n.*?(?=^## |\Z)",
        record,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"missing decision record section: {item_id}")
    canonical = "\n".join(
        line.rstrip() for line in match.group(0).splitlines()
    ).strip() + "\n"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
            "reproducible report revision",
            "reachable commit",
            "later revision cannot silently substitute",
            "product/harness baseline",
            "not the report checkout target",
            "tests/test_poc_web_api.py",
            "docs/mvp-scope-decisions.md",
            "docs/mvp-transition-decision.md",
            "python3 -m pip install -r requirements-pdf-eval.txt",
            "python3 -m unittest tests.test_mvp_acceptance_traceability",
            "python3 scripts/ci/repo_hygiene.py",
            "one `fail`, zero `unknown`, and four `pass`",
            "未達",
            "一部達成",
            "Phase13以降",
        ):
            self.assertIn(required_text, docs)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, docs)

        self.assertNotIn("8e9846828570cf89a062df3b4eb276e5ecc31647", docs)

    def test_scope_decisions_record_authoritative_approval_and_invalidation(self) -> None:
        self.assertTrue(
            SCOPE_DECISIONS_PATH.is_file(),
            msg=f"missing MVP scope decisions: "
            f"{SCOPE_DECISIONS_PATH.relative_to(REPO_ROOT)}",
        )

        record = SCOPE_DECISIONS_PATH.read_text(encoding="utf-8")
        normalized_record = " ".join(record.split())
        self.assertEqual(
            3,
            len(re.findall(r"^## OD-[A-Z-]+$", record, flags=re.MULTILINE)),
        )

        for required_text in (
            "Record schema: `veridoc-mvp-scope-decisions/v1`",
            "Decision revision: `p12g-02-v1`",
            "584ef2db12a6676abb65f75de1ec38145e06b487",
            "Target manifest revision: `phase12-mvp-v1`",
            "Target manifest Git blob: `13450762d323198b1b6e87315be173c784fc4880`",
            "Approved manifest contract SHA-256",
            "Approved OD-EFFICIENCY-SCOPE contract SHA-256",
            "Approved ROLE_PERMISSIONS contract SHA-256",
            "Decision owner: `TommyKammy`",
            "Approved by: `TommyKammy`",
            "Approval date: `2026-07-22`",
            "Approval status: `approved`",
            "at least three designated document reviewers",
            "paired cohort median",
            "reduced by at least 30%",
            "no high-risk miss",
            "not shown to a participant until that timed task is complete",
            "retaining direct participant identity",
            "`ROLE_PERMISSIONS`",
            "distinct authenticated actor",
            "preceding review/edit event",
            "permits unauthenticated non-approval operations",
            "currently accepts approval with no prior review/edit event",
            "not evidence that `AC-AUTH` is complete",
            "production IdP/SSO integration",
            "renewed approval",
            "did not supply or infer the approval",
        ):
            self.assertIn(required_text, normalized_record)

        for case_id in (
            "mvp-word-001",
            "mvp-excel-001",
            "mvp-text-pdf-001",
            "mvp-scanned-pdf-001",
            "mvp-record-pdf-001",
        ):
            self.assertIn(case_id, record)

        for role in (
            "viewer",
            "operator",
            "reviewer",
            "approver",
            "admin",
            "audit_viewer",
        ):
            self.assertRegex(record, rf"`{role}`")

        self.assertNotIn("pending authoritative approval", normalized_record)
        for fragment in ("/" + "Users" + "/", "C:" + "\\Users" + "\\"):
            self.assertNotIn(fragment, record)

        target_commit = _required_record_value(record, "Target product commit")
        target_manifest = _required_record_value(record, "Target manifest")
        target_blob = _required_record_value(record, "Target manifest Git blob")
        resolved_commit = subprocess.run(
            ["git", "rev-parse", "--verify", f"{target_commit}^{{commit}}"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(target_commit, resolved_commit)
        resolved_blob = subprocess.run(
            ["git", "rev-parse", f"{target_commit}:{target_manifest}"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(target_blob, resolved_blob)

        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        contract_fields = (
            "schema_version",
            "selection_status",
            "selection_revision",
            "fixture_manifest",
            "source_policy",
            "confidential_source_documents_allowed",
            "required_categories",
            "cases",
        )
        manifest_contract = {field: manifest.get(field) for field in contract_fields}
        manifest_contract_sha256 = hashlib.sha256(
            json.dumps(
                manifest_contract,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            _required_record_value(record, "Approved manifest contract SHA-256"),
            manifest_contract_sha256,
        )
        self.assertEqual(
            _required_record_value(
                record,
                "Approved OD-EFFICIENCY-SCOPE contract SHA-256",
            ),
            _scope_section_sha256(record, "OD-EFFICIENCY-SCOPE"),
        )

        from services.api.poc_web import ROLE_PERMISSIONS

        role_contract = {
            role: sorted(permissions)
            for role, permissions in sorted(ROLE_PERMISSIONS.items())
        }
        role_contract_sha256 = hashlib.sha256(
            json.dumps(
                role_contract,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            _required_record_value(
                record,
                "Approved ROLE_PERMISSIONS contract SHA-256",
            ),
            role_contract_sha256,
        )

        traceability = DOC_PATH.read_text(encoding="utf-8")
        gap_register = GAP_REGISTER_PATH.read_text(encoding="utf-8")
        for item_id in ("OD-TEMPLATES", "OD-EFFICIENCY-SCOPE", "OD-SEGREGATION"):
            traceability_row = re.search(
                rf"^\| {re.escape(item_id)} \|.*$",
                traceability,
                flags=re.MULTILINE,
            )
            gap_register_row = re.search(
                rf"^\| {re.escape(item_id)} \|.*$",
                gap_register,
                flags=re.MULTILINE,
            )
            self.assertIsNotNone(traceability_row)
            self.assertIsNotNone(gap_register_row)
            self.assertIn("**達成**", traceability_row.group(0))
            self.assertIn("達成 / pass", gap_register_row.group(0))

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
            "git log -1 --format=%H -- docs/mvp-acceptance-gap-register.md",
            "Criteria source Git blob",
            "Evaluator Git blob",
            "git rev-parse HEAD:<repo-relative-path>",
            "disappear after squash merge",
            "9981ffb9f3e633faedf5bc5c2bd3d5a4845424b7",
            "git checkout --detach",
            "anchor, not a checkout instruction",
            "datasets/mvp_evaluation_manifest_v1.json",
            "docs/mvp-scope-decisions.md",
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
            "authoritative review decision is required",
            "one `fail`, zero `unknown`, and four `pass`",
            "P12G-02",
            "P12G-13",
        ):
            self.assertIn(required_text, register)

        self.assertNotIn("8e9846828570cf89a062df3b4eb276e5ecc31647", register)

        criteria_blob = re.search(
            r"Criteria source Git blob:\n  `([0-9a-f]{40})`",
            register,
        )
        evaluator_blob = re.search(
            r"Evaluator Git blob:\n  `([0-9a-f]{40})`",
            register,
        )
        self.assertIsNotNone(criteria_blob)
        self.assertIsNotNone(evaluator_blob)
        self.assertEqual(_git_blob_id(DOC_PATH), criteria_blob.group(1))
        self.assertEqual(
            _git_blob_id(REPO_ROOT / "scripts" / "evaluate_dataset.py"),
            evaluator_blob.group(1),
        )

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, register)

    def test_committed_acceptance_report_sample_matches_current_decision_counts(
        self,
    ) -> None:
        sample = REPORT_SAMPLE_PATH.read_text(encoding="utf-8")
        self.assertIn("five `pass` and fifteen `fail`", sample)
        self.assertIn('"decision_counts": {"pass": 5, "fail": 15}', sample)
        self.assertIn('"phase13": ["AC-AUTH", "OD-SEGREGATION"]', sample)
        self.assertIn("decision_input_validation", sample)
        self.assertNotIn("all 20 are `fail`", sample)
        self.assertNotIn('"decision_counts": {"pass": 0, "fail": 20}', sample)


if __name__ == "__main__":
    unittest.main()
