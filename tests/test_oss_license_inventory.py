from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = REPO_ROOT / "docs" / "oss-license-inventory.md"


class OssLicenseInventoryTest(unittest.TestCase):
    def test_phase0_license_inventory_documents_required_risk_boundaries(self) -> None:
        self.assertTrue(
            INVENTORY_PATH.is_file(),
            msg=f"missing OSS license inventory: {INVENTORY_PATH.relative_to(REPO_ROOT)}",
        )

        inventory = INVENTORY_PATH.read_text(encoding="utf-8")

        for required_heading in (
            "## Dependency Inventory",
            "## Phase1 Provisional Decision",
            "## Explicit Risk Notes",
        ):
            self.assertIn(required_heading, inventory)

        for package_name in (
            "PyMuPDF",
            "pdf2docx",
            "pypdf",
            "pdfminer.six",
            "pdfplumber",
            "camelot-py",
            "python-docx",
            "openpyxl",
            "PDF.js 4.10.38",
        ):
            self.assertIn(package_name, inventory)

        self.assertIn("Apache-2.0", inventory)
        self.assertIn("AGPL-3.0", inventory)
        self.assertIn("evaluation-only", inventory)
        self.assertIn("no longer actively maintained", inventory)
        self.assertIn("Phase1-allowed", inventory)
        self.assertIn("defusedxml", inventory)
        self.assertIn("untrusted XLSX parsing is not Phase1-approved", inventory)
        self.assertIn("pins at 0.5.12 or later", inventory)
        self.assertIn("PyMuPDF dependency", inventory)


if __name__ == "__main__":
    unittest.main()
