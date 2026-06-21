from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
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
    def test_public_fixture_metrics_cover_phase0_acceptance_criteria(self) -> None:
        data = evaluate_dataset.load_json(CASES_PATH)

        metrics = evaluate_dataset.evaluate_cases(data)

        self.assertEqual(1.0, metrics.table_extraction_rate)
        self.assertEqual(0.5, metrics.cell_match_rate)
        self.assertEqual(0.5, metrics.source_linkage_rate)
        self.assertEqual(1, metrics.false_auto_confirmed_count)
        self.assertEqual(1, metrics.expected_table_count)
        self.assertEqual(2, metrics.expected_cell_count)
        self.assertEqual(2, metrics.expected_source_link_count)

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
