from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from scripts.ci.validate_mvp_human_review_evidence import (
    ALLOWED_SEALED_ARTIFACT_KINDS_BY_OUTCOME,
    APPROVED_CHECKLIST_REVISION,
    APPROVED_GOLD_ANSWER_REVISION,
    APPROVED_MANIFEST_CONTRACT_SHA256,
    APPROVED_MANIFEST_GIT_BLOB,
    APPROVED_PRACTICE_PACKAGE_PATH,
    APPROVED_PRACTICE_PACKAGE_SHA256,
    APPROVED_PRACTICE_REVISION,
    APPROVED_SOURCE_TREE_LISTING_SHA256,
    APPROVED_PRODUCT_COMMIT,
    APPROVED_PRODUCT_TREE,
    APPROVED_TASK_REVISION,
    PINNED_CHECKLIST_PACKAGE_PATH,
    PINNED_CHECKLIST_PACKAGE_SHA256,
    PINNED_GOLD_PACKAGE_PATH,
    PINNED_GOLD_PACKAGE_SHA256,
    PINNED_TASK_PACKAGE_PATH,
    PINNED_TASK_PACKAGE_SHA256,
    _loads_json_strict,
    build_assessor_attestation,
    build_study_evidence_envelope,
    build_sealed_evidence_envelope,
    build_run_claims,
    summarize_record as _raw_summarize_record,
    validate_record as _raw_validate_record,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = REPO_ROOT / "docs" / "mvp-human-review-protocol.md"
SCHEMA_PATH = REPO_ROOT / "docs" / "mvp-human-review-evidence.schema.json"
CHECKLIST_PATH = REPO_ROOT / "docs" / "mvp-human-review-execution-checklist.md"
PRACTICE_PACKAGE_PATH = (
    REPO_ROOT / "docs" / "mvp-human-review-practice-package.json"
)
GOLD_PACKAGE_PATH = REPO_ROOT / PINNED_GOLD_PACKAGE_PATH
TASK_PACKAGE_PATH = REPO_ROOT / PINNED_TASK_PACKAGE_PATH
COMPLETION_CHECKLIST_PACKAGE_PATH = (
    REPO_ROOT / PINNED_CHECKLIST_PACKAGE_PATH
)
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
APPROVED_TARGET_FORMATS = {
    "mvp-word-001": ("word_to_excel", "xlsx"),
    "mvp-excel-001": ("excel_to_word", "docx"),
    "mvp-text-pdf-001": ("pdf_to_excel", "xlsx"),
    "mvp-scanned-pdf-001": ("pdf_to_word", "docx"),
    "mvp-record-pdf-001": ("pdf_to_word", "docx"),
}
PINNED_GOLD_CASE_SHA256 = {
    "mvp-word-001": (
        "1f3337f012629121a802df23adbe80a887d005a8b45fb3dc0231518ce34b812f"
    ),
    "mvp-excel-001": (
        "fb3f3dae15793cc7d449d2787ca475094f42e4702de31d6f00dabbf972ef305c"
    ),
    "mvp-text-pdf-001": (
        "effd5300edac2fb18572182add5127ad45dda40bcbac11d3ecc8904d1614a597"
    ),
    "mvp-scanned-pdf-001": (
        "0d98675a91e5191fb25cdda96c4563bc070c0c0fcd1d536d4accccf1e6540cb9"
    ),
    "mvp-record-pdf-001": (
        "fbd0f75c1464b474f92bd29a8b41584707ef00d36e99c0dfa7dc320eb245696f"
    ),
}
PINNED_TASK_CASE_SHA256 = {
    "mvp-word-001": (
        "889b04fc2703ebcd7f16e24e19338da050985effe51c75668b84c2ad30c6d741"
    ),
    "mvp-excel-001": (
        "0d91c2dc290af17164eec2b9270c007663cfefe732394c5c68f5856fea186111"
    ),
    "mvp-text-pdf-001": (
        "e428760db75a24d477b0928d38bc9d3b2d2d5feeadaa1229d4b6ec5c61e012e4"
    ),
    "mvp-scanned-pdf-001": (
        "18e732cf59c29cf817ad87a80cfd020f64d34799c1ac8c4a354068577a9d13c5"
    ),
    "mvp-record-pdf-001": (
        "9bc7bd5bda936d9b20f9d26e5c6957903b13782857ccdbbb0a93de2643d4648b"
    ),
}
PINNED_TASK_ARM_SHA256 = {
    "manual": (
        "f8c40d44bd0d29ff8b93455e196acd1adb495f71e13696c207739c478bae5440"
    ),
    "veridoc": (
        "204ae4a4f884e48e0360e6f506f9ef57a7e6e98a50f519e2f06e9c9041daaf06"
    ),
}
PINNED_CHECKLIST_CASE_SHA256 = {
    "mvp-word-001": (
        "c4d31dabdc42f6241e41b8f64697bef4bafe567e1cb99e6cc37ce61229cc776f"
    ),
    "mvp-excel-001": (
        "53d67e61e555a885ef0ad1a18d47f059caafd271febdbfbcd7baea8a74a7a90f"
    ),
    "mvp-text-pdf-001": (
        "dc42d2a9eba69b437a176f280eae8b1bc2507833b08f056a903a6e0da22784e9"
    ),
    "mvp-scanned-pdf-001": (
        "21c5c92c086be5b0a5d3f83e9a373842ca054480743f8ab7aabe21d777945f3f"
    ),
    "mvp-record-pdf-001": (
        "b67ffa6777a3ca60aac5ca0227e0e672814071a15a85698ba2d380d97738d9ca"
    ),
}
CONSENT_FORM_VERSION = "CF-550E8400-E29B-41D4-A716-446655440201"
APPROVED_FIXTURES = {
    "mvp-word-001": (
        "word-to-excel-application",
        "datasets/fixtures/word/word-to-excel-application.docx",
        "8d3f4c25af465eb03bb1b2a624d14de27b1f777a4ec2cd5674563335d2b58cf1",
    ),
    "mvp-excel-001": (
        "excel-to-word-representative",
        "datasets/fixtures/excel/excel-to-word-representative.xlsx",
        "b6554a36e10c02b6db4bbb73b10a8156d136025eb46291b34f6f370addc13f35",
    ),
    "mvp-text-pdf-001": (
        "pdf-to-excel-table-report",
        "datasets/fixtures/pdf/pdf-to-excel-ruled-table.pdf",
        "a6a0e34591f46a15e12106ff5e3d319f7a8b1e10e835def09a75bf2064c2cce6",
    ),
    "mvp-scanned-pdf-001": (
        "scanned-pdf-representative",
        "datasets/fixtures/pdf/scanned-pdf-representative.pdf",
        "742cf1c91f4c8a00fd798ffff63727605c767d6ec0dd5718fd565617051c496a",
    ),
    "mvp-record-pdf-001": (
        "record-pdf-neutral-representative",
        "datasets/fixtures/pdf/record-pdf-neutral-representative.pdf",
        "076714fac54730e4012018bc11b48486de39ad3e83f9fb9aa1c29f98401516c6",
    ),
}
BUILD_PROVENANCE = {
    "record_id": "BLD-550E8400-E29B-41D4-A716-446655440001",
    "product_commit": APPROVED_PRODUCT_COMMIT,
    "product_tree": APPROVED_PRODUCT_TREE,
    "checkout_state": "clean",
    "derivation_status": "approved_source_tree_verified_execution_unattested",
    "source_tree_listing_sha256": (
        "0bec46f7d8240796a137a163c20c4ee5f98f867f5730d78fe56b571eeffd6b3c"
    ),
    "execution_attestation_status": "unverified_validation_only",
    "attestation_sha256": (
        "33476974ce1add5b3f890f432b8c306299bf28cb601d231c040d18775b068295"
    ),
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


def _opaque_record_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest().upper()
    return (
        f"{prefix}-{digest[:8]}-{digest[8:12]}-4{digest[12:15]}-"
        f"A{digest[15:18]}-{digest[18:30]}"
    )


def _sealed_artifact_path(record_id: str) -> str:
    return f"sealed_artifacts/{record_id}.bin"


def _synthetic_output_artifact_bytes(run: dict[str, object]) -> bytes:
    return f"{run['run_id']}\n".encode("utf-8")


def _synthetic_sealed_evidence_bytes(run: dict[str, object]) -> bytes:
    return json.dumps(
        run["sealed_evidence_envelope"],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _synthetic_study_evidence_bytes(record: dict[str, object]) -> bytes:
    return json.dumps(
        record["study_evidence_envelope"],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _synthetic_assessor_attestation_bytes(run: dict[str, object]) -> bytes:
    return json.dumps(
        build_assessor_attestation(run),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _synthetic_sealed_evidence_resolver(
    record: object,
) -> Callable[[str], bytes]:
    retained_by_id: dict[str, bytes] = {}
    if isinstance(record, dict) and isinstance(record.get("runs"), list):
        for run in record["runs"]:
            if (
                isinstance(run, dict)
                and isinstance(run.get("sealed_artifact_record_id"), str)
                and "sealed_evidence_envelope" in run
            ):
                retained_by_id[run["sealed_artifact_record_id"]] = (
                    _synthetic_sealed_evidence_bytes(run)
                )
    return retained_by_id.__getitem__


def _synthetic_study_evidence_resolver(
    record: object,
) -> Callable[[str], bytes]:
    retained_by_id: dict[str, bytes] = {}
    if (
        isinstance(record, dict)
        and isinstance(record.get("study_evidence_record_id"), str)
        and "study_evidence_envelope" in record
    ):
        retained_by_id[record["study_evidence_record_id"]] = (
            _synthetic_study_evidence_bytes(record)
        )
    return retained_by_id.__getitem__


def _synthetic_assessor_attestation_resolver(
    record: object,
) -> Callable[[str], bytes]:
    retained_by_id: dict[str, bytes] = {}
    if isinstance(record, dict) and isinstance(record.get("runs"), list):
        for run in record["runs"]:
            if (
                isinstance(run, dict)
                and isinstance(run.get("assessor_attestation_record_id"), str)
            ):
                retained_by_id[run["assessor_attestation_record_id"]] = (
                    _synthetic_assessor_attestation_bytes(run)
                )
    return retained_by_id.__getitem__


def _validate_record(record: object) -> list[str]:
    return _raw_validate_record(
        record,
        artifact_resolver=_synthetic_output_artifact_bytes,
        sealed_evidence_resolver=_synthetic_sealed_evidence_resolver(record),
        study_evidence_resolver=_synthetic_study_evidence_resolver(record),
        assessor_attestation_resolver=(
            _synthetic_assessor_attestation_resolver(record)
        ),
    )


def _summarize_record(record: dict[str, object]) -> dict[str, object]:
    return _raw_summarize_record(
        record,
        artifact_resolver=_synthetic_output_artifact_bytes,
        sealed_evidence_resolver=_synthetic_sealed_evidence_resolver(record),
        study_evidence_resolver=_synthetic_study_evidence_resolver(record),
        assessor_attestation_resolver=(
            _synthetic_assessor_attestation_resolver(record)
        ),
    )


def _seal_evidence_envelope(run: dict[str, object]) -> None:
    envelope = build_sealed_evidence_envelope(run)
    run["sealed_evidence_envelope"] = envelope
    run["sealed_artifact_sha256"] = hashlib.sha256(
        json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _seal_study_evidence_envelope(record: dict[str, object]) -> None:
    envelope = build_study_evidence_envelope(record)
    record["study_evidence_envelope"] = envelope
    record["study_evidence_sha256"] = hashlib.sha256(
        json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _seal_assessor_attestation(run: dict[str, object]) -> None:
    run["assessor_attestation_sha256"] = hashlib.sha256(
        _synthetic_assessor_attestation_bytes(run)
    ).hexdigest()


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
            for case_id in ALL_CASE_IDS:
                started_at = cursor
                ended_at = started_at + duration
                fixture_id, fixture_path, fixture_sha256 = APPROVED_FIXTURES[
                    case_id
                ]
                conversion_mode, target_artifact_type = (
                    APPROVED_TARGET_FORMATS[case_id]
                )
                run_id = (
                    f"RUN-{participant_id}-{case_id.upper()}-"
                    f"{arm.upper()}-1"
                )
                artifact_record_id = _opaque_record_id("SAR", run_id)
                runs.append(
                    {
                        "run_id": run_id,
                        "sealed_artifact_record_id": artifact_record_id,
                        "sealed_artifact_sha256": "",
                        "output_artifact_sha256": hashlib.sha256(
                            f"{run_id}\n".encode("utf-8")
                        ).hexdigest(),
                        "sealed_artifact_kind": "output_artifact",
                        "sealed_artifact_path": _sealed_artifact_path(
                            artifact_record_id
                        ),
                        "sealed_evidence_envelope": {},
                        "participant_id": participant_id,
                        "case_id": case_id,
                        "source_fixture_id": fixture_id,
                        "source_fixture_path": fixture_path,
                        "source_fixture_sha256": fixture_sha256,
                        "conversion_mode": conversion_mode,
                        "target_artifact_type": target_artifact_type,
                        "arm": arm,
                        "veridoc_build_provenance": (
                            copy.deepcopy(BUILD_PROVENANCE)
                            if arm == "veridoc"
                            else None
                        ),
                        "attempt_number": 1,
                        "task_revision": APPROVED_TASK_REVISION,
                        "task_package_path": PINNED_TASK_PACKAGE_PATH,
                        "task_package_sha256": PINNED_TASK_PACKAGE_SHA256,
                        "task_case_sha256": PINNED_TASK_CASE_SHA256[case_id],
                        "task_arm_sha256": PINNED_TASK_ARM_SHA256[arm],
                        "gold_answer_revision": APPROVED_GOLD_ANSWER_REVISION,
                        "gold_package_path": PINNED_GOLD_PACKAGE_PATH,
                        "gold_package_sha256": PINNED_GOLD_PACKAGE_SHA256,
                        "gold_case_sha256": PINNED_GOLD_CASE_SHA256[case_id],
                        "checklist_revision": APPROVED_CHECKLIST_REVISION,
                        "checklist_package_path": (
                            PINNED_CHECKLIST_PACKAGE_PATH
                        ),
                        "checklist_package_sha256": (
                            PINNED_CHECKLIST_PACKAGE_SHA256
                        ),
                        "checklist_case_sha256": (
                            PINNED_CHECKLIST_CASE_SHA256[case_id]
                        ),
                        "gold_answer_hidden_until_ended_at": True,
                        "gold_answer_compared_by_role": "independent_assessor",
                        "independent_assessor_id": _opaque_record_id(
                            "A", "independent-assessor"
                        ),
                        "assessor_attestation_record_id": _opaque_record_id(
                            "AAR", run_id
                        ),
                        "assessor_attestation_sha256": "",
                        "assessment_completed_at": _utc_text(
                            ended_at + timedelta(seconds=1)
                        ),
                        "gold_answer_comparison_withheld_from_participant": True,
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
                _seal_assessor_attestation(runs[-1])
                _seal_evidence_envelope(runs[-1])
                cursor = ended_at + timedelta(minutes=1)
            cursor += timedelta(minutes=5)
    record["runs"] = runs
    _seal_study_evidence_envelope(record)
    return record


def _add_excluded_veridoc_retry(record: dict[str, object]) -> None:
    runs = record["runs"]
    assert isinstance(runs, list)
    source = next(
        run
        for run in runs
        if isinstance(run, dict)
        and run["participant_id"] == "P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A"
        and run["case_id"] == "mvp-scanned-pdf-001"
        and run["arm"] == "veridoc"
    )
    participant_ends = [
        datetime.fromisoformat(str(run["ended_at"]).replace("Z", "+00:00"))
        for run in runs
        if isinstance(run, dict) and run["participant_id"] == "P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A"
    ]
    retry = copy.deepcopy(source)
    retry_start = max(participant_ends) + timedelta(minutes=1)
    retry.update(
        {
            "run_id": "RUN-P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A-MVP-SCANNED-PDF-001-VERIDOC-2",
            "sealed_artifact_record_id": _opaque_record_id(
                "SAR", "RUN-P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A-MVP-SCANNED-PDF-001-VERIDOC-2"
            ),
            "output_artifact_sha256": hashlib.sha256(
                b"RUN-P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A-MVP-SCANNED-PDF-001-VERIDOC-2\n"
            ).hexdigest(),
            "sealed_artifact_path": _sealed_artifact_path(
                _opaque_record_id(
                    "SAR",
                    "RUN-P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A-MVP-SCANNED-PDF-001-VERIDOC-2",
                )
            ),
            "attempt_number": 2,
            "started_at": _utc_text(retry_start),
            "ended_at": _utc_text(retry_start + timedelta(minutes=1)),
            "assessor_attestation_record_id": _opaque_record_id(
                "AAR",
                "RUN-P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A-"
                "MVP-SCANNED-PDF-001-VERIDOC-2",
            ),
            "assessment_completed_at": _utc_text(
                retry_start + timedelta(minutes=1, seconds=1)
            ),
            "over_detection_count": 1,
            "excluded": True,
            "exclusion_reason_code": "technical_failure",
        }
    )
    _seal_assessor_attestation(retry)
    _seal_evidence_envelope(retry)
    runs.append(retry)


def _add_withdrawn_participant(record: dict[str, object]) -> None:
    participants = record["participants"]
    runs = record["runs"]
    assert isinstance(participants, list)
    assert isinstance(runs, list)
    participants.append(
        {
            "participant_id": "P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C",
            "participation_status": "withdrawn",
            "withdrawn_at": "2026-08-10T01:02:00Z",
            "consent_status": "consented",
            "consented_at": "2026-07-26T00:06:00Z",
            "consent_form_version": CONSENT_FORM_VERSION,
            "relevant_experience_attested": True,
            "manual_practice_completed": True,
            "manual_practice_completed_at": "2026-07-26T00:10:00Z",
            "manual_practice_revision": APPROVED_PRACTICE_REVISION,
            "manual_practice_package_sha256": (
                APPROVED_PRACTICE_PACKAGE_SHA256
            ),
            "veridoc_practice_completed": True,
            "veridoc_practice_completed_at": "2026-07-26T00:20:00Z",
            "veridoc_practice_revision": APPROVED_PRACTICE_REVISION,
            "veridoc_practice_package_sha256": (
                APPROVED_PRACTICE_PACKAGE_SHA256
            ),
            "arm_order": ["manual", "veridoc"],
        }
    )
    source = next(
        run
        for run in runs
        if isinstance(run, dict)
        and run["participant_id"] == "P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A"
        and run["case_id"] == "mvp-word-001"
        and run["arm"] == "manual"
    )
    withdrawn_attempt = copy.deepcopy(source)
    withdrawn_attempt.update(
        {
            "run_id": "RUN-P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C-MVP-WORD-001-MANUAL-1",
            "sealed_artifact_record_id": _opaque_record_id(
                "SAR", "RUN-P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C-MVP-WORD-001-MANUAL-1"
            ),
            "output_artifact_sha256": hashlib.sha256(
                b"RUN-P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C-MVP-WORD-001-MANUAL-1\n"
            ).hexdigest(),
            "sealed_artifact_path": _sealed_artifact_path(
                _opaque_record_id(
                    "SAR",
                    "RUN-P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C-MVP-WORD-001-MANUAL-1",
                )
            ),
            "participant_id": "P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C",
            "started_at": "2026-08-10T01:00:00Z",
            "ended_at": "2026-08-10T01:02:00Z",
            "assessor_attestation_record_id": _opaque_record_id(
                "AAR",
                "RUN-P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C-"
                "MVP-WORD-001-MANUAL-1",
            ),
            "assessment_completed_at": "2026-08-10T01:02:01Z",
            "excluded": True,
            "exclusion_reason_code": "participant_withdrew",
        }
    )
    _seal_assessor_attestation(withdrawn_attempt)
    _seal_evidence_envelope(withdrawn_attempt)
    runs.append(withdrawn_attempt)
    _seal_study_evidence_envelope(record)


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
            "checklist-phase12-v1",
            "mvp-human-review-practice-package.json",
            "mvp-human-review-timed-task-package.json",
            "mvp-human-review-completion-checklist-package.json",
            "task_case_sha256",
            "task_arm_sha256",
            "checklist_case_sha256",
            "mvp_human_review_gold_package_v1.json",
            "gold_case_sha256",
            "target_artifact_type",
            "consent_status",
            "protocol_deviation",
            "within-participant",
            "paired cohort median",
            "high-risk miss",
            "over_detection_count",
            "direct participant identity",
            "independent_assessor",
            "participation_status",
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
        self.assertFalse(
            schema["$defs"]["veridocBuildProvenance"][
                "additionalProperties"
            ]
        )
        self.assertFalse(schema["$defs"]["run"]["additionalProperties"])
        self.assertFalse(
            schema["$defs"]["sealedEvidenceEnvelope"]["additionalProperties"]
        )
        self.assertFalse(
            schema["$defs"]["studyEvidenceEnvelope"]["additionalProperties"]
        )
        self.assertIn("practice_revision", schema["required"])
        self.assertIn("practice_package_path", schema["required"])
        self.assertIn("practice_package_sha256", schema["required"])
        self.assertIn("quality_approval", schema["required"])
        self.assertIn("study_evidence_record_id", schema["required"])
        self.assertIn("study_evidence_sha256", schema["required"])
        self.assertIn("study_evidence_envelope", schema["required"])
        self.assertIn(
            "gold_answer_hidden_until_ended_at",
            schema["$defs"]["run"]["required"],
        )
        self.assertIn(
            "gold_answer_comparison_withheld_from_participant",
            schema["$defs"]["run"]["required"],
        )
        self.assertIn(
            "participation_status",
            schema["$defs"]["participant"]["required"],
        )
        self.assertIn(
            "manual_practice_completed_at",
            schema["$defs"]["participant"]["required"],
        )
        self.assertIn(
            "withdrawn_at",
            schema["$defs"]["participant"]["required"],
        )
        for field in (
            "consent_status",
            "consented_at",
            "consent_form_version",
            "manual_practice_revision",
            "manual_practice_package_sha256",
            "veridoc_practice_revision",
            "veridoc_practice_package_sha256",
        ):
            self.assertIn(
                field,
                schema["$defs"]["participant"]["required"],
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
            APPROVED_PRODUCT_COMMIT,
            schema["properties"]["target_product_commit"]["const"],
        )
        self.assertEqual(
            APPROVED_MANIFEST_GIT_BLOB,
            schema["properties"]["manifest_git_blob"]["const"],
        )
        self.assertEqual(
            APPROVED_MANIFEST_CONTRACT_SHA256,
            schema["properties"]["manifest_contract_sha256"]["const"],
        )
        self.assertEqual(
            APPROVED_PRACTICE_REVISION,
            schema["properties"]["practice_revision"]["const"],
        )
        self.assertEqual(
            APPROVED_PRACTICE_PACKAGE_PATH,
            schema["properties"]["practice_package_path"]["const"],
        )
        self.assertEqual(
            APPROVED_PRACTICE_PACKAGE_SHA256,
            schema["properties"]["practice_package_sha256"]["const"],
        )
        for field in (
            "source_fixture_id",
            "source_fixture_path",
            "source_fixture_sha256",
            "conversion_mode",
            "target_artifact_type",
            "sealed_artifact_record_id",
            "sealed_artifact_sha256",
            "sealed_artifact_kind",
            "sealed_artifact_path",
            "output_artifact_sha256",
            "sealed_evidence_envelope",
            "veridoc_build_provenance",
            "task_package_path",
            "task_package_sha256",
            "task_case_sha256",
            "task_arm_sha256",
            "gold_package_path",
            "gold_package_sha256",
            "gold_case_sha256",
            "checklist_revision",
            "checklist_package_path",
            "checklist_package_sha256",
            "checklist_case_sha256",
        ):
            self.assertIn(field, schema["$defs"]["run"]["required"])
        self.assertEqual(
            APPROVED_PRODUCT_TREE,
            schema["$defs"]["veridocBuildProvenance"]["properties"][
                "product_tree"
            ]["const"],
        )
        self.assertEqual(
            APPROVED_SOURCE_TREE_LISTING_SHA256,
            schema["$defs"]["veridocBuildProvenance"]["properties"][
                "source_tree_listing_sha256"
            ]["const"],
        )
        self.assertEqual(
            "unverified_validation_only",
            schema["$defs"]["veridocBuildProvenance"]["properties"][
                "execution_attestation_status"
            ]["const"],
        )
        self.assertEqual(
            APPROVED_TASK_REVISION,
            schema["$defs"]["run"]["properties"]["task_revision"]["const"],
        )
        self.assertEqual(
            PINNED_TASK_PACKAGE_PATH,
            schema["$defs"]["run"]["properties"]["task_package_path"]["const"],
        )
        self.assertEqual(
            PINNED_TASK_PACKAGE_SHA256,
            schema["$defs"]["run"]["properties"]["task_package_sha256"]["const"],
        )
        self.assertEqual(
            APPROVED_GOLD_ANSWER_REVISION,
            schema["$defs"]["run"]["properties"]["gold_answer_revision"]["const"],
        )
        self.assertEqual(
            PINNED_GOLD_PACKAGE_PATH,
            schema["$defs"]["run"]["properties"]["gold_package_path"]["const"],
        )
        self.assertEqual(
            PINNED_GOLD_PACKAGE_SHA256,
            schema["$defs"]["run"]["properties"]["gold_package_sha256"]["const"],
        )
        self.assertEqual(
            APPROVED_CHECKLIST_REVISION,
            schema["$defs"]["run"]["properties"]["checklist_revision"]["const"],
        )
        self.assertEqual(
            PINNED_CHECKLIST_PACKAGE_PATH,
            schema["$defs"]["run"]["properties"]["checklist_package_path"][
                "const"
            ],
        )
        self.assertEqual(
            PINNED_CHECKLIST_PACKAGE_SHA256,
            schema["$defs"]["run"]["properties"]["checklist_package_sha256"][
                "const"
            ],
        )
        self.assertEqual(
            {
                "schema_version",
                "run_claims_sha256",
            },
            set(schema["$defs"]["sealedEvidenceEnvelope"]["required"]),
        )
        self.assertEqual(
            "veridoc-mvp-sealed-evidence-envelope/v1",
            schema["$defs"]["sealedEvidenceEnvelope"]["properties"][
                "schema_version"
            ]["const"],
        )

    def test_outcome_artifact_rules_match_schema_and_validator(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        schema_rules: dict[str, tuple[str, ...]] = {}
        for conditional in schema["$defs"]["run"]["allOf"]:
            outcome = (
                conditional.get("if", {})
                .get("properties", {})
                .get("outcome", {})
                .get("const")
            )
            artifact_rule = (
                conditional.get("then", {})
                .get("properties", {})
                .get("sealed_artifact_kind")
            )
            if outcome is None or artifact_rule is None:
                continue
            schema_rules[outcome] = tuple(
                artifact_rule.get("enum", [artifact_rule.get("const")])
            )

        self.assertEqual(
            ALLOWED_SEALED_ARTIFACT_KINDS_BY_OUTCOME,
            schema_rules,
        )
        all_artifact_kinds = {
            artifact_kind
            for allowed in ALLOWED_SEALED_ARTIFACT_KINDS_BY_OUTCOME.values()
            for artifact_kind in allowed
        }
        for outcome, allowed in ALLOWED_SEALED_ARTIFACT_KINDS_BY_OUTCOME.items():
            for artifact_kind in all_artifact_kinds:
                with self.subTest(
                    outcome=outcome,
                    artifact_kind=artifact_kind,
                ):
                    record = copy.deepcopy(self.valid_record)
                    run = record["runs"][0]
                    run["outcome"] = outcome
                    run["blocker_code"] = (
                        None
                        if outcome == "approved"
                        else "approval_unavailable"
                    )
                    run["sealed_artifact_kind"] = artifact_kind
                    if artifact_kind == "blocked_attempt_envelope":
                        run["sealed_artifact_path"] = None
                        run["output_artifact_sha256"] = None
                    _seal_evidence_envelope(run)
                    artifact_errors = [
                        error
                        for error in _validate_record(record)
                        if "sealed_artifact_kind" in error
                    ]
                    self.assertEqual(
                        artifact_kind in allowed,
                        not artifact_errors,
                    )

    def test_synthetic_validation_example_is_valid_and_recomputable(self) -> None:
        self.assertEqual([], _validate_record(self.valid_record))
        summary = _summarize_record(self.valid_record)
        self.assertEqual(6, summary["required_runs"])
        self.assertEqual(6, summary["recorded_runs"])
        self.assertEqual(2, summary["eligible_pair_count"])
        self.assertEqual(1, summary["ineligible_pair_count"])
        self.assertEqual(
            PINNED_GOLD_PACKAGE_SHA256,
            summary["gold_package_sha256"],
        )
        self.assertEqual(
            PINNED_TASK_PACKAGE_SHA256,
            summary["task_package_sha256"],
        )
        self.assertEqual(
            PINNED_CHECKLIST_PACKAGE_SHA256,
            summary["checklist_package_sha256"],
        )
        self.assertEqual(
            "unapproved_validation_only",
            summary["gold_package_approval_status"],
        )
        self.assertFalse(summary["execution_attestation_ready"])
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
            if pair["participant_id"] == "P-386BEEA7-81DC-408A-B045-D73A233C0DC5"
        )
        self.assertFalse(blocked_pair["eligible"])
        self.assertEqual("blocked", blocked_pair["veridoc_outcome"])
        self.assertEqual(
            "approval_unavailable",
            blocked_pair["veridoc_blocker_code"],
        )
        self.assertFalse(summary["efficiency_target_met"])
        self.assertFalse(summary["structured_high_risk_targets_ready"])
        self.assertEqual(
            APPROVED_MANIFEST_CONTRACT_SHA256,
            summary["manifest_contract_sha256"],
        )

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
                errors = _validate_record(mutated)
                self.assertIn(example["expected_error"], errors)

    def test_completed_record_requires_all_five_manifest_cases(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["study_status"] = "completed"
        self.assertIn(
            "completed study must declare all five Phase 12 case_ids",
            _validate_record(record),
        )

    def test_unapproved_structured_targets_keep_efficiency_fail_closed(
        self,
    ) -> None:
        record = _completed_record(self.valid_record)
        self.assertEqual([], _validate_record(record))
        summary = _summarize_record(record)
        self.assertEqual(0, summary["eligible_pair_count"])
        self.assertEqual(15, summary["ineligible_pair_count"])
        self.assertIsNone(summary["paired_median_reduction_percent"])
        self.assertTrue(
            all(pair["calculable"] for pair in summary["pair_results"])
        )
        self.assertTrue(
            all(
                {
                    "manual_seconds",
                    "veridoc_seconds",
                    "reduction_percent",
                }
                <= pair.keys()
                for pair in summary["pair_results"]
            )
        )
        self.assertEqual(
            {50.0},
            {
                pair["reduction_percent"]
                for pair in summary["pair_results"]
            },
        )
        self.assertFalse(summary["execution_attestation_ready"])
        self.assertTrue(summary["all_required_runs_accounted"])
        self.assertFalse(summary["structured_high_risk_targets_ready"])
        self.assertFalse(summary["efficiency_target_met"])

    def test_expected_high_risk_count_is_bound_to_pinned_gold_case(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["case_ids"] = ["mvp-scanned-pdf-001"]
        for run in record["runs"]:
            run["case_id"] = "mvp-scanned-pdf-001"
            run["source_fixture_id"] = APPROVED_FIXTURES[
                "mvp-scanned-pdf-001"
            ][0]
            run["source_fixture_path"] = APPROVED_FIXTURES[
                "mvp-scanned-pdf-001"
            ][1]
            run["source_fixture_sha256"] = APPROVED_FIXTURES[
                "mvp-scanned-pdf-001"
            ][2]
            (
                run["conversion_mode"],
                run["target_artifact_type"],
            ) = APPROVED_TARGET_FORMATS["mvp-scanned-pdf-001"]
            run["gold_case_sha256"] = PINNED_GOLD_CASE_SHA256[
                "mvp-scanned-pdf-001"
            ]
            run["task_case_sha256"] = PINNED_TASK_CASE_SHA256[
                "mvp-scanned-pdf-001"
            ]
            run["run_id"] = (
                f"RUN-{run['participant_id']}-MVP-SCANNED-PDF-001-"
                f"{str(run['arm']).upper()}-{run['attempt_number']}"
            )
            run["high_risk_expected_count"] = 0
        self.assertIn(
            "run[0].high_risk_expected_count must match pinned gold package "
            "count 1 for mvp-scanned-pdf-001",
            _validate_record(record),
        )

    def test_run_fixture_identity_is_bound_to_approved_source(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["source_fixture_sha256"] = "0" * 64
        self.assertIn(
            "run[0].source_fixture_sha256 must match approved manifest value "
            "'8d3f4c25af465eb03bb1b2a624d14de27b1f777a4ec2cd5674563335d2b58cf1'",
            _validate_record(record),
        )

    def test_run_target_format_is_bound_to_approved_manifest(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["conversion_mode"] = "pdf_to_word"
        record["runs"][0]["target_artifact_type"] = "docx"
        errors = _validate_record(record)
        self.assertIn(
            "run[0].conversion_mode must match approved manifest value "
            "'word_to_excel'",
            errors,
        )
        self.assertIn(
            "run[0].target_artifact_type must match approved manifest value "
            "'xlsx'",
            errors,
        )

    def test_run_scoring_is_bound_to_pinned_gold_content(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["gold_package_sha256"] = "0" * 64
        record["runs"][0]["gold_case_sha256"] = "f" * 64
        errors = _validate_record(record)
        self.assertIn(
            "run[0].gold_package_sha256 must match pinned gold package value "
            f"'{PINNED_GOLD_PACKAGE_SHA256}'",
            errors,
        )
        self.assertIn(
            "run[0].gold_case_sha256 must bind pinned gold case "
            "mvp-word-001 as "
            f"{PINNED_GOLD_CASE_SHA256['mvp-word-001']}",
            errors,
        )

    def test_run_is_bound_to_pinned_timed_task_and_arm_contract(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["task_package_sha256"] = "0" * 64
        record["runs"][0]["task_case_sha256"] = "f" * 64
        record["runs"][0]["task_arm_sha256"] = PINNED_TASK_ARM_SHA256[
            "veridoc"
        ]
        errors = _validate_record(record)
        self.assertIn(
            "run[0].task_package_sha256 must match pinned timed-task "
            f"package value '{PINNED_TASK_PACKAGE_SHA256}'",
            errors,
        )
        self.assertIn(
            "run[0].task_case_sha256 must bind pinned timed-task case "
            "mvp-word-001 as "
            f"{PINNED_TASK_CASE_SHA256['mvp-word-001']}",
            errors,
        )
        self.assertIn(
            "run[0].task_arm_sha256 must bind pinned manual assistance "
            f"contract as {PINNED_TASK_ARM_SHA256['manual']}",
            errors,
        )

    def test_run_is_bound_to_pinned_completion_checklist(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["checklist_package_sha256"] = "0" * 64
        record["runs"][0]["checklist_case_sha256"] = "f" * 64
        errors = _validate_record(record)
        self.assertIn(
            "run[0].checklist_package_sha256 must match pinned "
            "completion-checklist package value "
            f"'{PINNED_CHECKLIST_PACKAGE_SHA256}'",
            errors,
        )
        self.assertIn(
            "run[0].checklist_case_sha256 must bind pinned completion "
            "checklist for mvp-word-001 as "
            f"{PINNED_CHECKLIST_CASE_SHA256['mvp-word-001']}",
            errors,
        )

    def test_run_id_must_be_generated_from_pseudonymous_fields(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["run_id"] = "RUN-ALICE-EMPLOYEE-123"
        errors = _validate_record(record)
        self.assertIn("run[0].run_id is invalid", errors)

    def test_study_id_must_be_an_opaque_uuid(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["study_id"] = "HR-ALICE-EMPLOYEE-123"
        self.assertIn(
            "study_id must be an opaque HR-prefixed UUIDv4",
            _validate_record(record),
        )

    def test_approval_versions_must_be_privacy_safe_opaque_references(
        self,
    ) -> None:
        record = copy.deepcopy(self.valid_record)
        record["consent_approval"]["consent_form_version"] = (
            "ALICE-EMPLOYEE-123"
        )
        record["participants"][0]["consent_form_version"] = (
            "ALICE-EMPLOYEE-123"
        )
        record["quality_approval"]["external_record_version"] = (
            "ALICE-EMPLOYEE-123"
        )
        errors = _validate_record(record)
        self.assertIn(
            "consent_approval.consent_form_version must be an opaque "
            "CF-prefixed UUIDv4",
            errors,
        )
        self.assertIn(
            "participant[0].consent_form_version must be an opaque "
            "CF-prefixed UUIDv4",
            errors,
        )
        self.assertIn(
            "quality_approval.external_record_version must be an opaque "
            "QAR-prefixed UUIDv4",
            errors,
        )

    def test_participant_id_cannot_be_a_prefixed_employee_number(self) -> None:
        record = copy.deepcopy(self.valid_record)
        original = record["participants"][0]["participant_id"]
        direct_identifier = "P123456789"
        record["participants"][0]["participant_id"] = direct_identifier
        for run in record["runs"]:
            if run["participant_id"] == original:
                run["participant_id"] = direct_identifier
                run["run_id"] = run["run_id"].replace(
                    original,
                    direct_identifier,
                )
        errors = _validate_record(record)
        self.assertIn(
            "participant[0].participant_id must be an opaque P-prefixed UUIDv4",
            errors,
        )
        self.assertIn("run[0].run_id is invalid", errors)

    def test_rfc3339_timestamp_lexical_form_is_enforced(self) -> None:
        for value in (
            "2026-07-26X01:00:00Z",
            "2026-07-26T01:00Z",
            "2026-07-26T01:00:00",
        ):
            with self.subTest(value=value):
                record = copy.deepcopy(self.valid_record)
                record["runs"][0]["started_at"] = value
                self.assertIn(
                    "run[0].started_at must be a UTC RFC 3339 timestamp",
                    _validate_record(record),
                )

    def test_rfc3339_nanosecond_precision_is_preserved(self) -> None:
        for suffix in ("Z", "+00:00"):
            with self.subTest(suffix=suffix):
                record = copy.deepcopy(self.valid_record)
                record["runs"][0]["started_at"] = (
                    f"2026-07-26T01:00:00.000000001{suffix}"
                )
                record["runs"][0]["ended_at"] = (
                    f"2026-07-26T01:00:00.000000002{suffix}"
                )
                _seal_evidence_envelope(record["runs"][0])
                self.assertEqual([], _validate_record(record))

    def test_every_included_run_requires_completed_checklist(self) -> None:
        blocked = next(
            run
            for run in self.valid_record["runs"]
            if run["outcome"] == "blocked"
        )
        self.assertTrue(blocked["checklist_complete"])

        record = copy.deepcopy(self.valid_record)
        blocked = next(
            run for run in record["runs"] if run["outcome"] == "blocked"
        )
        blocked["checklist_complete"] = False
        self.assertIn(
            "checklist_complete must be true for every included outcome",
            "; ".join(_validate_record(record)),
        )

    def test_assessor_counts_are_bound_to_unique_sealed_artifacts(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["sealed_artifact_sha256"] = "0" * 64
        self.assertIn(
            "run[0].sealed_artifact_sha256 must be lowercase SHA-256",
            _validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        record["runs"][1]["sealed_artifact_record_id"] = record["runs"][0][
            "sealed_artifact_record_id"
        ]
        self.assertIn(
            "duplicate sealed_artifact_record_id: "
            + record["runs"][0]["sealed_artifact_record_id"],
            _validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["sealed_artifact_kind"] = (
            "blocked_attempt_envelope"
        )
        self.assertIn(
            "run[0].sealed_artifact_kind must be output_artifact "
            "for approved outcome",
            _validate_record(record),
        )

    def test_sealed_evidence_envelope_is_canonical_and_run_bound(self) -> None:
        blocked_index = next(
            index
            for index, run in enumerate(self.valid_record["runs"])
            if run["sealed_artifact_kind"] == "blocked_attempt_envelope"
        )
        blocked = self.valid_record["runs"][blocked_index]
        self.assertEqual(
            build_sealed_evidence_envelope(blocked),
            blocked["sealed_evidence_envelope"],
        )
        self.assertEqual(
            set(blocked) - {"sealed_evidence_envelope", "sealed_artifact_sha256"},
            set(build_run_claims(blocked)),
        )
        self.assertTrue(
            {
                "high_risk_expected_count",
                "high_risk_miss_count",
                "over_detection_count",
                "task_package_sha256",
                "gold_package_sha256",
                "checklist_package_sha256",
            }.issubset(build_run_claims(blocked))
        )

        for field, replacement in (
            ("blocker_code", "other_controlled"),
            ("started_at", "2026-07-26T02:59:59Z"),
            ("run_id", self.valid_record["runs"][0]["run_id"]),
            ("high_risk_expected_count", 1),
            ("high_risk_miss_count", 1),
            ("over_detection_count", 1),
        ):
            with self.subTest(field=field):
                record = copy.deepcopy(self.valid_record)
                record["runs"][blocked_index][field] = replacement
                self.assertIn(
                    f"run[{blocked_index}].sealed_evidence_envelope."
                    "run_claims_sha256 must match the run",
                    _validate_record(record),
                )

        record = copy.deepcopy(self.valid_record)
        record["runs"][blocked_index]["sealed_evidence_envelope"][
            "schema_version"
        ] = "veridoc-mvp-sealed-evidence-envelope/v0"
        self.assertIn(
            f"run[{blocked_index}].sealed_evidence_envelope.schema_version "
            "must match the run",
            _validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        record["runs"][blocked_index]["sealed_evidence_envelope"][
            "uncontrolled_note"
        ] = "mutable"
        self.assertIn(
            f"unknown run[{blocked_index}].sealed_evidence_envelope field: "
            "uncontrolled_note",
            _validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        record["runs"][blocked_index]["sealed_artifact_sha256"] = "1" * 64
        self.assertIn(
            f"run[{blocked_index}].sealed_artifact_sha256 must match the "
            "canonical sealed_evidence_envelope",
            _validate_record(record),
        )

        output = self.valid_record["runs"][0]
        self.assertEqual(
            build_sealed_evidence_envelope(output),
            output["sealed_evidence_envelope"],
        )
        for field in (
            "high_risk_expected_count",
            "high_risk_miss_count",
            "over_detection_count",
        ):
            with self.subTest(output_claim=field):
                record = copy.deepcopy(self.valid_record)
                record["runs"][0][field] += 1
                self.assertIn(
                    "run[0].sealed_evidence_envelope.run_claims_sha256 "
                    "must match the run",
                    _validate_record(record),
                )

    def test_recomputed_embedded_seal_requires_retained_record_match(self) -> None:
        retained_by_id = {
            run["sealed_artifact_record_id"]: _synthetic_sealed_evidence_bytes(
                run
            )
            for run in self.valid_record["runs"]
        }
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["over_detection_count"] = 7
        _seal_evidence_envelope(record["runs"][0])

        errors = _raw_validate_record(
            record,
            artifact_resolver=_synthetic_output_artifact_bytes,
            sealed_evidence_resolver=retained_by_id.__getitem__,
        )
        self.assertIn(
            "run[0].sealed_evidence_envelope must match the independently "
            "retained sealed evidence record",
            errors,
        )
        self.assertIn(
            "run[0].sealed_artifact_sha256 must match the independently "
            "retained sealed evidence record",
            errors,
        )

    def test_recomputed_study_seal_requires_retained_record_match(self) -> None:
        retained_study = _synthetic_study_evidence_bytes(self.valid_record)
        record = copy.deepcopy(self.valid_record)
        record["consent_approval"]["approved_at"] = "2026-07-26T00:04:00Z"
        record["participants"][0]["consented_at"] = "2026-07-26T00:05:00Z"
        _seal_study_evidence_envelope(record)

        errors = _raw_validate_record(
            record,
            artifact_resolver=_synthetic_output_artifact_bytes,
            sealed_evidence_resolver=_synthetic_sealed_evidence_resolver(record),
            study_evidence_resolver=lambda _record_id: retained_study,
            assessor_attestation_resolver=(
                _synthetic_assessor_attestation_resolver(record)
            ),
        )
        self.assertIn(
            "study_evidence_envelope must match the independently retained "
            "study evidence record",
            errors,
        )
        self.assertIn(
            "study_evidence_sha256 must match the independently retained "
            "study evidence record",
            errors,
        )

    def test_assessment_is_bound_to_retained_independent_assessor(self) -> None:
        retained_run_by_id = {
            run["sealed_artifact_record_id"]: _synthetic_sealed_evidence_bytes(
                run
            )
            for run in self.valid_record["runs"]
        }
        retained_assessor_by_id = {
            run[
                "assessor_attestation_record_id"
            ]: _synthetic_assessor_attestation_bytes(run)
            for run in self.valid_record["runs"]
        }
        record = copy.deepcopy(self.valid_record)
        run = record["runs"][0]
        run["independent_assessor_id"] = (
            "A-8F7D947A-1CE2-4B69-A31A-5A49BA1E94FC"
        )
        _seal_assessor_attestation(run)
        _seal_evidence_envelope(run)

        errors = _raw_validate_record(
            record,
            artifact_resolver=_synthetic_output_artifact_bytes,
            sealed_evidence_resolver=retained_run_by_id.__getitem__,
            study_evidence_resolver=_synthetic_study_evidence_resolver(record),
            assessor_attestation_resolver=retained_assessor_by_id.__getitem__,
        )
        self.assertIn(
            "run[0] assessment must match the independently retained "
            "assessor attestation",
            errors,
        )
        self.assertIn(
            "run[0].sealed_evidence_envelope must match the independently "
            "retained sealed evidence record",
            errors,
        )

        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["independent_assessor_id"] = (
            "A-" + record["runs"][0]["participant_id"][2:]
        )
        self.assertIn(
            "run[0].independent_assessor_id must not identify the participant",
            _validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        record["runs"][1]["assessor_attestation_record_id"] = record["runs"][0][
            "assessor_attestation_record_id"
        ]
        self.assertIn(
            "duplicate assessor_attestation_record_id: "
            + record["runs"][0]["assessor_attestation_record_id"],
            _validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["assessment_completed_at"] = record["runs"][0][
            "ended_at"
        ]
        self.assertIn(
            "run[0].assessment_completed_at must follow ended_at",
            _validate_record(record),
        )

    def test_retained_sealed_evidence_resolver_failures_are_controlled(
        self,
    ) -> None:
        def missing_record(record_id: str) -> bytes:
            raise KeyError(record_id)

        cases = (
            (
                "missing",
                missing_record,
                "cannot resolve an independently retained sealed evidence record",
            ),
            (
                "wrong-type",
                lambda _run: None,
                "sealed evidence resolver must return bytes",
            ),
            (
                "invalid-json",
                lambda _run: b'{"run_claims_sha256":',
                "independently retained sealed evidence record must be strict "
                "UTF-8 JSON",
            ),
        )
        for name, resolver, expected_error in cases:
            with self.subTest(name=name):
                self.assertTrue(
                    any(
                        expected_error in error
                        for error in _raw_validate_record(
                            self.valid_record,
                            artifact_resolver=_synthetic_output_artifact_bytes,
                            sealed_evidence_resolver=resolver,
                        )
                    )
                )

    def test_study_and_assessor_resolver_failures_are_controlled(self) -> None:
        def missing_record(record_id: str) -> bytes:
            raise KeyError(record_id)

        study_cases = (
            (
                missing_record,
                "cannot resolve an independently retained study evidence record",
            ),
            (lambda _record_id: None, "study evidence resolver must return bytes"),
            (
                lambda _record_id: b'{"study_claims_sha256":',
                "independently retained study evidence record must be strict "
                "UTF-8 JSON",
            ),
        )
        for resolver, expected_error in study_cases:
            with self.subTest(kind="study", expected_error=expected_error):
                errors = _raw_validate_record(
                    self.valid_record,
                    artifact_resolver=_synthetic_output_artifact_bytes,
                    sealed_evidence_resolver=(
                        _synthetic_sealed_evidence_resolver(self.valid_record)
                    ),
                    study_evidence_resolver=resolver,
                    assessor_attestation_resolver=(
                        _synthetic_assessor_attestation_resolver(
                            self.valid_record
                        )
                    ),
                )
                self.assertTrue(
                    any(expected_error in error for error in errors)
                )

        assessor_cases = (
            (
                missing_record,
                "cannot resolve an independently retained assessor attestation",
            ),
            (
                lambda _record_id: None,
                "assessor attestation resolver must return bytes",
            ),
            (
                lambda _record_id: b'{"assessor_id":',
                "independently retained assessor attestation must be strict "
                "UTF-8 JSON",
            ),
        )
        for resolver, expected_error in assessor_cases:
            with self.subTest(kind="assessor", expected_error=expected_error):
                errors = _raw_validate_record(
                    self.valid_record,
                    artifact_resolver=_synthetic_output_artifact_bytes,
                    sealed_evidence_resolver=(
                        _synthetic_sealed_evidence_resolver(self.valid_record)
                    ),
                    study_evidence_resolver=(
                        _synthetic_study_evidence_resolver(self.valid_record)
                    ),
                    assessor_attestation_resolver=resolver,
                )
                self.assertTrue(
                    any(expected_error in error for error in errors)
                )

    def test_output_artifacts_are_resolved_and_hashed(self) -> None:
        self.assertEqual([], _raw_validate_record(self.valid_record))

        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["output_artifact_sha256"] = "1" * 64
        self.assertIn(
            "run[0].output_artifact_sha256 must match the resolved "
            "output_artifact bytes",
            _raw_validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["sealed_artifact_path"] = record["runs"][1][
            "sealed_artifact_path"
        ]
        self.assertIn(
            "run[0].sealed_artifact_path must be derived from "
            "sealed_artifact_record_id",
            _raw_validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["sealed_artifact_path"] = (
            "sealed_artifacts/Alice-Employee-123.bin"
        )
        self.assertIn(
            "run[0].sealed_artifact_path must be derived from "
            "sealed_artifact_record_id",
            _raw_validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        missing_record_id = _opaque_record_id("SAR", "missing-output-artifact")
        record["runs"][0]["sealed_artifact_record_id"] = missing_record_id
        record["runs"][0]["sealed_artifact_path"] = _sealed_artifact_path(
            missing_record_id
        )
        _seal_evidence_envelope(record["runs"][0])
        self.assertIn(
            "run[0].sealed_artifact_path cannot be resolved within "
            "artifact_root",
            _raw_validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["sealed_artifact_path"] = "../outside.txt"
        self.assertIn(
            "run[0].sealed_artifact_path must be derived from "
            "sealed_artifact_record_id",
            _raw_validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        blocked_index = next(
            index
            for index, run in enumerate(record["runs"])
            if run["sealed_artifact_kind"] == "blocked_attempt_envelope"
        )
        record["runs"][blocked_index]["sealed_artifact_path"] = (
            "sealed_artifacts/unexpected.bin"
        )
        self.assertIn(
            f"run[{blocked_index}].sealed_artifact_path must be null for "
            "blocked_attempt_envelope",
            _validate_record(record),
        )

    def test_veridoc_runs_require_approved_build_provenance(self) -> None:
        record = copy.deepcopy(self.valid_record)
        veridoc = next(
            run for run in record["runs"] if run["arm"] == "veridoc"
        )
        provenance = veridoc["veridoc_build_provenance"]
        assert isinstance(provenance, dict)
        provenance["product_commit"] = "0" * 40
        self.assertIn(
            "run[1].veridoc_build_provenance.product_commit must be "
            f"'{APPROVED_PRODUCT_COMMIT}'",
            _validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["veridoc_build_provenance"] = copy.deepcopy(
            BUILD_PROVENANCE
        )
        self.assertIn(
            "run[0].veridoc_build_provenance must be null for manual arm",
            _validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        veridoc = next(
            run for run in record["runs"] if run["arm"] == "veridoc"
        )
        veridoc["veridoc_build_provenance"] = None
        self.assertIn(
            "run[1].veridoc_build_provenance must be an object",
            _validate_record(record),
        )

        record = copy.deepcopy(self.valid_record)
        veridoc = next(
            run for run in record["runs"] if run["arm"] == "veridoc"
        )
        provenance = veridoc["veridoc_build_provenance"]
        assert isinstance(provenance, dict)
        provenance["source_tree_listing_sha256"] = "a" * 64
        attestation_payload = {
            field: provenance[field]
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
        provenance["attestation_sha256"] = hashlib.sha256(
            json.dumps(
                attestation_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        errors = _validate_record(record)
        self.assertTrue(
            any(
                "source_tree_listing_sha256 must match the reproducibly "
                "derived approved source-tree listing"
                in error
                for error in errors
            )
        )
        self.assertFalse(
            any(
                "run[1].veridoc_build_provenance.attestation_sha256 "
                "must bind the canonical provenance record"
                in error
                for error in errors
            )
        )

        record = copy.deepcopy(self.valid_record)
        veridoc = next(
            run for run in record["runs"] if run["arm"] == "veridoc"
        )
        provenance = veridoc["veridoc_build_provenance"]
        assert isinstance(provenance, dict)
        provenance["attestation_sha256"] = "a" * 64
        self.assertTrue(
            any(
                "attestation_sha256 must bind the canonical provenance record"
                in error
                for error in _validate_record(record)
            )
        )

        record = copy.deepcopy(self.valid_record)
        veridoc = next(
            run for run in record["runs"] if run["arm"] == "veridoc"
        )
        provenance = veridoc["veridoc_build_provenance"]
        assert isinstance(provenance, dict)
        provenance["execution_attestation_status"] = "verified_external"
        self.assertIn(
            "run[1].veridoc_build_provenance.execution_attestation_status "
            "must be 'unverified_validation_only'",
            _validate_record(record),
        )

    def test_attempt_numbers_must_be_contiguous_from_one(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["attempt_number"] = 2
        self.assertIn(
            "P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A/mvp-word-001/manual attempt_number values "
            "must be contiguous from 1",
            _validate_record(record),
        )

    def test_attempt_contiguity_check_is_proportional_to_observed_attempts(
        self,
    ) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["attempt_number"] = 100_000_000
        self.assertIn(
            "P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A/mvp-word-001/manual attempt_number values "
            "must be contiguous from 1",
            _validate_record(record),
        )

    def test_attempt_timestamps_must_follow_attempt_number_order(self) -> None:
        record = _completed_record(self.valid_record)
        _add_excluded_veridoc_retry(record)
        runs = record["runs"]
        assert isinstance(runs, list)
        first = next(
            run
            for run in runs
            if isinstance(run, dict)
            and run["participant_id"] == "P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A"
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
            "P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A/mvp-scanned-pdf-001/veridoc attempt timestamps "
            "must follow attempt_number order",
            _validate_record(record),
        )

    def test_invalid_timing_exclusions_are_retained_but_not_compared(
        self,
    ) -> None:
        for started_at, ended_at in (
            (
                "2026-07-28T00:00:00.000000001Z",
                "2026-07-28T00:00:00.000000001Z",
            ),
            (
                "2026-07-28T00:00:00.000000002Z",
                "2026-07-28T00:00:00.000000001Z",
            ),
        ):
            with self.subTest(started_at=started_at, ended_at=ended_at):
                record = _completed_record(self.valid_record)
                _add_excluded_veridoc_retry(record)
                retry = record["runs"][-1]
                retry["started_at"] = started_at
                retry["ended_at"] = ended_at
                retry["assessment_completed_at"] = (
                    "2026-07-28T00:00:00.000000003Z"
                )
                retry["excluded_pause_seconds"] = 60
                retry["exclusion_reason_code"] = "invalid_timing"
                _seal_assessor_attestation(retry)
                _seal_evidence_envelope(retry)
                self.assertEqual([], _validate_record(record))
                summary = _summarize_record(record)
                self.assertEqual(1, summary["excluded_runs"])
                self.assertEqual(15, len(summary["pair_results"]))

    def test_invalid_timing_starts_enforce_arm_order(self) -> None:
        record = _completed_record(self.valid_record)
        _add_excluded_veridoc_retry(record)
        participant_id = "P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A"
        manual_starts = [
            datetime.fromisoformat(
                str(run["started_at"]).replace("Z", "+00:00")
            )
            for run in record["runs"]
            if run["participant_id"] == participant_id
            and run["arm"] == "manual"
        ]
        retry = record["runs"][-1]
        retry_started_at = min(manual_starts) - timedelta(seconds=1)
        retry["started_at"] = _utc_text(retry_started_at)
        retry["ended_at"] = _utc_text(
            retry_started_at - timedelta(seconds=1)
        )
        retry["excluded_pause_seconds"] = 60
        retry["exclusion_reason_code"] = "invalid_timing"

        self.assertIn(
            f"{participant_id} timed runs do not follow declared arm_order",
            _validate_record(record),
        )

    def test_invalid_timing_attempts_remain_in_activity_timeline(
        self,
    ) -> None:
        record = _completed_record(self.valid_record)
        _add_excluded_veridoc_retry(record)
        retry = record["runs"][-1]
        retry["started_at"] = "2026-07-26T00:05:30Z"
        retry["ended_at"] = "2026-07-26T00:05:30Z"
        retry["excluded_pause_seconds"] = 60
        retry["exclusion_reason_code"] = "invalid_timing"

        errors = _validate_record(record)
        participant_id = "P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A"
        self.assertIn(
            f"{participant_id}.consented_at must precede every timed run",
            errors,
        )
        self.assertIn(
            f"{participant_id}.manual_practice_completed_at must precede "
            "every timed run",
            errors,
        )
        self.assertIn(
            f"{participant_id}.veridoc_practice_completed_at must precede "
            "every timed run",
            errors,
        )

    def test_invalid_timing_activity_must_precede_withdrawal(self) -> None:
        record = _completed_record(self.valid_record)
        _add_withdrawn_participant(record)
        invalid_timing_attempt = record["runs"][-1]
        invalid_timing_attempt["started_at"] = "2026-08-10T01:03:00Z"
        invalid_timing_attempt["ended_at"] = "2026-08-10T01:03:00Z"
        invalid_timing_attempt["excluded_pause_seconds"] = 60
        invalid_timing_attempt["exclusion_reason_code"] = "invalid_timing"

        self.assertIn(
            "run[30] must start before "
            "P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C withdrawal",
            _validate_record(record),
        )

    def test_invalid_timing_exception_is_narrowly_scoped(self) -> None:
        included = copy.deepcopy(self.valid_record)
        included["runs"][0]["ended_at"] = included["runs"][0]["started_at"]
        self.assertIn(
            "ended_at must be after started_at",
            _validate_record(included),
        )

        other_exclusion = _completed_record(self.valid_record)
        _add_excluded_veridoc_retry(other_exclusion)
        retry = other_exclusion["runs"][-1]
        retry["ended_at"] = retry["started_at"]
        self.assertEqual("technical_failure", retry["exclusion_reason_code"])
        self.assertIn(
            "ended_at must be after started_at",
            _validate_record(other_exclusion),
        )

    def test_unknown_declared_case_is_rejected_without_crashing(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["case_ids"] = ["unknown-case"]
        for run in record["runs"]:
            run["case_id"] = "unknown-case"
        self.assertIn("unknown case_ids: unknown-case", _validate_record(record))

    def test_gold_answer_must_be_attested_hidden_until_timing_ends(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["gold_answer_hidden_until_ended_at"] = False
        self.assertIn(
            "run[0].gold_answer_hidden_until_ended_at may be false only for "
            "an excluded protocol_deviation",
            _validate_record(record),
        )

        excluded = _completed_record(self.valid_record)
        _add_excluded_veridoc_retry(excluded)
        excluded["runs"][-1]["gold_answer_hidden_until_ended_at"] = False
        excluded["runs"][-1]["exclusion_reason_code"] = "protocol_deviation"
        _seal_evidence_envelope(excluded["runs"][-1])
        self.assertEqual([], _validate_record(excluded))

        excluded["runs"][-1]["exclusion_reason_code"] = "technical_failure"
        _seal_evidence_envelope(excluded["runs"][-1])
        self.assertIn(
            "run[30].gold_answer_hidden_until_ended_at may be false only for "
            "an excluded protocol_deviation",
            _validate_record(excluded),
        )

    def test_gold_comparison_is_independent_and_withheld_from_participant(
        self,
    ) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["gold_answer_compared_by_role"] = "participant"
        record["runs"][0][
            "gold_answer_comparison_withheld_from_participant"
        ] = False
        errors = _validate_record(record)
        self.assertIn(
            "run[0].gold_answer_compared_by_role must be independent_assessor",
            errors,
        )
        self.assertIn(
            "run[0].gold_answer_comparison_withheld_from_participant "
            "must be true",
            errors,
        )

    def test_run_revisions_must_match_the_approved_protocol_contract(self) -> None:
        missing = copy.deepcopy(self.valid_record)
        del missing["runs"][0]["checklist_revision"]
        self.assertIn(
            "missing run[0] field: checklist_revision",
            _validate_record(missing),
        )

        record = copy.deepcopy(self.valid_record)
        record["runs"][0]["task_revision"] = "unapproved-task-v2"
        record["runs"][0]["gold_answer_revision"] = "unapproved-gold-v2"
        record["runs"][0]["checklist_revision"] = "unapproved-checklist-v2"
        errors = _validate_record(record)
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
        self.assertIn(
            "run[0].checklist_revision must match approved protocol revision "
            f"{APPROVED_CHECKLIST_REVISION}",
            errors,
        )

    def test_case_revisions_are_fixed_across_cohort_and_excluded_attempts(
        self,
    ) -> None:
        record = copy.deepcopy(self.valid_record)
        for run in record["runs"]:
            if run["participant_id"] == "P-E136C88A-C95D-47D2-AB32-F5C3AD66F2F5":
                run["task_revision"] = "different-task-v2"
        self.assertIn(
            "all retained runs for mvp-word-001 must use the same task_revision",
            _validate_record(record),
        )

        completed = _completed_record(self.valid_record)
        _add_excluded_veridoc_retry(completed)
        completed["runs"][-1]["gold_answer_revision"] = "different-gold-v2"
        self.assertIn(
            "all retained runs for mvp-scanned-pdf-001 must use the same "
            "gold_answer_revision",
            _validate_record(completed),
        )

        checklist_drift = copy.deepcopy(self.valid_record)
        checklist_drift["runs"][1]["checklist_revision"] = (
            "different-checklist-v2"
        )
        self.assertIn(
            "all retained runs for mvp-word-001 must use the same "
            "checklist_revision",
            _validate_record(checklist_drift),
        )
        self.assertIn(
            "paired runs must use the same checklist_revision",
            _validate_record(checklist_drift),
        )

    def test_approval_must_strictly_precede_every_run(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["consent_approval"]["approved_at"] = record["runs"][0]["started_at"]
        self.assertIn(
            "consent approval must precede every timed run",
            _validate_record(record),
        )
        record = copy.deepcopy(self.valid_record)
        record["quality_approval"]["approved_at"] = record["runs"][0]["started_at"]
        self.assertIn(
            "quality approval must precede every timed run",
            _validate_record(record),
        )

    def test_quality_approval_is_separate_and_required(self) -> None:
        record = copy.deepcopy(self.valid_record)
        del record["quality_approval"]
        self.assertIn(
            "missing record field: quality_approval",
            _validate_record(record),
        )
        record = copy.deepcopy(self.valid_record)
        record["quality_approval"]["approved_by_role"] = "study_owner"
        record["quality_approval"]["external_record_version"] = ""
        errors = _validate_record(record)
        self.assertIn(
            "quality_approval.approved_by_role must be quality_approver",
            errors,
        )
        self.assertIn(
            "quality_approval.external_record_version must be an opaque "
            "QAR-prefixed UUIDv4",
            errors,
        )

    def test_participant_run_intervals_must_not_overlap(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["runs"][1]["started_at"] = record["runs"][0]["started_at"]
        record["runs"][1]["ended_at"] = record["runs"][0]["ended_at"]
        errors = _validate_record(record)
        self.assertTrue(
            any(error.startswith("P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A timed runs overlap:") for error in errors)
        )

    def test_excluded_veridoc_retry_is_reported_per_arm(
        self,
    ) -> None:
        record = _completed_record(self.valid_record)
        _add_excluded_veridoc_retry(record)
        self.assertEqual([], _validate_record(record))
        summary = _summarize_record(record)
        self.assertEqual(1, summary["retry_runs"])
        self.assertEqual(1, summary["excluded_runs"])
        self.assertEqual(1, summary["arm_metrics"]["veridoc"]["retry_runs"])
        self.assertEqual(1, summary["arm_metrics"]["veridoc"]["excluded_runs"])
        self.assertEqual(1, summary["arm_metrics"]["veridoc"]["over_detections"])
        self.assertEqual(
            16,
            summary["arm_metrics"]["veridoc"]["approved_completions"],
        )
        self.assertEqual(1, summary["totals"]["retry_runs"])
        self.assertEqual(1, summary["totals"]["excluded_runs"])
        self.assertEqual(1, summary["totals"]["over_detections"])
        self.assertEqual(31, summary["totals"]["approved_completions"])
        self.assertFalse(summary["efficiency_target_met"])

    def test_withdrawn_participant_attempts_remain_outside_completed_cohort(
        self,
    ) -> None:
        record = _completed_record(self.valid_record)
        _add_withdrawn_participant(record)
        self.assertEqual([], _validate_record(record))
        summary = _summarize_record(record)
        self.assertEqual(30, summary["required_runs"])
        self.assertEqual(31, summary["recorded_runs"])
        self.assertEqual(0, summary["eligible_pair_count"])
        self.assertEqual(20, summary["ineligible_pair_count"])
        self.assertEqual(20, len(summary["pair_results"]))
        self.assertFalse(summary["execution_attestation_ready"])
        self.assertEqual(1, summary["totals"]["excluded_runs"])
        self.assertEqual(31, summary["totals"]["approved_completions"])
        self.assertFalse(summary["efficiency_target_met"])
        withdrawn_pairs = [
            pair
            for pair in summary["pair_results"]
            if pair["participant_id"]
            == "P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C"
        ]
        self.assertEqual(5, len(withdrawn_pairs))
        partial_pair = next(
            pair
            for pair in withdrawn_pairs
            if pair["case_id"] == "mvp-word-001"
        )
        self.assertEqual(["manual"], partial_pair["recorded_arms"])
        self.assertEqual([], partial_pair["included_arms"])
        self.assertEqual(["veridoc"], partial_pair["missing_arms"])
        self.assertEqual("approved", partial_pair["manual_outcome"])
        self.assertTrue(partial_pair["manual_excluded"])
        self.assertEqual(
            "participant_withdrew",
            partial_pair["manual_exclusion_reason_code"],
        )
        missing_pair = next(
            pair
            for pair in withdrawn_pairs
            if pair["case_id"] == "mvp-excel-001"
        )
        self.assertEqual([], missing_pair["recorded_arms"])
        self.assertEqual(["manual", "veridoc"], missing_pair["missing_arms"])
        self.assertIsNone(missing_pair["manual_outcome"])
        self.assertIsNone(missing_pair["veridoc_outcome"])

    def test_withdrawn_partial_pair_availability_matrix_is_reported(
        self,
    ) -> None:
        scenarios = {
            "no_attempt": (False, [], [], ["manual", "veridoc"], None),
            "excluded_manual": (
                True,
                ["manual"],
                [],
                ["veridoc"],
                True,
            ),
            "included_manual": (
                True,
                ["manual"],
                ["manual"],
                ["veridoc"],
                False,
            ),
        }
        for (
            scenario,
            (
                retain_attempt,
                expected_recorded,
                expected_included,
                expected_missing,
                expected_excluded,
            ),
        ) in scenarios.items():
            with self.subTest(scenario=scenario):
                record = _completed_record(self.valid_record)
                _add_withdrawn_participant(record)
                attempt = record["runs"][-1]
                if not retain_attempt:
                    record["runs"].pop()
                elif scenario == "included_manual":
                    attempt["excluded"] = False
                    attempt["exclusion_reason_code"] = None
                    _seal_evidence_envelope(attempt)

                self.assertEqual([], _validate_record(record))
                pair = next(
                    pair
                    for pair in _summarize_record(record)["pair_results"]
                    if pair["participant_id"]
                    == "P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C"
                    and pair["case_id"] == "mvp-word-001"
                )
                self.assertFalse(pair["calculable"])
                self.assertFalse(pair["eligible"])
                self.assertEqual(expected_recorded, pair["recorded_arms"])
                self.assertEqual(expected_included, pair["included_arms"])
                self.assertEqual(expected_missing, pair["missing_arms"])
                self.assertEqual(expected_excluded, pair["manual_excluded"])

    def test_withdrawn_participant_attempt_requires_both_practices(
        self,
    ) -> None:
        record = _completed_record(self.valid_record)
        _add_withdrawn_participant(record)
        participant = record["participants"][-1]
        participant["manual_practice_completed"] = False
        participant["manual_practice_completed_at"] = None
        participant["veridoc_practice_completed"] = False
        participant["veridoc_practice_completed_at"] = None

        errors = _validate_record(record)
        for arm in ("manual", "veridoc"):
            self.assertIn(
                f"{participant['participant_id']}.{arm}_practice_completed "
                "must be true before timed activity",
                errors,
            )

    def test_withdrawn_participant_attempt_requires_arm_order(
        self,
    ) -> None:
        record = _completed_record(self.valid_record)
        _add_withdrawn_participant(record)
        participant = record["participants"][-1]
        participant["arm_order"] = None

        self.assertIn(
            f"{participant['participant_id']}.arm_order is required before "
            "timed activity",
            _validate_record(record),
        )

    def test_withdrawn_participant_cannot_begin_with_second_arm(
        self,
    ) -> None:
        record = _completed_record(self.valid_record)
        _add_withdrawn_participant(record)
        participant = record["participants"][-1]
        participant["arm_order"] = ["veridoc", "manual"]

        self.assertIn(
            f"{participant['participant_id']} timed runs do not follow "
            "declared arm_order",
            _validate_record(record),
        )

    def test_withdrawal_boundary_rejects_an_attempt_that_ends_after_it(
        self,
    ) -> None:
        record = _completed_record(self.valid_record)
        _add_withdrawn_participant(record)
        record["participants"][-1]["withdrawn_at"] = "2026-08-10T01:01:00Z"
        self.assertIn(
            "run[30] must not start at or end after P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C withdrawal",
            _validate_record(record),
        )

    def test_participant_withdrew_marker_establishes_withdrawal_boundary(
        self,
    ) -> None:
        record = _completed_record(self.valid_record)
        _add_withdrawn_participant(record)
        participant = record["participants"][-1]
        participant_id = participant["participant_id"]
        participant["withdrawn_at"] = "2026-08-10T01:05:00Z"
        marker = record["runs"][-1]
        resumed = copy.deepcopy(marker)
        resumed_run_id = (
            f"RUN-{participant_id}-MVP-WORD-001-MANUAL-2"
        )
        resumed.update(
            {
                "run_id": resumed_run_id,
                "sealed_artifact_record_id": _opaque_record_id(
                    "SAR", resumed_run_id
                ),
                "output_artifact_sha256": hashlib.sha256(
                    f"{resumed_run_id}\n".encode("utf-8")
                ).hexdigest(),
                "sealed_artifact_path": _sealed_artifact_path(
                    _opaque_record_id("SAR", resumed_run_id)
                ),
                "attempt_number": 2,
                "started_at": "2026-08-10T01:03:00Z",
                "ended_at": "2026-08-10T01:04:00Z",
                "excluded": False,
                "exclusion_reason_code": None,
            }
        )
        _seal_evidence_envelope(resumed)
        record["runs"].append(resumed)

        self.assertIn(
            f"run[30].ended_at must equal {participant_id} withdrawn_at "
            "for participant_withdrew exclusion",
            _validate_record(record),
        )

    def test_withdrawn_participant_may_retain_incomplete_practice(self) -> None:
        record = _completed_record(self.valid_record)
        record["participants"].append(
            {
                "participant_id": "P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C",
                "participation_status": "withdrawn",
                "withdrawn_at": "2026-07-26T00:30:00Z",
                "consent_status": "consented",
                "consented_at": "2026-07-26T00:06:00Z",
                "consent_form_version": CONSENT_FORM_VERSION,
                "relevant_experience_attested": True,
                "manual_practice_completed": False,
                "manual_practice_completed_at": None,
                "manual_practice_revision": APPROVED_PRACTICE_REVISION,
                "manual_practice_package_sha256": (
                    APPROVED_PRACTICE_PACKAGE_SHA256
                ),
                "veridoc_practice_completed": False,
                "veridoc_practice_completed_at": None,
                "veridoc_practice_revision": APPROVED_PRACTICE_REVISION,
                "veridoc_practice_package_sha256": (
                    APPROVED_PRACTICE_PACKAGE_SHA256
                ),
                "arm_order": None,
            }
        )
        _seal_study_evidence_envelope(record)
        self.assertEqual([], _validate_record(record))

        record["participants"][-1]["manual_practice_completed_at"] = (
            "2026-07-26T00:10:00Z"
        )
        self.assertIn(
            "participant[3].manual_practice_completed_at must be null when "
            "manual_practice_completed is false",
            _validate_record(record),
        )

        completed = _completed_record(self.valid_record)
        completed["participants"][0]["manual_practice_completed"] = False
        completed["participants"][0]["manual_practice_completed_at"] = None
        self.assertIn(
            "participant[0].manual_practice_completed must be true",
            _validate_record(completed),
        )

    def test_withdrawn_participant_practice_must_not_cross_withdrawal(
        self,
    ) -> None:
        record = _completed_record(self.valid_record)
        _add_withdrawn_participant(record)
        record["participants"][-1]["withdrawn_at"] = "2026-07-26T00:15:00Z"
        self.assertIn(
            "P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C."
            "veridoc_practice_completed_at must not occur after withdrawal",
            _validate_record(record),
        )

    def test_withdrawn_participant_recorded_pair_is_reported_ineligible(
        self,
    ) -> None:
        record = _completed_record(self.valid_record)
        _add_withdrawn_participant(record)
        record["participants"][-1]["withdrawn_at"] = "2026-08-10T01:06:00Z"
        record["runs"].pop()
        for arm, start in (
            ("manual", datetime(2026, 8, 10, 1, 0, tzinfo=timezone.utc)),
            ("veridoc", datetime(2026, 8, 10, 1, 3, tzinfo=timezone.utc)),
        ):
            source = next(
                run
                for run in record["runs"]
                if run["participant_id"] == "P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A"
                and run["case_id"] == "mvp-word-001"
                and run["arm"] == arm
            )
            run = copy.deepcopy(source)
            run_id = f"RUN-P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C-MVP-WORD-001-{arm.upper()}-1"
            run.update(
                {
                    "run_id": run_id,
                    "sealed_artifact_record_id": _opaque_record_id(
                        "SAR", run_id
                    ),
                    "output_artifact_sha256": hashlib.sha256(
                        f"{run_id}\n".encode("utf-8")
                    ).hexdigest(),
                    "sealed_artifact_path": _sealed_artifact_path(
                        _opaque_record_id("SAR", run_id)
                    ),
                    "participant_id": "P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C",
                    "started_at": _utc_text(start),
                    "ended_at": _utc_text(start + timedelta(minutes=2)),
                    "assessor_attestation_record_id": _opaque_record_id(
                        "AAR", run_id
                    ),
                    "assessment_completed_at": _utc_text(
                        start + timedelta(minutes=2, seconds=1)
                    ),
                }
            )
            _seal_assessor_attestation(run)
            _seal_evidence_envelope(run)
            record["runs"].append(run)

        _seal_study_evidence_envelope(record)
        self.assertEqual([], _validate_record(record))
        summary = _summarize_record(record)
        self.assertEqual(20, len(summary["pair_results"]))
        self.assertEqual(0, summary["eligible_pair_count"])
        self.assertEqual(20, summary["ineligible_pair_count"])
        self.assertFalse(summary["execution_attestation_ready"])
        withdrawn_pair = next(
            pair
            for pair in summary["pair_results"]
            if pair["participant_id"] == "P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C"
            and pair["case_id"] == "mvp-word-001"
        )
        self.assertFalse(withdrawn_pair["eligible"])
        self.assertEqual(
            ["manual", "veridoc"],
            withdrawn_pair["recorded_arms"],
        )
        self.assertEqual(
            ["manual", "veridoc"],
            withdrawn_pair["included_arms"],
        )
        self.assertEqual([], withdrawn_pair["missing_arms"])
        self.assertEqual("approved", withdrawn_pair["manual_outcome"])
        self.assertEqual("approved", withdrawn_pair["veridoc_outcome"])

        duplicate = copy.deepcopy(
            next(
                run
                for run in record["runs"]
                if run["participant_id"] == "P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C"
                and run["arm"] == "manual"
            )
        )
        duplicate.update(
            {
                "run_id": "RUN-P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C-MVP-WORD-001-MANUAL-2",
                "sealed_artifact_record_id": _opaque_record_id(
                    "SAR", "RUN-P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C-MVP-WORD-001-MANUAL-2"
                ),
                "output_artifact_sha256": hashlib.sha256(
                    b"RUN-P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C-MVP-WORD-001-MANUAL-2\n"
                ).hexdigest(),
                "sealed_artifact_path": _sealed_artifact_path(
                    _opaque_record_id(
                        "SAR",
                        "RUN-P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C-MVP-WORD-001-MANUAL-2",
                    )
                ),
                "attempt_number": 2,
                "started_at": "2026-08-10T01:06:00Z",
                "ended_at": "2026-08-10T01:08:00Z",
            }
        )
        _seal_evidence_envelope(duplicate)
        record["runs"].append(duplicate)
        self.assertIn(
            "P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C/mvp-word-001/manual must have at most one non-excluded "
            "run after withdrawal",
            _validate_record(record),
        )
        self.assertIn(
            "run[32] must not start at or end after P-D3EB1620-02C3-4DA9-8B2C-ECB3D72FEC1C withdrawal",
            _validate_record(record),
        )

    def test_completed_cohort_still_requires_three_reviewers(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["participants"][2]["participation_status"] = "withdrawn"
        record["participants"][2]["withdrawn_at"] = "2026-07-28T00:00:00Z"
        self.assertIn(
            "completed participant cohort must contain at least three reviewers",
            _validate_record(record),
        )

    def test_controlled_blocker_is_accounted_but_excluded_from_median(self) -> None:
        for artifact_kind in (
            "output_artifact",
            "blocked_attempt_envelope",
        ):
            with self.subTest(artifact_kind=artifact_kind):
                record = _completed_record(self.valid_record)
                blocked = next(
                    run
                    for run in record["runs"]
                    if run["participant_id"]
                    == "P-386BEEA7-81DC-408A-B045-D73A233C0DC5"
                    and run["case_id"] == "mvp-word-001"
                    and run["arm"] == "veridoc"
                )
                blocked["outcome"] = "blocked"
                blocked["blocker_code"] = "approval_unavailable"
                blocked["checklist_complete"] = True
                blocked["sealed_artifact_kind"] = artifact_kind
                if artifact_kind == "blocked_attempt_envelope":
                    blocked["sealed_artifact_path"] = None
                    blocked["output_artifact_sha256"] = None
                _seal_evidence_envelope(blocked)
                self.assertEqual([], _validate_record(record))
                summary = _summarize_record(record)
                self.assertTrue(summary["all_required_runs_accounted"])
                self.assertEqual(0, summary["eligible_pair_count"])
                self.assertEqual(15, summary["ineligible_pair_count"])
                self.assertIsNone(
                    summary["paired_median_reduction_percent"]
                )
                self.assertFalse(summary["execution_attestation_ready"])
                self.assertFalse(summary["efficiency_target_met"])

    def test_practice_revision_is_required_and_controlled(self) -> None:
        record = copy.deepcopy(self.valid_record)
        del record["practice_revision"]
        errors = _validate_record(record)
        self.assertIn("missing record field: practice_revision", errors)
        self.assertIn(
            "practice_revision must be 'practice-phase12-v1'",
            errors,
        )

    def test_practice_package_is_immutable_and_attested_per_arm(self) -> None:
        package_sha256 = hashlib.sha256(
            PRACTICE_PACKAGE_PATH.read_bytes()
        ).hexdigest()
        self.assertEqual(APPROVED_PRACTICE_PACKAGE_SHA256, package_sha256)
        package = json.loads(PRACTICE_PACKAGE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            APPROVED_PRACTICE_REVISION,
            package["practice_revision"],
        )
        for document in ("protocol", "execution_checklist"):
            document_path = package["training_material"][
                f"{document}_path"
            ]
            declared_sha256 = package["training_material"][
                f"{document}_sha256"
            ]
            actual_sha256 = hashlib.sha256(
                (REPO_ROOT / document_path).read_bytes()
            ).hexdigest()
            self.assertEqual(declared_sha256, actual_sha256)

        record = copy.deepcopy(self.valid_record)
        record["participants"][0]["manual_practice_revision"] = (
            "practice-phase12-v2"
        )
        record["participants"][0]["veridoc_practice_package_sha256"] = "0" * 64
        errors = _validate_record(record)
        self.assertIn(
            "participant[0].manual_practice_revision must be "
            "practice-phase12-v1",
            errors,
        )
        self.assertIn(
            "participant[0].veridoc_practice_package_sha256 must match "
            "approved practice package",
            errors,
        )

    def test_gold_package_is_immutable_and_case_scoped(self) -> None:
        package_sha256 = hashlib.sha256(
            GOLD_PACKAGE_PATH.read_bytes()
        ).hexdigest()
        self.assertEqual(PINNED_GOLD_PACKAGE_SHA256, package_sha256)
        package = json.loads(GOLD_PACKAGE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            "unapproved_validation_only",
            package["approval_status"],
        )
        self.assertEqual(
            set(ALL_CASE_IDS),
            {case["case_id"] for case in package["cases"]},
        )
        scanned_case = next(
            case
            for case in package["cases"]
            if case["case_id"] == "mvp-scanned-pdf-001"
        )
        self.assertEqual(
            1,
            len(scanned_case["expected_high_risk_targets"]),
        )

    def test_timed_task_package_is_immutable_and_arm_scoped(self) -> None:
        package_sha256 = hashlib.sha256(
            TASK_PACKAGE_PATH.read_bytes()
        ).hexdigest()
        self.assertEqual(PINNED_TASK_PACKAGE_SHA256, package_sha256)
        package = json.loads(TASK_PACKAGE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(APPROVED_TASK_REVISION, package["task_revision"])
        self.assertEqual(
            {"manual", "veridoc"},
            set(package["arm_contracts"]),
        )
        self.assertNotEqual(
            PINNED_TASK_ARM_SHA256["manual"],
            PINNED_TASK_ARM_SHA256["veridoc"],
        )
        self.assertEqual(
            set(ALL_CASE_IDS),
            {case["case_id"] for case in package["cases"]},
        )

    def test_completion_checklist_package_is_immutable_and_shared(self) -> None:
        package_sha256 = hashlib.sha256(
            COMPLETION_CHECKLIST_PACKAGE_PATH.read_bytes()
        ).hexdigest()
        self.assertEqual(PINNED_CHECKLIST_PACKAGE_SHA256, package_sha256)
        package = json.loads(
            COMPLETION_CHECKLIST_PACKAGE_PATH.read_text(encoding="utf-8")
        )
        self.assertEqual(
            APPROVED_CHECKLIST_REVISION,
            package["checklist_revision"],
        )
        arm_application = package["shared_instructions"]["arm_application"]
        self.assertIn("unchanged", arm_application)
        self.assertIn("both the manual and VeriDoc arms", arm_application)
        self.assertEqual(
            [f"CHK-{number:02d}" for number in range(1, 9)],
            [item["item_id"] for item in package["items"]],
        )
        self.assertEqual(
            set(ALL_CASE_IDS),
            {case["case_id"] for case in package["cases"]},
        )

    def test_practice_must_precede_participants_earliest_timed_run(self) -> None:
        record = copy.deepcopy(self.valid_record)
        record["participants"][0]["manual_practice_completed_at"] = record[
            "runs"
        ][0]["started_at"]
        self.assertIn(
            "P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A.manual_practice_completed_at must precede every timed run",
            _validate_record(record),
        )

    def test_each_participant_consent_is_versioned_and_precedes_activity(
        self,
    ) -> None:
        record = copy.deepcopy(self.valid_record)
        record["participants"][0]["consent_status"] = "not_consented"
        record["participants"][0]["consent_form_version"] = (
            "CF-550E8400-E29B-41D4-A716-446655440299"
        )
        record["participants"][0]["consented_at"] = record["participants"][0][
            "manual_practice_completed_at"
        ]
        errors = _validate_record(record)
        self.assertIn(
            "participant[0].consent_status must be consented",
            errors,
        )
        self.assertIn(
            "participant[0].consent_form_version must match consent_approval",
            errors,
        )
        self.assertIn(
            "P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A.consented_at must "
            "precede manual_practice_completed_at",
            errors,
        )

        record = copy.deepcopy(self.valid_record)
        record["participants"][0]["consented_at"] = "2026-07-25T23:59:59Z"
        self.assertIn(
            "P-4E7ECEFA-49B4-4F0E-BD08-0DF31E92503A.consented_at must "
            "follow consent approval",
            _validate_record(record),
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
                "consent_approval.approved_by_role must be study_owner",
            ),
            (
                "/participants/0/participation_status",
                [],
                "participant[0].participation_status is invalid",
            ),
            (
                "/participants/0/manual_practice_completed_at",
                [],
                "participant[0].manual_practice_completed_at must be a UTC "
                "RFC 3339 timestamp",
            ),
            (
                "/runs/0/participant_id",
                [],
                "run[0].participant_id is not declared",
            ),
            ("/runs/0/case_id", {}, "run[0].case_id is not declared"),
            ("/runs/0/arm", [], "run[0].arm is invalid"),
            (
                "/runs/0/sealed_artifact_kind",
                [],
                "run[0].sealed_artifact_kind is invalid",
            ),
            (
                "/runs/0/gold_answer_compared_by_role",
                [],
                "run[0].gold_answer_compared_by_role must be "
                "independent_assessor",
            ),
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
                self.assertIn(expected_error, _validate_record(mutated))

    def test_every_leaf_type_mutation_fails_closed_without_crashing(
        self,
    ) -> None:
        leaf_paths = []

        def collect_leaf_paths(value, path=()) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    collect_leaf_paths(item, path + (key,))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    collect_leaf_paths(item, path + (index,))
            else:
                leaf_paths.append(path)

        collect_leaf_paths(self.valid_record)
        wrong_type_values = (None, [], {}, "", True, -1, 10**100)
        for path in leaf_paths:
            original = self.valid_record
            for key in path:
                original = original[key]
            for replacement in wrong_type_values:
                if type(replacement) is type(original):
                    continue
                mutated = copy.deepcopy(self.valid_record)
                target = mutated
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = replacement
                self.assertTrue(
                    _validate_record(mutated),
                    msg=(
                        "wrong JSON type was accepted at "
                        f"{'/'.join(map(str, path))}: {replacement!r}"
                    ),
                )

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

    def test_validator_cli_rejects_duplicate_json_object_keys(self) -> None:
        duplicate = VALID_EXAMPLE_PATH.read_text(encoding="utf-8").replace(
            '"high_risk_miss_count": 0,',
            '"high_risk_miss_count": 1,\n'
            '      "high_risk_miss_count": 0,',
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            duplicate_path = Path(directory) / "duplicate.json"
            duplicate_path.write_text(duplicate, encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), str(duplicate_path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(2, completed.returncode)
        self.assertIn(
            "duplicate JSON object key: high_risk_miss_count",
            completed.stderr,
        )

    def test_validator_cli_rejects_invalid_utf8_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            invalid_path = Path(directory) / "invalid-utf8.json"
            invalid_path.write_bytes(
                b'{"study_id":"' + bytes([0xFF]) + b'"}'
            )
            completed = subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), str(invalid_path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(2, completed.returncode)
        self.assertIn("Unable to read evidence:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_validator_cli_rejects_lone_surrogate_without_traceback(self) -> None:
        invalid = VALID_EXAMPLE_PATH.read_text(encoding="utf-8").replace(
            '"case_id": "mvp-word-001"',
            '"case_id": "\\ud800"',
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            invalid_path = Path(directory) / "lone-surrogate.json"
            invalid_path.write_text(invalid, encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    str(invalid_path),
                    "--artifact-root",
                    str(REPO_ROOT / "datasets"),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(2, completed.returncode)
        self.assertIn(
            "JSON strings must contain only Unicode scalar values",
            completed.stderr,
        )
        self.assertNotIn("Traceback", completed.stderr)

    def test_strict_json_accepts_a_valid_surrogate_pair(self) -> None:
        self.assertEqual("😀", _loads_json_strict('"\\ud83d\\ude00"'))

    def test_validator_cli_rejects_integer_limit_failure_without_traceback(
        self,
    ) -> None:
        digit_limit = 640
        malformed = '{"oversized_integer": ' + "1" * (digit_limit + 1) + "}"
        with tempfile.TemporaryDirectory() as directory:
            invalid_path = Path(directory) / "oversized-integer.json"
            invalid_path.write_text(malformed, encoding="utf-8")
            environment = os.environ.copy()
            environment["PYTHONINTMAXSTRDIGITS"] = str(digit_limit)
            completed = subprocess.run(
                [sys.executable, str(VALIDATOR_PATH), str(invalid_path)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
            )
        self.assertEqual(2, completed.returncode)
        self.assertIn("Unable to read evidence:", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_validator_cli_rejects_unsupported_json_numbers(
        self,
    ) -> None:
        invalid_documents = {
            "nan": '{"value": NaN}',
            "infinity": '{"value": Infinity}',
            "overflowing-float": '{"value": 1e400}',
        }
        for name, document in invalid_documents.items():
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    invalid_path = Path(directory) / f"{name}.json"
                    invalid_path.write_text(document, encoding="utf-8")
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(VALIDATOR_PATH),
                            str(invalid_path),
                        ],
                        cwd=REPO_ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                self.assertEqual(2, completed.returncode)
                self.assertIn("Unable to read evidence:", completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
