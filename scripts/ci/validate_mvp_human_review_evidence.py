#!/usr/bin/env python3
"""Validate and summarize P12G-13 human-review evidence using the stdlib only."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import math
import re
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "datasets"
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
PINNED_TASK_PACKAGE_PATH = "docs/mvp-human-review-timed-task-package.json"
PINNED_TASK_PACKAGE_SHA256 = (
    "55c15447c23b46cfee458a0bd13c3eac9916454b446a459dd2588412708aba47"
)
PINNED_CHECKLIST_PACKAGE_PATH = (
    "docs/mvp-human-review-completion-checklist-package.json"
)
PINNED_CHECKLIST_PACKAGE_SHA256 = (
    "15c40eebd279600abb8d0f0eaef8c6ecd595f77bf0b81cc0bbe5a7de01fc1b64"
)
PINNED_GOLD_PACKAGE_PATH = "datasets/mvp_human_review_gold_package_v1.json"
PINNED_GOLD_PACKAGE_SHA256 = (
    "d4dd34836d38eecc721af3d512caa978eaf9fa40cdf988d48e72ef8f1db44716"
)
APPROVED_PRACTICE_REVISION = "practice-phase12-v1"
APPROVED_PRACTICE_PACKAGE_PATH = (
    "docs/mvp-human-review-practice-package.json"
)
APPROVED_PRACTICE_PACKAGE_SHA256 = (
    "936f47e58b073eb18d02a3858f6a8e298f87f2e4242f9949dc4fbc9117fd6a82"
)
APPROVED_PRACTICE_TRAINING_DOCUMENTS = {
    "protocol": "docs/mvp-human-review-protocol.md",
    "execution_checklist": "docs/mvp-human-review-execution-checklist.md",
}
APPROVED_RUN_REVISIONS = {
    "task_revision": APPROVED_TASK_REVISION,
    "gold_answer_revision": APPROVED_GOLD_ANSWER_REVISION,
    "checklist_revision": APPROVED_CHECKLIST_REVISION,
}
ARMS = ("manual", "veridoc")
ALLOWED_SEALED_ARTIFACT_KINDS_BY_OUTCOME = {
    "approved": ("output_artifact",),
    "blocked": ("output_artifact", "blocked_attempt_envelope"),
}
ALLOWED_SEALED_ARTIFACT_KINDS = frozenset(
    artifact_kind
    for artifact_kinds in ALLOWED_SEALED_ARTIFACT_KINDS_BY_OUTCOME.values()
    for artifact_kind in artifact_kinds
)
SEALED_EVIDENCE_ENVELOPE_SCHEMA_VERSION = (
    "veridoc-mvp-sealed-evidence-envelope/v1"
)
STUDY_EVIDENCE_ENVELOPE_SCHEMA_VERSION = (
    "veridoc-mvp-study-evidence-envelope/v1"
)
ASSESSOR_ATTESTATION_SCHEMA_VERSION = (
    "veridoc-mvp-assessor-attestation/v1"
)
RUN_CLAIMS_EXCLUDED_FIELDS = frozenset(
    {
        "sealed_evidence_envelope",
        "sealed_artifact_sha256",
    }
)
STUDY_CLAIMS_EXCLUDED_FIELDS = frozenset(
    {
        "runs",
        "study_evidence_sha256",
        "study_evidence_envelope",
    }
)
ArtifactResolver = Callable[[dict[str, Any]], bytes]
SealedEvidenceResolver = Callable[[str], bytes]
StudyEvidenceResolver = Callable[[str], bytes]
AssessorAttestationResolver = Callable[[str], bytes]
UUID4_TOKEN_RE = (
    r"[0-9A-F]{8}-[0-9A-F]{4}-4[0-9A-F]{3}-"
    r"[89AB][0-9A-F]{3}-[0-9A-F]{12}"
)
PARTICIPANT_ID_RE = re.compile(rf"^P-{UUID4_TOKEN_RE}$")
STUDY_ID_RE = re.compile(rf"^HR-{UUID4_TOKEN_RE}$")
CONSENT_FORM_VERSION_RE = re.compile(rf"^CF-{UUID4_TOKEN_RE}$")
QUALITY_APPROVAL_RECORD_VERSION_RE = re.compile(rf"^QAR-{UUID4_TOKEN_RE}$")
RUN_ID_RE = re.compile(
    rf"^RUN-P-{UUID4_TOKEN_RE}-MVP-[A-Z0-9-]+-"
    r"(?:MANUAL|VERIDOC)-[1-9][0-9]*$"
)
SEALED_ARTIFACT_RECORD_ID_RE = re.compile(rf"^SAR-{UUID4_TOKEN_RE}$")
STUDY_EVIDENCE_RECORD_ID_RE = re.compile(rf"^HSR-{UUID4_TOKEN_RE}$")
ASSESSOR_ID_RE = re.compile(rf"^A-{UUID4_TOKEN_RE}$")
ASSESSOR_ATTESTATION_RECORD_ID_RE = re.compile(rf"^AAR-{UUID4_TOKEN_RE}$")
SEALED_ARTIFACT_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
BUILD_PROVENANCE_RECORD_ID_RE = re.compile(rf"^BLD-{UUID4_TOKEN_RE}$")
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RFC3339_UTC_RE = re.compile(
    r"^(?P<whole>[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?(?:Z|\+00:00)$"
)
NANOSECONDS_PER_SECOND = 1_000_000_000


class StrictJsonError(ValueError):
    """Raised when JSON has no deterministic representation."""


class DuplicateKeyError(StrictJsonError):
    """Raised when a JSON object contains an ambiguous duplicate key."""


class ApprovedManifestError(RuntimeError):
    """Raised when the approved manifest contract cannot be reconstructed."""


class PinnedGoldPackageError(RuntimeError):
    """Raised when the pinned validation gold package cannot be verified."""


class PinnedTaskPackageError(RuntimeError):
    """Raised when the pinned timed-task package cannot be verified."""


class PinnedChecklistPackageError(RuntimeError):
    """Raised when the pinned completion-checklist package cannot be verified."""


@dataclass(frozen=True, order=True)
class ExactUtcTimestamp:
    """UTC timestamp whose ordering preserves all nine RFC 3339 digits."""

    epoch_nanoseconds: int
    source: str = field(compare=False)

    @classmethod
    def parse(cls, value: str) -> ExactUtcTimestamp:
        match = RFC3339_UTC_RE.fullmatch(value)
        if match is None:
            raise ValueError("not a UTC RFC 3339 timestamp")
        whole = datetime.strptime(
            match.group("whole"),
            "%Y-%m-%dT%H:%M:%S",
        ).replace(tzinfo=timezone.utc)
        fraction = (match.group("fraction") or "").ljust(9, "0")
        return cls(
            epoch_nanoseconds=(
                calendar.timegm(whole.utctimetuple())
                * NANOSECONDS_PER_SECOND
                + int(fraction or "0")
            ),
            source=value,
        )


@dataclass(frozen=True)
class RunTiming:
    """Separate retained timing evidence from measurement-eligible timing."""

    started_at: ExactUtcTimestamp
    ended_at: ExactUtcTimestamp
    excluded_pause_seconds: int | None
    is_invalid_timing_exclusion: bool

    @property
    def elapsed_nanoseconds(self) -> int:
        return (
            self.ended_at.epoch_nanoseconds
            - self.started_at.epoch_nanoseconds
        )

    @property
    def is_interval_usable(self) -> bool:
        return (
            not self.is_invalid_timing_exclusion
            and self.elapsed_nanoseconds > 0
        )

    def correction_seconds(self) -> float:
        pause_seconds = self.excluded_pause_seconds
        if pause_seconds is None:
            raise ValueError("excluded pause is not a non-negative integer")
        return (
            self.elapsed_nanoseconds
            - pause_seconds * NANOSECONDS_PER_SECOND
        ) / NANOSECONDS_PER_SECOND


def _reject_duplicate_object_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateKeyError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _reject_non_json_constant(value: str) -> Any:
    raise StrictJsonError(f"invalid JSON numeric constant: {value}")


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise StrictJsonError("JSON number is outside the finite runtime range")
    return parsed


def _require_unicode_scalars(value: Any) -> None:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise StrictJsonError(
                "JSON strings must contain only Unicode scalar values"
            ) from exc
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _require_unicode_scalars(key)
            _require_unicode_scalars(item)
        return
    if isinstance(value, list):
        for item in value:
            _require_unicode_scalars(item)


def _loads_json_strict(text: str) -> Any:
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_non_json_constant,
            parse_float=_parse_finite_json_float,
        )
        _require_unicode_scalars(value)
        return value
    except StrictJsonError:
        raise
    except (RecursionError, ValueError) as exc:
        # JSONDecodeError and Python's integer digit-limit failure both derive
        # from ValueError; excessive nesting raises RecursionError. Normalize
        # decoder failures at this single input boundary.
        raise StrictJsonError(str(exc)) from exc


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


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def build_run_claims(run: dict[str, Any]) -> dict[str, Any]:
    """Return every non-recursive run field covered by the audit seal."""

    return {
        field: run[field]
        for field in sorted(run)
        if field not in RUN_CLAIMS_EXCLUDED_FIELDS
    }


def build_sealed_evidence_envelope(run: dict[str, Any]) -> dict[str, Any]:
    """Build the canonical audit envelope shared by every run."""

    return {
        "schema_version": SEALED_EVIDENCE_ENVELOPE_SCHEMA_VERSION,
        "run_claims_sha256": _canonical_json_sha256(build_run_claims(run)),
    }


def build_study_claims(record: dict[str, Any]) -> dict[str, Any]:
    """Return final study and participant fields covered by the study seal."""

    return {
        field: record[field]
        for field in sorted(record)
        if field not in STUDY_CLAIMS_EXCLUDED_FIELDS
    }


def build_study_evidence_envelope(record: dict[str, Any]) -> dict[str, Any]:
    """Build the canonical envelope for study-level precondition evidence."""

    return {
        "schema_version": STUDY_EVIDENCE_ENVELOPE_SCHEMA_VERSION,
        "study_claims_sha256": _canonical_json_sha256(build_study_claims(record)),
    }


def build_assessor_attestation(run: dict[str, Any]) -> dict[str, Any]:
    """Build the independently retained assessor identity attestation."""

    return {
        "schema_version": ASSESSOR_ATTESTATION_SCHEMA_VERSION,
        "attestation_record_id": run.get("assessor_attestation_record_id"),
        "run_id": run.get("run_id"),
        "participant_id": run.get("participant_id"),
        "assessor_id": run.get("independent_assessor_id"),
        "assessor_role": run.get("gold_answer_compared_by_role"),
        "assessor_is_participant": False,
        "assessed_at": run.get("assessment_completed_at"),
        "comparison_withheld_from_participant": run.get(
            "gold_answer_comparison_withheld_from_participant"
        ),
    }


def _sealed_artifact_relative_path(
    run: dict[str, Any],
    label: str,
    errors: list[str],
) -> Path | None:
    artifact_path = run.get("sealed_artifact_path")
    artifact_record_id = run.get("sealed_artifact_record_id")
    expected_artifact_path = (
        f"sealed_artifacts/{artifact_record_id}.bin"
        if isinstance(artifact_record_id, str)
        else None
    )
    if not isinstance(artifact_path, str) or not artifact_path:
        errors.append(
            f"{label}.sealed_artifact_path must be a non-empty relative path "
            "for output_artifact"
        )
        return None
    if artifact_path != expected_artifact_path:
        errors.append(
            f"{label}.sealed_artifact_path must be derived from "
            "sealed_artifact_record_id"
        )
        return None

    path_parts = artifact_path.split("/")
    relative_path = Path(artifact_path)
    if (
        relative_path.is_absolute()
        or any(
            part in {"", ".", ".."}
            or SEALED_ARTIFACT_PATH_SEGMENT_RE.fullmatch(part) is None
            for part in path_parts
        )
    ):
        errors.append(
            f"{label}.sealed_artifact_path must remain within artifact_root"
        )
        return None
    return relative_path


def _read_sealed_artifact(
    relative_path: Path,
    artifact_root: Path,
    label: str,
    errors: list[str],
) -> bytes | None:

    try:
        resolved_path = (artifact_root / relative_path).resolve(strict=True)
        resolved_path.relative_to(artifact_root)
    except (OSError, ValueError):
        errors.append(
            f"{label}.sealed_artifact_path cannot be resolved within "
            "artifact_root"
        )
        return None

    if not resolved_path.is_file():
        errors.append(f"{label}.sealed_artifact_path must resolve to a file")
        return None

    try:
        return resolved_path.read_bytes()
    except OSError:
        errors.append(f"{label}.sealed_artifact_path cannot be read")
        return None


def _read_retained_record(
    record_id: Any,
    *,
    record_id_pattern: re.Pattern[str],
    directory: str,
    record_kind: str,
    artifact_root: Path,
    label: str,
    errors: list[str],
) -> bytes | None:
    if (
        not isinstance(record_id, str)
        or record_id_pattern.fullmatch(record_id) is None
    ):
        return None
    relative_path = Path(directory) / f"{record_id}.json"
    try:
        resolved_path = (artifact_root / relative_path).resolve(strict=True)
        resolved_path.relative_to(artifact_root)
    except (OSError, ValueError):
        errors.append(
            f"{label} cannot resolve an independently retained {record_kind} "
            "within artifact_root"
        )
        return None
    if not resolved_path.is_file():
        errors.append(
            f"{label} must resolve to an independently retained {record_kind} file"
        )
        return None
    try:
        return resolved_path.read_bytes()
    except OSError:
        errors.append(f"{label} resolved {record_kind} cannot be read")
        return None


def _read_sealed_evidence_record(
    run: dict[str, Any],
    artifact_root: Path,
    label: str,
    errors: list[str],
) -> bytes | None:
    return _read_retained_record(
        run.get("sealed_artifact_record_id"),
        record_id_pattern=SEALED_ARTIFACT_RECORD_ID_RE,
        directory="sealed_records",
        record_kind="sealed evidence record",
        artifact_root=artifact_root,
        label=f"{label}.sealed_artifact_record_id",
        errors=errors,
    )


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
    except (UnicodeDecodeError, StrictJsonError) as exc:
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
    except (UnicodeDecodeError, StrictJsonError) as exc:
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
        conversion_mode = case.get("conversion_mode")
        expected_artifacts = case.get("expected_artifacts")
        review_focus = case.get("review_focus")
        targets = case.get("expected_high_risk_targets")
        if not isinstance(case_id, str) or not isinstance(fixture_id, str):
            raise ApprovedManifestError("approved case identity is incomplete")
        if (
            not isinstance(conversion_mode, str)
            or not isinstance(expected_artifacts, list)
            or len(expected_artifacts) != 1
            or not isinstance(expected_artifacts[0], dict)
            or not isinstance(expected_artifacts[0].get("type"), str)
            or not isinstance(expected_artifacts[0].get("expectations"), list)
            or not expected_artifacts[0]["expectations"]
            or any(
                not isinstance(expectation, str) or not expectation.strip()
                for expectation in expected_artifacts[0]["expectations"]
            )
            or not isinstance(review_focus, list)
            or not review_focus
            or any(
                not isinstance(item, str) or not item.strip()
                for item in review_focus
            )
        ):
            raise ApprovedManifestError(
                f"approved timed-task case contract is incomplete for {case_id}"
            )
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
            "conversion_mode": conversion_mode,
            "target_artifact_type": expected_artifacts[0]["type"],
            "expectations": expected_artifacts[0]["expectations"],
            "review_focus": review_focus,
            "high_risk_expected_count": len(targets),
        }
    if set(case_contracts) != EXPECTED_CASE_IDS:
        raise ApprovedManifestError(
            "approved manifest case set does not match Phase 12 scope"
        )
    return case_contracts, structured_high_risk_targets_ready


def _require_non_empty_strings(value: Any, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise PinnedTaskPackageError(
            f"pinned timed-task package {label} must contain non-empty strings"
        )
    return value


@lru_cache(maxsize=1)
def _load_pinned_task_package_contract() -> tuple[
    dict[str, dict[str, Any]], dict[str, str]
]:
    try:
        package_bytes = (REPO_ROOT / PINNED_TASK_PACKAGE_PATH).read_bytes()
    except OSError as exc:
        raise PinnedTaskPackageError(
            "pinned timed-task package cannot be read"
        ) from exc
    actual_sha256 = hashlib.sha256(package_bytes).hexdigest()
    if actual_sha256 != PINNED_TASK_PACKAGE_SHA256:
        raise PinnedTaskPackageError(
            "pinned timed-task package SHA-256 mismatch: "
            f"expected {PINNED_TASK_PACKAGE_SHA256}, got {actual_sha256}"
        )
    try:
        package = _loads_json_strict(package_bytes.decode("utf-8"))
    except (UnicodeDecodeError, StrictJsonError) as exc:
        raise PinnedTaskPackageError(
            "pinned timed-task package is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(package, dict):
        raise PinnedTaskPackageError(
            "pinned timed-task package must be an object"
        )
    expected_top_fields = {
        "schema_version",
        "task_revision",
        "decision_revision",
        "approved_efficiency_scope_sha256",
        "manifest_contract_sha256",
        "contract_status",
        "common_instructions",
        "arm_contracts",
        "cases",
    }
    if set(package) != expected_top_fields:
        raise PinnedTaskPackageError(
            "pinned timed-task package fields do not match the closed contract"
        )
    for field, expected in (
        (
            "schema_version",
            "veridoc-mvp-human-review-timed-task-package/v1",
        ),
        ("task_revision", APPROVED_TASK_REVISION),
        ("decision_revision", "p12g-02-v1"),
        (
            "approved_efficiency_scope_sha256",
            "3d9d05671895ec8d6e8b14f44b6a8dd7f99aa17b7b65871b78fb56a49966b6fb",
        ),
        ("manifest_contract_sha256", APPROVED_MANIFEST_CONTRACT_SHA256),
        ("contract_status", "protocol_pinned"),
    ):
        if package.get(field) != expected:
            raise PinnedTaskPackageError(
                f"pinned timed-task package {field} must be {expected!r}"
            )

    common_instructions = package.get("common_instructions")
    expected_common_fields = {
        "objective",
        "start_condition",
        "stop_condition",
        "prohibited_assistance",
    }
    if (
        not isinstance(common_instructions, dict)
        or set(common_instructions) != expected_common_fields
    ):
        raise PinnedTaskPackageError(
            "pinned timed-task package common instructions are invalid"
        )
    for field in ("objective", "start_condition", "stop_condition"):
        value = common_instructions.get(field)
        if not isinstance(value, str) or not value.strip():
            raise PinnedTaskPackageError(
                f"pinned timed-task package common {field} is invalid"
            )
    _require_non_empty_strings(
        common_instructions.get("prohibited_assistance"),
        "common prohibited_assistance",
    )

    arm_contracts = package.get("arm_contracts")
    if not isinstance(arm_contracts, dict) or set(arm_contracts) != {
        "manual",
        "veridoc",
    }:
        raise PinnedTaskPackageError(
            "pinned timed-task package arm contracts are invalid"
        )
    arm_sha256: dict[str, str] = {}
    for arm in ("manual", "veridoc"):
        contract = arm_contracts.get(arm)
        if not isinstance(contract, dict) or set(contract) != {
            "allowed_tools",
            "prohibited_tools",
        }:
            raise PinnedTaskPackageError(
                f"pinned timed-task package {arm} contract is invalid"
            )
        _require_non_empty_strings(
            contract.get("allowed_tools"),
            f"{arm} allowed_tools",
        )
        _require_non_empty_strings(
            contract.get("prohibited_tools"),
            f"{arm} prohibited_tools",
        )
        arm_sha256[arm] = _canonical_json_sha256(contract)

    cases = package.get("cases")
    if not isinstance(cases, list):
        raise PinnedTaskPackageError(
            "pinned timed-task package cases must be an array"
        )
    expected_case_fields = {
        "case_id",
        "fixture_id",
        "conversion_mode",
        "target_artifact_type",
        "expectations",
        "review_focus",
    }
    case_contracts: dict[str, dict[str, Any]] = {}
    for case in cases:
        if not isinstance(case, dict) or set(case) != expected_case_fields:
            raise PinnedTaskPackageError(
                "pinned timed-task package cases must match the closed contract"
            )
        case_id = case.get("case_id")
        fixture_id = case.get("fixture_id")
        conversion_mode = case.get("conversion_mode")
        target_artifact_type = case.get("target_artifact_type")
        if (
            not isinstance(case_id, str)
            or case_id in case_contracts
            or not isinstance(fixture_id, str)
            or not isinstance(conversion_mode, str)
            or not isinstance(target_artifact_type, str)
        ):
            raise PinnedTaskPackageError(
                "pinned timed-task package case identity is invalid"
            )
        _require_non_empty_strings(
            case.get("expectations"),
            f"{case_id} expectations",
        )
        _require_non_empty_strings(
            case.get("review_focus"),
            f"{case_id} review_focus",
        )
        case_contracts[case_id] = {
            "fixture_id": fixture_id,
            "conversion_mode": conversion_mode,
            "target_artifact_type": target_artifact_type,
            "expectations": case["expectations"],
            "review_focus": case["review_focus"],
            "task_case_sha256": _canonical_json_sha256(case),
        }
    if set(case_contracts) != EXPECTED_CASE_IDS:
        raise PinnedTaskPackageError(
            "pinned timed-task package case set does not match Phase 12 scope"
        )
    return case_contracts, arm_sha256


@lru_cache(maxsize=1)
def _load_pinned_checklist_package_contract() -> dict[
    str, dict[str, str]
]:
    try:
        package_bytes = (
            REPO_ROOT / PINNED_CHECKLIST_PACKAGE_PATH
        ).read_bytes()
    except OSError as exc:
        raise PinnedChecklistPackageError(
            "pinned completion-checklist package cannot be read"
        ) from exc
    actual_sha256 = hashlib.sha256(package_bytes).hexdigest()
    if actual_sha256 != PINNED_CHECKLIST_PACKAGE_SHA256:
        raise PinnedChecklistPackageError(
            "pinned completion-checklist package SHA-256 mismatch: "
            f"expected {PINNED_CHECKLIST_PACKAGE_SHA256}, got {actual_sha256}"
        )
    try:
        package = _loads_json_strict(package_bytes.decode("utf-8"))
    except (UnicodeDecodeError, StrictJsonError) as exc:
        raise PinnedChecklistPackageError(
            "pinned completion-checklist package is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(package, dict):
        raise PinnedChecklistPackageError(
            "pinned completion-checklist package must be an object"
        )
    expected_top_fields = {
        "schema_version",
        "checklist_revision",
        "decision_revision",
        "approved_efficiency_scope_sha256",
        "task_package_path",
        "task_package_sha256",
        "contract_status",
        "shared_instructions",
        "items",
        "cases",
    }
    if set(package) != expected_top_fields:
        raise PinnedChecklistPackageError(
            "pinned completion-checklist package fields do not match "
            "the closed contract"
        )
    for field, expected in (
        (
            "schema_version",
            "veridoc-mvp-human-review-completion-checklist-package/v1",
        ),
        ("checklist_revision", APPROVED_CHECKLIST_REVISION),
        ("decision_revision", "p12g-02-v1"),
        (
            "approved_efficiency_scope_sha256",
            "3d9d05671895ec8d6e8b14f44b6a8dd7f99aa17b7b65871b78fb56a49966b6fb",
        ),
        ("task_package_path", PINNED_TASK_PACKAGE_PATH),
        ("task_package_sha256", PINNED_TASK_PACKAGE_SHA256),
        ("contract_status", "protocol_pinned"),
    ):
        if package.get(field) != expected:
            raise PinnedChecklistPackageError(
                f"pinned completion-checklist package {field} "
                f"must be {expected!r}"
            )

    shared_instructions = package.get("shared_instructions")
    expected_instruction_fields = {
        "arm_application",
        "completion_rule",
        "blocked_rule",
        "deviation_rule",
        "gold_boundary",
    }
    if (
        not isinstance(shared_instructions, dict)
        or set(shared_instructions) != expected_instruction_fields
        or any(
            not isinstance(shared_instructions[field], str)
            or not shared_instructions[field].strip()
            for field in expected_instruction_fields
        )
    ):
        raise PinnedChecklistPackageError(
            "pinned completion-checklist shared instructions are invalid"
        )

    items = package.get("items")
    expected_item_ids = [f"CHK-{number:02d}" for number in range(1, 9)]
    if (
        not isinstance(items, list)
        or [
            item.get("item_id")
            for item in items
            if isinstance(item, dict)
        ]
        != expected_item_ids
        or any(
            not isinstance(item, dict)
            or set(item) != {"item_id", "requirement"}
            or not isinstance(item.get("requirement"), str)
            or not item["requirement"].strip()
            for item in items
        )
    ):
        raise PinnedChecklistPackageError(
            "pinned completion-checklist items are invalid"
        )

    cases = package.get("cases")
    if not isinstance(cases, list):
        raise PinnedChecklistPackageError(
            "pinned completion-checklist cases must be an array"
        )
    case_contracts: dict[str, dict[str, str]] = {}
    for case in cases:
        if (
            not isinstance(case, dict)
            or set(case) != {"case_id", "task_case_sha256"}
        ):
            raise PinnedChecklistPackageError(
                "pinned completion-checklist cases must match "
                "the closed contract"
            )
        case_id = case.get("case_id")
        task_case_sha256 = case.get("task_case_sha256")
        if (
            not isinstance(case_id, str)
            or case_id in case_contracts
            or not isinstance(task_case_sha256, str)
            or SHA256_RE.fullmatch(task_case_sha256) is None
        ):
            raise PinnedChecklistPackageError(
                "pinned completion-checklist case identity is invalid"
            )
        case_contracts[case_id] = {
            "task_case_sha256": task_case_sha256,
            "checklist_case_sha256": _canonical_json_sha256(
                {
                    "shared_instructions": shared_instructions,
                    "items": items,
                    "case": case,
                }
            ),
        }
    if set(case_contracts) != EXPECTED_CASE_IDS:
        raise PinnedChecklistPackageError(
            "pinned completion-checklist case set does not match "
            "Phase 12 scope"
        )
    return case_contracts


@lru_cache(maxsize=1)
def _load_pinned_gold_package_contract() -> tuple[
    dict[str, dict[str, Any]], bool
]:
    try:
        package_bytes = (REPO_ROOT / PINNED_GOLD_PACKAGE_PATH).read_bytes()
    except OSError as exc:
        raise PinnedGoldPackageError(
            "pinned gold package cannot be read"
        ) from exc
    actual_sha256 = hashlib.sha256(package_bytes).hexdigest()
    if actual_sha256 != PINNED_GOLD_PACKAGE_SHA256:
        raise PinnedGoldPackageError(
            "pinned gold package SHA-256 mismatch: "
            f"expected {PINNED_GOLD_PACKAGE_SHA256}, got {actual_sha256}"
        )
    try:
        package = _loads_json_strict(package_bytes.decode("utf-8"))
    except (UnicodeDecodeError, StrictJsonError) as exc:
        raise PinnedGoldPackageError(
            "pinned gold package is not strict UTF-8 JSON"
        ) from exc
    if not isinstance(package, dict):
        raise PinnedGoldPackageError("pinned gold package must be an object")
    expected_top_fields = {
        "schema_version",
        "gold_answer_revision",
        "source_manifest_path",
        "source_manifest_revision",
        "approval_status",
        "cases",
    }
    if set(package) != expected_top_fields:
        raise PinnedGoldPackageError(
            "pinned gold package fields do not match the closed contract"
        )
    for field, expected in (
        (
            "schema_version",
            "veridoc-mvp-human-review-gold-package/v1",
        ),
        ("gold_answer_revision", APPROVED_GOLD_ANSWER_REVISION),
        ("source_manifest_path", APPROVED_MANIFEST_PATH),
        ("source_manifest_revision", "phase12-mvp-v1"),
    ):
        if package.get(field) != expected:
            raise PinnedGoldPackageError(
                f"pinned gold package {field} must be {expected!r}"
            )
    approval_status = package.get("approval_status")
    if approval_status != "unapproved_validation_only":
        raise PinnedGoldPackageError(
            "pinned gold package approval_status must be "
            "'unapproved_validation_only'"
        )
    cases = package.get("cases")
    if not isinstance(cases, list):
        raise PinnedGoldPackageError(
            "pinned gold package cases must be an array"
        )
    case_contracts: dict[str, dict[str, Any]] = {}
    expected_case_fields = {
        "case_id",
        "conversion_mode",
        "expected_artifact_types",
        "expected_high_risk_targets",
    }
    for case in cases:
        if not isinstance(case, dict) or set(case) != expected_case_fields:
            raise PinnedGoldPackageError(
                "pinned gold package cases must match the closed contract"
            )
        case_id = case.get("case_id")
        conversion_mode = case.get("conversion_mode")
        artifact_types = case.get("expected_artifact_types")
        targets = case.get("expected_high_risk_targets")
        if (
            not isinstance(case_id, str)
            or case_id in case_contracts
            or not isinstance(conversion_mode, str)
            or not isinstance(artifact_types, list)
            or len(artifact_types) != 1
            or not isinstance(artifact_types[0], str)
            or not isinstance(targets, list)
            or any(not isinstance(target, dict) for target in targets)
        ):
            raise PinnedGoldPackageError(
                "pinned gold package case content is invalid"
            )
        case_contracts[case_id] = {
            "conversion_mode": conversion_mode,
            "target_artifact_type": artifact_types[0],
            "high_risk_expected_count": len(targets),
            "gold_case_sha256": _canonical_json_sha256(case),
        }
    if set(case_contracts) != EXPECTED_CASE_IDS:
        raise PinnedGoldPackageError(
            "pinned gold package case set does not match Phase 12 scope"
        )
    return case_contracts, approval_status == "approved"


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


def _parse_utc(
    value: Any,
    label: str,
    errors: list[str],
) -> ExactUtcTimestamp | None:
    if (
        not isinstance(value, str)
        or RFC3339_UTC_RE.fullmatch(value) is None
    ):
        errors.append(f"{label} must be a UTC RFC 3339 timestamp")
        return None
    try:
        return ExactUtcTimestamp.parse(value)
    except ValueError:
        errors.append(f"{label} must be a UTC RFC 3339 timestamp")
        return None


def _validate_run_timing(
    run: dict[str, Any],
    label: str,
    errors: list[str],
) -> RunTiming | None:
    """Validate timing without rejecting an honestly retained timer failure."""

    started_at = _parse_utc(run.get("started_at"), f"{label}.started_at", errors)
    ended_at = _parse_utc(run.get("ended_at"), f"{label}.ended_at", errors)
    pause_seconds = run.get("excluded_pause_seconds")
    if not _is_non_negative_int(pause_seconds):
        errors.append(
            f"{label}.excluded_pause_seconds must be a non-negative integer"
        )
        normalized_pause = None
    else:
        normalized_pause = pause_seconds
    if started_at is None or ended_at is None:
        return None

    timing = RunTiming(
        started_at=started_at,
        ended_at=ended_at,
        excluded_pause_seconds=normalized_pause,
        is_invalid_timing_exclusion=(
            run.get("excluded") is True
            and run.get("exclusion_reason_code") == "invalid_timing"
        ),
    )
    if timing.is_invalid_timing_exclusion:
        return timing
    if timing.elapsed_nanoseconds <= 0:
        errors.append("ended_at must be after started_at")
    elif (
        normalized_pause is not None
        and normalized_pause * NANOSECONDS_PER_SECOND
        >= timing.elapsed_nanoseconds
    ):
        errors.append("excluded_pause_seconds must be less than elapsed time")
    return timing


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


def validate_record(
    record: Any,
    *,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    artifact_resolver: ArtifactResolver | None = None,
    sealed_evidence_resolver: SealedEvidenceResolver | None = None,
    study_evidence_resolver: StudyEvidenceResolver | None = None,
    assessor_attestation_resolver: AssessorAttestationResolver | None = None,
) -> list[str]:
    """Return deterministic human-readable validation errors.

    Every retained-record resolver is a trust boundary: it receives only an
    opaque record ID and must read an independently retained immutable record.
    """

    errors: list[str] = []
    schema = _load_schema()
    artifact_root = artifact_root.resolve()
    if not isinstance(record, dict):
        return ["record must be an object"]
    try:
        _require_unicode_scalars(record)
    except StrictJsonError as exc:
        return [str(exc)]
    except RecursionError:
        return ["record nesting exceeds validator limit"]

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
        ("practice_revision", APPROVED_PRACTICE_REVISION),
        ("practice_package_path", APPROVED_PRACTICE_PACKAGE_PATH),
        ("practice_package_sha256", APPROVED_PRACTICE_PACKAGE_SHA256),
    ):
        _validate_constant(record, field, expected, errors)

    try:
        practice_package_bytes = (
            REPO_ROOT / APPROVED_PRACTICE_PACKAGE_PATH
        ).read_bytes()
    except OSError:
        errors.append("approved practice package cannot be read")
    else:
        practice_package_sha256 = hashlib.sha256(
            practice_package_bytes
        ).hexdigest()
        if practice_package_sha256 != APPROVED_PRACTICE_PACKAGE_SHA256:
            errors.append(
                "approved practice package content must match "
                f"{APPROVED_PRACTICE_PACKAGE_SHA256}"
            )
        try:
            practice_package = _loads_json_strict(
                practice_package_bytes.decode("utf-8")
            )
        except (UnicodeDecodeError, StrictJsonError):
            errors.append("approved practice package must be strict UTF-8 JSON")
        else:
            if not isinstance(practice_package, dict):
                errors.append("approved practice package must be an object")
            else:
                for field, expected in (
                    (
                        "schema_version",
                        "veridoc-mvp-human-review-practice-package/v1",
                    ),
                    ("practice_revision", APPROVED_PRACTICE_REVISION),
                    ("decision_revision", "p12g-02-v1"),
                ):
                    if practice_package.get(field) != expected:
                        errors.append(
                            f"approved practice package {field} must be "
                            f"{expected!r}"
                        )
                training_material = practice_package.get(
                    "training_material"
                )
                if not isinstance(training_material, dict):
                    errors.append(
                        "approved practice package training_material must "
                        "be an object"
                    )
                    training_material = {}
                for document, expected_path in (
                    APPROVED_PRACTICE_TRAINING_DOCUMENTS.items()
                ):
                    label = (
                        "approved practice package training_material."
                        f"{document}"
                    )
                    document_path = training_material.get(
                        f"{document}_path"
                    )
                    document_sha256 = training_material.get(
                        f"{document}_sha256"
                    )
                    if document_path != expected_path:
                        errors.append(
                            f"{label}_path must be {expected_path!r}"
                        )
                        continue
                    if (
                        not isinstance(document_sha256, str)
                        or SHA256_RE.fullmatch(document_sha256) is None
                    ):
                        errors.append(f"{label}_sha256 is invalid")
                        continue
                    try:
                        actual_document_sha256 = hashlib.sha256(
                            (REPO_ROOT / document_path).read_bytes()
                        ).hexdigest()
                    except OSError:
                        errors.append(f"{label} cannot be read")
                    else:
                        if actual_document_sha256 != document_sha256:
                            errors.append(
                                f"{label} must match declared SHA-256"
                            )
                instructions = training_material.get("instructions")
                if (
                    not isinstance(instructions, list)
                    or not instructions
                    or any(
                        not isinstance(instruction, str)
                        or not instruction.strip()
                        for instruction in instructions
                    )
                ):
                    errors.append(
                        "approved practice package training instructions "
                        "must be non-empty strings"
                    )
                for arm in ("manual", "veridoc"):
                    practice = practice_package.get(f"{arm}_practice")
                    label = f"approved practice package {arm}_practice"
                    if not isinstance(practice, dict):
                        errors.append(f"{label} must be an object")
                        continue
                    fixture_path = practice.get("source_fixture_path")
                    fixture_sha256 = practice.get("source_fixture_sha256")
                    if (
                        not isinstance(fixture_path, str)
                        or not fixture_path.startswith("datasets/fixtures/")
                    ):
                        errors.append(
                            f"{label}.source_fixture_path is invalid"
                        )
                        continue
                    if (
                        not isinstance(fixture_sha256, str)
                        or SHA256_RE.fullmatch(fixture_sha256) is None
                    ):
                        errors.append(
                            f"{label}.source_fixture_sha256 is invalid"
                        )
                        continue
                    try:
                        actual_fixture_sha256 = hashlib.sha256(
                            (REPO_ROOT / fixture_path).read_bytes()
                        ).hexdigest()
                    except OSError:
                        errors.append(f"{label} fixture cannot be read")
                    else:
                        if actual_fixture_sha256 != fixture_sha256:
                            errors.append(
                                f"{label} fixture must match declared SHA-256"
                            )

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
    approved_at: ExactUtcTimestamp | None = None
    approved_consent_form_version: str | None = None
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
            or CONSENT_FORM_VERSION_RE.fullmatch(consent_version) is None
        ):
            errors.append(
                "consent_approval.consent_form_version must be an opaque "
                "CF-prefixed UUIDv4"
            )
        else:
            approved_consent_form_version = consent_version
        if consent.get("direct_identifiers_stored") is not False:
            errors.append("consent_approval.direct_identifiers_stored must be false")

    quality_approval = record.get("quality_approval")
    quality_approved_at: ExactUtcTimestamp | None = None
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
            or QUALITY_APPROVAL_RECORD_VERSION_RE.fullmatch(
                external_record_version
            )
            is None
        ):
            errors.append(
                "quality_approval.external_record_version must be an opaque "
                "QAR-prefixed UUIDv4"
            )

    participants = record.get("participants")
    participant_ids: set[str] = set()
    participant_statuses: dict[str, str] = {}
    participant_orders: dict[str, tuple[str, str]] = {}
    participant_withdrawn_at: dict[str, ExactUtcTimestamp] = {}
    participant_consented_at: dict[str, ExactUtcTimestamp] = {}
    practice_completed_by_participant: dict[str, set[str]] = defaultdict(set)
    practice_completed_at_by_participant: dict[
        str, dict[str, ExactUtcTimestamp]
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
        if participant.get("consent_status") != "consented":
            errors.append(f"{label}.consent_status must be consented")
        consented_at = _parse_utc(
            participant.get("consented_at"),
            f"{label}.consented_at",
            errors,
        )
        if consented_at is not None and isinstance(participant_id, str):
            participant_consented_at[participant_id] = consented_at
        participant_consent_version = participant.get("consent_form_version")
        if (
            not isinstance(participant_consent_version, str)
            or CONSENT_FORM_VERSION_RE.fullmatch(
                participant_consent_version
            )
            is None
        ):
            errors.append(
                f"{label}.consent_form_version must be an opaque "
                "CF-prefixed UUIDv4"
            )
        elif (
            approved_consent_form_version is not None
            and participant_consent_version != approved_consent_form_version
        ):
            errors.append(
                f"{label}.consent_form_version must match consent_approval"
            )
        if participant.get("relevant_experience_attested") is not True:
            errors.append(f"{label}.relevant_experience_attested must be true")
        for arm in ("manual", "veridoc"):
            completed_field = f"{arm}_practice_completed"
            completed_at_field = f"{arm}_practice_completed_at"
            revision_field = f"{arm}_practice_revision"
            package_sha256_field = f"{arm}_practice_package_sha256"
            completed = participant.get(completed_field)
            completed_at_value = participant.get(completed_at_field)
            if participant.get(revision_field) != APPROVED_PRACTICE_REVISION:
                errors.append(
                    f"{label}.{revision_field} must be "
                    f"{APPROVED_PRACTICE_REVISION}"
                )
            if (
                participant.get(package_sha256_field)
                != APPROVED_PRACTICE_PACKAGE_SHA256
            ):
                errors.append(
                    f"{label}.{package_sha256_field} must match approved "
                    "practice package"
                )
            if participation_status == "completed":
                if completed is not True:
                    errors.append(f"{label}.{completed_field} must be true")
            elif not isinstance(completed, bool):
                errors.append(f"{label}.{completed_field} must be boolean")
            if completed is True:
                if isinstance(participant_id, str):
                    practice_completed_by_participant[participant_id].add(
                        completed_field
                    )
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
    task_case_contracts: dict[str, dict[str, Any]] = {}
    task_arm_sha256: dict[str, str] = {}
    checklist_case_contracts: dict[str, dict[str, str]] = {}
    gold_case_contracts: dict[str, dict[str, Any]] = {}
    try:
        (
            approved_case_contracts,
            _structured_high_risk_targets_ready,
        ) = _load_approved_manifest_contract()
    except ApprovedManifestError as exc:
        errors.append(f"approved manifest contract is unavailable: {exc}")
    try:
        (
            task_case_contracts,
            task_arm_sha256,
        ) = _load_pinned_task_package_contract()
    except PinnedTaskPackageError as exc:
        errors.append(f"pinned timed-task package is unavailable: {exc}")
    try:
        checklist_case_contracts = (
            _load_pinned_checklist_package_contract()
        )
    except PinnedChecklistPackageError as exc:
        errors.append(
            f"pinned completion-checklist package is unavailable: {exc}"
        )
    try:
        (
            gold_case_contracts,
            _gold_targets_approved,
        ) = _load_pinned_gold_package_contract()
    except PinnedGoldPackageError as exc:
        errors.append(f"pinned gold package is unavailable: {exc}")
    for case_id in sorted(
        approved_case_contracts.keys() & gold_case_contracts.keys()
    ):
        for field in ("conversion_mode", "target_artifact_type"):
            if (
                approved_case_contracts[case_id][field]
                != gold_case_contracts[case_id][field]
            ):
                errors.append(
                    f"pinned gold package {field} for {case_id} must match "
                    "the approved manifest"
                )
    for case_id in sorted(
        approved_case_contracts.keys() & task_case_contracts.keys()
    ):
        for field in (
            "fixture_id",
            "conversion_mode",
            "target_artifact_type",
            "expectations",
            "review_focus",
        ):
            if (
                approved_case_contracts[case_id][field]
                != task_case_contracts[case_id][field]
            ):
                errors.append(
                    f"pinned timed-task package {field} for {case_id} "
                    "must match the approved manifest"
                )
    for case_id in sorted(
        task_case_contracts.keys() & checklist_case_contracts.keys()
    ):
        if (
            checklist_case_contracts[case_id]["task_case_sha256"]
            != task_case_contracts[case_id]["task_case_sha256"]
        ):
            errors.append(
                "pinned completion-checklist task case for "
                f"{case_id} must match the timed-task package"
            )
    run_ids: set[str] = set()
    sealed_artifact_record_ids: set[str] = set()
    assessor_attestation_record_ids: set[str] = set()
    attempt_keys: set[tuple[Any, ...]] = set()
    attempt_numbers_by_group: dict[
        tuple[str, str, str], set[int]
    ] = defaultdict(set)
    attempt_timing_by_group: dict[
        tuple[str, str, str],
        dict[int, tuple[ExactUtcTimestamp, ExactUtcTimestamp]],
    ] = defaultdict(dict)
    revisions_by_case: dict[
        tuple[str, str], set[str]
    ] = defaultdict(set)
    included_by_pair: dict[
        tuple[str, str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    usable_intervals_by_participant_arm: dict[
        tuple[str, str],
        list[tuple[ExactUtcTimestamp, ExactUtcTimestamp]],
    ] = defaultdict(list)
    activity_starts_by_participant_arm: dict[
        tuple[str, str], list[ExactUtcTimestamp]
    ] = defaultdict(list)
    times_by_participant: dict[
        str, list[tuple[ExactUtcTimestamp, ExactUtcTimestamp, str]]
    ] = defaultdict(list)
    activity_starts_by_participant: dict[
        str, list[tuple[ExactUtcTimestamp, str]]
    ] = defaultdict(list)
    withdrawal_marker_ends: dict[
        str, list[tuple[ExactUtcTimestamp, str]]
    ] = defaultdict(list)
    withdrawal_marker_participants: set[str] = set()

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
        output_artifact_sha256 = run.get("output_artifact_sha256")
        if output_artifact_sha256 is not None and (
            not isinstance(output_artifact_sha256, str)
            or SHA256_RE.fullmatch(output_artifact_sha256) is None
            or output_artifact_sha256 == "0" * 64
        ):
            errors.append(
                f"{label}.output_artifact_sha256 must be null or lowercase "
                "SHA-256"
            )
        sealed_artifact_kind = run.get("sealed_artifact_kind")
        if (
            not isinstance(sealed_artifact_kind, str)
            or sealed_artifact_kind not in ALLOWED_SEALED_ARTIFACT_KINDS
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
                ("conversion_mode", "conversion_mode"),
                ("target_artifact_type", "target_artifact_type"),
            ):
                if run.get(field) != approved_case[contract_field]:
                    errors.append(
                        f"{label}.{field} must match approved manifest "
                        f"value {approved_case[contract_field]!r}"
                    )
        for field, expected_value in (
            ("task_package_path", PINNED_TASK_PACKAGE_PATH),
            ("task_package_sha256", PINNED_TASK_PACKAGE_SHA256),
        ):
            if run.get(field) != expected_value:
                errors.append(
                    f"{label}.{field} must match pinned timed-task package "
                    f"value {expected_value!r}"
                )
        task_case = (
            task_case_contracts.get(case_id)
            if isinstance(case_id, str)
            else None
        )
        if (
            task_case is not None
            and run.get("task_case_sha256")
            != task_case["task_case_sha256"]
        ):
            errors.append(
                f"{label}.task_case_sha256 must bind pinned timed-task case "
                f"{case_id} as {task_case['task_case_sha256']}"
            )
        for field, expected_value in (
            ("gold_package_path", PINNED_GOLD_PACKAGE_PATH),
            ("gold_package_sha256", PINNED_GOLD_PACKAGE_SHA256),
        ):
            if run.get(field) != expected_value:
                errors.append(
                    f"{label}.{field} must match pinned gold package "
                    f"value {expected_value!r}"
                )
        gold_case = (
            gold_case_contracts.get(case_id)
            if isinstance(case_id, str)
            else None
        )
        if (
            gold_case is not None
            and run.get("gold_case_sha256")
            != gold_case["gold_case_sha256"]
        ):
            errors.append(
                f"{label}.gold_case_sha256 must bind pinned gold case "
                f"{case_id} as {gold_case['gold_case_sha256']}"
            )
        for field, expected_value in (
            ("checklist_package_path", PINNED_CHECKLIST_PACKAGE_PATH),
            ("checklist_package_sha256", PINNED_CHECKLIST_PACKAGE_SHA256),
        ):
            if run.get(field) != expected_value:
                errors.append(
                    f"{label}.{field} must match pinned completion-checklist "
                    f"package value {expected_value!r}"
                )
        checklist_case = (
            checklist_case_contracts.get(case_id)
            if isinstance(case_id, str)
            else None
        )
        if (
            checklist_case is not None
            and run.get("checklist_case_sha256")
            != checklist_case["checklist_case_sha256"]
        ):
            errors.append(
                f"{label}.checklist_case_sha256 must bind pinned completion "
                f"checklist for {case_id} as "
                f"{checklist_case['checklist_case_sha256']}"
            )
        arm = run.get("arm")
        arm_is_valid = isinstance(arm, str) and arm in {"manual", "veridoc"}
        if not arm_is_valid:
            errors.append(f"{label}.arm is invalid")
        elif run.get("task_arm_sha256") != task_arm_sha256.get(arm):
            errors.append(
                f"{label}.task_arm_sha256 must bind pinned {arm} "
                f"assistance contract as {task_arm_sha256.get(arm)}"
            )
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

        gold_answer_hidden = run.get(
            "gold_answer_hidden_until_ended_at"
        )
        if not isinstance(gold_answer_hidden, bool):
            errors.append(
                f"{label}.gold_answer_hidden_until_ended_at must be boolean"
            )
        if run.get("gold_answer_compared_by_role") != "independent_assessor":
            errors.append(
                f"{label}.gold_answer_compared_by_role must be "
                "independent_assessor"
            )
        independent_assessor_id = run.get("independent_assessor_id")
        if (
            not isinstance(independent_assessor_id, str)
            or ASSESSOR_ID_RE.fullmatch(independent_assessor_id) is None
        ):
            errors.append(
                f"{label}.independent_assessor_id must be an opaque "
                "A-prefixed UUIDv4"
            )
        elif (
            isinstance(participant_id, str)
            and participant_id.startswith("P-")
            and independent_assessor_id[2:] == participant_id[2:]
        ):
            errors.append(
                f"{label}.independent_assessor_id must not identify the participant"
            )
        assessor_attestation_record_id = run.get(
            "assessor_attestation_record_id"
        )
        if (
            not isinstance(assessor_attestation_record_id, str)
            or ASSESSOR_ATTESTATION_RECORD_ID_RE.fullmatch(
                assessor_attestation_record_id
            )
            is None
        ):
            errors.append(
                f"{label}.assessor_attestation_record_id must be an opaque "
                "AAR-prefixed UUIDv4"
            )
        elif assessor_attestation_record_id in assessor_attestation_record_ids:
            errors.append(
                "duplicate assessor_attestation_record_id: "
                f"{assessor_attestation_record_id}"
            )
        else:
            assessor_attestation_record_ids.add(assessor_attestation_record_id)
        assessor_attestation_sha256 = run.get("assessor_attestation_sha256")
        if (
            not isinstance(assessor_attestation_sha256, str)
            or SHA256_RE.fullmatch(assessor_attestation_sha256) is None
            or assessor_attestation_sha256 == "0" * 64
        ):
            errors.append(
                f"{label}.assessor_attestation_sha256 must be a non-zero "
                "lowercase SHA-256"
            )
        if (
            run.get("gold_answer_comparison_withheld_from_participant")
            is not True
        ):
            errors.append(
                f"{label}.gold_answer_comparison_withheld_from_participant "
                "must be true"
            )

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

        timing = _validate_run_timing(run, label, errors)
        assessment_completed_at = _parse_utc(
            run.get("assessment_completed_at"),
            f"{label}.assessment_completed_at",
            errors,
        )
        if (
            timing is not None
            and assessment_completed_at is not None
            and assessment_completed_at <= timing.ended_at
        ):
            errors.append(
                f"{label}.assessment_completed_at must follow ended_at"
            )
        if (
            timing is not None
            and participant_is_declared
            and arm_is_valid
        ):
            activity_starts_by_participant[participant_id].append(
                (timing.started_at, label)
            )
            activity_starts_by_participant_arm[
                (participant_id, arm)
            ].append(timing.started_at)
            if timing.is_interval_usable:
                usable_intervals_by_participant_arm[
                    (participant_id, arm)
                ].append(
                    (timing.started_at, timing.ended_at)
                )
                times_by_participant[participant_id].append(
                    (timing.started_at, timing.ended_at, label)
                )
                if (
                    case_is_declared
                    and attempt_is_valid
                ):
                    attempt_timing_by_group[
                        (participant_id, case_id, arm)
                    ][attempt_number] = (
                        timing.started_at,
                        timing.ended_at,
                    )

        retained_attestation_bytes: bytes | None
        if assessor_attestation_resolver is None:
            retained_attestation_bytes = _read_retained_record(
                assessor_attestation_record_id,
                record_id_pattern=ASSESSOR_ATTESTATION_RECORD_ID_RE,
                directory="assessor_records",
                record_kind="assessor attestation",
                artifact_root=artifact_root,
                label=f"{label}.assessor_attestation_record_id",
                errors=errors,
            )
        elif not isinstance(assessor_attestation_record_id, str):
            retained_attestation_bytes = None
        else:
            try:
                retained_attestation_bytes = assessor_attestation_resolver(
                    assessor_attestation_record_id
                )
            except (KeyError, OSError, ValueError) as exc:
                errors.append(
                    f"{label}.assessor_attestation_record_id cannot resolve "
                    f"an independently retained assessor attestation: {exc}"
                )
                retained_attestation_bytes = None
            else:
                if not isinstance(retained_attestation_bytes, bytes):
                    errors.append(
                        f"{label}.assessor attestation resolver must return bytes"
                    )
                    retained_attestation_bytes = None
        if retained_attestation_bytes is not None:
            try:
                retained_attestation = _loads_json_strict(
                    retained_attestation_bytes.decode("utf-8")
                )
            except (UnicodeDecodeError, StrictJsonError):
                errors.append(
                    f"{label}.independently retained assessor attestation "
                    "must be strict UTF-8 JSON"
                )
            else:
                expected_attestation = build_assessor_attestation(run)
                if retained_attestation != expected_attestation:
                    errors.append(
                        f"{label} assessment must match the independently "
                        "retained assessor attestation"
                    )
                if (
                    isinstance(assessor_attestation_sha256, str)
                    and _canonical_json_sha256(retained_attestation)
                    != assessor_attestation_sha256
                ):
                    errors.append(
                        f"{label}.assessor_attestation_sha256 must match the "
                        "independently retained assessor attestation"
                    )

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
        allowed_artifact_kinds = (
            ALLOWED_SEALED_ARTIFACT_KINDS_BY_OUTCOME.get(outcome)
            if isinstance(outcome, str)
            else None
        )
        if (
            allowed_artifact_kinds is not None
            and sealed_artifact_kind not in allowed_artifact_kinds
        ):
            errors.append(
                f"{label}.sealed_artifact_kind must be "
                f"{' or '.join(allowed_artifact_kinds)} for {outcome} outcome"
            )
        if sealed_artifact_kind == "output_artifact":
            if not isinstance(output_artifact_sha256, str):
                errors.append(
                    f"{label}.output_artifact_sha256 must be lowercase "
                    "SHA-256 for output_artifact"
                )
            relative_artifact_path = _sealed_artifact_relative_path(
                run,
                label,
                errors,
            )
            artifact_bytes: bytes | None = None
            if relative_artifact_path is not None:
                if artifact_resolver is None:
                    artifact_bytes = _read_sealed_artifact(
                        relative_artifact_path,
                        artifact_root,
                        label,
                        errors,
                    )
                else:
                    try:
                        artifact_bytes = artifact_resolver(run)
                    except (OSError, ValueError) as exc:
                        errors.append(
                            f"{label}.sealed_artifact_path cannot be resolved: "
                            f"{exc}"
                        )
                    if not isinstance(artifact_bytes, bytes):
                        errors.append(
                            f"{label}.sealed artifact resolver must return bytes"
                        )
                        artifact_bytes = None
            if (
                artifact_bytes is not None
                and isinstance(output_artifact_sha256, str)
                and SHA256_RE.fullmatch(output_artifact_sha256) is not None
                and hashlib.sha256(artifact_bytes).hexdigest()
                != output_artifact_sha256
            ):
                errors.append(
                    f"{label}.output_artifact_sha256 must match the resolved "
                    "output_artifact bytes"
                )
        elif sealed_artifact_kind == "blocked_attempt_envelope":
            if run.get("sealed_artifact_path") is not None:
                errors.append(
                    f"{label}.sealed_artifact_path must be null for "
                    "blocked_attempt_envelope"
                )
            if output_artifact_sha256 is not None:
                errors.append(
                    f"{label}.output_artifact_sha256 must be null for "
                    "blocked_attempt_envelope"
                )

        sealed_evidence_envelope = run.get("sealed_evidence_envelope")
        envelope_schema = schema["$defs"]["sealedEvidenceEnvelope"]
        _unknown_fields(
            sealed_evidence_envelope,
            set(envelope_schema["properties"]),
            f"{label}.sealed_evidence_envelope",
            errors,
        )
        _required_fields(
            sealed_evidence_envelope,
            set(envelope_schema["required"]),
            f"{label}.sealed_evidence_envelope",
            errors,
        )
        if isinstance(sealed_evidence_envelope, dict):
            expected_envelope = build_sealed_evidence_envelope(run)
            for field, expected in expected_envelope.items():
                if sealed_evidence_envelope.get(field) != expected:
                    errors.append(
                        f"{label}.sealed_evidence_envelope.{field} "
                        "must match the run"
                    )
            if (
                sealed_evidence_envelope == expected_envelope
                and isinstance(sealed_artifact_sha256, str)
                and sealed_artifact_sha256
                != _canonical_json_sha256(sealed_evidence_envelope)
            ):
                errors.append(
                    f"{label}.sealed_artifact_sha256 must match the "
                    "canonical sealed_evidence_envelope"
                )
        retained_evidence_bytes: bytes | None
        if sealed_evidence_resolver is None:
            retained_evidence_bytes = _read_sealed_evidence_record(
                run,
                artifact_root,
                label,
                errors,
            )
        else:
            if not isinstance(sealed_artifact_record_id, str):
                retained_evidence_bytes = None
            else:
                try:
                    retained_evidence_bytes = sealed_evidence_resolver(
                        sealed_artifact_record_id
                    )
                except (KeyError, OSError, ValueError) as exc:
                    errors.append(
                        f"{label}.sealed_artifact_record_id cannot resolve an "
                        f"independently retained sealed evidence record: {exc}"
                    )
                    retained_evidence_bytes = None
                else:
                    if not isinstance(retained_evidence_bytes, bytes):
                        errors.append(
                            f"{label}.sealed evidence resolver must return bytes"
                        )
                        retained_evidence_bytes = None
        if retained_evidence_bytes is not None:
            try:
                retained_evidence = _loads_json_strict(
                    retained_evidence_bytes.decode("utf-8")
                )
            except (UnicodeDecodeError, StrictJsonError):
                errors.append(
                    f"{label}.independently retained sealed evidence record "
                    "must be strict UTF-8 JSON"
                )
            else:
                if retained_evidence != sealed_evidence_envelope:
                    errors.append(
                        f"{label}.sealed_evidence_envelope must match the "
                        "independently retained sealed evidence record"
                    )
                if (
                    isinstance(sealed_artifact_sha256, str)
                    and _canonical_json_sha256(retained_evidence)
                    != sealed_artifact_sha256
                ):
                    errors.append(
                        f"{label}.sealed_artifact_sha256 must match the "
                        "independently retained sealed evidence record"
                    )

        if (
            gold_answer_hidden is False
            and not (
                excluded is True
                and exclusion_reason == "protocol_deviation"
            )
        ):
            errors.append(
                f"{label}.gold_answer_hidden_until_ended_at may be false "
                "only for an excluded protocol_deviation"
            )
        elif excluded is False and gold_answer_hidden is not True:
            errors.append(
                f"{label}.gold_answer_hidden_until_ended_at must be true "
                "for every included run"
            )
        if (
            excluded is True
            and exclusion_reason == "participant_withdrew"
            and participant_is_declared
        ):
            withdrawal_marker_participants.add(participant_id)
            if timing is not None:
                withdrawal_marker_ends[participant_id].append(
                    (timing.ended_at, label)
                )

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
            and case_id in gold_case_contracts
            and _is_non_negative_int(expected)
            and expected
            != gold_case_contracts[case_id]["high_risk_expected_count"]
        ):
            pinned_expected = gold_case_contracts[case_id][
                "high_risk_expected_count"
            ]
            errors.append(
                f"{label}.high_risk_expected_count must match pinned "
                f"gold package count {pinned_expected} for {case_id}"
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

    all_activity_starts = [
        started_at
        for participant_starts in activity_starts_by_participant.values()
        for started_at, _ in participant_starts
    ]
    if (
        approved_at is not None
        and all_activity_starts
        and approved_at >= min(all_activity_starts)
    ):
        errors.append("consent approval must precede every timed run")
    if (
        quality_approved_at is not None
        and all_activity_starts
        and quality_approved_at >= min(all_activity_starts)
    ):
        errors.append("quality approval must precede every timed run")
    required_practice_completions = {
        "manual_practice_completed",
        "veridoc_practice_completed",
    }
    for participant_id, activity_starts in sorted(
        activity_starts_by_participant.items()
    ):
        if not activity_starts:
            continue
        practice_times = practice_completed_at_by_participant[participant_id]
        completed_practices = practice_completed_by_participant[participant_id]
        for field in sorted(
            required_practice_completions - completed_practices
        ):
            errors.append(
                f"{participant_id}.{field} must be true before "
                "timed activity"
            )
        if participant_id not in participant_orders:
            errors.append(
                f"{participant_id}.arm_order is required before timed activity"
            )
        earliest_activity = min(started_at for started_at, _ in activity_starts)
        for field, completed_at in sorted(practice_times.items()):
            if completed_at >= earliest_activity:
                errors.append(
                    f"{participant_id}.{field} must precede every timed run"
                )
    for participant_id, consented_at in sorted(
        participant_consented_at.items()
    ):
        if approved_at is not None and consented_at <= approved_at:
            errors.append(
                f"{participant_id}.consented_at must follow "
                "consent approval"
            )
        practice_times = practice_completed_at_by_participant[participant_id]
        for field, completed_at in sorted(practice_times.items()):
            if consented_at >= completed_at:
                errors.append(
                    f"{participant_id}.consented_at must precede {field}"
                )
        activity_starts = activity_starts_by_participant[participant_id]
        if activity_starts:
            earliest_activity = min(
                started_at for started_at, _ in activity_starts
            )
            if consented_at >= earliest_activity:
                errors.append(
                    f"{participant_id}.consented_at must precede every "
                    "timed run"
                )
    for participant_id, withdrawn_at in sorted(
        participant_withdrawn_at.items()
    ):
        consented_at = participant_consented_at.get(participant_id)
        if consented_at is not None and consented_at >= withdrawn_at:
            errors.append(
                f"{participant_id}.consented_at must precede withdrawal"
            )
        for field, completed_at in sorted(
            practice_completed_at_by_participant[participant_id].items()
        ):
            if completed_at > withdrawn_at:
                errors.append(
                    f"{participant_id}.{field} must not occur after "
                    "withdrawal"
                )
        for marker_ended_at, run_label in withdrawal_marker_ends[
            participant_id
        ]:
            if marker_ended_at != withdrawn_at:
                errors.append(
                    f"{run_label}.ended_at must equal {participant_id} "
                    "withdrawn_at for participant_withdrew exclusion"
                )
        usable_interval_labels = {
            run_label
            for _, _, run_label in times_by_participant[participant_id]
        }
        for started_at, run_label in activity_starts_by_participant[
            participant_id
        ]:
            if (
                run_label not in usable_interval_labels
                and started_at >= withdrawn_at
            ):
                errors.append(
                    f"{run_label} must start before "
                    f"{participant_id} withdrawal"
                )
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
            and participant_id in withdrawal_marker_participants
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
        else:
            for case_id in sorted(declared_cases):
                for arm in ("manual", "veridoc"):
                    included = included_by_pair[
                        (participant_id, case_id, arm)
                    ]
                    if len(included) != 1:
                        errors.append(
                            f"{participant_id}/{case_id}/{arm} must have "
                            "exactly one non-excluded run"
                        )

                manual = included_by_pair[
                    (participant_id, case_id, "manual")
                ]
                veridoc = included_by_pair[
                    (participant_id, case_id, "veridoc")
                ]
                if len(manual) == 1 and len(veridoc) == 1:
                    for field in (
                        "task_revision",
                        "gold_answer_revision",
                        "checklist_revision",
                    ):
                        if manual[0].get(field) != veridoc[0].get(field):
                            errors.append(
                                f"paired runs must use the same {field}"
                            )

        order = participant_orders.get(participant_id)
        if order is not None:
            first_starts = activity_starts_by_participant_arm[
                (participant_id, order[0])
            ]
            first_intervals = usable_intervals_by_participant_arm[
                (participant_id, order[0])
            ]
            second_starts = activity_starts_by_participant_arm[
                (participant_id, order[1])
            ]
            first_activity_boundaries = [
                *first_starts,
                *(ended_at for _, ended_at in first_intervals),
            ]
            if (
                second_starts
                and (
                    not first_activity_boundaries
                    or max(first_activity_boundaries) > min(second_starts)
                )
            ):
                errors.append(
                    f"{participant_id} timed runs do not follow declared arm_order"
                )

    study_evidence_record_id = record.get("study_evidence_record_id")
    if (
        not isinstance(study_evidence_record_id, str)
        or STUDY_EVIDENCE_RECORD_ID_RE.fullmatch(study_evidence_record_id)
        is None
    ):
        errors.append(
            "study_evidence_record_id must be an opaque HSR-prefixed UUIDv4"
        )
    study_evidence_sha256 = record.get("study_evidence_sha256")
    if (
        not isinstance(study_evidence_sha256, str)
        or SHA256_RE.fullmatch(study_evidence_sha256) is None
        or study_evidence_sha256 == "0" * 64
    ):
        errors.append(
            "study_evidence_sha256 must be a non-zero lowercase SHA-256"
        )
    study_evidence_envelope = record.get("study_evidence_envelope")
    study_envelope_schema = schema["$defs"]["studyEvidenceEnvelope"]
    _unknown_fields(
        study_evidence_envelope,
        set(study_envelope_schema["properties"]),
        "study_evidence_envelope",
        errors,
    )
    _required_fields(
        study_evidence_envelope,
        set(study_envelope_schema["required"]),
        "study_evidence_envelope",
        errors,
    )
    if isinstance(study_evidence_envelope, dict):
        expected_study_envelope = build_study_evidence_envelope(record)
        for field, expected in expected_study_envelope.items():
            if study_evidence_envelope.get(field) != expected:
                errors.append(
                    f"study_evidence_envelope.{field} must match the study"
                )
        if (
            study_evidence_envelope == expected_study_envelope
            and isinstance(study_evidence_sha256, str)
            and study_evidence_sha256
            != _canonical_json_sha256(study_evidence_envelope)
        ):
            errors.append(
                "study_evidence_sha256 must match the canonical "
                "study_evidence_envelope"
            )

    retained_study_bytes: bytes | None
    if study_evidence_resolver is None:
        retained_study_bytes = _read_retained_record(
            study_evidence_record_id,
            record_id_pattern=STUDY_EVIDENCE_RECORD_ID_RE,
            directory="study_records",
            record_kind="study evidence record",
            artifact_root=artifact_root,
            label="study_evidence_record_id",
            errors=errors,
        )
    elif not isinstance(study_evidence_record_id, str):
        retained_study_bytes = None
    else:
        try:
            retained_study_bytes = study_evidence_resolver(
                study_evidence_record_id
            )
        except (KeyError, OSError, ValueError) as exc:
            errors.append(
                "study_evidence_record_id cannot resolve an independently "
                f"retained study evidence record: {exc}"
            )
            retained_study_bytes = None
        else:
            if not isinstance(retained_study_bytes, bytes):
                errors.append("study evidence resolver must return bytes")
                retained_study_bytes = None
    if retained_study_bytes is not None:
        try:
            retained_study_evidence = _loads_json_strict(
                retained_study_bytes.decode("utf-8")
            )
        except (UnicodeDecodeError, StrictJsonError):
            errors.append(
                "independently retained study evidence record must be strict "
                "UTF-8 JSON"
            )
        else:
            if retained_study_evidence != study_evidence_envelope:
                errors.append(
                    "study_evidence_envelope must match the independently "
                    "retained study evidence record"
                )
            if (
                isinstance(study_evidence_sha256, str)
                and _canonical_json_sha256(retained_study_evidence)
                != study_evidence_sha256
            ):
                errors.append(
                    "study_evidence_sha256 must match the independently "
                    "retained study evidence record"
                )

    return sorted(set(errors))


def summarize_record(
    record: dict[str, Any],
    *,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    artifact_resolver: ArtifactResolver | None = None,
    sealed_evidence_resolver: SealedEvidenceResolver | None = None,
    study_evidence_resolver: StudyEvidenceResolver | None = None,
    assessor_attestation_resolver: AssessorAttestationResolver | None = None,
) -> dict[str, Any]:
    """Compute protocol-defined metrics after validation."""

    errors = validate_record(
        record,
        artifact_root=artifact_root,
        artifact_resolver=artifact_resolver,
        sealed_evidence_resolver=sealed_evidence_resolver,
        study_evidence_resolver=study_evidence_resolver,
        assessor_attestation_resolver=assessor_attestation_resolver,
    )
    if errors:
        raise ValueError("record is invalid: " + "; ".join(errors))
    _, structured_high_risk_targets_ready = (
        _load_pinned_gold_package_contract()
    )

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
    included_runs = [run for run in record["runs"] if not run["excluded"]]
    included_by_arm = {
        (run["participant_id"], run["case_id"], run["arm"]): run
        for run in included_runs
    }
    recorded_by_arm: dict[tuple[str, str, str], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for run in record["runs"]:
        recorded_by_arm[
            (run["participant_id"], run["case_id"], run["arm"])
        ].append(run)
    pair_results: list[dict[str, Any]] = []
    eligible_pair_results: list[dict[str, Any]] = []

    def correction_seconds(run: dict[str, Any]) -> float:
        timing = RunTiming(
            started_at=ExactUtcTimestamp.parse(run["started_at"]),
            ended_at=ExactUtcTimestamp.parse(run["ended_at"]),
            excluded_pause_seconds=run["excluded_pause_seconds"],
            is_invalid_timing_exclusion=False,
        )
        return timing.correction_seconds()

    for participant_id in sorted(participant_statuses):
        for case_id in sorted(record["case_ids"]):
            arm_keys = {
                arm: (participant_id, case_id, arm)
                for arm in ARMS
            }
            included = {
                arm: included_by_arm.get(arm_keys[arm])
                for arm in ARMS
            }
            recorded = {
                arm: recorded_by_arm.get(arm_keys[arm], [])
                for arm in ARMS
            }
            reporting = {
                arm: included[arm]
                or (
                    max(
                        recorded[arm],
                        key=lambda run: run["attempt_number"],
                    )
                    if recorded[arm]
                    else None
                )
                for arm in ARMS
            }
            manual = included["manual"]
            veridoc = included["veridoc"]
            calculable = (
                manual is not None
                and veridoc is not None
                and manual["outcome"] == "approved"
                and veridoc["outcome"] == "approved"
                and manual["checklist_complete"]
                and veridoc["checklist_complete"]
            )
            eligible = (
                calculable
                and participant_statuses[participant_id] == "completed"
                and record["study_status"] == "validation_example"
            )
            pair_result = {
                "participant_id": participant_id,
                "case_id": case_id,
                "calculable": calculable,
                "eligible": eligible,
                "recorded_arms": [
                    arm for arm in ARMS if recorded[arm]
                ],
                "included_arms": [
                    arm for arm in ARMS if included[arm] is not None
                ],
                "missing_arms": [
                    arm for arm in ARMS if not recorded[arm]
                ],
            }
            for arm in ARMS:
                reporting_run = reporting[arm]
                pair_result.update(
                    {
                        f"{arm}_outcome": (
                            reporting_run["outcome"]
                            if reporting_run is not None
                            else None
                        ),
                        f"{arm}_blocker_code": (
                            reporting_run["blocker_code"]
                            if reporting_run is not None
                            else None
                        ),
                        f"{arm}_excluded": (
                            reporting_run["excluded"]
                            if reporting_run is not None
                            else None
                        ),
                        f"{arm}_exclusion_reason_code": (
                            reporting_run["exclusion_reason_code"]
                            if reporting_run is not None
                            else None
                        ),
                    }
                )
            if calculable:
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
                if eligible:
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
        "task_package_path": PINNED_TASK_PACKAGE_PATH,
        "task_package_sha256": PINNED_TASK_PACKAGE_SHA256,
        "checklist_package_path": PINNED_CHECKLIST_PACKAGE_PATH,
        "checklist_package_sha256": PINNED_CHECKLIST_PACKAGE_SHA256,
        "gold_package_path": PINNED_GOLD_PACKAGE_PATH,
        "gold_package_sha256": PINNED_GOLD_PACKAGE_SHA256,
        "gold_package_approval_status": "unapproved_validation_only",
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
    parser.add_argument(
        "--artifact-root",
        type=Path,
        default=None,
        help=(
            "root directory used to resolve output artifacts and independently "
            "retained run, study, and assessor records "
            "(default: the evidence record directory)"
        ),
    )
    args = parser.parse_args(argv)

    try:
        record = _loads_json_strict(args.record.read_text(encoding="utf-8"))
    except (
        OSError,
        UnicodeDecodeError,
        StrictJsonError,
    ) as exc:
        print(f"Unable to read evidence: {exc}", file=sys.stderr)
        return 2

    artifact_root = args.artifact_root or args.record.parent
    errors = validate_record(record, artifact_root=artifact_root)
    if errors:
        print("Human-review evidence validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            summarize_record(record, artifact_root=artifact_root),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
