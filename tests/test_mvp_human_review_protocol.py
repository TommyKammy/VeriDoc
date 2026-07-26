from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.ci.validate_mvp_human_review_evidence import (
    APPROVED_GOLD_ANSWER_REVISION,
    APPROVED_TASK_REVISION,
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
ALL_CASE_IDS = (
    "mvp-word-001",
    "mvp-excel-001",
    "mvp-text-pdf-001",
    "mvp-scanned-pdf-001",
    "mvp-record-pdf-001",
)
EXPECTED_HIGH_RISK_COUNTS = {
    "mvp-word-001": 0,
    "mvp-excel-001": 0,
    "mvp-text-pdf-001": 0,
    "mvp-scanned-pdf-001": 1,
    "mvp-record-pdf-001": 0,
}


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


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _completed_record(base_record: dict[str, object]) -> dict[str, object]:
    record = copy.deepcopy(base_record)
    record["study_status"] = "completed"
    record["case_ids"] = list(ALL_CASE_IDS)
    participants = record["participants"]
    assert isinstance(participants, list)
    runs: list[dict[str, object]] = []
    epoch = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    for participant_index, participant in enumerate(participants):
        assert isinstance(participant, dict)
        participant_id = participant["participant_id"]
        arm_order = participant["arm_order"]
        assert isinstance(participant_id, str)
        assert isinstance(arm_order, list)
        cursor = epoch + timedelta(days=participant_index)
        for arm in arm_order:
            assert arm in {"manual", "veridoc"}
            duration = timedelta(seconds=120 if arm == "manual" else 60)
            for case_index, case_id in enumerate(ALL_CASE_IDS, start=1):
                started_at = cursor
                ended_at = started_at + duration
                runs.append(
                    {
                        "run_id": (
                            f"RUN-{participant_id}-{case_index}-{arm.upper()}-1"
                        ),
                        "participant_id": participant_id,
                        "case_id": case_id,
                        "arm": arm,
                        "attempt_number": 1,
                        "task_revision": APPROVED_TASK_REVISION,
                        "gold_answer_revision": APPROVED_GOLD_ANSWER_REVISION,
                        "gold_answer_hidden_until_ended_at": True,
                        "started_at": _utc_text(started_at),
                        "ended_at": _utc_text(ended_at),
                        "excluded_pause_seconds": 0,
                        "outcome": "approved",
                        "checklist_complete": True,
                        "blocker_code": None,
                        "high_risk_expected_count": EXPECTED_HIGH_RISK_COUNTS[
                            case_id
                        ],
                        "high_risk_miss_count": 0,
                        "over_detection_count": 0,
                        "excluded": False,
                        "exclusion_reason_code": None,
                    }
                )
                cursor = ended_at + timedelta(minutes=1)
            cursor += timedelta(minutes=5)
    record["runs"] = runs
    return record


def _add_excluded_veridoc_retry_with_miss(record: dict[str, object]) -> None:
    runs = record["runs"]
    assert isinstance(runs, list)
    source = next(
        run
        for run in runs
        if isinstance(run, dict)
        and run["participant_id"] == "P001"
        and run["case_id"] == "mvp-scanned-pdf-001"
        and run["arm"] == "veridoc"
    )
    participant_ends = [
        datetime.fromisoformat(str(run["ended_at"]).replace("Z", "+00:00"))
        for run in runs
        if isinstance(run, dict) and run["participant_id"] == "P001"
    ]
    retry = copy.deepcopy(source)
    retry_start = max(participant_ends) + timedelta(minutes=1)
    retry.update(
        {
            "run_id": "RUN-P001-4-VERIDOC-2",
            "attempt_number": 2,
            "started_at": _utc_text(retry_start),
            "ended_at": _utc_text(retry_start + timedelta(minutes=1)),
            "high_risk_miss_count": 1,
            "excluded": True,
            "exclusion_reason_code": "technical_failure",
        }
    )
    runs.append(retry)


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
        self.assertFalse(schema["$defs"]["qualityApproval"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["participant"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["run"]["additionalProperties"])
        self.assertIn("practice_revision", schema["required"])
        self.assertIn("quality_approval", schema["required"])
        self.assertIn(
            "gold_answer_hidden_until_ended_at",
            schema["$defs"]["run"]["required"],
        )
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
        self.assertEqual(
            APPROVED_TASK_REVISION,
            schema["$defs"]["run"]["properties"]["task_revision"]["const"],
        )
        self.assertEqual(
            APPROVED_GOLD_ANSWER_REVISION,
            schema["$defs"]["run"]["properties"]["gold_answer_revision"]["const"],
        )

    def test_synthetic_validation_example_is_valid_and_recomputable(self) -> None:
        self.assertEqual([], validate_record(self.valid_record))
        summary = summarize_record(self.valid_record)
        self.assertEqual(6, summary["required_runs"])
        self.assertEqual(6, summary["recorded_runs"])
        self.assertEqual(2, summary["eligible_pair_count"])
        self.assertEqual(1, summary["ineligible_pair_count"])
        self.assertEqual(3, len(summary["pair_results"]))
        self.assertEqual(35.0, summary["paired_median_reduction_percent"])
        self.assertEqual(1, summary["arm_metrics"]["veridoc"]["blockers"])
        self.assertEqual(
            {
                metric: sum(
                    summary["arm_metrics"][arm][metric]
                    for arm in ("manual", "veridoc")
                )
                for metric in (
                    "high_risk_misses",
                    "over_detections",
                    "approved_completions",
                    "blockers",
                    "retry_runs",
                    "excluded_runs",
                )
            },
            summary["totals"],
        )
        blocked_pair = next(
            pair
            for pair in summary["pair_results"]
            if pair["participant_id"] == "P002"
        )
        self.assertFalse(blocked_pair["eligible"])
        self.assertEqual("blocked", blocked_pair["veridoc_outcome"])
        self.assertEqual(
            "approval_unavailable",
            blocked_pair["veridoc_blocker_code"],
        )
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

    def test_completed_record_can_meet_target_with_fixed_contract(self) -> None:
        record = _completed_record(self.valid_record)
        self.assertEqual([], validate_record(record))
        summary = summarize_record(record)
        self.assertEqual(15, summary["eligible_pair_count"])
        self.assertEqual(50.0, summary["paired_median_reduction_percent"])
        self.assertTrue(summary["all_required_runs_accounted"])
        self.assertTrue(summary["efficiency_target_met"])

    def test_expected_high_risk_count_is_bound_to_manifest(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["case_ids"] = ["mvp-scanned-pdf-001"]
        for run in record["runs"]:
            run["case_id"] = "mvp-scanned-pdf-001"
        self.assertIn(
            "run[0].high_risk_expected_count must match manifest count "
            "1 for mvp-scanned-pdf-001",
            validate_record(record),
        )

    def test_attempt_numbers_must_be_contiguous_from_one(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["attempt_number"] = 2
        self.assertIn(
            "P001/mvp-word-001/manual attempt_number values "
            "must be contiguous from 1",
            validate_record(record),
        )

    def test_attempt_timestamps_must_follow_attempt_number_order(self) -> None:
        record = _completed_record(self.valid_record)
        _add_excluded_veridoc_retry_with_miss(record)
        runs = record["runs"]
        assert isinstance(runs, list)
        first = next(
            run
            for run in runs
            if isinstance(run, dict)
            and run["participant_id"] == "P001"
            and run["case_id"] == "mvp-scanned-pdf-001"
            and run["arm"] == "veridoc"
            and run["attempt_number"] == 1
        )
        retry = runs[-1]
        assert isinstance(retry, dict)
        first["started_at"], retry["started_at"] = (
            retry["started_at"],
            first["started_at"],
        )
        first["ended_at"], retry["ended_at"] = (
            retry["ended_at"],
            first["ended_at"],
        )
        self.assertIn(
            "P001/mvp-scanned-pdf-001/veridoc attempt timestamps "
            "must follow attempt_number order",
            validate_record(record),
        )

    def test_unknown_declared_case_is_rejected_without_crashing(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["case_ids"] = ["unknown-case"]
        for run in record["runs"]:
            run["case_id"] = "unknown-case"
        self.assertIn("unknown case_ids: unknown-case", validate_record(record))

    def test_gold_answer_must_be_attested_hidden_until_timing_ends(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["gold_answer_hidden_until_ended_at"] = False
        self.assertIn(
            "run[0].gold_answer_hidden_until_ended_at must be true",
            validate_record(record),
        )

    def test_run_revisions_must_match_the_approved_protocol_contract(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["task_revision"] = "unapproved-task-v2"
        record["runs"][0]["gold_answer_revision"] = "unapproved-gold-v2"
        errors = validate_record(record)
        self.assertIn(
            "run[0].task_revision must match approved protocol revision "
            f"{APPROVED_TASK_REVISION}",
            errors,
        )
        self.assertIn(
            "run[0].gold_answer_revision must match approved protocol revision "
            f"{APPROVED_GOLD_ANSWER_REVISION}",
            errors,
        )

    def test_case_revisions_are_fixed_across_cohort_and_excluded_attempts(
        self,
    ) -> None:
        record = copy.deepcopy(self.valid_record)
        for run in record["runs"]:
            if run["participant_id"] == "P003":
                run["task_revision"] = "different-task-v2"
        self.assertIn(
            "all retained runs for mvp-word-001 must use the same task_revision",
            validate_record(record),
        )

        completed = _completed_record(self.valid_record)
        _add_excluded_veridoc_retry_with_miss(completed)
        completed["runs"][-1]["gold_answer_revision"] = "different-gold-v2"
        self.assertIn(
            "all retained runs for mvp-scanned-pdf-001 must use the same "
            "gold_answer_revision",
            validate_record(completed),
        )

    def test_approval_must_strictly_precede_every_run(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["consent_approval"]["approved_at"] = record["runs"][0]["started_at"]
        self.assertIn(
            "consent approval must precede every timed run",
            validate_record(record),
        )
        record = copy.deepcopy(self.valid_record)
        record["quality_approval"]["approved_at"] = record["runs"][0]["started_at"]
        self.assertIn(
            "quality approval must precede every timed run",
            validate_record(record),
        )

    def test_quality_approval_is_separate_and_required(self) -> None:
        record = copy.deepcopy(self.valid_record)
        del record["quality_approval"]
        self.assertIn(
            "missing record field: quality_approval",
            validate_record(record),
        )
        record = copy.deepcopy(self.valid_record)
        record["quality_approval"]["approved_by_role"] = "study_owner"
        record["quality_approval"]["external_record_version"] = ""
        errors = validate_record(record)
        self.assertIn(
            "quality_approval.approved_by_role must be quality_approver",
            errors,
        )
        self.assertIn(
            "quality_approval.external_record_version is invalid",
            errors,
        )

    def test_participant_run_intervals_must_not_overlap(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][1]["started_at"] = record["runs"][0]["started_at"]
        record["runs"][1]["ended_at"] = record["runs"][0]["ended_at"]
        errors = validate_record(record)
        self.assertTrue(
            any(error.startswith("P001 timed runs overlap:") for error in errors)
        )

    def test_excluded_veridoc_miss_blocks_target_and_is_reported_per_arm(
        self,
    ) -> None:
        record = _completed_record(self.valid_record)
        _add_excluded_veridoc_retry_with_miss(record)
        self.assertEqual([], validate_record(record))
        summary = summarize_record(record)
        self.assertEqual(1, summary["retry_runs"])
        self.assertEqual(1, summary["excluded_runs"])
        self.assertEqual(1, summary["arm_metrics"]["veridoc"]["retry_runs"])
        self.assertEqual(1, summary["arm_metrics"]["veridoc"]["excluded_runs"])
        self.assertEqual(1, summary["arm_metrics"]["veridoc"]["high_risk_misses"])
        self.assertEqual(1, summary["totals"]["retry_runs"])
        self.assertEqual(1, summary["totals"]["excluded_runs"])
        self.assertEqual(1, summary["totals"]["high_risk_misses"])
        self.assertFalse(summary["efficiency_target_met"])

    def test_controlled_blocker_is_accounted_but_excluded_from_median(self) -> None:
        record = _completed_record(self.valid_record)
        blocked = next(
            run
            for run in record["runs"]
            if run["participant_id"] == "P002"
            and run["case_id"] == "mvp-word-001"
            and run["arm"] == "veridoc"
        )
        blocked["outcome"] = "blocked"
        blocked["blocker_code"] = "approval_unavailable"
        self.assertEqual([], validate_record(record))
        summary = summarize_record(record)
        self.assertTrue(summary["all_required_runs_accounted"])
        self.assertEqual(14, summary["eligible_pair_count"])
        self.assertEqual(1, summary["ineligible_pair_count"])
        self.assertEqual(50.0, summary["paired_median_reduction_percent"])
        self.assertTrue(summary["efficiency_target_met"])

    def test_practice_revision_is_required_and_controlled(self) -> None:
        record = copy.deepcopy(self.valid_record)
        del record["practice_revision"]
        errors = validate_record(record)
        self.assertIn("missing record field: practice_revision", errors)
        self.assertIn("practice_revision is invalid", errors)

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
                "consent_approval.approved_by_role must be study_owner",
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
