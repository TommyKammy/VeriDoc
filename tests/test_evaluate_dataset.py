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


spec = importlib.util.spec_from_file_location("evaluate_dataset", SCRIPT_PATH)
assert spec is not None
evaluate_dataset = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = evaluate_dataset
spec.loader.exec_module(evaluate_dataset)


class EvaluateDatasetTest(unittest.TestCase):
    def valid_cases_data(self) -> dict[str, object]:
        return copy.deepcopy(evaluate_dataset.load_json(CASES_PATH))

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

    def test_source_matching_requires_concrete_anchor(self) -> None:
        self.assertFalse(
            evaluate_dataset.source_matches(
                {"source": {}},
                {"source": {}},
            )
        )

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

    def test_rejects_duplicate_table_ids_before_indexing(self) -> None:
        data = self.valid_cases_data()
        expected = data["cases"][0]["expected"]
        expected["tables"].append(copy.deepcopy(expected["tables"][0]))

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "duplicate table id"):
            self.evaluate_valid_cases(data)

    def test_rejects_duplicate_cell_ids_before_indexing(self) -> None:
        data = self.valid_cases_data()
        cells = data["cases"][0]["expected"]["tables"][0]["cells"]
        cells.append(copy.deepcopy(cells[0]))

        with self.assertRaisesRegex(evaluate_dataset.EvaluationCaseError, "duplicate cell id"):
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


if __name__ == "__main__":
    unittest.main()
