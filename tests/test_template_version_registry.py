from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from core.ir.template_versions import TemplateVersionError, TemplateVersionRegistry


REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATH = REPO_ROOT / "core" / "ir" / "examples" / "sample-template-definition.json"


class TemplateVersionRegistryTest(unittest.TestCase):
    def load_sample(self) -> dict[str, object]:
        return json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))

    def template_version(
        self,
        version: str,
        *,
        template_id: str = "synthetic-batch-record",
        status: str = "active",
    ) -> dict[str, object]:
        template = copy.deepcopy(self.load_sample())
        template["template_id"] = template_id
        template["version"] = version
        template["status"] = status
        template["effective"] = {"from": f"2026-01-0{version[0]}T00:00:00Z"}
        return template

    def test_selects_highest_active_version_and_allows_explicit_old_version(self) -> None:
        registry = TemplateVersionRegistry(
            [
                self.template_version("1.0.0"),
                self.template_version("1.1.0", status="inactive"),
                self.template_version("2.0.0"),
            ]
        )

        self.assertEqual(registry.select_active("synthetic-batch-record")["version"], "2.0.0")
        self.assertEqual(
            registry.get_version("synthetic-batch-record", "1.0.0")["version"],
            "1.0.0",
        )

        with self.assertRaisesRegex(TemplateVersionError, "inactive"):
            registry.get_version("synthetic-batch-record", "1.1.0")

    def test_rejects_version_collisions_and_missing_active_versions(self) -> None:
        first = self.template_version("1.0.0")
        duplicate = self.template_version("1.0.0")

        with self.assertRaisesRegex(TemplateVersionError, "duplicate"):
            TemplateVersionRegistry([first, duplicate])

        registry = TemplateVersionRegistry([self.template_version("1.0.0", status="inactive")])
        with self.assertRaisesRegex(TemplateVersionError, "active"):
            registry.select_active("synthetic-batch-record")


if __name__ == "__main__":
    unittest.main()
