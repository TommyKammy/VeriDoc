from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "core" / "ir" / "document-ir-v0.schema.json"
SAMPLE_PATH = REPO_ROOT / "core" / "ir" / "examples" / "sample-document-ir-v0.json"
V1_SCHEMA_PATH = REPO_ROOT / "core" / "ir" / "document-ir-v1.schema.json"
V1_SAMPLE_PATH = REPO_ROOT / "core" / "ir" / "examples" / "sample-document-ir-v1.json"
VALIDATOR_PATH = REPO_ROOT / "scripts" / "ci" / "validate_document_ir.py"


class DocumentIrSchemaTest(unittest.TestCase):
    def test_sample_document_ir_v0_validates_against_schema(self) -> None:
        self.assertTrue(SCHEMA_PATH.is_file(), f"missing schema: {SCHEMA_PATH.relative_to(REPO_ROOT)}")
        self.assertTrue(SAMPLE_PATH.is_file(), f"missing sample: {SAMPLE_PATH.relative_to(REPO_ROOT)}")
        self.assertTrue(VALIDATOR_PATH.is_file(), f"missing validator: {VALIDATOR_PATH.relative_to(REPO_ROOT)}")

        result = subprocess.run(
            [
                sys.executable,
                str(VALIDATOR_PATH),
                "--schema",
                str(SCHEMA_PATH),
                "--document",
                str(SAMPLE_PATH),
            ],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"validator failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_sample_document_ir_v1_validates_against_schema(self) -> None:
        sample = json.loads(V1_SAMPLE_PATH.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as temp_dir:
            document_path = Path(temp_dir) / "sample-document-ir-v1.json"
            document_path.write_text(json.dumps(sample["expected_ir"]), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(V1_SCHEMA_PATH),
                    "--document",
                    str(document_path),
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"validator failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_value_metadata_fields_are_required_on_blocks(self) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        block_metadata = schema["properties"]["blocks"]["items"]["properties"]["value_metadata"]

        self.assertEqual(
            {
                "source_page",
                "bbox",
                "extractor",
                "confidence",
                "requires_review",
            },
            set(block_metadata["required"]),
        )

    def test_validator_rejects_non_finite_json_numbers(self) -> None:
        document = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
        document["blocks"][0]["value_metadata"]["confidence"] = float("nan")

        with tempfile.TemporaryDirectory() as temp_dir:
            document_path = Path(temp_dir) / "non-finite-document-ir.json"
            document_path.write_text(json.dumps(document), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--document",
                    str(document_path),
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0, msg="validator unexpectedly accepted NaN")
        self.assertIn("non-finite JSON number is not allowed: NaN", result.stderr)

    def test_validator_accepts_integral_json_numbers_for_integer_fields(self) -> None:
        document = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
        document["pages"][0]["page_number"] = 1.0
        document["blocks"][0]["value_metadata"]["source_page"] = 1.0

        with tempfile.TemporaryDirectory() as temp_dir:
            document_path = Path(temp_dir) / "integral-number-document-ir.json"
            document_path.write_text(json.dumps(document), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--document",
                    str(document_path),
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"validator rejected integral JSON numbers\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )

    def test_validator_rejects_fractional_json_numbers_for_integer_fields(self) -> None:
        document = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
        document["blocks"][0]["value_metadata"]["source_page"] = 1.5

        with tempfile.TemporaryDirectory() as temp_dir:
            document_path = Path(temp_dir) / "fractional-source-page-document-ir.json"
            document_path.write_text(json.dumps(document), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--document",
                    str(document_path),
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0, msg="validator unexpectedly accepted fractional source_page")
        self.assertIn("$.blocks[0].value_metadata.source_page: expected type 'integer'", result.stderr)

    def test_validator_rejects_source_page_not_declared_in_pages(self) -> None:
        document = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
        document["blocks"][0]["value_metadata"]["source_page"] = 99

        with tempfile.TemporaryDirectory() as temp_dir:
            document_path = Path(temp_dir) / "undeclared-source-page-document-ir.json"
            document_path.write_text(json.dumps(document), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--document",
                    str(document_path),
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0, msg="validator unexpectedly accepted undeclared source_page")
        self.assertIn("$.blocks[0].value_metadata.source_page: references undeclared page 99", result.stderr)

    def test_validator_rejects_duplicate_page_numbers(self) -> None:
        document = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
        document["pages"].append({**document["pages"][0], "width": 612.0})

        with tempfile.TemporaryDirectory() as temp_dir:
            document_path = Path(temp_dir) / "duplicate-page-number-document-ir.json"
            document_path.write_text(json.dumps(document), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--document",
                    str(document_path),
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0, msg="validator unexpectedly accepted duplicate page_number")
        self.assertIn("$.pages[1].page_number: duplicates page number 1", result.stderr)

    def test_validator_rejects_bbox_outside_referenced_page(self) -> None:
        document = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
        page = document["pages"][0]
        bbox = document["blocks"][0]["value_metadata"]["bbox"]
        bbox["x"] = page["width"] - 5
        bbox["width"] = 20

        with tempfile.TemporaryDirectory() as temp_dir:
            document_path = Path(temp_dir) / "out-of-page-bbox-document-ir.json"
            document_path.write_text(json.dumps(document), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(SCHEMA_PATH),
                    "--document",
                    str(document_path),
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0, msg="validator unexpectedly accepted bbox outside page")
        self.assertIn("$.blocks[0].value_metadata.bbox: extends past page 1 width 595.0", result.stderr)

    def test_validator_rejects_v1_source_page_not_declared_in_pages(self) -> None:
        sample = json.loads(V1_SAMPLE_PATH.read_text(encoding="utf-8"))
        document = sample["expected_ir"]
        document["blocks"][0]["source_page"] = 99

        with tempfile.TemporaryDirectory() as temp_dir:
            document_path = Path(temp_dir) / "undeclared-v1-source-page-document-ir.json"
            document_path.write_text(json.dumps(document), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(V1_SCHEMA_PATH),
                    "--document",
                    str(document_path),
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0, msg="validator unexpectedly accepted undeclared v1 source_page")
        self.assertIn("$.blocks[0].source_page: references undeclared page 99", result.stderr)

    def test_validator_rejects_v1_zero_sized_pages(self) -> None:
        sample = json.loads(V1_SAMPLE_PATH.read_text(encoding="utf-8"))
        document = sample["expected_ir"]
        document["pages"][0]["width"] = 0

        with tempfile.TemporaryDirectory() as temp_dir:
            document_path = Path(temp_dir) / "zero-width-v1-page-document-ir.json"
            document_path.write_text(json.dumps(document), encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR_PATH),
                    "--schema",
                    str(V1_SCHEMA_PATH),
                    "--document",
                    str(document_path),
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0, msg="validator unexpectedly accepted zero-width v1 page")
        self.assertIn("$.pages[0].width: value must be greater than 0", result.stderr)


if __name__ == "__main__":
    unittest.main()
