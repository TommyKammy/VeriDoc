from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ADR_PATH = REPO_ROOT / "adr" / "ADR-003-windows-desktop-technology.md"
INSTALLER_ADR_PATH = REPO_ROOT / "adr" / "ADR-004-desktop-distribution-update.md"
DESKTOP_PATH = REPO_ROOT / "apps" / "desktop" / "README.md"
PACKAGE_DRY_RUN_PATH = REPO_ROOT / "scripts" / "desktop_package_dry_run.py"
CI_WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"


class DesktopTechnologyDecisionDocsTest(unittest.TestCase):
    def test_adr_records_desktop_technology_comparison_and_thin_client_boundary(
        self,
    ) -> None:
        self.assertTrue(
            ADR_PATH.is_file(),
            msg=f"missing desktop technology ADR: {ADR_PATH.relative_to(REPO_ROOT)}",
        )

        adr = ADR_PATH.read_text(encoding="utf-8")
        adr_flat = " ".join(adr.split())

        for required_heading in (
            "# ADR-003: Windows Desktop Technology Selection",
            "## Candidate Comparison",
            "## Decision",
            "## Non-Selected Options",
            "## Thin Client Boundary",
            "## Follow-Up Implementation Plan",
        ):
            self.assertIn(required_heading, adr)

        for required_text in (
            "Tauri v2",
            ".NET WPF",
            ".NET WinUI",
            "Web UI asset reuse",
            "Windows integration",
            "Distribution and maintenance",
            "CI ease",
            "OSS and license posture",
            "Selected: Tauri v2",
            "LLM/model runtime is not bundled",
            "delegates conversion and inference to the local API",
            "apps/desktop/README.md",
        ):
            self.assertIn(required_text, adr)

        for required_text in (
            "WPF is not selected",
            "WinUI is not selected",
            "reuse the existing web UI",
        ):
            self.assertIn(required_text, adr_flat)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, adr)

    def test_apps_desktop_readme_defines_minimal_follow_up_boundary(self) -> None:
        self.assertTrue(
            DESKTOP_PATH.is_file(),
            msg=f"missing desktop app boundary doc: {DESKTOP_PATH.relative_to(REPO_ROOT)}",
        )

        readme = DESKTOP_PATH.read_text(encoding="utf-8")

        for required_text in (
            "# VeriDoc Desktop App",
            "ADR-003",
            "Tauri v2",
            "thin client",
            "API delegation",
            "P5-02",
            "P5-09",
            "LLM/model runtime is not bundled",
        ):
            self.assertIn(required_text, readme)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, readme)

    def test_desktop_distribution_and_update_decision_is_recorded(self) -> None:
        self.assertTrue(
            INSTALLER_ADR_PATH.is_file(),
            msg=(
                "missing desktop distribution/update ADR: "
                f"{INSTALLER_ADR_PATH.relative_to(REPO_ROOT)}"
            ),
        )

        adr = INSTALLER_ADR_PATH.read_text(encoding="utf-8")
        adr_flat = " ".join(adr.split())

        for required_heading in (
            "# ADR-004: Desktop Distribution and Update",
            "## Candidate Comparison",
            "## Decision",
            "## Non-Selected Options",
            "## Minimum Package Procedure",
            "## Open Release Gates",
            "## Verification",
        ):
            self.assertIn(required_heading, adr)

        for required_text in (
            "Tauri v2 NSIS installer",
            "Tauri updater",
            "MSIX",
            "MSI",
            "ClickOnce",
            "Windows 10 22H2 or later",
            "scripts/desktop_package_dry_run.py --dry-run",
            "code-signing certificate",
            "update signing keys",
            "rollback",
            "managed endpoint distribution",
        ):
            self.assertIn(required_text, adr)

        for required_text in (
            "MSIX is not selected",
            "MSI is not selected",
            "ClickOnce is not selected",
        ):
            self.assertIn(required_text, adr_flat)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, adr)

    def test_desktop_package_dry_run_script_documents_minimal_package_path(self) -> None:
        self.assertTrue(
            PACKAGE_DRY_RUN_PATH.is_file(),
            msg=f"missing package dry-run script: {PACKAGE_DRY_RUN_PATH.relative_to(REPO_ROOT)}",
        )

        script = PACKAGE_DRY_RUN_PATH.read_text(encoding="utf-8")

        for required_text in (
            "tauri build --bundles nsis",
            "TAURI_SIGNING_PRIVATE_KEY",
            "TAURI_SIGNING_PRIVATE_KEY_PASSWORD",
            "VERIDOC_DESKTOP_UPDATE_ENDPOINT",
            "apps/desktop",
            "dry-run",
        ):
            self.assertIn(required_text, script)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, script)

    def test_ci_runs_desktop_package_dry_run(self) -> None:
        self.assertTrue(
            CI_WORKFLOW_PATH.is_file(),
            msg=f"missing CI workflow: {CI_WORKFLOW_PATH.relative_to(REPO_ROOT)}",
        )

        workflow = CI_WORKFLOW_PATH.read_text(encoding="utf-8")

        self.assertIn("Run desktop package dry-run", workflow)
        self.assertIn("python3 scripts/desktop_package_dry_run.py --dry-run", workflow)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, workflow)


if __name__ == "__main__":
    unittest.main()
