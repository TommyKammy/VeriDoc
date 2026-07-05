from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "evaluate_dataset.py"
CASES_PATH = REPO_ROOT / "datasets" / "gold" / "evaluation_cases_v0.json"
HIGH_RISK_LABELS_PATH = REPO_ROOT / "datasets" / "gold" / "high_risk_labels_v0.json"
LLM_STABILITY_RUNS_PATH = REPO_ROOT / "datasets" / "gold" / "llm_stability_runs_v0.json"
POC_COMPARISON_PATH = REPO_ROOT / "datasets" / "gold" / "poc_mode_comparison_v1.json"
GMP_ACCEPTANCE_PATH = REPO_ROOT / "datasets" / "gold" / "gmp_acceptance_v1.json"


spec = importlib.util.spec_from_file_location("evaluate_dataset", SCRIPT_PATH)
assert spec is not None
evaluate_dataset = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = evaluate_dataset
spec.loader.exec_module(evaluate_dataset)


class EvaluateDatasetTest(unittest.TestCase):
    def valid_cases_data(self) -> dict[str, object]:
        return copy.deepcopy(evaluate_dataset.load_json(CASES_PATH))

    def valid_llm_stability_data(self) -> dict[str, object]:
        return copy.deepcopy(evaluate_dataset.load_json(LLM_STABILITY_RUNS_PATH))

    def valid_poc_comparison_data(self) -> dict[str, object]:
        return copy.deepcopy(evaluate_dataset.load_json(POC_COMPARISON_PATH))

    def valid_gmp_acceptance_data(self) -> dict[str, object]:
        return copy.deepcopy(evaluate_dataset.load_json(GMP_ACCEPTANCE_PATH))

    def valid_high_risk_labels_data(self) -> dict[str, object]:
        return copy.deepcopy(evaluate_dataset.load_json(HIGH_RISK_LABELS_PATH))

    def prepare_gmp_acceptance_repo(self, temp_root: Path) -> None:
        shutil.copytree(REPO_ROOT / "datasets", temp_root / "datasets")
        (temp_root / "docs").mkdir()
        for doc_name in (
            "change-management-reevaluation.md",
            "gmp04-electronic-records-signatures.md",
            "gmp07-validation-draft.md",
            "gmp08-acceptance-evaluation.md",
        ):
            shutil.copy2(REPO_ROOT / "docs" / doc_name, temp_root / "docs" / doc_name)
        (temp_root / "scripts").mkdir()
        shutil.copy2(
            REPO_ROOT / "scripts" / "evaluate_dataset.py",
            temp_root / "scripts" / "evaluate_dataset.py",
        )
        (temp_root / "scripts" / "ci").mkdir()
        shutil.copy2(
            REPO_ROOT / "scripts" / "ci" / "repo_hygiene.py",
            temp_root / "scripts" / "ci" / "repo_hygiene.py",
        )
        (temp_root / "tests").mkdir()
        shutil.copy2(
            REPO_ROOT / "tests" / "test_poc_web_api.py",
            temp_root / "tests" / "test_poc_web_api.py",
        )

    def evaluate_valid_cases(self, data: dict[str, object]) -> object:
        return evaluate_dataset.evaluate_cases(data, manifest_root=REPO_ROOT)

    def evaluate_with_fixture(
        self,
        data: dict[str, object],
        fixture: dict[str, object],
        fixture_metadata: dict[str, object] | None = None,
        manifest_policy: dict[str, object] | None = None,
        fixture_relpath: str = "datasets/fixtures/fixture.json",
    ) -> object:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            fixture_dir = temp_root / "datasets" / "fixtures"
            fixture_dir.mkdir(parents=True)
            fixture_path = temp_root / fixture_relpath
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path = fixture_dir / "manifest.json"
            fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
            manifest_fixture = {
                "id": data["cases"][0]["fixture_id"],
                "anonymization": "synthetic",
                "public_review_safe": True,
                "confidentiality": "public",
                "path": fixture_relpath,
            }
            if fixture_metadata is not None:
                manifest_fixture.update(fixture_metadata)
            policy = {
                "allowed_fixture_root": "datasets/fixtures",
                "public_only": True,
                "confidential_source_documents_allowed": False,
            }
            if manifest_policy is not None:
                policy.update(manifest_policy)
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": evaluate_dataset.FIXTURE_MANIFEST_SCHEMA_VERSION,
                        "policy": policy,
                        "fixtures": [manifest_fixture],
                    }
                ),
                encoding="utf-8",
            )
            data["dataset_manifest"] = "datasets/fixtures/manifest.json"

            return evaluate_dataset.evaluate_cases(data, manifest_root=temp_root)

    def test_public_fixture_metrics_cover_phase0_acceptance_criteria(self) -> None:
        data = evaluate_dataset.load_json(CASES_PATH)

        metrics = self.evaluate_valid_cases(data)

        self.assertEqual(1.0, metrics.table_extraction_rate)
        self.assertEqual(0.5, metrics.cell_match_rate)
        self.assertEqual(0.5, metrics.source_linkage_rate)
        self.assertEqual(1, metrics.false_auto_confirmed_count)
        self.assertEqual(1, metrics.expected_table_count)
        self.assertEqual(2, metrics.expected_cell_count)
        self.assertEqual(2, metrics.expected_source_link_count)

    def test_llm_stability_metrics_quantify_repeated_output_drift(self) -> None:
        metrics = evaluate_dataset.evaluate_llm_stability(self.valid_llm_stability_data())

        self.assertEqual("synthetic-batch-record-001", metrics.input_id)
        self.assertEqual(3, metrics.run_count)
        self.assertEqual(2 / 3, metrics.plan_agreement_rate)
        self.assertEqual(2 / 3, metrics.confirmed_value_agreement_rate)
        self.assertEqual(2 / 3, metrics.schema_failure_rate)
        self.assertEqual(1 / 2, metrics.repair_success_rate)
        self.assertEqual(1 / 3, metrics.deterministic_fallback_rate)
        self.assertEqual(0, metrics.external_ai_api_guard_violation_count)
        self.assertEqual(2, metrics.distinct_plan_count)
        self.assertEqual(2, metrics.distinct_confirmed_value_count)
        self.assertEqual(2, metrics.unstable_example_count)
        self.assertEqual(
            (
                {
                    "reference_run_id": "run-001",
                    "run_id": "run-002",
                    "changed": "confirmed_values",
                },
                {
                    "reference_run_id": "run-001",
                    "run_id": "run-003",
                    "changed": "conversion_plan",
                },
            ),
            metrics.unstable_examples,
        )

    def test_poc_mode_comparison_measures_required_phase1_modes(self) -> None:
        metrics = evaluate_dataset.evaluate_poc_mode_comparison(
            self.valid_poc_comparison_data(), repo_root=REPO_ROOT
        )

        self.assertEqual(3, metrics.mode_count)
        self.assertEqual(0, metrics.high_risk_false_auto_confirmed_count)
        self.assertTrue(metrics.target_met)
        self.assertEqual(12.0, metrics.manual_correction_time.baseline_minutes)
        self.assertEqual(5.0, metrics.manual_correction_time.assisted_minutes)
        self.assertEqual(7.0, metrics.manual_correction_time.reduction_minutes)
        self.assertEqual(7 / 12, metrics.manual_correction_time.reduction_rate)
        self.assertTrue(metrics.manual_correction_time.target_met)
        self.assertEqual(
            ["no_llm", "standard", "high_quality"],
            [mode["mode"] for mode in metrics.as_dict()["modes"]],
        )
        self.assertEqual([2, 1, 0], [mode["warning_count"] for mode in metrics.as_dict()["modes"]])
        self.assertEqual(
            [
                {
                    "baseline_mode": "no_llm",
                    "candidate_mode": "standard",
                    "review_item_added_count": 0,
                    "review_item_removed_count": 1,
                    "warning_added_count": 0,
                    "warning_removed_count": 1,
                    "added_review_items": [],
                    "removed_review_items": [
                        "sample-document-ir-v0:block-002:lot_number"
                    ],
                    "added_warnings": [],
                    "removed_warnings": ["lot-number-mismatch"],
                },
                {
                    "baseline_mode": "no_llm",
                    "candidate_mode": "high_quality",
                    "review_item_added_count": 0,
                    "review_item_removed_count": 1,
                    "warning_added_count": 0,
                    "warning_removed_count": 2,
                    "added_review_items": [],
                    "removed_review_items": [
                        "sample-document-ir-v0:block-002:lot_number"
                    ],
                    "added_warnings": [],
                    "removed_warnings": [
                        "lot-number-mismatch",
                        "missing-source-anchor",
                    ],
                },
            ],
            metrics.as_dict()["mode_diffs"],
        )
        high_quality = metrics.as_dict()["modes"][2]
        self.assertEqual(1.0, high_quality["cell_match_rate"])
        self.assertEqual(1.0, high_quality["source_linkage_rate"])
        self.assertEqual(2, high_quality["requires_review_count"])

    def test_poc_mode_comparison_rejects_missing_required_mode_before_scoring(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"] = [mode for mode in data["modes"] if mode["mode"] != "high_quality"]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "exactly no_llm"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_missing_dataset_manifest_before_scoring(self) -> None:
        data = self.valid_poc_comparison_data()
        data.pop("dataset_manifest")

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "dataset_manifest"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_missing_manual_correction_time(self) -> None:
        data = self.valid_poc_comparison_data()
        data.pop("manual_correction_time")

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "manual_correction_time"
        ):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_allows_zero_assisted_manual_correction_time(self) -> None:
        data = self.valid_poc_comparison_data()
        data["manual_correction_time"]["assisted_minutes"] = 0.0

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(0.0, metrics.manual_correction_time.assisted_minutes)
        self.assertEqual(12.0, metrics.manual_correction_time.reduction_minutes)
        self.assertEqual(1.0, metrics.manual_correction_time.reduction_rate)
        self.assertTrue(metrics.manual_correction_time.target_met)

    def test_poc_mode_comparison_reports_slower_assisted_manual_correction_time(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["manual_correction_time"]["assisted_minutes"] = 13.0

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(13.0, metrics.manual_correction_time.assisted_minutes)
        self.assertEqual(-1.0, metrics.manual_correction_time.reduction_minutes)
        self.assertEqual(-1 / 12, metrics.manual_correction_time.reduction_rate)
        self.assertFalse(metrics.manual_correction_time.target_met)
        self.assertFalse(metrics.target_met)

    def test_poc_mode_comparison_rejects_legacy_schema_without_manual_times(self) -> None:
        data = self.valid_poc_comparison_data()
        data["schema_version"] = "veridoc-poc-mode-comparison/v0"
        data.pop("manual_correction_time")

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "unsupported PoC comparison schema_version"
        ):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_non_canonical_dataset_manifest_before_scoring(
        self,
    ) -> None:
        for manifest_path in (
            str(REPO_ROOT / "datasets" / "fixtures" / "manifest.json"),
            "datasets/fixtures/side-manifest.json",
            "datasets/fixtures/../fixtures/manifest.json",
        ):
            data = self.valid_poc_comparison_data()
            data["dataset_manifest"] = manifest_path

            with self.subTest(manifest_path=manifest_path), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                "dataset_manifest must be datasets/fixtures/manifest.json",
            ):
                evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_missing_evaluation_cases_before_scoring(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data.pop("evaluation_cases")

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "evaluation_cases"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_missing_high_risk_label_coverage_per_mode(
        self,
    ) -> None:
        for mode_index, mode_name in enumerate(evaluate_dataset.REQUIRED_POC_MODES):
            data = self.valid_poc_comparison_data()
            data["modes"][mode_index]["high_risk_items"] = data["modes"][mode_index][
                "high_risk_items"
            ][:1]

            with self.subTest(mode=mode_name), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError, "cover all"
            ):
                evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_missing_warning_lists_before_diffing(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][1].pop("warnings")

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "warnings"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_high_risk_label_drift_before_scoring(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][0]["high_risk_items"][0]["expected_value"] = "SAMPLE-LOT-999"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "high-risk labels"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_high_risk_label_index_allows_same_taxonomy_label_in_distinct_blocks(
        self,
    ) -> None:
        data = self.valid_high_risk_labels_data()
        duplicate_label = copy.deepcopy(data["items"][0])
        duplicate_label["id"] = "gold-duplicate-block"
        duplicate_label["block_id"] = "block-003"
        data["items"].append(duplicate_label)

        labels = evaluate_dataset.high_risk_label_index(data)

        self.assertIn(
            ("sample-document-ir-v0", "block-002", "lot_number"),
            labels,
        )
        self.assertIn(
            ("sample-document-ir-v0", "block-003", "lot_number"),
            labels,
        )

    def test_high_risk_label_index_rejects_missing_expected_value(self) -> None:
        data = self.valid_high_risk_labels_data()
        del data["items"][0]["expected_value"]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "expected_value"):
            evaluate_dataset.high_risk_label_index(data)

    def test_high_risk_label_index_rejects_label_outside_taxonomy(self) -> None:
        data = self.valid_high_risk_labels_data()
        data["items"][0]["label_id"] = "lot_numbre"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "label_taxonomy"):
            evaluate_dataset.high_risk_label_index(data)

    def test_high_risk_labels_reject_unknown_fixture_block_id(self) -> None:
        data = self.valid_high_risk_labels_data()
        data["items"][0]["block_id"] = "block-missing"
        labels = evaluate_dataset.high_risk_label_index(data)
        fixture_paths = evaluate_dataset.fixture_paths_from_manifest(
            evaluate_dataset.load_json(REPO_ROOT / "datasets" / "fixtures" / "manifest.json"),
            REPO_ROOT,
        )

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "block_id"):
            evaluate_dataset.validate_high_risk_labels_against_fixtures(labels, fixture_paths)

    def test_poc_mode_comparison_rejects_duplicate_value_from_different_block(
        self,
    ) -> None:
        labels_data = self.valid_high_risk_labels_data()
        duplicate_label = copy.deepcopy(labels_data["items"][0])
        duplicate_label["id"] = "gold-duplicate-block-value"
        duplicate_label["block_id"] = "block-003"
        labels_data["items"].append(duplicate_label)
        labels = evaluate_dataset.high_risk_label_index(labels_data)

        fixture_paths = evaluate_dataset.fixture_paths_from_manifest(
            evaluate_dataset.load_json(REPO_ROOT / "datasets" / "fixtures" / "manifest.json"),
            REPO_ROOT,
        )
        label_blocks = evaluate_dataset.validate_high_risk_labels_against_fixtures(
            evaluate_dataset.high_risk_label_index(self.valid_high_risk_labels_data()),
            fixture_paths,
        )
        label_blocks[
            ("sample-document-ir-v0", "block-003", "lot_number")
        ] = {
            "id": "block-003",
            "text": "Lot Number: SAMPLE-LOT-001",
            "source": {
                "source_page": 1,
                "bbox": {
                    "x": 300.0,
                    "y": 112.0,
                    "width": 180.0,
                    "height": 18.0,
                },
            },
            "requires_review": True,
        }
        mode_record = self.valid_poc_comparison_data()["modes"][2]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "captured actual value"):
            evaluate_dataset.poc_mode_actual_values_by_high_risk_label(
                mode_record,
                self.valid_cases_data(),
                labels,
                label_blocks,
                "modes[2]",
            )

    def test_poc_mode_comparison_rejects_missing_high_risk_block_binding(self) -> None:
        data = self.valid_poc_comparison_data()
        del data["modes"][0]["high_risk_items"][0]["block_id"]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "block_id"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_missing_expected_high_risk_value(self) -> None:
        data = self.valid_poc_comparison_data()
        del data["modes"][0]["high_risk_items"][0]["expected_value"]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "expected_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_numeric_expected_value_for_boolean_label(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][0]["high_risk_items"][1]["expected_value"] = 1

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "expected_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_missing_actual_high_risk_value(self) -> None:
        data = self.valid_poc_comparison_data()
        del data["modes"][0]["high_risk_items"][0]["actual_value"]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "actual_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_confirmed_actual_high_risk_mismatch(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][0]["high_risk_items"][0]["status"] = "confirmed"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "status"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_confirmed_matching_high_risk_value(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][0]["status"] = "confirmed"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "requires_review"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_reviewed_mismatch_in_inflated_rate(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][0]["actual_value"] = "SAMPLE-LOT-002"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "actual_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_actual_high_risk_value_from_wrong_cell(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][0]["actual_value"] = "Lot number"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "actual_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_prefix_high_risk_identifier_match(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["cases"][0]["actual"]["tables"][0]["cells"][1][
            "text"
        ] = "SAMPLE-LOT-001-REV2"
        data["modes"][2]["metrics"]["cell_match_rate"] = 0.5

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "actual_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_non_string_high_risk_value_mismatch(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][1]["actual_value"] = False

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "actual_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_rejects_string_actual_for_non_string_high_risk_label(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][1]["actual_value"] = "SAMPLE-LOT-001"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "actual_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_high_risk_item_accepts_semantically_equal_numeric_value(self) -> None:
        labels = {
            ("sample-document-ir-v0", "block-002", "numeric_value"): {
                "expected_value": 1,
                "risk_level": "high",
                "requires_review": True,
            }
        }
        item = {
            "fixture_id": "sample-document-ir-v0",
            "block_id": "block-002",
            "label_id": "numeric_value",
            "expected_value": 1,
            "actual_value": 1.0,
            "risk_level": "high",
            "requires_review": True,
            "status": "requires_review",
        }

        self.assertTrue(
            evaluate_dataset.validate_poc_high_risk_item_against_label(
                item, labels, "modes[0].high_risk_items[0]"
            )
        )

    def test_poc_high_risk_item_rejects_large_numeric_drift(self) -> None:
        labels = {
            ("sample-document-ir-v0", "block-002", "numeric_value"): {
                "expected_value": 10**12,
                "risk_level": "high",
                "requires_review": True,
            }
        }
        item = {
            "fixture_id": "sample-document-ir-v0",
            "block_id": "block-002",
            "label_id": "numeric_value",
            "expected_value": 10**12,
            "actual_value": 10**12 + 1,
            "risk_level": "high",
            "requires_review": True,
            "status": "requires_review",
        }

        self.assertFalse(
            evaluate_dataset.validate_poc_high_risk_item_against_label(
                item, labels, "modes[0].high_risk_items[0]"
            )
        )

    def test_poc_mode_high_risk_values_accept_parsed_full_field_cell(self) -> None:
        cases_data = self.valid_cases_data()
        mode_record = self.valid_poc_comparison_data()["modes"][2]
        cases_data["cases"][0]["expected"]["tables"][0]["cells"][1][
            "text"
        ] = "Lot Number: SAMPLE-LOT-001"
        mode_record["cases"][0]["actual"]["tables"][0]["cells"][1][
            "text"
        ] = "Lot Number: SAMPLE-LOT-001"
        labels = evaluate_dataset.high_risk_label_index(self.valid_high_risk_labels_data())
        fixture_paths = evaluate_dataset.fixture_paths_from_manifest(
            evaluate_dataset.load_json(REPO_ROOT / "datasets" / "fixtures" / "manifest.json"),
            REPO_ROOT,
        )
        label_blocks = evaluate_dataset.validate_high_risk_labels_against_fixtures(
            labels,
            fixture_paths,
        )

        actual_values = evaluate_dataset.poc_mode_actual_values_by_high_risk_label(
            mode_record,
            cases_data,
            labels,
            label_blocks,
            "modes[2]",
        )

        self.assertIn(
            "SAMPLE-LOT-001",
            actual_values[("sample-document-ir-v0", "block-002", "lot_number")],
        )

    def test_poc_mode_comparison_recomputes_cell_match_rate_from_mode_cases(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["cases"][0]["actual"]["tables"][0]["cells"][0]["text"] = "Batch"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "cell_match_rate"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_recomputes_table_extraction_rate_from_mode_cases(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["cases"][0]["actual"]["tables"] = []

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "table_extraction_rate"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_recomputes_source_linkage_rate_from_mode_cases(self) -> None:
        data = self.valid_poc_comparison_data()
        del data["modes"][2]["cases"][0]["actual"]["tables"][0]["cells"][1]["source"]

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "source_linkage_rate"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

    def test_poc_mode_comparison_counts_high_risk_auto_confirmation_failures(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][0]["high_risk_items"][0]["auto_confirmed"] = True

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(1, metrics.high_risk_false_auto_confirmed_count)
        self.assertFalse(metrics.target_met)

    def test_poc_mode_comparison_counts_captured_cell_auto_confirmation_failures(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["cases"][0]["actual"]["tables"][0]["cells"][1][
            "auto_confirmed"
        ] = True

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(
            1,
            metrics.as_dict()["modes"][2]["high_risk_false_auto_confirmed_count"],
        )
        self.assertEqual(1, metrics.high_risk_false_auto_confirmed_count)
        self.assertFalse(metrics.target_met)

    def test_poc_mode_comparison_counts_wrong_captured_cell_auto_confirmation(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][0]["cases"][0]["actual"]["tables"][0]["cells"][1][
            "auto_confirmed"
        ] = True

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(
            1,
            metrics.as_dict()["modes"][0]["high_risk_false_auto_confirmed_count"],
        )
        self.assertEqual(1, metrics.high_risk_false_auto_confirmed_count)
        self.assertFalse(metrics.target_met)

    def test_poc_mode_comparison_deduplicates_mirrored_auto_confirmation_sources(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][0]["auto_confirmed"] = True
        data["modes"][2]["cases"][0]["actual"]["tables"][0]["cells"][1][
            "auto_confirmed"
        ] = True

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(
            1,
            metrics.as_dict()["modes"][2]["high_risk_false_auto_confirmed_count"],
        )
        self.assertEqual(1, metrics.high_risk_false_auto_confirmed_count)
        self.assertFalse(metrics.target_met)

    def test_poc_mode_comparison_counts_boolean_review_cell_auto_confirmation(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["cases"][0]["actual"]["tables"][0]["cells"][0][
            "auto_confirmed"
        ] = True

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(
            1,
            metrics.as_dict()["modes"][2]["high_risk_false_auto_confirmed_count"],
        )
        self.assertEqual(1, metrics.high_risk_false_auto_confirmed_count)
        self.assertFalse(metrics.target_met)

    def test_gmp_acceptance_reports_15_7_criteria(self) -> None:
        metrics = evaluate_dataset.evaluate_gmp_acceptance(
            self.valid_gmp_acceptance_data(), repo_root=REPO_ROOT
        )

        report = metrics.as_dict()

        self.assertTrue(report["target_met"])
        self.assertEqual(8, report["criterion_count"])
        self.assertEqual(0, report["failed_criterion_count"])
        self.assertEqual(0, report["high_risk_false_auto_confirmed_count"])
        self.assertEqual("datasets/gold/poc_mode_comparison_v1.json", report["poc_comparison"])
        self.assertEqual(
            [
                "high_risk_review",
                "missed_detection_zero",
                "source_traceability",
                "originality",
                "audit_trail",
                "completeness",
                "reproducibility",
                "segregation_of_duties",
            ],
            [criterion["id"] for criterion in report["criteria"]],
        )
        self.assertTrue(all(criterion["status"] == "pass" for criterion in report["criteria"]))
        segregation = report["criteria"][-1]
        self.assertEqual("segregation_of_duties", segregation["id"])
        self.assertEqual(
            "review approval flows with authenticated actor identity",
            segregation["scope"],
        )
        self.assertNotIn("excluded_contexts", segregation)
        self.assertIn("Authenticated role-token flows", segregation["notes"])
        self.assertIn("no-auth approval attempts are forbidden", segregation["notes"])

    def test_gmp_acceptance_requires_canonical_dataset_manifest(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["dataset_manifest"] = "datasets/fixtures/alternate_manifest.json"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "dataset_manifest must be datasets/fixtures/manifest.json",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_requires_rerun_command(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["verification_commands"] = ["python3 -m pytest tests -q"]

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "verification_commands must include",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_absolute_path_verification_command(self) -> None:
        commands = (
            f"python3 {'/' + 'private' + '/recompute.py'}",
            evaluate_dataset.EXPECTED_GMP_ACCEPTANCE_COMMAND
            + " "
            + f"python3 {'/' + 'private' + '/recompute.py'}",
            "PYTHONHOME="
            + "/"
            + "private "
            + "python3 scripts/evaluate_dataset.py",
            "PYTHONPATH="
            + "D:"
            + "\\private "
            + "python3 scripts/evaluate_dataset.py",
        )
        for command in commands:
            with self.subTest(command=command):
                data = self.valid_gmp_acceptance_data()
                data["verification_commands"].append(command)

                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError,
                    r"verification_commands\[\d+\] must not contain absolute paths",
                ):
                    evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_non_public_verification_command_path(self) -> None:
        commands = (
            "python3 private/recompute.py",
            evaluate_dataset.EXPECTED_GMP_ACCEPTANCE_COMMAND
            + " python3 private/recompute.py",
            "PYTHONHOME=private "
            + evaluate_dataset.EXPECTED_GMP_ACCEPTANCE_COMMAND,
            "PYTHONPATH=private "
            + evaluate_dataset.EXPECTED_GMP_ACCEPTANCE_COMMAND,
        )
        for command in commands:
            with self.subTest(command=command):
                data = self.valid_gmp_acceptance_data()
                data["verification_commands"].append(command)

                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError,
                    r"verification_commands\[\d+\] must reference public repository files",
                ):
                    evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_non_public_path_option_assignment(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["verification_commands"].append(
            "python3 -m pytest --rootdir=private tests -q"
        )

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            r"verification_commands\[\d+\] must reference public repository files",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_bare_non_public_command_path(self) -> None:
        commands = (
            "python3 private_runner",
            "pytest private",
        )
        for command in commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temp_dir:
                temp_root = Path(temp_dir)
                self.prepare_gmp_acceptance_repo(temp_root)
                (temp_root / "private").mkdir()
                (temp_root / "private_runner").write_text(
                    "not public verification evidence",
                    encoding="utf-8",
                )
                data = self.valid_gmp_acceptance_data()
                data["verification_commands"].append(command)

                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError,
                    r"verification_commands\[\d+\] must reference public repository files",
                ):
                    evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

    def test_gmp_acceptance_rejects_private_python_module_target(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["verification_commands"].append("python3 -m private_runner")

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            r"verification_commands\[\d+\] must reference public repository files",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_shell_control_verification_command(self) -> None:
        commands = (
            "python3 scripts/evaluate_dataset.py;/" + "private/recompute.py",
            evaluate_dataset.EXPECTED_GMP_ACCEPTANCE_COMMAND
            + " && python3 scripts/evaluate_dataset.py",
        )
        for command in commands:
            with self.subTest(command=command):
                data = self.valid_gmp_acceptance_data()
                data["verification_commands"].append(command)

                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError,
                    r"verification_commands\[\d+\] must not contain shell control operators",
                ):
                    evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_shell_expansion_verification_command(self) -> None:
        commands = (
            "python3 $PRIVATE_RECOMPUTE",
            "PYTHONPATH=$PRIVATE_LIB python3 scripts/evaluate_dataset.py",
            "python3 $(pwd)/scripts/evaluate_dataset.py",
            "pytest *",
            "python3 ~",
            "python3 scripts/*.py",
        )
        for command in commands:
            with self.subTest(command=command):
                data = self.valid_gmp_acceptance_data()
                data["verification_commands"].append(command)

                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError,
                    r"verification_commands\[\d+\] must not contain shell expansion tokens",
                ):
                    evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_missing_public_verification_command_path(
        self,
    ) -> None:
        data = self.valid_gmp_acceptance_data()
        data["verification_commands"].append("python3 scripts/ci/deleted_gate.py")

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            r"verification_commands\[\d+\] must reference existing public repository paths",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_missing_criterion_evidence_ref(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["criteria"][0]["evidence_refs"] = ["datasets/gold/deleted-evidence.json"]

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            r"criteria\[0\]\.evidence_refs\[0\] must reference an existing file",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_rejects_non_public_criterion_evidence_ref(self) -> None:
        data = self.valid_gmp_acceptance_data()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            self.prepare_gmp_acceptance_repo(temp_root)
            private_evidence = temp_root / "private" / "confidential-record.pdf"
            private_evidence.parent.mkdir()
            private_evidence.write_text("not public synthetic evidence", encoding="utf-8")
            data["criteria"][0]["evidence_refs"] = [
                private_evidence.relative_to(temp_root).as_posix()
            ]

            with self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                r"criteria\[0\]\.evidence_refs\[0\] must reference public synthetic GMP evidence",
            ):
                evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

    def test_gmp_acceptance_rejects_public_path_to_non_public_evidence_target(
        self,
    ) -> None:
        data = self.valid_gmp_acceptance_data()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            self.prepare_gmp_acceptance_repo(temp_root)
            private_evidence = temp_root / "private" / "confidential-record.pdf"
            private_evidence.parent.mkdir()
            private_evidence.write_text("not public synthetic evidence", encoding="utf-8")
            public_ref = Path("datasets/gold/confidential-record.pdf")
            try:
                os.symlink(private_evidence, temp_root / public_ref)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            data["criteria"][0]["evidence_refs"] = [public_ref.as_posix()]

            with self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                r"criteria\[0\]\.evidence_refs\[0\] must reference public synthetic GMP evidence",
            ):
                evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

    def test_gmp_acceptance_rejects_unmanifested_fixture_evidence_ref(self) -> None:
        data = self.valid_gmp_acceptance_data()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            self.prepare_gmp_acceptance_repo(temp_root)
            undeclared_evidence = Path("datasets/fixtures/raw-record.pdf")
            (temp_root / undeclared_evidence).write_text(
                "not manifest-declared synthetic fixture evidence",
                encoding="utf-8",
            )
            data["criteria"][0]["evidence_refs"] = [undeclared_evidence.as_posix()]

            with self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                r"criteria\[0\]\.evidence_refs\[0\] must reference manifest-declared "
                "public synthetic GMP fixture evidence",
            ):
                evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

    def test_gmp_acceptance_rejects_source_traceability_without_recomputed_linkage(
        self,
    ) -> None:
        data = self.valid_gmp_acceptance_data()
        poc_data = self.valid_poc_comparison_data()
        for mode in poc_data["modes"]:
            if mode["mode"] == "high_quality":
                mode["cases"][0]["actual"]["tables"][0]["cells"][0].pop("source")
                mode["metrics"]["source_linkage_rate"] = 0.5

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            self.prepare_gmp_acceptance_repo(temp_root)
            (temp_root / POC_COMPARISON_PATH.relative_to(REPO_ROOT)).write_text(
                json.dumps(poc_data),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                "source_traceability cannot pass when high_quality source linkage is incomplete",
            ):
                evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

    def test_gmp_acceptance_rejects_tampered_review_evidence_package(self) -> None:
        def omit_dataset_manifest(
            data: dict[str, object], poc_data: dict[str, object]
        ) -> None:
            data.pop("dataset_manifest")

        def replace_rerun_command(
            data: dict[str, object], poc_data: dict[str, object]
        ) -> None:
            data["verification_commands"] = ["python3 -m pytest tests -q"]

        def delete_evidence_ref(
            data: dict[str, object], poc_data: dict[str, object]
        ) -> None:
            data["criteria"][0]["evidence_refs"] = ["datasets/gold/deleted-evidence.json"]

        def remove_high_quality_source_anchor(
            data: dict[str, object], poc_data: dict[str, object]
        ) -> None:
            for mode in poc_data["modes"]:
                if mode["mode"] == "high_quality":
                    mode["cases"][0]["actual"]["tables"][0]["cells"][0].pop("source")
                    mode["metrics"]["source_linkage_rate"] = 0.5

        cases = (
            ("dataset_manifest", omit_dataset_manifest, "dataset_manifest"),
            ("verification_commands", replace_rerun_command, "verification_commands"),
            (
                "evidence_refs",
                delete_evidence_ref,
                r"criteria\[0\]\.evidence_refs\[0\] must reference an existing file",
            ),
            (
                "source_traceability",
                remove_high_quality_source_anchor,
                "source_traceability cannot pass when high_quality source linkage is incomplete",
            ),
        )
        for name, tamper, expected_error in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                temp_root = Path(temp_dir)
                data = self.valid_gmp_acceptance_data()
                poc_data = self.valid_poc_comparison_data()
                tamper(data, poc_data)
                self.prepare_gmp_acceptance_repo(temp_root)
                (temp_root / POC_COMPARISON_PATH.relative_to(REPO_ROOT)).write_text(
                    json.dumps(poc_data),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    evaluate_dataset.EvaluationCaseError, expected_error
                ):
                    evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

    def test_gmp_acceptance_ignores_manual_correction_timing_gate(self) -> None:
        data = self.valid_gmp_acceptance_data()
        poc_data = self.valid_poc_comparison_data()
        poc_data["manual_correction_time"]["assisted_minutes"] = 13.0

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            self.prepare_gmp_acceptance_repo(temp_root)
            (temp_root / POC_COMPARISON_PATH.relative_to(REPO_ROOT)).write_text(
                json.dumps(poc_data),
                encoding="utf-8",
            )

            metrics = evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=temp_root)

        self.assertTrue(metrics.target_met)
        self.assertEqual(0, metrics.failed_criterion_count)
        self.assertEqual((), metrics.failed_criteria)

    def test_gmp_acceptance_rejects_unqualified_sod_pass(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["criteria"][7].pop("scope")

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            r"criteria\[7\]\.scope must qualify segregation_of_duties pass status",
        ):
            evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

    def test_gmp_acceptance_fails_when_audit_evidence_is_unmet(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["criteria"][4]["status"] = "fail"

        metrics = evaluate_dataset.evaluate_gmp_acceptance(data, repo_root=REPO_ROOT)

        self.assertFalse(metrics.target_met)
        self.assertEqual(1, metrics.failed_criterion_count)
        self.assertEqual("audit_trail", metrics.failed_criteria[0]["id"])

    def test_change_management_requires_gmp_acceptance_gate(self) -> None:
        docs = (
            REPO_ROOT / "docs" / "change-management-reevaluation.md"
        ).read_text(encoding="utf-8")
        command = evaluate_dataset.EXPECTED_GMP_ACCEPTANCE_COMMAND
        gate_start = docs.index("### GMP Acceptance Gate")
        checklist_start = docs.index("## PR Checklist")

        self.assertIn(command, docs[gate_start:checklist_start])
        self.assertIn(command, docs[checklist_start:])

    def test_llm_stability_agreement_rates_do_not_depend_on_run_order(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"] = [data["runs"][2], data["runs"][1], data["runs"][0]]

        metrics = evaluate_dataset.evaluate_llm_stability(data)

        self.assertEqual(2 / 3, metrics.plan_agreement_rate)
        self.assertEqual(2 / 3, metrics.confirmed_value_agreement_rate)
        self.assertEqual(
            (
                {
                    "reference_run_id": "run-001",
                    "run_id": "run-002",
                    "changed": "confirmed_values",
                },
                {
                    "reference_run_id": "run-001",
                    "run_id": "run-003",
                    "changed": "conversion_plan",
                },
            ),
            metrics.unstable_examples,
        )

    def test_llm_stability_reference_run_matches_plan_and_value_majorities(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][2]["confirmed_values"] = copy.deepcopy(data["runs"][1]["confirmed_values"])

        metrics = evaluate_dataset.evaluate_llm_stability(data)

        self.assertEqual(
            (
                {
                    "reference_run_id": "run-002",
                    "run_id": "run-001",
                    "changed": "confirmed_values",
                },
                {
                    "reference_run_id": "run-002",
                    "run_id": "run-003",
                    "changed": "conversion_plan",
                },
            ),
            metrics.unstable_examples,
        )

    def test_llm_stability_reports_separate_references_without_joint_majority(self) -> None:
        data = self.valid_llm_stability_data()
        run_004 = copy.deepcopy(data["runs"][2])
        run_004["run_id"] = "run-004"
        run_004["conversion_plan"]["operations"][0]["rationale"] = (
            "The alternate synthetic record wording labels the release date directly."
        )
        data["runs"].append(run_004)
        data["n"] = 4
        data["runs"][0]["confirmed_values"][1]["value"] = "2026-01-17"

        metrics = evaluate_dataset.evaluate_llm_stability(data)

        self.assertEqual(4, metrics.unstable_example_count)
        self.assertEqual(
            (
                {
                    "reference_plan_run_id": "run-001",
                    "reference_confirmed_values_run_id": "run-003",
                    "run_id": "run-001",
                    "changed": "confirmed_values",
                },
                {
                    "reference_plan_run_id": "run-001",
                    "reference_confirmed_values_run_id": "run-003",
                    "run_id": "run-002",
                    "changed": "confirmed_values",
                },
                {
                    "reference_plan_run_id": "run-001",
                    "reference_confirmed_values_run_id": "run-003",
                    "run_id": "run-003",
                    "changed": "conversion_plan",
                },
            ),
            metrics.unstable_examples,
        )

    def test_llm_stability_rejects_empty_confirmed_values_before_scoring(self) -> None:
        data = self.valid_llm_stability_data()
        for run in data["runs"]:
            run["confirmed_values"] = []

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "at least one public confirmed value"
        ):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_rejects_non_public_source_kind_before_scoring(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][0]["conversion_plan"]["source_kind"] = "real_confidential_record"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "public-only synthetic or anonymized"
        ):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_rejects_invalid_run_count_before_scoring(self) -> None:
        data = self.valid_llm_stability_data()
        data["n"] = 4

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "runs length"):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_rejects_invalid_conversion_plan_before_scoring(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][0]["conversion_plan"]["constraints"]["external_transmission"] = True

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "conversion_plan"):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_requires_explicit_run_outcome(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][0].pop("outcome")

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "outcome"):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_rejects_schema_passed_fallback(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][0]["outcome"]["deterministic_fallback_used"] = True

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "fallback requires"
        ):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_rejects_schema_failure_without_repair_or_fallback(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][1]["outcome"] = {
            "schema_validation_passed": False,
            "repair_attempted": True,
            "repair_succeeded": False,
            "deterministic_fallback_used": False,
            "external_ai_api_transmission_attempted": False,
        }

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "repaired or use"):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_rejects_repaired_run_that_also_uses_fallback(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][1]["outcome"] = {
            "schema_validation_passed": False,
            "repair_attempted": True,
            "repair_succeeded": True,
            "deterministic_fallback_used": True,
            "external_ai_api_transmission_attempted": False,
        }

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "both repair"):
            evaluate_dataset.evaluate_llm_stability(data)

    def test_llm_stability_counts_external_ai_api_guard_violations(self) -> None:
        data = self.valid_llm_stability_data()
        data["runs"][0]["outcome"]["external_ai_api_transmission_attempted"] = True

        metrics = evaluate_dataset.evaluate_llm_stability(data)

        self.assertEqual(1, metrics.external_ai_api_guard_violation_count)

    def test_missing_actual_cell_counts_as_missing_source_link(self) -> None:
        data = self.valid_cases_data()
        case = data["cases"][0]
        actual_table = case["actual"]["tables"][0]
        actual_table["cells"] = actual_table["cells"][:1]

        metrics = self.evaluate_valid_cases(data)

        self.assertEqual(2, metrics.expected_source_link_count)
        self.assertEqual(1, metrics.matched_source_link_count)
        self.assertEqual(0.5, metrics.source_linkage_rate)

    def test_rejects_unknown_case_fixture_id(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["fixture_id"] = "missing-fixture"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "unknown fixture_id"):
            self.evaluate_valid_cases(data)

    def test_rejects_placeholder_fixture_without_path(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["fixture_id"] = "placeholder-text-pdf"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "unknown fixture_id"):
            self.evaluate_valid_cases(data)

    def test_rejects_non_public_manifest_fixture_before_scoring(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "public confidentiality"):
            self.evaluate_with_fixture(
                data,
                fixture,
                fixture_metadata={"confidentiality": "confidential"},
            )

    def test_rejects_pending_anonymization_fixture_with_path_before_scoring(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "synthetic or anonymized"
        ):
            self.evaluate_with_fixture(
                data,
                fixture,
                fixture_metadata={"anonymization": "pending_synthetic_fixture"},
            )

    def test_rejects_fixture_root_policy_traversal_before_scoring(self) -> None:
        for allowed_root, fixture_relpath in (
            ("../fixtures", "datasets/fixtures/fixture.json"),
            ("datasets/..", "secret/fixture.json"),
            ("fixtures", "fixtures/fixture.json"),
        ):
            data = self.valid_cases_data()
            fixture = evaluate_dataset.load_json(
                REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
            )

            with self.subTest(allowed_root=allowed_root), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError, "allowed_fixture_root"
            ):
                self.evaluate_with_fixture(
                    data,
                    fixture,
                    manifest_policy={"allowed_fixture_root": allowed_root},
                    fixture_relpath=fixture_relpath,
                )

    def test_rejects_non_canonical_dataset_manifest_before_scoring(self) -> None:
        for manifest_path in (
            str(REPO_ROOT / "datasets" / "fixtures" / "manifest.json"),
            "datasets/fixtures/side-manifest.json",
            "datasets/fixtures/../fixtures/manifest.json",
        ):
            data = self.valid_cases_data()
            data["dataset_manifest"] = manifest_path

            with self.subTest(manifest_path=manifest_path), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                "dataset_manifest must be datasets/fixtures/manifest.json",
            ):
                self.evaluate_valid_cases(data)

    def test_rejects_expected_table_missing_from_declared_fixture(self) -> None:
        data = self.valid_cases_data()
        expected_table = data["cases"][0]["expected"]["tables"][0]
        expected_table["id"] = "missing-table"
        expected_table["fixture_table_id"] = "missing-table"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "is not present in fixture"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_expected_table_with_mismatched_fixture_anchor(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["expected"]["tables"][0]["fixture_table_id"] = "other-table"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "matching fixture_table_id"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_unsupported_fixture_schema_version(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )
        fixture["schema_version"] = "veridoc-evaluation-fixture/v999"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "unsupported fixture schema_version"
        ):
            self.evaluate_with_fixture(data, fixture)

    def test_rejects_case_document_id_drift_from_fixture(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["document_id"] = "other-document"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "document_id"):
            self.evaluate_valid_cases(data)

    def test_rejects_expected_cell_text_or_source_drift_from_fixture(self) -> None:
        data = self.valid_cases_data()
        cell = data["cases"][0]["expected"]["tables"][0]["cells"][1]
        cell["text"] = "SAMPLE-LOT-999"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "text does not match"):
            self.evaluate_valid_cases(data)

        data = self.valid_cases_data()
        cell = data["cases"][0]["expected"]["tables"][0]["cells"][1]
        cell["source"]["bbox"]["x"] = 999.0

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "source does not match"):
            self.evaluate_valid_cases(data)

    def test_rejects_expected_cell_requires_review_drift_from_fixture(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["expected"]["tables"][0]["cells"][1]["requires_review"] = False

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "requires_review"):
            self.evaluate_valid_cases(data)

    def test_rejects_expected_cell_requires_review_non_boolean_before_scoring(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )
        fixture["tables"][0]["cells"][1]["requires_review"] = False
        data["cases"][0]["expected"]["tables"][0]["cells"][1]["requires_review"] = 0

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "requires_review must be a boolean"
        ):
            self.evaluate_with_fixture(data, fixture)

    def test_rejects_malformed_fixture_source_anchor_before_scoring(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )
        fixture["tables"][0]["cells"][1]["source"] = {}
        data["cases"][0]["expected"]["tables"][0]["cells"][1]["source"] = {}

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "source must define"):
            self.evaluate_with_fixture(data, fixture)

    def test_rejects_non_finite_fixture_geometry_before_scoring(self) -> None:
        for update_fixture in (
            lambda fixture: fixture["pages"][0].update({"width": float("inf")}),
            lambda fixture: fixture["tables"][0]["cells"][1]["source"]["bbox"].update(
                {"x": float("nan")}
            ),
        ):
            data = self.valid_cases_data()
            fixture = evaluate_dataset.load_json(
                REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
            )
            update_fixture(fixture)
            data["cases"][0]["expected"]["tables"][0]["cells"][1]["source"] = copy.deepcopy(
                fixture["tables"][0]["cells"][1]["source"]
            )

            with self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError, "non-finite JSON number"
            ):
                self.evaluate_with_fixture(data, fixture)

    def test_rejects_oversized_integer_fixture_geometry_before_scoring(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )
        fixture["pages"][0]["width"] = 10**400

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "page width"):
            self.evaluate_with_fixture(data, fixture)

    def test_rejects_missing_or_non_string_expected_cell_text_before_scoring(self) -> None:
        for text_value in (None, 123, "   "):
            data = self.valid_cases_data()
            expected_cell = data["cases"][0]["expected"]["tables"][0]["cells"][1]
            if text_value is None:
                del expected_cell["text"]
            else:
                expected_cell["text"] = text_value

            with self.subTest(text_value=text_value), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError, "text must be a non-empty string"
            ):
                self.evaluate_valid_cases(data)

    def test_rejects_missing_or_non_string_fixture_cell_text_before_scoring(self) -> None:
        for text_value in (None, 123, "   "):
            data = self.valid_cases_data()
            fixture = evaluate_dataset.load_json(
                REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
            )
            fixture_cell = fixture["tables"][0]["cells"][1]
            expected_cell = data["cases"][0]["expected"]["tables"][0]["cells"][1]
            if text_value is None:
                del fixture_cell["text"]
                del expected_cell["text"]
            else:
                fixture_cell["text"] = text_value
                expected_cell["text"] = text_value

            with self.subTest(text_value=text_value), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError, "text must be a non-empty string"
            ):
                self.evaluate_with_fixture(data, fixture)

    def test_source_matching_requires_concrete_anchor(self) -> None:
        self.assertFalse(
            evaluate_dataset.source_matches(
                {"source": {}},
                {"source": {}},
            )
        )

    def test_actual_source_anchor_must_be_valid_before_credit(self) -> None:
        data = self.valid_cases_data()
        actual_source = data["cases"][0]["actual"]["tables"][0]["cells"][0]["source"]
        actual_source["source_page"] = True

        metrics = self.evaluate_valid_cases(data)

        self.assertEqual(0, metrics.matched_source_link_count)
        self.assertEqual(0.0, metrics.source_linkage_rate)

    def test_rejects_source_anchor_outside_declared_page_geometry_before_scoring(self) -> None:
        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )
        fixture["tables"][0]["cells"][1]["source"]["source_page"] = 99
        data["cases"][0]["expected"]["tables"][0]["cells"][1]["source"]["source_page"] = 99

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "source_page"):
            self.evaluate_with_fixture(data, fixture)

        data = self.valid_cases_data()
        fixture = evaluate_dataset.load_json(
            REPO_ROOT / "datasets" / "fixtures" / "sample-document-ir-v0.json"
        )
        fixture["tables"][0]["cells"][1]["source"]["bbox"]["x"] = 580.0
        data["cases"][0]["expected"]["tables"][0]["cells"][1]["source"]["bbox"]["x"] = 580.0

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "page geometry"):
            self.evaluate_with_fixture(data, fixture)

    def test_direct_evaluation_uses_explicit_manifest_root_from_any_cwd(self) -> None:
        data = self.valid_cases_data()
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as other_dir:
            try:
                os.chdir(other_dir)
                metrics = self.evaluate_valid_cases(data)
            finally:
                os.chdir(original_cwd)

        self.assertEqual(1.0, metrics.table_extraction_rate)

    def test_rejects_unsupported_evaluation_schema_version(self) -> None:
        data = self.valid_cases_data()
        data["schema_version"] = "veridoc-evaluation-cases/v999"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "unsupported evaluation schema_version"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_non_phase0_scope_before_scoring(self) -> None:
        data = self.valid_cases_data()
        data["scope"]["phase"] = "phase1"

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "phase0"):
            self.evaluate_valid_cases(data)

    def test_rejects_duplicate_table_ids_before_indexing(self) -> None:
        data = self.valid_cases_data()
        expected = data["cases"][0]["expected"]
        expected["tables"].append(copy.deepcopy(expected["tables"][0]))

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "duplicate table id"):
            self.evaluate_valid_cases(data)

    def test_rejects_empty_expected_tables_before_scoring(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["expected"]["tables"] = []

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "tables must contain at least one table"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_non_object_case_sections_before_indexing(self) -> None:
        for section in ("expected", "actual"):
            data = self.valid_cases_data()
            data["cases"][0][section] = []

            with self.subTest(section=section), self.assertRaisesRegex(
                evaluate_dataset.EvaluationCaseError,
                "expected and actual sections must be objects",
            ):
                self.evaluate_valid_cases(data)

    def test_rejects_duplicate_cell_ids_before_indexing(self) -> None:
        data = self.valid_cases_data()
        cells = data["cases"][0]["expected"]["tables"][0]["cells"]
        cells.append(copy.deepcopy(cells[0]))

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "duplicate cell id"):
            self.evaluate_valid_cases(data)

    def test_rejects_empty_expected_table_cells_before_scoring(self) -> None:
        data = self.valid_cases_data()
        data["cases"][0]["expected"]["tables"][0]["cells"] = []

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "cells must contain at least one cell"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_empty_evaluation_case_list_before_scoring(self) -> None:
        data = self.valid_cases_data()
        data["cases"] = []

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "at least one evaluation case"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_duplicate_case_ids_before_scoring(self) -> None:
        data = self.valid_cases_data()
        data["cases"].append(copy.deepcopy(data["cases"][0]))

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "duplicate case id"):
            self.evaluate_valid_cases(data)

    def test_rejects_non_string_actual_cell_text_before_scoring(self) -> None:
        data = self.valid_cases_data()
        actual_cell = data["cases"][0]["actual"]["tables"][0]["cells"][0]
        actual_cell["text"] = None

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "actual cell 'table-001-r1-c1': text"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_non_string_extra_actual_cell_text_before_scoring(self) -> None:
        data = self.valid_cases_data()
        actual_cells = data["cases"][0]["actual"]["tables"][0]["cells"]
        actual_cells.append(
            {
                "id": "table-001-extra",
                "text": 123,
                "source": {},
                "auto_confirmed": False,
            }
        )

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError, "actual cell 'table-001-extra': text"
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_non_boolean_actual_auto_confirmed_before_scoring(self) -> None:
        data = self.valid_cases_data()
        actual_cell = data["cases"][0]["actual"]["tables"][0]["cells"][1]
        actual_cell["auto_confirmed"] = "true"

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "actual cell 'table-001-r1-c2': auto_confirmed must be a boolean",
        ):
            self.evaluate_valid_cases(data)

    def test_rejects_non_boolean_extra_actual_auto_confirmed_before_scoring(self) -> None:
        data = self.valid_cases_data()
        actual_cells = data["cases"][0]["actual"]["tables"][0]["cells"]
        actual_cells.append(
            {
                "id": "table-001-extra",
                "text": "extra",
                "source": {},
                "auto_confirmed": "yes",
            }
        )

        with self.assertRaisesRegex(
            evaluate_dataset.EvaluationCaseError,
            "actual cell 'table-001-extra': auto_confirmed must be a boolean",
        ):
            self.evaluate_valid_cases(data)

    def test_cli_emits_json_metrics_for_local_or_ci_verification(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--cases", str(CASES_PATH)],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        metrics = json.loads(proc.stdout)
        self.assertEqual(1.0, metrics["table_extraction_rate"])
        self.assertEqual(0.5, metrics["cell_match_rate"])
        self.assertEqual(0.5, metrics["source_linkage_rate"])
        self.assertEqual(1, metrics["false_auto_confirmed_count"])

    def test_cli_emits_llm_stability_metrics_for_phase1_scope_decision(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--llm-stability-runs",
                str(LLM_STABILITY_RUNS_PATH),
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        metrics = json.loads(proc.stdout)
        self.assertEqual(3, metrics["run_count"])
        self.assertEqual(2 / 3, metrics["plan_agreement_rate"])
        self.assertEqual(2 / 3, metrics["confirmed_value_agreement_rate"])
        self.assertEqual(2 / 3, metrics["schema_failure_rate"])
        self.assertEqual(1 / 2, metrics["repair_success_rate"])
        self.assertEqual(1 / 3, metrics["deterministic_fallback_rate"])
        self.assertEqual(0, metrics["external_ai_api_guard_violation_count"])
        self.assertEqual(2, metrics["unstable_example_count"])

    def test_cli_emits_poc_mode_comparison_for_phase1_acceptance(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--poc-comparison",
                str(POC_COMPARISON_PATH),
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        metrics = json.loads(proc.stdout)
        self.assertEqual(["no_llm", "standard", "high_quality"], metrics["required_modes"])
        self.assertEqual(0, metrics["high_risk_false_auto_confirmed_count"])
        self.assertEqual(7.0, metrics["manual_correction_time"]["reduction_minutes"])
        self.assertEqual(7 / 12, metrics["manual_correction_time"]["reduction_rate"])
        self.assertEqual(2, metrics["mode_diffs"][1]["warning_removed_count"])
        self.assertTrue(metrics["target_met"])

    def test_cli_emits_llm_stability_report_for_phase9_handoff(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--llm-stability-report",
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        report = json.loads(proc.stdout)
        self.assertEqual(
            "veridoc-llm-stability-evaluation/v0",
            report["schema_version"],
        )
        self.assertEqual(
            0,
            report["llm_stability"]["external_ai_api_guard_violation_count"],
        )
        self.assertEqual(
            2,
            report["poc_mode_comparison"]["mode_diffs"][1]["warning_removed_count"],
        )
        self.assertEqual(
            "datasets/gold/llm_stability_runs_v0.json",
            report["phase9_handoff"]["stability_source"],
        )

    def test_cli_llm_stability_report_preserves_custom_input_paths(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--llm-stability-report",
                "--llm-stability-runs",
                "datasets/gold/llm_stability_runs_v0.json",
                "--poc-comparison",
                "datasets/gold/poc_mode_comparison_v1.json",
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        report = json.loads(proc.stdout)
        self.assertEqual(
            "datasets/gold/llm_stability_runs_v0.json",
            report["phase9_handoff"]["stability_source"],
        )
        self.assertEqual(
            "datasets/gold/poc_mode_comparison_v1.json",
            report["phase9_handoff"]["poc_comparison_source"],
        )

    def test_cli_emits_gmp_acceptance_for_phase0_acceptance(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--gmp-acceptance",
                str(GMP_ACCEPTANCE_PATH),
            ],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        metrics = json.loads(proc.stdout)
        self.assertEqual(8, metrics["criterion_count"])
        self.assertEqual(0, metrics["failed_criterion_count"])
        self.assertTrue(metrics["target_met"])

    def test_cli_fails_when_gmp_acceptance_target_is_unmet(self) -> None:
        data = self.valid_gmp_acceptance_data()
        data["criteria"][4]["status"] = "fail"

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            self.prepare_gmp_acceptance_repo(temp_root)
            gmp_path = temp_root / GMP_ACCEPTANCE_PATH.relative_to(REPO_ROOT)
            gmp_path.write_text(json.dumps(data), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "--gmp-acceptance",
                    str(gmp_path),
                ],
                cwd=REPO_ROOT,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        self.assertEqual("", proc.stderr)
        self.assertEqual(1, proc.returncode)
        metrics = json.loads(proc.stdout)
        self.assertFalse(metrics["target_met"])
        self.assertEqual(1, metrics["failed_criterion_count"])
        self.assertEqual("audit_trail", metrics["failed_criteria"][0]["id"])


if __name__ == "__main__":
    unittest.main()
