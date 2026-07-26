from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.ci.validate_mvp_human_review_evidence import (
    summarize_record,
    validate_record,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = REPO_ROOT / "docs" / "mvp-human-review-protocol.md"
SCHEMA_PATH = REPO_ROOT / "docs" / "mvp-human-review-evidence.schema.json"
CHECKLIST_PATH = REPO_ROOT / "docs" / "mvp-human-review-execution-checklist.md"
VALID_EXAMPLE_PATH = REPO_ROOT / "datasets" / "mvp_human_review_evidence_valid.json"
INVALID_EXAMPLES_PATH = (
    REPO_ROOT / "datasets" / "mvp_human_review_evidence_invalid_examples.json"
)
VALIDATOR_PATH = (
    REPO_ROOT / "scripts" / "ci" / "validate_mvp_human_review_evidence.py"
)


def _replace_json_pointer(document: object, pointer: str, value: object) -> None:
    parts = [
        part.replace("~1", "/").replace("~0", "~")
        for part in pointer.removeprefix("/").split("/")
    ]
    target = document
    for part in parts[:-1]:
        if isinstance(target, list):
            target = target[int(part)]
        else:
            target = target[part]  # type: ignore[index]
    if isinstance(target, list):
        target[int(parts[-1])] = value
    else:
        target[parts[-1]] = value  # type: ignore[index]


class MvpHumanReviewProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.valid_record = json.loads(VALID_EXAMPLE_PATH.read_text(encoding="utf-8"))

    def test_protocol_and_checklist_bind_approved_revisions_and_calculations(
        self,
    ) -> None:
        protocol = PROTOCOL_PATH.read_text(encoding="utf-8")
        checklist = CHECKLIST_PATH.read_text(encoding="utf-8")
        for marker in (
            "p12g-13-human-review-v1",
            "veridoc-mvp-human-review-evidence/v1",
            "p12g-02-v1",
            "phase12-mvp-v1",
            "within-participant",
            "paired cohort median",
            "high-risk miss",
            "over_detection_count",
            "direct participant identity",
            "P12G-14",
        ):
            self.assertIn(marker, protocol)
        for marker in (
            "Study setup",
            "Per-run procedure",
            "Per-run record template",
            "Study closeout",
            "excluded_pause_seconds",
            "high_risk_miss_count",
            "blocker_code",
            "validate_mvp_human_review_evidence.py",
        ):
            self.assertIn(marker, checklist)

    def test_schema_is_closed_and_revision_bound(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            "https://json-schema.org/draft/2020-12/schema",
            schema["$schema"],
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["$defs"]["consentApproval"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["participant"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["run"]["additionalProperties"])
        self.assertEqual(
            "p12g-13-human-review-v1",
            schema["properties"]["protocol_version"]["const"],
        )
        self.assertEqual(
            "p12g-02-v1",
            schema["properties"]["decision_revision"]["const"],
        )
        self.assertEqual(
            "phase12-mvp-v1",
            schema["properties"]["manifest_revision"]["const"],
        )

    def test_synthetic_validation_example_is_valid_and_recomputable(self) -> None:
        self.assertEqual([], validate_record(self.valid_record))
        summary = summarize_record(self.valid_record)
        self.assertEqual(6, summary["required_runs"])
        self.assertEqual(6, summary["recorded_runs"])
        self.assertEqual(2, summary["eligible_pair_count"])
        self.assertEqual(35.0, summary["paired_median_reduction_percent"])
        self.assertEqual(1, summary["arm_metrics"]["veridoc"]["blockers"])
        self.assertFalse(summary["efficiency_target_met"])

    def test_declared_invalid_examples_are_rejected_for_the_stated_reason(
        self,
    ) -> None:
        vectors = json.loads(INVALID_EXAMPLES_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            "datasets/mvp_human_review_evidence_valid.json",
            vectors["base_record"],
        )
        self.assertGreaterEqual(len(vectors["examples"]), 3)
        for example in vectors["examples"]:
            with self.subTest(example=example["id"]):
                mutated = copy.deepcopy(self.valid_record)
                _replace_json_pointer(mutated, example["path"], example["value"])
                errors = validate_record(mutated)
                self.assertIn(example["expected_error"], errors)

    def test_completed_record_requires_all_five_manifest_cases(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["study_status"] = "completed"
        self.assertIn(
            "completed study must declare all five Phase 12 case_ids",
            validate_record(record),
        )

    def test_validator_fails_closed_on_wrong_json_types(self) -> None:
        mutations = (
            (
                "/study_status",
                [],
                "study_status must be validation_example or completed",
            ),
            (
                "/consent_approval/approved_by_role",
                [],
                "consent_approval.approved_by_role is invalid",
            ),
            (
                "/runs/0/participant_id",
                [],
                "run[0].participant_id is not declared",
            ),
            ("/runs/0/case_id", {}, "run[0].case_id is not declared"),
            ("/runs/0/arm", [], "run[0].arm is invalid"),
            (
                "/runs/0/exclusion_reason_code",
                [],
                "run[0].exclusion_reason_code must be null when included",
            ),
        )
        for path, value, expected_error in mutations:
            with self.subTest(path=path):
                mutated = copy.deepcopy(self.valid_record)
                _replace_json_pointer(mutated, path, value)
                self.assertIn(expected_error, validate_record(mutated))

    def test_validator_cli_accepts_the_validation_example(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(VALIDATOR_PATH), str(VALID_EXAMPLE_PATH)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, msg=completed.stderr)
        summary = json.loads(completed.stdout)
        self.assertEqual("validation_example", summary["study_status"])
        self.assertFalse(summary["efficiency_target_met"])


if __name__ == "__main__":
    unittest.main()
