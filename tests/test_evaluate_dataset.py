from __future__ import annotations

import copy
import importlib.util
import json
import os
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
POC_COMPARISON_PATH = REPO_ROOT / "datasets" / "gold" / "poc_mode_comparison_v0.json"


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

    def valid_high_risk_labels_data(self) -> dict[str, object]:
        return copy.deepcopy(evaluate_dataset.load_json(HIGH_RISK_LABELS_PATH))

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
        self.assertEqual(
            ["no_llm", "standard", "high_quality"],
            [mode["mode"] for mode in metrics.as_dict()["modes"]],
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

    def test_poc_mode_comparison_rejects_non_string_high_risk_value_mismatch(
        self,
    ) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][1]["actual_value"] = False

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "actual_value"):
            evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

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

    def test_poc_mode_comparison_counts_both_auto_confirmation_sources(self) -> None:
        data = self.valid_poc_comparison_data()
        data["modes"][2]["high_risk_items"][0]["auto_confirmed"] = True
        data["modes"][2]["cases"][0]["actual"]["tables"][0]["cells"][1][
            "auto_confirmed"
        ] = True

        metrics = evaluate_dataset.evaluate_poc_mode_comparison(data, repo_root=REPO_ROOT)

        self.assertEqual(
            2,
            metrics.as_dict()["modes"][2]["high_risk_false_auto_confirmed_count"],
        )
        self.assertEqual(2, metrics.high_risk_false_auto_confirmed_count)
        self.assertFalse(metrics.target_met)

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
        self.assertTrue(metrics["target_met"])


if __name__ == "__main__":
    unittest.main()
