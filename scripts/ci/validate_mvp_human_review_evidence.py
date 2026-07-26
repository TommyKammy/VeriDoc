#!/usr/bin/env python3
"""Validate and summarize P12G-13 human-review evidence using the stdlib only."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "docs" / "mvp-human-review-evidence.schema.json"
MANIFEST_PATH = REPO_ROOT / "datasets" / "mvp_evaluation_manifest_v1.json"
EXPECTED_CASE_IDS = {
    "mvp-word-001",
    "mvp-excel-001",
    "mvp-text-pdf-001",
    "mvp-scanned-pdf-001",
    "mvp-record-pdf-001",
}
PARTICIPANT_ID_RE = re.compile(r"^P[0-9]{3,}$")
STUDY_ID_RE = re.compile(r"^HR-[A-Z0-9][A-Z0-9-]{2,63}$")
RUN_ID_RE = re.compile(r"^RUN-[A-Z0-9][A-Z0-9-]{2,63}$")
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_expected_high_risk_counts() -> dict[str, int]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {
        case["id"]: len(case["expected_high_risk_targets"])
        for case in manifest["cases"]
    }


def _unknown_fields(
    value: Any,
    allowed: set[str],
    label: str,
    errors: list[str],
) -> None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be an object")
        return
    for field in sorted(set(value) - allowed):
        errors.append(f"unknown {label} field: {field}")


def _required_fields(
    value: Any,
    required: set[str],
    label: str,
    errors: list[str],
) -> None:
    if not isinstance(value, dict):
        return
    for field in sorted(required - set(value)):
        errors.append(f"missing {label} field: {field}")


def _parse_utc(value: Any, label: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str):
        errors.append(f"{label} must be a UTC RFC 3339 timestamp")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{label} must be a UTC RFC 3339 timestamp")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        errors.append(f"{label} must be a UTC RFC 3339 timestamp")
        return None
    return parsed


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_constant(
    record: dict[str, Any],
    field: str,
    expected: Any,
    errors: list[str],
) -> None:
    if record.get(field) != expected:
        errors.append(f"{field} must be {expected!r}")


def validate_record(record: Any) -> list[str]:
    """Return deterministic human-readable validation errors."""

    errors: list[str] = []
    schema = _load_schema()
    if not isinstance(record, dict):
        return ["record must be an object"]

    top_allowed = set(schema["properties"])
    top_required = set(schema["required"])
    _unknown_fields(record, top_allowed, "record", errors)
    _required_fields(record, top_required, "record", errors)

    for field, expected in (
        ("schema_version", "veridoc-mvp-human-review-evidence/v1"),
        ("protocol_version", "p12g-13-human-review-v1"),
        ("decision_revision", "p12g-02-v1"),
        ("manifest_revision", "phase12-mvp-v1"),
    ):
        _validate_constant(record, field, expected, errors)

    practice_revision = record.get("practice_revision")
    if (
        not isinstance(practice_revision, str)
        or REVISION_RE.fullmatch(practice_revision) is None
    ):
        errors.append("practice_revision is invalid")

    study_id = record.get("study_id")
    if not isinstance(study_id, str) or STUDY_ID_RE.fullmatch(study_id) is None:
        errors.append("study_id must match ^HR-[A-Z0-9][A-Z0-9-]{2,63}$")

    study_status = record.get("study_status")
    if not isinstance(study_status, str) or study_status not in {
        "validation_example",
        "completed",
    }:
        errors.append("study_status must be validation_example or completed")

    case_ids = record.get("case_ids")
    declared_cases: set[str] = set()
    if not isinstance(case_ids, list) or not case_ids:
        errors.append("case_ids must be a non-empty array")
    elif any(not isinstance(case_id, str) for case_id in case_ids):
        errors.append("case_ids must contain strings")
    else:
        declared_cases = set(case_ids)
        if len(declared_cases) != len(case_ids):
            errors.append("case_ids must be unique")
        unknown_cases = sorted(declared_cases - EXPECTED_CASE_IDS)
        if unknown_cases:
            errors.append("unknown case_ids: " + ", ".join(unknown_cases))
        if study_status == "completed" and declared_cases != EXPECTED_CASE_IDS:
            errors.append("completed study must declare all five Phase 12 case_ids")

    consent = record.get("consent_approval")
    approved_at: datetime | None = None
    consent_schema = schema["$defs"]["consentApproval"]
    _unknown_fields(
        consent,
        set(consent_schema["properties"]),
        "consent_approval",
        errors,
    )
    _required_fields(
        consent,
        set(consent_schema["required"]),
        "consent_approval",
        errors,
    )
    if isinstance(consent, dict):
        if consent.get("approval_status") != "approved":
            errors.append("consent_approval.approval_status must be approved")
        approved_at = _parse_utc(
            consent.get("approved_at"),
            "consent_approval.approved_at",
            errors,
        )
        approved_by_role = consent.get("approved_by_role")
        if not isinstance(approved_by_role, str) or approved_by_role not in {
            "study_owner",
            "quality_approver",
        }:
            errors.append("consent_approval.approved_by_role is invalid")
        consent_version = consent.get("consent_form_version")
        if (
            not isinstance(consent_version, str)
            or REVISION_RE.fullmatch(consent_version) is None
        ):
            errors.append("consent_approval.consent_form_version is invalid")
        if consent.get("direct_identifiers_stored") is not False:
            errors.append("consent_approval.direct_identifiers_stored must be false")

    participants = record.get("participants")
    participant_ids: set[str] = set()
    participant_orders: dict[str, tuple[str, str]] = {}
    if not isinstance(participants, list):
        errors.append("participants must be an array")
        participants = []
    elif len(participants) < 3:
        errors.append("participants must contain at least three reviewers")

    participant_schema = schema["$defs"]["participant"]
    for index, participant in enumerate(participants):
        label = f"participant[{index}]"
        _unknown_fields(
            participant,
            set(participant_schema["properties"]),
            "participant",
            errors,
        )
        _required_fields(
            participant,
            set(participant_schema["required"]),
            label,
            errors,
        )
        if not isinstance(participant, dict):
            continue
        participant_id = participant.get("participant_id")
        if (
            not isinstance(participant_id, str)
            or PARTICIPANT_ID_RE.fullmatch(participant_id) is None
        ):
            errors.append(f"{label}.participant_id is invalid")
        elif participant_id in participant_ids:
            errors.append(f"duplicate participant_id: {participant_id}")
        else:
            participant_ids.add(participant_id)
        for field in (
            "relevant_experience_attested",
            "manual_practice_completed",
            "veridoc_practice_completed",
        ):
            if participant.get(field) is not True:
                errors.append(f"{label}.{field} must be true")
        arm_order = participant.get("arm_order")
        if arm_order not in (
            ["manual", "veridoc"],
            ["veridoc", "manual"],
        ):
            errors.append(f"{label}.arm_order is invalid")
        elif isinstance(participant_id, str):
            participant_orders[participant_id] = (arm_order[0], arm_order[1])

    order_counts = Counter(participant_orders.values())
    if participant_orders:
        manual_first = order_counts[("manual", "veridoc")]
        veridoc_first = order_counts[("veridoc", "manual")]
        if manual_first == 0 or veridoc_first == 0:
            errors.append("both arm orders must be represented")
        if abs(manual_first - veridoc_first) > 1:
            errors.append("arm-order participant counts may differ by at most one")

    runs = record.get("runs")
    if not isinstance(runs, list):
        errors.append("runs must be an array")
        runs = []
    elif len(runs) < 2 * len(participants) * len(declared_cases):
        errors.append("runs do not account for every participant/case/arm")

    run_schema = schema["$defs"]["run"]
    expected_high_risk_counts = _load_expected_high_risk_counts()
    run_ids: set[str] = set()
    attempt_keys: set[tuple[Any, ...]] = set()
    attempt_numbers_by_group: dict[
        tuple[str, str, str], set[int]
    ] = defaultdict(set)
    revisions_by_case: dict[
        tuple[str, str], set[str]
    ] = defaultdict(set)
    included_by_pair: dict[
        tuple[str, str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    times_by_participant_arm: dict[tuple[str, str], list[tuple[datetime, datetime]]] = (
        defaultdict(list)
    )
    times_by_participant: dict[
        str, list[tuple[datetime, datetime, str]]
    ] = defaultdict(list)
    all_started_at: list[datetime] = []

    for index, run in enumerate(runs):
        label = f"run[{index}]"
        _unknown_fields(run, set(run_schema["properties"]), "run", errors)
        _required_fields(run, set(run_schema["required"]), label, errors)
        if not isinstance(run, dict):
            continue

        run_id = run.get("run_id")
        if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
            errors.append(f"{label}.run_id is invalid")
        elif run_id in run_ids:
            errors.append(f"duplicate run_id: {run_id}")
        else:
            run_ids.add(run_id)

        participant_id = run.get("participant_id")
        participant_is_declared = (
            isinstance(participant_id, str) and participant_id in participant_ids
        )
        if not participant_is_declared:
            errors.append(f"{label}.participant_id is not declared")
        case_id = run.get("case_id")
        case_is_declared = isinstance(case_id, str) and case_id in declared_cases
        if not case_is_declared:
            errors.append(f"{label}.case_id is not declared")
        arm = run.get("arm")
        arm_is_valid = isinstance(arm, str) and arm in {"manual", "veridoc"}
        if not arm_is_valid:
            errors.append(f"{label}.arm is invalid")
        attempt_number = run.get("attempt_number")
        attempt_is_valid = (
            isinstance(attempt_number, int)
            and not isinstance(attempt_number, bool)
            and attempt_number >= 1
        )
        if not attempt_is_valid:
            errors.append(f"{label}.attempt_number must be a positive integer")

        if (
            participant_is_declared
            and case_is_declared
            and arm_is_valid
            and attempt_is_valid
        ):
            attempt_key = (participant_id, case_id, arm, attempt_number)
            if attempt_key in attempt_keys:
                errors.append(f"duplicate run attempt: {attempt_key!r}")
            else:
                attempt_keys.add(attempt_key)
                attempt_numbers_by_group[
                    (participant_id, case_id, arm)
                ].add(attempt_number)

        for field in ("task_revision", "gold_answer_revision"):
            revision = run.get(field)
            if not isinstance(revision, str) or REVISION_RE.fullmatch(revision) is None:
                errors.append(f"{label}.{field} is invalid")
            elif case_is_declared:
                revisions_by_case[(case_id, field)].add(revision)

        started_at = _parse_utc(run.get("started_at"), f"{label}.started_at", errors)
        ended_at = _parse_utc(run.get("ended_at"), f"{label}.ended_at", errors)
        elapsed_seconds: float | None = None
        if started_at is not None and ended_at is not None:
            elapsed_seconds = (ended_at - started_at).total_seconds()
            if elapsed_seconds <= 0:
                errors.append("ended_at must be after started_at")
            elif participant_is_declared and arm_is_valid:
                times_by_participant_arm[(participant_id, arm)].append(
                    (started_at, ended_at)
                )
                times_by_participant[participant_id].append(
                    (started_at, ended_at, label)
                )
                all_started_at.append(started_at)

        pause_seconds = run.get("excluded_pause_seconds")
        if not _is_non_negative_int(pause_seconds):
            errors.append(
                f"{label}.excluded_pause_seconds must be a non-negative integer"
            )
        elif elapsed_seconds is not None and pause_seconds >= elapsed_seconds:
            errors.append("excluded_pause_seconds must be less than elapsed time")

        outcome = run.get("outcome")
        blocker_code = run.get("blocker_code")
        if outcome == "approved":
            if blocker_code is not None:
                errors.append(f"{label}.blocker_code must be null for approved outcome")
        elif outcome == "blocked":
            if not isinstance(blocker_code, str) or blocker_code not in {
                "source_unreadable",
                "tool_unavailable",
                "required_information_missing",
                "approval_unavailable",
                "other_controlled",
            }:
                errors.append(f"{label}.blocker_code is required for blocked outcome")
        else:
            errors.append(f"{label}.outcome is invalid")

        excluded = run.get("excluded")
        exclusion_reason = run.get("exclusion_reason_code")
        if not isinstance(excluded, bool):
            errors.append(f"{label}.excluded must be boolean")
        elif excluded and (
            not isinstance(exclusion_reason, str)
            or exclusion_reason
            not in {
                "technical_failure",
                "protocol_deviation",
                "participant_withdrew",
                "invalid_timing",
            }
        ):
            errors.append(f"{label}.exclusion_reason_code is required when excluded")
        elif not excluded and exclusion_reason is not None:
            errors.append(f"{label}.exclusion_reason_code must be null when included")

        checklist_complete = run.get("checklist_complete")
        if not isinstance(checklist_complete, bool):
            errors.append(f"{label}.checklist_complete must be boolean")
        elif excluded is False and not checklist_complete:
            errors.append(f"{label}.checklist_complete must be true when included")

        for field in (
            "high_risk_expected_count",
            "high_risk_miss_count",
            "over_detection_count",
        ):
            if not _is_non_negative_int(run.get(field)):
                errors.append(f"{label}.{field} must be a non-negative integer")
        expected = run.get("high_risk_expected_count")
        misses = run.get("high_risk_miss_count")
        if (
            case_is_declared
            and _is_non_negative_int(expected)
            and expected != expected_high_risk_counts[case_id]
        ):
            errors.append(
                f"{label}.high_risk_expected_count must match manifest count "
                f"{expected_high_risk_counts[case_id]} for {case_id}"
            )
        if (
            _is_non_negative_int(expected)
            and _is_non_negative_int(misses)
            and misses > expected
        ):
            errors.append(
                f"{label}.high_risk_miss_count cannot exceed "
                "high_risk_expected_count"
            )

        if (
            excluded is False
            and participant_is_declared
            and case_is_declared
            and arm_is_valid
        ):
            included_by_pair[(participant_id, case_id, arm)].append(run)

    if (
        approved_at is not None
        and all_started_at
        and approved_at >= min(all_started_at)
    ):
        errors.append("consent approval must precede every timed run")

    for group, attempt_numbers in sorted(attempt_numbers_by_group.items()):
        expected_numbers = set(range(1, max(attempt_numbers) + 1))
        if attempt_numbers != expected_numbers:
            errors.append(
                f"{group[0]}/{group[1]}/{group[2]} attempt_number values "
                "must be contiguous from 1"
            )

    for (case_id, field), revisions in sorted(revisions_by_case.items()):
        if len(revisions) > 1:
            errors.append(
                f"all retained runs for {case_id} must use the same {field}"
            )

    for participant_id, intervals in sorted(times_by_participant.items()):
        ordered = sorted(intervals)
        for previous, current in zip(ordered, ordered[1:]):
            if current[0] < previous[1]:
                errors.append(
                    f"{participant_id} timed runs overlap: "
                    f"{previous[2]} and {current[2]}"
                )

    for participant_id in sorted(participant_ids):
        for case_id in sorted(declared_cases):
            for arm in ("manual", "veridoc"):
                included = included_by_pair[(participant_id, case_id, arm)]
                if len(included) != 1:
                    errors.append(
                        f"{participant_id}/{case_id}/{arm} must have exactly "
                        "one non-excluded run"
                    )

            manual = included_by_pair[(participant_id, case_id, "manual")]
            veridoc = included_by_pair[(participant_id, case_id, "veridoc")]
            if len(manual) == 1 and len(veridoc) == 1:
                for field in ("task_revision", "gold_answer_revision"):
                    if manual[0].get(field) != veridoc[0].get(field):
                        errors.append(f"paired runs must use the same {field}")

        order = participant_orders.get(participant_id)
        if order is not None:
            first_times = times_by_participant_arm[(participant_id, order[0])]
            second_times = times_by_participant_arm[(participant_id, order[1])]
            if first_times and second_times:
                if max(end for _, end in first_times) > min(
                    start for start, _ in second_times
                ):
                    errors.append(
                        f"{participant_id} timed runs do not follow declared arm_order"
                    )

    return sorted(set(errors))


def summarize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Compute protocol-defined metrics after validation."""

    errors = validate_record(record)
    if errors:
        raise ValueError("record is invalid: " + "; ".join(errors))

    participants = [item["participant_id"] for item in record["participants"]]
    included = [run for run in record["runs"] if not run["excluded"]]
    by_pair = {
        (run["participant_id"], run["case_id"], run["arm"]): run
        for run in included
    }
    pair_results: list[dict[str, Any]] = []
    eligible_pair_results: list[dict[str, Any]] = []

    def correction_seconds(run: dict[str, Any]) -> float:
        start = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(run["ended_at"].replace("Z", "+00:00"))
        return (end - start).total_seconds() - run["excluded_pause_seconds"]

    for participant_id in sorted(participants):
        for case_id in sorted(record["case_ids"]):
            manual = by_pair[(participant_id, case_id, "manual")]
            veridoc = by_pair[(participant_id, case_id, "veridoc")]
            eligible = (
                manual["outcome"] == "approved"
                and veridoc["outcome"] == "approved"
                and manual["checklist_complete"]
                and veridoc["checklist_complete"]
            )
            pair_result = {
                "participant_id": participant_id,
                "case_id": case_id,
                "eligible": eligible,
                "manual_outcome": manual["outcome"],
                "manual_blocker_code": manual["blocker_code"],
                "veridoc_outcome": veridoc["outcome"],
                "veridoc_blocker_code": veridoc["blocker_code"],
            }
            if eligible:
                manual_seconds = correction_seconds(manual)
                veridoc_seconds = correction_seconds(veridoc)
                reduction = (
                    100.0
                    * (manual_seconds - veridoc_seconds)
                    / manual_seconds
                )
                pair_result.update(
                    {
                        "manual_seconds": manual_seconds,
                        "veridoc_seconds": veridoc_seconds,
                        "reduction_percent": reduction,
                    }
                )
                eligible_pair_results.append(pair_result)
            pair_results.append(pair_result)

    paired_median = (
        statistics.median(
            item["reduction_percent"] for item in eligible_pair_results
        )
        if eligible_pair_results
        else None
    )
    arm_metrics: dict[str, dict[str, int]] = {}
    for arm in ("manual", "veridoc"):
        all_arm_runs = [run for run in record["runs"] if run["arm"] == arm]
        included_arm_runs = [run for run in included if run["arm"] == arm]
        arm_metrics[arm] = {
            "high_risk_misses": sum(
                run["high_risk_miss_count"] for run in all_arm_runs
            ),
            "over_detections": sum(
                run["over_detection_count"] for run in all_arm_runs
            ),
            "approved_completions": sum(
                run["outcome"] == "approved" for run in included_arm_runs
            ),
            "blockers": sum(
                run["outcome"] == "blocked" for run in all_arm_runs
            ),
            "retry_runs": sum(
                run["attempt_number"] > 1 for run in all_arm_runs
            ),
            "excluded_runs": sum(run["excluded"] for run in all_arm_runs),
        }

    required_groups = {
        (participant_id, case_id, arm)
        for participant_id in participants
        for case_id in record["case_ids"]
        for arm in ("manual", "veridoc")
    }
    recorded_groups = {
        (run["participant_id"], run["case_id"], run["arm"])
        for run in record["runs"]
    }
    all_required_runs_accounted = required_groups <= recorded_groups

    return {
        "study_id": record["study_id"],
        "study_status": record["study_status"],
        "required_runs": 2 * len(participants) * len(record["case_ids"]),
        "recorded_runs": len(record["runs"]),
        "excluded_runs": sum(run["excluded"] for run in record["runs"]),
        "retry_runs": sum(run["attempt_number"] > 1 for run in record["runs"]),
        "all_required_runs_accounted": all_required_runs_accounted,
        "eligible_pair_count": len(eligible_pair_results),
        "ineligible_pair_count": len(pair_results) - len(eligible_pair_results),
        "pair_results": pair_results,
        "paired_median_reduction_percent": paired_median,
        "arm_metrics": arm_metrics,
        "efficiency_target_met": (
            record["study_status"] == "completed"
            and paired_median is not None
            and paired_median >= 30.0
            and arm_metrics["veridoc"]["high_risk_misses"] == 0
            and all_required_runs_accounted
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate and summarize P12G-13 human-review evidence."
    )
    parser.add_argument("record", type=Path, help="human-review evidence JSON")
    args = parser.parse_args(argv)

    try:
        record = json.loads(args.record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Unable to read evidence: {exc}", file=sys.stderr)
        return 2

    errors = validate_record(record)
    if errors:
        print("Human-review evidence validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(json.dumps(summarize_record(record), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
