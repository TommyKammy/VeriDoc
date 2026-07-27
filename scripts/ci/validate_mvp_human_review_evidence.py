#!/usr/bin/env python3
"""Validate and summarize P12G-13 human-review evidence using the stdlib only."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "docs" / "mvp-human-review-evidence.schema.json"
APPROVED_PRODUCT_COMMIT = "584ef2db12a6676abb65f75de1ec38145e06b487"
APPROVED_PRODUCT_TREE = "d7b1714ab9e7f42c5299a4e4b5197e4669a035b9"
APPROVED_SOURCE_TREE_LISTING_SHA256 = (
    "0bec46f7d8240796a137a163c20c4ee5f98f867f5730d78fe56b571eeffd6b3c"
)
APPROVED_MANIFEST_PATH = "datasets/mvp_evaluation_manifest_v1.json"
APPROVED_MANIFEST_GIT_BLOB = "13450762d323198b1b6e87315be173c784fc4880"
APPROVED_MANIFEST_CONTRACT_SHA256 = (
    "5d91a67915d79c649954c5c8af02e74d08d94d0b97e7e673a7db690df61ebfff"
)
MANIFEST_CONTRACT_FIELDS = (
    "schema_version",
    "selection_status",
    "selection_revision",
    "fixture_manifest",
    "source_policy",
    "confidential_source_documents_allowed",
    "acceptance_limits",
    "required_categories",
    "cases",
)
EXPECTED_CASE_IDS = {
    "mvp-word-001",
    "mvp-excel-001",
    "mvp-text-pdf-001",
    "mvp-scanned-pdf-001",
    "mvp-record-pdf-001",
}
APPROVED_TASK_REVISION = "task-phase12-v1"
APPROVED_GOLD_ANSWER_REVISION = "gold-phase12-v1"
APPROVED_CHECKLIST_REVISION = "checklist-phase12-v1"
APPROVED_RUN_REVISIONS = {
    "task_revision": APPROVED_TASK_REVISION,
    "gold_answer_revision": APPROVED_GOLD_ANSWER_REVISION,
    "checklist_revision": APPROVED_CHECKLIST_REVISION,
}
UUID4_TOKEN_RE = (
    r"[0-9A-F]{8}-[0-9A-F]{4}-4[0-9A-F]{3}-"
    r"[89AB][0-9A-F]{3}-[0-9A-F]{12}"
)
PARTICIPANT_ID_RE = re.compile(rf"^P-{UUID4_TOKEN_RE}$")
STUDY_ID_RE = re.compile(rf"^HR-{UUID4_TOKEN_RE}$")
RUN_ID_RE = re.compile(
    rf"^RUN-P-{UUID4_TOKEN_RE}-MVP-[A-Z0-9-]+-"
    r"(?:MANUAL|VERIDOC)-[1-9][0-9]*$"
)
SEALED_ARTIFACT_RECORD_ID_RE = re.compile(rf"^SAR-{UUID4_TOKEN_RE}$")
BUILD_PROVENANCE_RECORD_ID_RE = re.compile(rf"^BLD-{UUID4_TOKEN_RE}$")
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RFC3339_UTC_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|\+00:00)$"
)


class DuplicateKeyError(ValueError):
    """Raised when a JSON object contains an ambiguous duplicate key."""


class ApprovedManifestError(RuntimeError):
    """Raised when the approved manifest contract cannot be reconstructed."""


def _reject_duplicate_object_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateKeyError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _loads_json_strict(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_reject_duplicate_object_keys)


def _load_schema() -> dict[str, Any]:
    schema = _loads_json_strict(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise ValueError("human-review schema must be an object")
    return schema


def _git_show_approved(path: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "show", f"{APPROVED_PRODUCT_COMMIT}:{path}"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ApprovedManifestError(
            f"unable to read {path!r} at approved commit "
            f"{APPROVED_PRODUCT_COMMIT}"
        ) from exc


def _git_blob_oid(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def _canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _approved_product_tree() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", f"{APPROVED_PRODUCT_COMMIT}^{{tree}}"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ApprovedManifestError(
            "unable to resolve the approved product tree"
        ) from exc


def _approved_source_tree_listing_sha256() -> str:
    try:
        tree_manifest = subprocess.run(
            [
                "git",
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                APPROVED_PRODUCT_COMMIT,
            ],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ApprovedManifestError(
            "unable to derive the approved source-tree listing"
        ) from exc
    return hashlib.sha256(tree_manifest).hexdigest()


@lru_cache(maxsize=1)
def _load_approved_manifest_contract() -> tuple[
    dict[str, dict[str, Any]], bool
]:
    product_tree = _approved_product_tree()
    if product_tree != APPROVED_PRODUCT_TREE:
        raise ApprovedManifestError(
            "approved product tree mismatch: "
            f"expected {APPROVED_PRODUCT_TREE}, got {product_tree}"
        )
    source_tree_listing_sha256 = _approved_source_tree_listing_sha256()
    if source_tree_listing_sha256 != APPROVED_SOURCE_TREE_LISTING_SHA256:
        raise ApprovedManifestError(
            "approved source-tree listing SHA-256 mismatch: "
            f"expected {APPROVED_SOURCE_TREE_LISTING_SHA256}, "
            f"got {source_tree_listing_sha256}"
        )
    manifest_bytes = _git_show_approved(APPROVED_MANIFEST_PATH)
    manifest_blob = _git_blob_oid(manifest_bytes)
    if manifest_blob != APPROVED_MANIFEST_GIT_BLOB:
        raise ApprovedManifestError(
            "approved manifest Git blob mismatch: "
            f"expected {APPROVED_MANIFEST_GIT_BLOB}, got {manifest_blob}"
        )
    try:
        manifest = _loads_json_strict(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateKeyError) as exc:
        raise ApprovedManifestError("approved manifest is not strict JSON") from exc
    if not isinstance(manifest, dict):
        raise ApprovedManifestError("approved manifest must be an object")

    fixture_manifest_path = manifest.get("fixture_manifest")
    if not isinstance(fixture_manifest_path, str):
        raise ApprovedManifestError(
            "approved manifest fixture_manifest must be a path"
        )
    try:
        fixture_manifest = _loads_json_strict(
            _git_show_approved(fixture_manifest_path).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateKeyError) as exc:
        raise ApprovedManifestError(
            "approved fixture manifest is not strict JSON"
        ) from exc
    if not isinstance(fixture_manifest, dict) or not isinstance(
        fixture_manifest.get("fixtures"), list
    ):
        raise ApprovedManifestError(
            "approved fixture manifest must contain fixtures"
        )

    cases = manifest.get("cases")
    if not isinstance(cases, list) or any(
        not isinstance(case, dict) for case in cases
    ):
        raise ApprovedManifestError("approved manifest must contain case objects")
    fixture_by_id = {
        fixture.get("id"): fixture
        for fixture in fixture_manifest["fixtures"]
        if isinstance(fixture, dict) and isinstance(fixture.get("id"), str)
    }
    selected_fixture_ids = sorted(
        {
            case.get("fixture_id")
            for case in cases
            if isinstance(case.get("fixture_id"), str)
        }
    )
    if len(selected_fixture_ids) != len(cases):
        raise ApprovedManifestError(
            "approved cases must bind unique fixture identifiers"
        )
    try:
        selected_fixture_manifest = {
            key: value
            for key, value in fixture_manifest.items()
            if key != "fixtures"
        }
        selected_fixture_manifest["fixtures"] = [
            fixture
            for fixture in fixture_manifest["fixtures"]
            if isinstance(fixture, dict)
            and fixture.get("id") in selected_fixture_ids
        ]
        selected_fixture_contents = {}
        for fixture_id in selected_fixture_ids:
            fixture = fixture_by_id[fixture_id]
            fixture_path = fixture["path"]
            fixture_content = _git_show_approved(fixture_path)
            selected_fixture_contents[fixture_id] = {
                "path": fixture_path,
                "present": True,
                "sha256": hashlib.sha256(fixture_content).hexdigest(),
            }
    except (KeyError, TypeError) as exc:
        raise ApprovedManifestError(
            "approved fixture identities are incomplete"
        ) from exc

    manifest_contract = {
        field: manifest.get(field) for field in MANIFEST_CONTRACT_FIELDS
    }
    manifest_contract["fixture_approval_contract"] = {
        "fixture_manifest": selected_fixture_manifest,
        "selected_fixture_contents": selected_fixture_contents,
    }
    contract_sha256 = _canonical_json_sha256(manifest_contract)
    if contract_sha256 != APPROVED_MANIFEST_CONTRACT_SHA256:
        raise ApprovedManifestError(
            "approved manifest contract SHA-256 mismatch: "
            f"expected {APPROVED_MANIFEST_CONTRACT_SHA256}, "
            f"got {contract_sha256}"
        )

    case_contracts: dict[str, dict[str, Any]] = {}
    structured_high_risk_targets_ready = True
    for case in cases:
        case_id = case.get("id")
        fixture_id = case.get("fixture_id")
        fixture_path = case.get("fixture_path")
        targets = case.get("expected_high_risk_targets")
        if not isinstance(case_id, str) or not isinstance(fixture_id, str):
            raise ApprovedManifestError("approved case identity is incomplete")
        fixture = fixture_by_id.get(fixture_id)
        if (
            not isinstance(fixture, dict)
            or not isinstance(fixture.get("path"), str)
            or fixture_path != fixture["path"]
        ):
            raise ApprovedManifestError(
                f"approved fixture path mismatch for {case_id}"
            )
        if not isinstance(targets, list):
            structured_high_risk_targets_ready = False
            targets = []
        case_contracts[case_id] = {
            "fixture_id": fixture_id,
            "fixture_path": fixture_path,
            "fixture_sha256": selected_fixture_contents[fixture_id]["sha256"],
            "high_risk_expected_count": len(targets),
        }
    if set(case_contracts) != EXPECTED_CASE_IDS:
        raise ApprovedManifestError(
            "approved manifest case set does not match Phase 12 scope"
        )
    return case_contracts, structured_high_risk_targets_ready


def _expected_run_id(
    participant_id: str,
    case_id: str,
    arm: str,
    attempt_number: int,
) -> str:
    return (
        f"RUN-{participant_id}-{case_id.upper()}-"
        f"{arm.upper()}-{attempt_number}"
    )


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
    if (
        not isinstance(value, str)
        or RFC3339_UTC_RE.fullmatch(value) is None
    ):
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
        ("target_product_commit", APPROVED_PRODUCT_COMMIT),
        ("manifest_git_blob", APPROVED_MANIFEST_GIT_BLOB),
        ("manifest_contract_sha256", APPROVED_MANIFEST_CONTRACT_SHA256),
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
        errors.append("study_id must be an opaque HR-prefixed UUIDv4")

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
        if consent.get("approved_by_role") != "study_owner":
            errors.append(
                "consent_approval.approved_by_role must be study_owner"
            )
        consent_version = consent.get("consent_form_version")
        if (
            not isinstance(consent_version, str)
            or REVISION_RE.fullmatch(consent_version) is None
        ):
            errors.append("consent_approval.consent_form_version is invalid")
        if consent.get("direct_identifiers_stored") is not False:
            errors.append("consent_approval.direct_identifiers_stored must be false")

    quality_approval = record.get("quality_approval")
    quality_approved_at: datetime | None = None
    quality_schema = schema["$defs"]["qualityApproval"]
    _unknown_fields(
        quality_approval,
        set(quality_schema["properties"]),
        "quality_approval",
        errors,
    )
    _required_fields(
        quality_approval,
        set(quality_schema["required"]),
        "quality_approval",
        errors,
    )
    if isinstance(quality_approval, dict):
        if quality_approval.get("approval_status") != "approved":
            errors.append("quality_approval.approval_status must be approved")
        quality_approved_at = _parse_utc(
            quality_approval.get("approved_at"),
            "quality_approval.approved_at",
            errors,
        )
        if quality_approval.get("approved_by_role") != "quality_approver":
            errors.append(
                "quality_approval.approved_by_role must be quality_approver"
            )
        external_record_version = quality_approval.get(
            "external_record_version"
        )
        if (
            not isinstance(external_record_version, str)
            or REVISION_RE.fullmatch(external_record_version) is None
        ):
            errors.append(
                "quality_approval.external_record_version is invalid"
            )

    participants = record.get("participants")
    participant_ids: set[str] = set()
    participant_statuses: dict[str, str] = {}
    participant_orders: dict[str, tuple[str, str]] = {}
    participant_withdrawn_at: dict[str, datetime] = {}
    practice_completed_at_by_participant: dict[
        str, dict[str, datetime]
    ] = defaultdict(dict)
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
            errors.append(
                f"{label}.participant_id must be an opaque P-prefixed UUIDv4"
            )
        elif participant_id in participant_ids:
            errors.append(f"duplicate participant_id: {participant_id}")
        else:
            participant_ids.add(participant_id)
        participation_status = participant.get("participation_status")
        if (
            not isinstance(participation_status, str)
            or participation_status not in {"completed", "withdrawn"}
        ):
            errors.append(f"{label}.participation_status is invalid")
        elif isinstance(participant_id, str):
            participant_statuses[participant_id] = participation_status
        withdrawn_at_value = participant.get("withdrawn_at")
        if participation_status == "completed":
            if withdrawn_at_value is not None:
                errors.append(f"{label}.withdrawn_at must be null when completed")
        elif participation_status == "withdrawn":
            withdrawn_at = _parse_utc(
                withdrawn_at_value,
                f"{label}.withdrawn_at",
                errors,
            )
            if withdrawn_at is not None and isinstance(participant_id, str):
                participant_withdrawn_at[participant_id] = withdrawn_at
        if participant.get("relevant_experience_attested") is not True:
            errors.append(f"{label}.relevant_experience_attested must be true")
        for arm in ("manual", "veridoc"):
            completed_field = f"{arm}_practice_completed"
            completed_at_field = f"{arm}_practice_completed_at"
            completed = participant.get(completed_field)
            completed_at_value = participant.get(completed_at_field)
            if participation_status == "completed":
                if completed is not True:
                    errors.append(f"{label}.{completed_field} must be true")
            elif not isinstance(completed, bool):
                errors.append(f"{label}.{completed_field} must be boolean")
            if completed is True:
                completed_at = _parse_utc(
                    completed_at_value,
                    f"{label}.{completed_at_field}",
                    errors,
                )
                if completed_at is not None and isinstance(participant_id, str):
                    practice_completed_at_by_participant[participant_id][
                        completed_at_field
                    ] = completed_at
            elif completed is False and completed_at_value is not None:
                errors.append(
                    f"{label}.{completed_at_field} must be null when "
                    f"{completed_field} is false"
                )
        arm_order = participant.get("arm_order")
        arm_order_is_valid = arm_order in (
            ["manual", "veridoc"],
            ["veridoc", "manual"],
        )
        if participation_status == "completed" and not arm_order_is_valid:
            errors.append(f"{label}.arm_order is invalid")
        elif (
            participation_status == "withdrawn"
            and arm_order is not None
            and not arm_order_is_valid
        ):
            errors.append(
                f"{label}.arm_order must be null or a controlled arm order"
            )
        elif arm_order_is_valid and isinstance(participant_id, str):
            participant_orders[participant_id] = (arm_order[0], arm_order[1])

    completed_participant_ids = {
        participant_id
        for participant_id, status in participant_statuses.items()
        if status == "completed" and participant_id in participant_ids
    }
    if len(completed_participant_ids) < 3:
        errors.append(
            "completed participant cohort must contain at least three reviewers"
        )
    completed_orders = {
        participant_id: participant_orders[participant_id]
        for participant_id in completed_participant_ids
        if participant_id in participant_orders
    }
    order_counts = Counter(completed_orders.values())
    if completed_participant_ids:
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
    elif len(runs) < 2 * len(completed_participant_ids) * len(declared_cases):
        errors.append("runs do not account for every participant/case/arm")

    run_schema = schema["$defs"]["run"]
    build_schema = schema["$defs"]["veridocBuildProvenance"]
    approved_case_contracts: dict[str, dict[str, Any]] = {}
    structured_high_risk_targets_ready = False
    try:
        (
            approved_case_contracts,
            _structured_high_risk_targets_ready,
        ) = _load_approved_manifest_contract()
    except ApprovedManifestError as exc:
        errors.append(f"approved manifest contract is unavailable: {exc}")
    run_ids: set[str] = set()
    sealed_artifact_record_ids: set[str] = set()
    attempt_keys: set[tuple[Any, ...]] = set()
    attempt_numbers_by_group: dict[
        tuple[str, str, str], set[int]
    ] = defaultdict(set)
    attempt_timing_by_group: dict[
        tuple[str, str, str], dict[int, tuple[datetime, datetime]]
    ] = defaultdict(dict)
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
    withdrawal_markers: set[str] = set()
    all_started_at: list[datetime] = []

    for index, run in enumerate(runs):
        label = f"run[{index}]"
        _unknown_fields(run, set(run_schema["properties"]), "run", errors)
        _required_fields(run, set(run_schema["required"]), label, errors)
        if not isinstance(run, dict):
            continue

        run_id = run.get("run_id")
        run_id_is_valid = (
            isinstance(run_id, str) and RUN_ID_RE.fullmatch(run_id) is not None
        )
        if not run_id_is_valid:
            errors.append(f"{label}.run_id is invalid")
        elif run_id in run_ids:
            errors.append(f"duplicate run_id: {run_id}")
        else:
            run_ids.add(run_id)

        sealed_artifact_record_id = run.get("sealed_artifact_record_id")
        if (
            not isinstance(sealed_artifact_record_id, str)
            or SEALED_ARTIFACT_RECORD_ID_RE.fullmatch(
                sealed_artifact_record_id
            )
            is None
        ):
            errors.append(
                f"{label}.sealed_artifact_record_id must be an opaque "
                "SAR-prefixed UUIDv4"
            )
        elif sealed_artifact_record_id in sealed_artifact_record_ids:
            errors.append(
                "duplicate sealed_artifact_record_id: "
                f"{sealed_artifact_record_id}"
            )
        else:
            sealed_artifact_record_ids.add(sealed_artifact_record_id)
        sealed_artifact_sha256 = run.get("sealed_artifact_sha256")
        if (
            not isinstance(sealed_artifact_sha256, str)
            or SHA256_RE.fullmatch(sealed_artifact_sha256) is None
            or sealed_artifact_sha256 == "0" * 64
        ):
            errors.append(
                f"{label}.sealed_artifact_sha256 must be lowercase SHA-256"
            )
        sealed_artifact_kind = run.get("sealed_artifact_kind")
        if (
            not isinstance(sealed_artifact_kind, str)
            or sealed_artifact_kind
            not in {
                "output_artifact",
                "blocked_attempt_envelope",
            }
        ):
            errors.append(f"{label}.sealed_artifact_kind is invalid")

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
        approved_case = (
            approved_case_contracts.get(case_id)
            if isinstance(case_id, str)
            else None
        )
        for field in (
            "source_fixture_id",
            "source_fixture_path",
            "source_fixture_sha256",
        ):
            value = run.get(field)
            if not isinstance(value, str):
                errors.append(f"{label}.{field} must be a string")
        source_fixture_sha256 = run.get("source_fixture_sha256")
        if (
            isinstance(source_fixture_sha256, str)
            and SHA256_RE.fullmatch(source_fixture_sha256) is None
        ):
            errors.append(
                f"{label}.source_fixture_sha256 must be lowercase SHA-256"
            )
        if approved_case is not None:
            for field, contract_field in (
                ("source_fixture_id", "fixture_id"),
                ("source_fixture_path", "fixture_path"),
                ("source_fixture_sha256", "fixture_sha256"),
            ):
                if run.get(field) != approved_case[contract_field]:
                    errors.append(
                        f"{label}.{field} must match approved manifest "
                        f"value {approved_case[contract_field]!r}"
                    )
        arm = run.get("arm")
        arm_is_valid = isinstance(arm, str) and arm in {"manual", "veridoc"}
        if not arm_is_valid:
            errors.append(f"{label}.arm is invalid")
        build_provenance = run.get("veridoc_build_provenance")
        if arm == "manual":
            if build_provenance is not None:
                errors.append(
                    f"{label}.veridoc_build_provenance must be null "
                    "for manual arm"
                )
        elif arm == "veridoc":
            _unknown_fields(
                build_provenance,
                set(build_schema["properties"]),
                f"{label}.veridoc_build_provenance",
                errors,
            )
            _required_fields(
                build_provenance,
                set(build_schema["required"]),
                f"{label}.veridoc_build_provenance",
                errors,
            )
            if isinstance(build_provenance, dict):
                build_record_id = build_provenance.get("record_id")
                if (
                    not isinstance(build_record_id, str)
                    or BUILD_PROVENANCE_RECORD_ID_RE.fullmatch(build_record_id)
                    is None
                ):
                    errors.append(
                        f"{label}.veridoc_build_provenance.record_id must "
                        "be an opaque BLD-prefixed UUIDv4"
                    )
                for field, expected_value in (
                    ("product_commit", APPROVED_PRODUCT_COMMIT),
                    ("product_tree", APPROVED_PRODUCT_TREE),
                    ("checkout_state", "clean"),
                    (
                        "derivation_status",
                        "approved_source_tree_verified_execution_unattested",
                    ),
                    (
                        "execution_attestation_status",
                        "unverified_validation_only",
                    ),
                ):
                    if build_provenance.get(field) != expected_value:
                        errors.append(
                            f"{label}.veridoc_build_provenance.{field} "
                            f"must be {expected_value!r}"
                        )
                for field in (
                    "source_tree_listing_sha256",
                    "attestation_sha256",
                ):
                    value = build_provenance.get(field)
                    if (
                        not isinstance(value, str)
                        or SHA256_RE.fullmatch(value) is None
                        or value == "0" * 64
                    ):
                        errors.append(
                            f"{label}.veridoc_build_provenance.{field} "
                            "must be lowercase SHA-256"
                        )
                if (
                    build_provenance.get("source_tree_listing_sha256")
                    != APPROVED_SOURCE_TREE_LISTING_SHA256
                ):
                    errors.append(
                        f"{label}.veridoc_build_provenance."
                        "source_tree_listing_sha256 must match the "
                        "reproducibly derived approved source-tree listing "
                        f"{APPROVED_SOURCE_TREE_LISTING_SHA256}"
                    )
                attestation_payload = {
                    field: build_provenance.get(field)
                    for field in (
                        "record_id",
                        "product_commit",
                        "product_tree",
                        "checkout_state",
                        "derivation_status",
                        "source_tree_listing_sha256",
                        "execution_attestation_status",
                    )
                }
                expected_attestation_sha256 = _canonical_json_sha256(
                    attestation_payload
                )
                if (
                    build_provenance.get("attestation_sha256")
                    != expected_attestation_sha256
                ):
                    errors.append(
                        f"{label}.veridoc_build_provenance."
                        "attestation_sha256 must bind the canonical "
                        f"provenance record as {expected_attestation_sha256}"
                    )
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
            if run_id_is_valid:
                expected_run_id = _expected_run_id(
                    participant_id,
                    case_id,
                    arm,
                    attempt_number,
                )
                if run_id != expected_run_id:
                    errors.append(
                        f"{label}.run_id must equal generated opaque ID "
                        f"{expected_run_id}"
                    )

        for field in (
            "task_revision",
            "gold_answer_revision",
            "checklist_revision",
        ):
            revision = run.get(field)
            if not isinstance(revision, str) or REVISION_RE.fullmatch(revision) is None:
                errors.append(f"{label}.{field} is invalid")
            else:
                expected_revision = APPROVED_RUN_REVISIONS[field]
                if revision != expected_revision:
                    errors.append(
                        f"{label}.{field} must match approved protocol "
                        f"revision {expected_revision}"
                    )
                if case_is_declared:
                    revisions_by_case[(case_id, field)].add(revision)

        if run.get("gold_answer_hidden_until_ended_at") is not True:
            errors.append(
                f"{label}.gold_answer_hidden_until_ended_at must be true"
            )
        if run.get("gold_answer_compared_by_role") != "independent_assessor":
            errors.append(
                f"{label}.gold_answer_compared_by_role must be "
                "independent_assessor"
            )
        if (
            run.get("gold_answer_comparison_withheld_from_participant")
            is not True
        ):
            errors.append(
                f"{label}.gold_answer_comparison_withheld_from_participant "
                "must be true"
            )

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
                if (
                    case_is_declared
                    and attempt_is_valid
                    and elapsed_seconds > 0
                ):
                    attempt_timing_by_group[
                        (participant_id, case_id, arm)
                    ][attempt_number] = (started_at, ended_at)

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
            if sealed_artifact_kind != "output_artifact":
                errors.append(
                    f"{label}.sealed_artifact_kind must be output_artifact "
                    "for approved outcome"
                )
        elif outcome == "blocked":
            if not isinstance(blocker_code, str) or blocker_code not in {
                "source_unreadable",
                "tool_unavailable",
                "required_information_missing",
                "approval_unavailable",
                "other_controlled",
            }:
                errors.append(f"{label}.blocker_code is required for blocked outcome")
            if sealed_artifact_kind != "blocked_attempt_envelope":
                errors.append(
                    f"{label}.sealed_artifact_kind must be "
                    "blocked_attempt_envelope for blocked outcome"
                )
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
        if (
            excluded is True
            and exclusion_reason == "participant_withdrew"
            and participant_is_declared
        ):
            withdrawal_markers.add(participant_id)

        checklist_complete = run.get("checklist_complete")
        if not isinstance(checklist_complete, bool):
            errors.append(f"{label}.checklist_complete must be boolean")
        elif (
            excluded is False
            and not checklist_complete
        ):
            errors.append(
                f"{label}.checklist_complete must be true for every "
                "included outcome"
            )

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
            isinstance(case_id, str)
            and case_id in approved_case_contracts
            and _is_non_negative_int(expected)
            and expected
            != approved_case_contracts[case_id]["high_risk_expected_count"]
        ):
            approved_expected = approved_case_contracts[case_id][
                "high_risk_expected_count"
            ]
            errors.append(
                f"{label}.high_risk_expected_count must match approved "
                f"manifest count {approved_expected} for {case_id}"
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
    if (
        quality_approved_at is not None
        and all_started_at
        and quality_approved_at >= min(all_started_at)
    ):
        errors.append("quality approval must precede every timed run")
    for participant_id, practice_times in sorted(
        practice_completed_at_by_participant.items()
    ):
        participant_runs = times_by_participant[participant_id]
        if not participant_runs:
            continue
        earliest_run = min(start for start, _, _ in participant_runs)
        for field, completed_at in sorted(practice_times.items()):
            if completed_at >= earliest_run:
                errors.append(
                    f"{participant_id}.{field} must precede every timed run"
                )
    for participant_id, withdrawn_at in sorted(
        participant_withdrawn_at.items()
    ):
        for started_at, ended_at, run_label in times_by_participant[participant_id]:
            if started_at >= withdrawn_at or ended_at > withdrawn_at:
                errors.append(
                    f"{run_label} must not start at or end after "
                    f"{participant_id} withdrawal"
                )

    for group, attempt_numbers in sorted(attempt_numbers_by_group.items()):
        ordered_attempt_numbers = sorted(attempt_numbers)
        if any(
            attempt_number != expected
            for expected, attempt_number in enumerate(
                ordered_attempt_numbers,
                start=1,
            )
        ):
            errors.append(
                f"{group[0]}/{group[1]}/{group[2]} attempt_number values "
                "must be contiguous from 1"
            )
        timings = attempt_timing_by_group[group]
        ordered_timings = [
            (attempt_number, timings[attempt_number])
            for attempt_number in sorted(attempt_numbers)
            if attempt_number in timings
        ]
        for previous, current in zip(ordered_timings, ordered_timings[1:]):
            if current[1][0] < previous[1][1]:
                errors.append(
                    f"{group[0]}/{group[1]}/{group[2]} attempt timestamps "
                    "must follow attempt_number order"
                )
                break

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
        participation_status = participant_statuses.get(participant_id)
        if (
            participation_status == "completed"
            and participant_id in withdrawal_markers
        ):
            errors.append(
                f"{participant_id} completed participant cannot have a "
                "participant_withdrew exclusion"
            )
        if participation_status != "completed":
            for case_id in sorted(declared_cases):
                for arm in ("manual", "veridoc"):
                    included = included_by_pair[
                        (participant_id, case_id, arm)
                    ]
                    if len(included) > 1:
                        errors.append(
                            f"{participant_id}/{case_id}/{arm} must have at "
                            "most one non-excluded run after withdrawal"
                        )
            continue
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
                for field in (
                    "task_revision",
                    "gold_answer_revision",
                    "checklist_revision",
                ):
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
    _, structured_high_risk_targets_ready = _load_approved_manifest_contract()

    completed_participants = [
        item["participant_id"]
        for item in record["participants"]
        if item["participation_status"] == "completed"
    ]
    participant_statuses = {
        item["participant_id"]: item["participation_status"]
        for item in record["participants"]
    }
    # This protocol version intentionally has no trusted execution-attestation
    # path. Its only accepted status is unverified_validation_only.
    execution_attestation_ready = False
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

    for participant_id in sorted(participant_statuses):
        for case_id in sorted(record["case_ids"]):
            manual = by_pair.get((participant_id, case_id, "manual"))
            veridoc = by_pair.get((participant_id, case_id, "veridoc"))
            if manual is None or veridoc is None:
                continue
            eligible = (
                participant_statuses[participant_id] == "completed"
                and record["study_status"] == "validation_example"
                and manual["outcome"] == "approved"
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
        arm_metrics[arm] = {
            "high_risk_misses": sum(
                run["high_risk_miss_count"] for run in all_arm_runs
            ),
            "over_detections": sum(
                run["over_detection_count"] for run in all_arm_runs
            ),
            "approved_completions": sum(
                run["outcome"] == "approved" and run["checklist_complete"]
                for run in all_arm_runs
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
        for participant_id in completed_participants
        for case_id in record["case_ids"]
        for arm in ("manual", "veridoc")
    }
    recorded_groups = {
        (run["participant_id"], run["case_id"], run["arm"])
        for run in record["runs"]
    }
    all_required_runs_accounted = required_groups <= recorded_groups
    totals = {
        metric: sum(arm_metrics[arm][metric] for arm in ("manual", "veridoc"))
        for metric in (
            "high_risk_misses",
            "over_detections",
            "approved_completions",
            "blockers",
            "retry_runs",
            "excluded_runs",
        )
    }

    return {
        "study_id": record["study_id"],
        "study_status": record["study_status"],
        "target_product_commit": APPROVED_PRODUCT_COMMIT,
        "manifest_git_blob": APPROVED_MANIFEST_GIT_BLOB,
        "manifest_contract_sha256": APPROVED_MANIFEST_CONTRACT_SHA256,
        "structured_high_risk_targets_ready": (
            structured_high_risk_targets_ready
        ),
        "execution_attestation_ready": execution_attestation_ready,
        "required_runs": 2
        * len(completed_participants)
        * len(record["case_ids"]),
        "recorded_runs": len(record["runs"]),
        "excluded_runs": sum(run["excluded"] for run in record["runs"]),
        "retry_runs": sum(run["attempt_number"] > 1 for run in record["runs"]),
        "all_required_runs_accounted": all_required_runs_accounted,
        "eligible_pair_count": len(eligible_pair_results),
        "ineligible_pair_count": len(pair_results) - len(eligible_pair_results),
        "pair_results": pair_results,
        "paired_median_reduction_percent": paired_median,
        "arm_metrics": arm_metrics,
        "totals": totals,
        "efficiency_target_met": (
            record["study_status"] == "completed"
            and structured_high_risk_targets_ready
            and execution_attestation_ready
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
        record = _loads_json_strict(args.record.read_text(encoding="utf-8"))
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateKeyError,
    ) as exc:
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
