from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "datasets" / "fixtures" / "manifest.json"
GOLD_LABELS_PATH = REPO_ROOT / "datasets" / "gold" / "high_risk_labels_v0.json"


class DatasetFixturesTest(unittest.TestCase):
    def test_manifest_defines_public_fixture_policy_and_source_slots(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

        policy = manifest["policy"]
        self.assertTrue(policy["public_only"])
        self.assertFalse(policy["confidential_source_documents_allowed"])
        self.assertEqual("datasets/fixtures", policy["allowed_fixture_root"])
        self.assertEqual("datasets/gold", policy["gold_root"])

        source_types = {fixture["source_type"] for fixture in manifest["fixtures"]}
        self.assertEqual(set(manifest["required_source_types"]), source_types)

        for fixture in manifest["fixtures"]:
            self.assertEqual("public", fixture["confidentiality"])
            self.assertTrue(fixture["public_review_safe"])
            self.assertIn(
                fixture["anonymization"],
                {"synthetic", "anonymized", "pending_synthetic_fixture"},
            )
            if fixture["path"] is not None:
                relpath = Path(fixture["path"])
                self.assertFalse(relpath.is_absolute(), msg=f"fixture path must be repo-relative: {relpath}")
                self.assertTrue((REPO_ROOT / relpath).is_file(), msg=f"missing fixture file: {relpath}")

    def test_high_risk_gold_labels_are_anchored_to_public_fixtures(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        gold = json.loads(GOLD_LABELS_PATH.read_text(encoding="utf-8"))

        self.assertTrue(gold["scope"]["public_only"])
        self.assertFalse(gold["scope"]["confidential_source_documents_allowed"])
        self.assertFalse(gold["scope"]["production_or_gmp_claim"])

        fixtures_by_id = {fixture["id"]: fixture for fixture in manifest["fixtures"]}
        taxonomy_by_id = {label["id"]: label for label in gold["label_taxonomy"]}

        self.assertGreaterEqual(len(gold["items"]), 1)
        for item in gold["items"]:
            fixture = fixtures_by_id[item["fixture_id"]]
            label = taxonomy_by_id[item["label_id"]]

            self.assertEqual("public", fixture["confidentiality"])
            self.assertTrue(fixture["public_review_safe"])
            self.assertEqual("high", item["risk_level"])
            self.assertEqual("high", label["risk_level"])
            self.assertTrue(item["requires_review"])
            self.assertRegex(item["block_id"], r"^block-[0-9]{3}$")
            self.assertIn("source_page", item["evidence"])
            self.assertIn("bbox", item["evidence"])


if __name__ == "__main__":
    unittest.main()
