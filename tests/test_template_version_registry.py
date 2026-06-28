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
        effective_from: str | None = None,
        effective_until: str | None = None,
    ) -> dict[str, object]:
        template = copy.deepcopy(self.load_sample())
        template["template_id"] = template_id
        template["version"] = version
        template["status"] = status
        template["effective"] = {"from": effective_from or f"2026-01-0{version[0]}T00:00:00Z"}
        if effective_until is not None:
            template["effective"]["until"] = effective_until
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

    def test_default_active_selection_honors_effective_windows(self) -> None:
        registry = TemplateVersionRegistry(
            [
                self.template_version("1.0.0"),
                self.template_version(
                    "9.0.0",
                    effective_from="9999-01-01T00:00:00Z",
                ),
            ]
        )

        self.assertEqual(registry.select_active("synthetic-batch-record")["version"], "1.0.0")

    def test_effective_selection_parses_timezone_offsets_before_comparing(self) -> None:
        registry = TemplateVersionRegistry(
            [
                self.template_version(
                    "1.0.0",
                    effective_from="2026-01-01T00:00:00+09:00",
                    effective_until="2026-01-01T02:00:00+09:00",
                ),
                self.template_version("2.0.0", effective_from="2026-01-01T00:00:00Z"),
            ]
        )

        self.assertEqual(
            registry.select_active("synthetic-batch-record", as_of="2025-12-31T16:00:00Z")[
                "version"
            ],
            "1.0.0",
        )
        with self.assertRaisesRegex(TemplateVersionError, "not effective"):
            registry.get_version(
                "synthetic-batch-record",
                "1.0.0",
                as_of="2025-12-31T17:00:00Z",
            )

    def test_effective_metadata_requires_parseable_timezone_aware_timestamps(self) -> None:
        for timestamp in ("2026-01-01T00:00:00", "not-a-timestamp"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaisesRegex(TemplateVersionError, "timezone|ISO-8601"):
                    TemplateVersionRegistry(
                        [self.template_version("1.0.0", effective_from=timestamp)]
                    )


if __name__ == "__main__":
    unittest.main()
