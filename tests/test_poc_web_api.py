from __future__ import annotations

import base64
import hashlib
from html.parser import HTMLParser
from io import BytesIO
import json
import os
import re
import tempfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event, Lock, Thread
from typing import Optional
from xml.etree import ElementTree
import zlib
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from apps.desktop.api_client import ApiCredentialStore, DesktopApiClient, DesktopApiClientConfig
from core.parsers.docx_extraction import extract_docx_structure
from core.parsers.pdf_table_extraction import (
    ExtractedTable,
    TableBBox,
    TableExtractionCandidate,
    TableExtractionReport,
)
from core.parsers.xlsx_extraction import extract_xlsx_structure
import services.api.poc_web as poc_web
from services.api.job_queue import JobQueue
from services.api.poc_web import (
    JobAuditEventStore,
    PocWebRequestHandler,
    ReviewAuditEventStore,
    convert_uploaded_document,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_MANIFEST_PATH = REPO_ROOT / "datasets" / "fixtures" / "manifest.json"

_HTML_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class _PocUiRegionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.region_fields: dict[str, set[str]] = {}
        self.element_regions: dict[str, tuple[str, ...]] = {}
        self._stack: list[tuple[str, Optional[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record_element(tag, attrs, should_push=tag not in _HTML_VOID_TAGS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record_element(tag, attrs, should_push=False)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                return

    def _record_element(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        should_push: bool,
    ) -> None:
        attrs_by_name = dict(attrs)
        region = attrs_by_name.get("data-poc-ui-region")
        if region:
            self.region_fields[region] = set(
                (attrs_by_name.get("data-api-fields") or "").split()
            )
        active_regions = tuple(
            stack_region
            for _, stack_region in self._stack
            if stack_region is not None
        )
        if region:
            active_regions = (*active_regions, region)
        element_id = attrs_by_name.get("id")
        if element_id:
            self.element_regions[element_id] = active_regions
        if should_push:
            self._stack.append((tag, region))


def _write_docx(path: Path, document_xml: str) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
""",
        )
        archive.writestr("word/document.xml", document_xml)


def _sample_docx_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Batch Summary</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Lot SAMPLE-001 requires review</w:t></w:r></w:p>
  </w:body>
</w:document>
"""


def _sample_xlsx_bytes() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="WBS" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>
""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
""",
        )
        archive.writestr(
            "xl/styles.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="00000"/></numFmts>
  <cellXfs count="2">
    <xf numFmtId="0"/>
    <xf numFmtId="164" applyNumberFormat="1"/>
  </cellXfs>
</styleSheet>
""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:D3"/>
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>ID</t></is></c>
      <c r="B1" t="inlineStr"><is><t>Task</t></is></c>
      <c r="C1" t="inlineStr"><is><t>Due</t></is></c>
      <c r="D1" t="inlineStr"><is><t>Cost</t></is></c>
    </row>
    <row r="2">
      <c r="A2" s="1"><v>123</v></c>
      <c r="B2" t="inlineStr"><is><t>Template review</t></is></c>
      <c r="C2" t="d"><v>2026-07-03</v></c>
      <c r="D2"><v>12.5</v></c>
    </row>
  </sheetData>
</worksheet>
""",
        )
    return output.getvalue()


def _representative_excel_to_word_xlsx_bytes() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="WBS" sheetId="1" r:id="rId1"/>
    <sheet name="Issue Log" sheetId="2" r:id="rId2"/>
    <sheet name="Ledger" sheetId="3" r:id="rId3"/>
  </sheets>
</workbook>
""",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
""",
        )
        archive.writestr(
            "xl/styles.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="00000"/></numFmts>
  <cellXfs count="2">
    <xf numFmtId="0"/>
    <xf numFmtId="164" applyNumberFormat="1"/>
  </cellXfs>
</styleSheet>
""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:D3"/>
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>WBS ID</t></is></c>
      <c r="B1" t="inlineStr"><is><t>Task</t></is></c>
      <c r="C1" t="inlineStr"><is><t>Due</t></is></c>
      <c r="D1" t="inlineStr"><is><t>Progress</t></is></c>
    </row>
    <row r="2">
      <c r="A2" s="1"><v>42</v></c>
      <c r="B2" t="inlineStr"><is><t>Template mapping review</t></is></c>
      <c r="C2" t="d"><v>2026-07-10</v></c>
      <c r="D2"><v>0.75</v></c>
    </row>
  </sheetData>
</worksheet>
""",
        )
        archive.writestr(
            "xl/worksheets/sheet2.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:E3"/>
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>Issue ID</t></is></c>
      <c r="B1" t="inlineStr"><is><t>Status</t></is></c>
      <c r="C1" t="inlineStr"><is><t>Opened</t></is></c>
      <c r="D1" t="inlineStr"><is><t>Severity</t></is></c>
      <c r="E1" t="inlineStr"><is><t>Owner</t></is></c>
    </row>
    <row r="2">
      <c r="A2" t="inlineStr"><is><t>ISS-001</t></is></c>
      <c r="B2" t="inlineStr"><is><t>Open</t></is></c>
      <c r="C2" t="d"><v>2026-07-11</v></c>
      <c r="D2"><v>3</v></c>
      <c r="E2" t="inlineStr"><is><t>QA</t></is></c>
    </row>
  </sheetData>
</worksheet>
""",
        )
        archive.writestr(
            "xl/worksheets/sheet3.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <dimension ref="A1:D3"/>
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>Entry ID</t></is></c>
      <c r="B1" t="inlineStr"><is><t>Posting Date</t></is></c>
      <c r="C1" t="inlineStr"><is><t>Amount</t></is></c>
      <c r="D1" t="inlineStr"><is><t>Cleared</t></is></c>
    </row>
    <row r="2">
      <c r="A2" s="1"><v>17</v></c>
      <c r="B2" t="d"><v>2026-07-12</v></c>
      <c r="C2"><v>1234.50</v></c>
      <c r="D2" t="b"><v>1</v></c>
    </row>
  </sheetData>
</worksheet>
""",
        )
    return output.getvalue()


def test_convert_uploaded_document_surfaces_review_items_and_download_payload() -> None:
    parser_output = {
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "text": "Lot: SAMPLE-001",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 16, "unit": "pt"},
                        "confidence": 0.41,
                        "low_confidence": True,
                    }
                ],
            }
        ]
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "requires_review"
    assert re.fullmatch(r"conversion-[0-9a-f]{32}", result["conversion_id"])
    assert result["validation"]["requires_review"] is True
    assert result["review_items"] == [
        {
            "document_id": "phase0-output",
            "block_id": "block-0001",
            "source_id": "phase0-output:block-0001",
            "source_page": 1,
            "source_confidence": 0.41,
            "source_bbox": {
                "x": 10.0,
                "y": 20.0,
                "width": 120.0,
                "height": 16.0,
                "unit": "pt",
                "origin": "top-left",
            },
            "source_page_geometry": {
                "page_number": 1,
                "width": 320.0,
                "height": 240.0,
                "unit": "pt",
            },
            "text": "Lot: SAMPLE-001",
            "warnings": ["blocks[0].low confidence; block marked requires_review"],
        }
    ]
    assert result["download"]["filename"] == "phase0-output.veridoc-result.json"
    downloaded = json.loads(result["download"]["content"].decode("utf-8"))
    assert "conversion_id" not in downloaded
    assert downloaded["validation"]["requires_review"] is True
    assert downloaded["document_ir"]["blocks"][0]["review"]["requires_review"] is True


def test_convert_uploaded_document_returns_artifact_manifest_contract() -> None:
    parser_output = {
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "text": "Lot: SAMPLE-001",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 16, "unit": "pt"},
                        "confidence": 0.91,
                    }
                ],
            }
        ]
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["document_ir"]["document"]["id"] == "phase0-output"
    assert result["review_items"] == []
    assert result["warnings"] == []
    assert result["audit"] == {
        "schema_version": "veridoc-poc-conversion-audit/v1",
        "conversion_id": result["conversion_id"],
        "input": {
            "filename": "phase0-output.json",
            "source_type": "unknown",
            "sha256": result["hashes"]["source_sha256"],
            "conversion_mode": "auto",
        },
        "source_filename": "phase0-output.json",
        "source_type": "unknown",
        "source_sha256": result["hashes"]["source_sha256"],
        "conversion_mode": "auto",
        "conversion_settings": {
            "use_llm": {"requested": False, "enabled": False, "status": "disabled"},
            "use_ocr": {"requested": False, "enabled": False, "status": "disabled"},
        },
        "llm": {
            "requested": False,
            "enabled": False,
            "status": "disabled",
            "model": None,
            "base_url_type": None,
            "prompt": {"id": "veridoc_conversion_plan", "version": "poc-08"},
            "schema_version": 1,
            "parameters": {},
        },
        "conversion_plan": {
            "requested": False,
            "status": "disabled",
            "adopted": False,
            "schema_version": 1,
            "plan_hash": None,
        },
        "validation": {"ok": True, "requires_review": False, "warning_count": 0},
        "warnings": {"count": 0},
        "review_items": {"count": 0},
    }

    assert result["artifacts"] == [
        {
            "id": "debug-json",
            "kind": "debug",
            "format": "json",
            "filename": "phase0-output.veridoc-result.json",
            "content_type": "application/json; charset=utf-8",
            "size_bytes": len(result["download"]["content"]),
            "sha256": result["hashes"]["output_sha256"],
            "metadata": {
                "role": "debug",
                "conversion_mode": "auto",
                "source_filename": "phase0-output.json",
                "source_type": "unknown",
                "source_sha256": result["hashes"]["source_sha256"],
                "output_sha256": result["hashes"]["output_sha256"],
                "validation": {"ok": True, "requires_review": False, "warning_count": 0},
                "warnings": {"count": 0},
                "review_items": {"count": 0},
                "download": {
                    "available": True,
                    "field": "download",
                },
            },
        }
    ]


def test_convert_uploaded_document_records_requested_upload_settings_metadata() -> None:
    parser_output = {
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [{"text": "Lot: SAMPLE-001", "confidence": 0.91}],
            }
        ]
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        output_format="docx",
        template_id="batch-record",
    )

    assert result["audit"]["requested_output_format"] == "docx"
    assert result["audit"]["template_id"] == "batch-record"


def test_convert_uploaded_document_adopts_schema_valid_local_llm_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    parser_output = {
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "text": "Lot: SAMPLE-001",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 16, "unit": "pt"},
                        "confidence": 0.91,
                    }
                ],
            }
        ]
    }
    plan = {
        "schema_version": 1,
        "source_kind": "synthetic_text",
        "operations": [
            {
                "id": "extract-lot",
                "action": "extract_field",
                "inputs": ["Lot: SAMPLE-001"],
                "output": "lot_number",
                "rationale": "The lot value is explicitly present in the source text.",
            }
        ],
        "constraints": {"external_transmission": False},
    }
    synthetic_inputs: list[str] = []

    class FakeLocalLLMAdapter:
        base_url = "http://127.1.2.3:11434/v1"
        model = "fake-local-model"
        timeout_seconds = 30
        max_tokens = 1024

        def create_conversion_plan(self, synthetic_text: str) -> dict[str, object]:
            synthetic_inputs.append(synthetic_text)
            return plan

    monkeypatch.setattr(
        poc_web,
        "_configured_llm_conversion_plan_adapter",
        lambda: (FakeLocalLLMAdapter(), None),
    )

    result = convert_uploaded_document(
        filename="phase8-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        use_llm=True,
    )

    assert synthetic_inputs
    assert "Lot: SAMPLE-001" in synthetic_inputs[0]
    assert result["warnings"] == []
    assert result["audit"]["conversion_settings"]["use_llm"] == {
        "requested": True,
        "enabled": True,
        "status": "enabled",
    }
    assert result["audit"]["conversion_plan"] == {
        "requested": True,
        "status": "adopted",
        "adopted": True,
        "schema_version": 1,
        "plan_hash": hashlib.sha256(
            json.dumps(plan, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        ).hexdigest(),
        "plan": plan,
    }
    assert result["audit"]["llm"] == {
        "requested": True,
        "enabled": True,
        "status": "enabled",
        "model": "fake-local-model",
        "base_url_type": "local",
        "prompt": {"id": "veridoc_conversion_plan", "version": "poc-08"},
        "schema_version": 1,
        "parameters": {"max_tokens": 1024, "timeout_seconds": 30},
    }
    assert result["document_ir"]["blocks"][0]["text"] == "Lot: SAMPLE-001"
    downloaded = json.loads(result["download"]["content"])
    assert downloaded["audit"]["conversion_plan"]["status"] == "adopted"


def test_convert_uploaded_document_falls_back_for_schema_invalid_local_llm_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_output = {
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "text": "Lot: SAMPLE-001",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 16, "unit": "pt"},
                        "confidence": 0.91,
                    }
                ],
            }
        ]
    }

    invalid_plan = {
        "schema_version": 1,
        "source_kind": "synthetic_text",
        "operations": [
            {
                "id": "send-out",
                "action": "extract_field",
                "inputs": ["Lot: SAMPLE-001"],
                "output": "lot_number",
                "rationale": "Unsafe plan must be rejected.",
            }
        ],
        "constraints": {"external_transmission": True},
    }

    class FakeLocalLLMAdapter:
        def create_conversion_plan(self, synthetic_text: str) -> dict[str, object]:
            return invalid_plan

    monkeypatch.setattr(
        poc_web,
        "_configured_llm_conversion_plan_adapter",
        lambda: (FakeLocalLLMAdapter(), None),
    )

    result = convert_uploaded_document(
        filename="phase8-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        use_llm=True,
    )

    assert result["status"] == "requires_review"
    assert result["warnings"] == [
        (
            "LLM conversion plan fallback llm_fallback_schema_invalid: "
            "LLM conversion plan rejected: schema invalid; "
            "deterministic conversion used; "
            "requires review"
        )
    ]
    assert {
        "document_id": "phase8-output",
        "block_id": "__conversion_plan__",
        "source_id": "phase8-output:conversion-plan",
        "source_page": 1,
        "source_confidence": None,
        "text": "",
        "warnings": result["warnings"],
        "llm_involved": True,
    } in result["review_items"]
    assert result["audit"]["conversion_settings"]["use_llm"] == {
        "requested": True,
        "enabled": False,
        "status": "blocked",
        "reason": "schema_invalid",
    }
    assert result["audit"]["conversion_plan"] == {
        "requested": True,
        "status": "fallback",
        "adopted": False,
        "schema_version": 1,
        "plan_hash": hashlib.sha256(
            json.dumps(
                invalid_plan,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        "reason": "schema_invalid",
        "warning_code": "llm_fallback_schema_invalid",
    }
    assert result["document_ir"]["blocks"][0]["text"] == "Lot: SAMPLE-001"
    downloaded = json.loads(result["download"]["content"])
    assert downloaded["audit"]["conversion_plan"]["status"] == "fallback"
    assert downloaded["review_items"] == result["review_items"]


def test_convert_uploaded_document_falls_back_when_local_llm_plan_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_output = {
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "text": "Lot: SAMPLE-001",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 16, "unit": "pt"},
                        "confidence": 0.91,
                    }
                ],
            }
        ]
    }

    class FakeLocalLLMAdapter:
        base_url = "http://127.1.2.3:11434/v1"
        model = "fake-local-model"
        timeout_seconds = 30
        max_tokens = 1024

        def create_conversion_plan(self, synthetic_text: str) -> dict[str, object]:
            raise poc_web.ConversionPlanValidationError("local LLM response body is not valid JSON")

    monkeypatch.setattr(
        poc_web,
        "_configured_llm_conversion_plan_adapter",
        lambda: (FakeLocalLLMAdapter(), None),
    )

    result = convert_uploaded_document(
        filename="phase8-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        use_llm=True,
    )

    assert result["status"] == "requires_review"
    assert result["warnings"] == [
        (
            "LLM conversion plan fallback llm_fallback_schema_invalid: "
            "LLM conversion plan rejected: schema invalid; "
            "deterministic conversion used; "
            "requires review"
        )
    ]
    assert result["audit"]["conversion_settings"]["use_llm"] == {
        "requested": True,
        "enabled": False,
        "status": "blocked",
        "reason": "schema_invalid",
    }
    assert result["audit"]["conversion_plan"] == {
        "requested": True,
        "status": "fallback",
        "adopted": False,
        "schema_version": 1,
        "plan_hash": None,
        "reason": "schema_invalid",
        "warning_code": "llm_fallback_schema_invalid",
    }
    assert result["audit"]["llm"]["base_url_type"] == "local"
    downloaded = json.loads(result["download"]["content"])
    assert downloaded["audit"]["conversion_plan"]["plan_hash"] is None


def test_convert_uploaded_document_hashes_rejected_real_local_llm_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_output = {
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "text": "Lot: SAMPLE-001",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 16, "unit": "pt"},
                        "confidence": 0.91,
                    }
                ],
            }
        ]
    }
    rejected_plan = {
        "schema_version": 1,
        "source_kind": "synthetic_text",
        "operations": [
            {
                "id": "send-out",
                "action": "extract_field",
                "inputs": ["Lot: SAMPLE-001"],
                "output": "lot_number",
                "rationale": "Rejected plans still need deterministic audit fingerprints.",
            }
        ],
        "constraints": {"external_transmission": True},
    }
    payloads: list[dict[str, object]] = []

    def transport(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        payloads.append(payload)
        return {"choices": [{"message": {"content": rejected_plan}}]}

    adapter = poc_web.LocalLLMConversionPlanAdapter(
        base_url="http://127.0.0.1:8000/v1",
        model="fake-local-model",
        transport=transport,
    )
    monkeypatch.setattr(
        poc_web,
        "_configured_llm_conversion_plan_adapter",
        lambda: (adapter, None),
    )

    result = convert_uploaded_document(
        filename="phase8-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        use_llm=True,
    )

    expected_plan_hash = hashlib.sha256(
        json.dumps(
            rejected_plan,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert len(payloads) == 2
    assert result["status"] == "requires_review"
    assert result["audit"]["conversion_plan"] == {
        "requested": True,
        "status": "fallback",
        "adopted": False,
        "schema_version": 1,
        "plan_hash": expected_plan_hash,
        "reason": "schema_invalid",
        "warning_code": "llm_fallback_schema_invalid",
    }
    assert result["audit"]["llm"]["base_url_type"] == "local"
    downloaded = json.loads(result["download"]["content"])
    assert downloaded["audit"]["conversion_plan"]["plan_hash"] == expected_plan_hash


def test_convert_uploaded_document_schema_invalid_llm_fallback_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_output = {
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "text": "Lot: SAMPLE-001",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 16, "unit": "pt"},
                        "confidence": 0.91,
                    }
                ],
            }
        ]
    }
    invalid_plans = [
        {
            "schema_version": 1,
            "source_kind": "synthetic_text",
            "operations": [
                {
                    "id": "send-out",
                    "action": "extract_field",
                    "inputs": ["Lot: SAMPLE-001"],
                    "output": "lot_number",
                    "rationale": "Unsafe plan must be rejected.",
                }
            ],
            "constraints": {"external_transmission": True},
        },
        {
            "schema_version": 1,
            "source_kind": "synthetic_text",
            "operations": [
                {
                    "id": "unsupported-action",
                    "action": "invent_field",
                    "inputs": ["Lot: SAMPLE-001"],
                    "output": "lot_number",
                    "rationale": "Unsupported action must be rejected.",
                }
            ],
            "constraints": {"external_transmission": False},
        },
    ]
    results = []
    monkeypatch.setattr(poc_web, "_conversion_id", lambda: "conversion-fixed")

    for plan in invalid_plans:
        class FakeLocalLLMAdapter:
            def create_conversion_plan(self, synthetic_text: str) -> dict[str, object]:
                return plan

        monkeypatch.setattr(
            poc_web,
            "_configured_llm_conversion_plan_adapter",
            lambda: (FakeLocalLLMAdapter(), None),
        )
        results.append(
            convert_uploaded_document(
                filename="phase8-output.json",
                content=json.dumps(parser_output).encode("utf-8"),
                use_llm=True,
            )
        )

    assert results[0]["warnings"] == results[1]["warnings"]
    assert results[0]["review_items"] == results[1]["review_items"]
    first_plan_audit = dict(results[0]["audit"]["conversion_plan"])
    second_plan_audit = dict(results[1]["audit"]["conversion_plan"])
    first_plan_hash = first_plan_audit.pop("plan_hash")
    second_plan_hash = second_plan_audit.pop("plan_hash")
    assert first_plan_audit == second_plan_audit
    assert first_plan_hash == hashlib.sha256(
        json.dumps(
            invalid_plans[0],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert second_plan_hash == hashlib.sha256(
        json.dumps(
            invalid_plans[1],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    assert results[0]["hashes"]["output_sha256"] != results[1]["hashes"]["output_sha256"]
    assert results[0]["download"]["content"] != results[1]["download"]["content"]


def test_configured_llm_adapter_validates_later_configured_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles_path = tmp_path / "inference_profiles.json"
    profiles_path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "base_url_env": "VERIDOC_STANDARD_OPENAI_BASE_URL",
                        "model_env": "VERIDOC_STANDARD_MODEL",
                        "optional_env": [],
                    },
                    {
                        "base_url_env": "VERIDOC_HIGH_QUALITY_OPENAI_BASE_URL",
                        "model_env": "VERIDOC_HIGH_QUALITY_MODEL",
                        "optional_env": [],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(poc_web, "INFERENCE_PROFILES_PATH", profiles_path)
    monkeypatch.setenv("VERIDOC_STANDARD_OPENAI_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("VERIDOC_STANDARD_MODEL", "local-json-model")
    monkeypatch.setenv("VERIDOC_HIGH_QUALITY_OPENAI_BASE_URL", "http://127.0.0.1:9000/v1")
    monkeypatch.delenv("VERIDOC_HIGH_QUALITY_MODEL", raising=False)

    adapter, reason = poc_web._configured_llm_conversion_plan_adapter()

    assert adapter is None
    assert reason == "missing_required_model"


def test_configured_llm_adapter_rejects_non_finite_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profiles_path = tmp_path / "inference_profiles.json"
    profiles_path.write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "base_url_env": "VERIDOC_STANDARD_OPENAI_BASE_URL",
                        "model_env": "VERIDOC_STANDARD_MODEL",
                        "optional_env": ["VERIDOC_STANDARD_TIMEOUT_SECONDS"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(poc_web, "INFERENCE_PROFILES_PATH", profiles_path)
    monkeypatch.setenv("VERIDOC_STANDARD_OPENAI_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("VERIDOC_STANDARD_MODEL", "local-json-model")
    monkeypatch.setenv("VERIDOC_STANDARD_TIMEOUT_SECONDS", "inf")

    adapter, reason = poc_web._configured_llm_conversion_plan_adapter()

    assert adapter is None
    assert reason == "invalid_configuration"


@pytest.mark.parametrize(
    ("conversion_mode", "artifact_format", "artifact_content_type"),
    (
        (
            "pdf_to_word",
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            "pdf_to_excel",
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    ),
)
def test_convert_uploaded_document_manifest_names_mode_artifacts_safely(
    conversion_mode: str,
    artifact_format: str,
    artifact_content_type: str,
) -> None:
    parser_output = {
        "source_type": "pdf",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [{"text": "PDF text", "confidence": 0.95}],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="nested/CON\x00 report:name.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode=conversion_mode,
    )

    assert result["download"]["filename"] == "CON report-name.veridoc-result.json"
    assert result["download"]["content_type"] == "application/json; charset=utf-8"
    primary_artifact, debug_artifact = result["artifacts"]
    primary_content = primary_artifact.pop("content")
    assert isinstance(primary_content, bytes)
    assert primary_artifact == {
        "id": f"primary-{artifact_format}",
        "kind": "primary",
        "format": artifact_format,
        "filename": f"CON report-name.veridoc-{conversion_mode.replace('_', '-')}.{artifact_format}",
        "content_type": artifact_content_type,
        "size_bytes": len(primary_content),
        "sha256": hashlib.sha256(primary_content).hexdigest(),
        "metadata": {
            "role": "primary",
            "conversion_mode": conversion_mode,
            "source_filename": "CON report:name.json",
            "source_type": "pdf",
            "source_sha256": result["hashes"]["source_sha256"],
            "output_sha256": primary_artifact["sha256"],
            "validation": result["audit"]["validation"],
            "warnings": {"count": len(result["warnings"])},
            "review_items": {"count": len(result["review_items"])},
            "download": {
                "available": True,
                "field": "artifacts[0].content_base64",
            },
        },
    }
    with ZipFile(BytesIO(primary_content)) as archive:
        assert (
            "word/document.xml"
            if artifact_format == "docx"
            else "xl/worksheets/sheet1.xml"
        ) in archive.namelist()
    assert debug_artifact == {
        "id": "debug-json",
        "kind": "debug",
        "format": "json",
        "filename": "CON report-name.veridoc-result.json",
        "content_type": "application/json; charset=utf-8",
        "size_bytes": len(result["download"]["content"]),
        "sha256": result["hashes"]["output_sha256"],
        "metadata": {
            "role": "debug",
            "conversion_mode": conversion_mode,
            "source_filename": "CON report:name.json",
            "source_type": "pdf",
            "source_sha256": result["hashes"]["source_sha256"],
            "output_sha256": debug_artifact["sha256"],
            "validation": result["audit"]["validation"],
            "warnings": {"count": len(result["warnings"])},
            "review_items": {"count": len(result["review_items"])},
            "download": {
                "available": True,
                "field": "download",
            },
        },
    }


def test_excel_to_word_primary_docx_renders_sheet_values_for_review() -> None:
    result = convert_uploaded_document(
        filename="wbs.xlsx",
        content=_sample_xlsx_bytes(),
        conversion_mode="excel_to_word",
    )

    assert result["status"] == "requires_review"
    assert result["warnings"] == [
        "conversion mode excel_to_word selected",
        "blocks[0].bbox missing; block marked requires_review",
    ]

    primary_artifact, debug_artifact = result["artifacts"]
    primary_content = primary_artifact.pop("content")
    assert isinstance(primary_content, bytes)
    assert primary_artifact == {
        "id": "primary-docx",
        "kind": "primary",
        "format": "docx",
        "filename": "wbs.veridoc-excel-to-word.docx",
        "content_type": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        "size_bytes": len(primary_content),
        "sha256": hashlib.sha256(primary_content).hexdigest(),
        "metadata": {
            "role": "primary",
            "conversion_mode": "excel_to_word",
            "source_filename": "wbs.xlsx",
            "source_type": "xlsx",
            "source_sha256": result["hashes"]["source_sha256"],
            "output_sha256": primary_artifact["sha256"],
            "validation": result["audit"]["validation"],
            "warnings": {"count": len(result["warnings"])},
            "review_items": {"count": len(result["review_items"])},
            "download": {
                "available": True,
                "field": "artifacts[0].content_base64",
            },
        },
    }
    assert debug_artifact["id"] == "debug-json"
    with ZipFile(BytesIO(primary_content)) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
    assert "wbs.xlsx" in document_xml
    assert "Sheet: WBS" in document_xml
    assert "Template review" in document_xml
    assert "2026-07-03" in document_xml
    assert "12.5" in document_xml
    assert "00123" in document_xml

    downloaded = json.loads(result["download"]["content"].decode("utf-8"))
    assert downloaded["document_ir"]["document"]["source_type"] == "xlsx"
    table_blocks = [
        block for block in downloaded["document_ir"]["blocks"] if block["type"] == "table"
    ]
    assert table_blocks[0]["rows"] == [
        ["Sheet: WBS"],
        ["ID", "Task", "Due", "Cost"],
        ["00123", "Template review", "2026-07-03", "12.5"],
    ]


def test_excel_to_word_representative_workbook_surfaces_reviewable_tables(
    tmp_path: Path,
) -> None:
    manifest = json.loads(FIXTURE_MANIFEST_PATH.read_text(encoding="utf-8"))
    fixtures = [
        fixture
        for fixture in manifest["fixtures"]
        if fixture["source_type"] == "excel"
        and fixture.get("excel_to_word_representative") is True
    ]

    assert {fixture["id"] for fixture in fixtures} == {"excel-to-word-representative"}

    for fixture in fixtures:
        fixture_relative_path = Path(fixture["path"])
        assert not fixture_relative_path.is_absolute(), fixture["id"]
        assert _repo_tracks_path(fixture_relative_path), fixture["id"]
        fixture_path = REPO_ROOT / fixture_relative_path
        assert fixture_path.is_file(), fixture["id"]

        result = convert_uploaded_document(
            filename=fixture_path.name,
            content=fixture_path.read_bytes(),
            conversion_mode="excel_to_word",
        )

        assert result["status"] == "requires_review"
        primary_artifact = result["artifacts"][0]
        assert primary_artifact["id"] == "primary-docx", fixture["id"]
        assert primary_artifact["filename"] == (
            "excel-to-word-representative.veridoc-excel-to-word.docx"
        )
        assert primary_artifact["content_type"] == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert primary_artifact["metadata"]["download"] == {
            "available": True,
            "field": "artifacts[0].content_base64",
        }

        primary_path = tmp_path / primary_artifact["filename"]
        primary_path.write_bytes(primary_artifact["content"])
        docx = extract_docx_structure(primary_path)
        table_rows = [block.rows for block in docx.blocks if block.kind == "table"]
        expectations = fixture["excel_to_word_expectations"]
        assert table_rows == expectations["table_rows"], fixture["id"]

        downloaded = json.loads(result["download"]["content"].decode("utf-8"))
        table_blocks = [
            block
            for block in downloaded["document_ir"]["blocks"]
            if block["type"] == "table"
        ]
        assert [block["rows"] for block in table_blocks] == table_rows, fixture["id"]
        for warning in expectations["warnings"]:
            assert warning in result["warnings"], fixture["id"]


def test_pdf_to_word_representative_text_pdf_surfaces_editable_docx(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pymupdf", reason="PyMuPDF eval dependency is not installed")

    manifest = json.loads(FIXTURE_MANIFEST_PATH.read_text(encoding="utf-8"))
    fixtures = [
        fixture
        for fixture in manifest["fixtures"]
        if fixture["source_type"] == "text_pdf"
        and fixture.get("pdf_to_word_representative") is True
    ]

    assert {fixture["id"] for fixture in fixtures} == {"pdf-to-word-representative"}

    for fixture in fixtures:
        fixture_relative_path = Path(fixture["path"])
        assert not fixture_relative_path.is_absolute(), fixture["id"]
        assert _repo_tracks_path(fixture_relative_path), fixture["id"]
        fixture_path = REPO_ROOT / fixture_relative_path
        assert fixture_path.is_file(), fixture["id"]

        result = convert_uploaded_document(
            filename=fixture_path.name,
            content=fixture_path.read_bytes(),
            conversion_mode="pdf_to_word",
        )

        assert result["status"] == "requires_review"
        primary_artifact = result["artifacts"][0]
        assert primary_artifact["id"] == "primary-docx", fixture["id"]
        assert primary_artifact["filename"] == (
            "pdf-to-word-representative.veridoc-pdf-to-word.docx"
        )
        assert primary_artifact["content_type"] == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        assert primary_artifact["metadata"]["download"] == {
            "available": True,
            "field": "artifacts[0].content_base64",
        }

        primary_path = tmp_path / primary_artifact["filename"]
        primary_path.write_bytes(primary_artifact["content"])
        docx = extract_docx_structure(primary_path)
        expectations = fixture["pdf_to_word_expectations"]
        headings = [block.text for block in docx.blocks if block.kind == "heading"]
        paragraphs = [block.text for block in docx.blocks if block.kind == "paragraph"]
        table_rows = [block.rows for block in docx.blocks if block.kind == "table"]
        assert expectations["heading_texts"] == headings, fixture["id"]
        assert expectations["paragraph_texts"] == paragraphs[: len(expectations["paragraph_texts"])]
        assert table_rows == expectations["table_rows"], fixture["id"]

        downloaded = json.loads(result["download"]["content"].decode("utf-8"))
        table_blocks = [
            block
            for block in downloaded["document_ir"]["blocks"]
            if block["type"] == "table"
        ]
        assert [block["rows"] for block in table_blocks] == table_rows, fixture["id"]
        for warning in expectations["warnings"]:
            assert warning in result["warnings"], fixture["id"]


def test_excel_to_word_json_prefers_page_table_rows_over_matching_sheet_rows() -> None:
    parser_output = {
        "source_type": "xlsx",
        "extractor": "xlsx",
        "sheets": [
            {
                "name": "WBS",
                "cells": [
                    {"ref": "A1", "value": "ID"},
                    {"ref": "C1", "value": "Task"},
                    {"ref": "A2", "value": "00123"},
                    {"ref": "C2", "value": "Template review"},
                ],
            }
        ],
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "extractor": "xlsx",
                        "text": (
                            "Sheet: WBS\n"
                            "A1: ID\n"
                            "C1: Task\n"
                            "A2: 00123\n"
                            "C2: Template review"
                        ),
                        "rows": [["ID", "Task"], ["00123", "Template review"]],
                    }
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="page-and-sheet-rows.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="excel_to_word",
    )

    downloaded = json.loads(result["download"]["content"].decode("utf-8"))
    table_blocks = [
        block for block in downloaded["document_ir"]["blocks"] if block["type"] == "table"
    ]
    assert table_blocks[0]["rows"] == [["ID", "Task"], ["00123", "Template review"]]


def test_convert_uploaded_document_blocks_primary_render_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_docx_renderer(document_ir: dict, output_path: Path) -> None:
        raise ValueError("docx renderer fixture failure")

    monkeypatch.setattr(
        poc_web,
        "render_editable_docx_from_pdf_ir",
        failing_docx_renderer,
    )
    parser_output = {
        "source_type": "pdf",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "text": "PDF text",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 16, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="pdf_to_word",
    )

    assert result["status"] == "blocked"
    assert result["warnings"] == [
        "conversion mode pdf_to_word selected",
        "pdf_to_word reconstruction preserves editable text structure for review; exact PDF layout, fonts, coordinates, columns, footnotes, and OCR fidelity are not guaranteed",
        "primary artifact generation failed: docx renderer fixture failure",
    ]
    assert [artifact["id"] for artifact in result["artifacts"]] == ["debug-json"]


def test_convert_uploaded_document_skips_primary_render_for_invalid_ir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_docx_renderer(document_ir: dict, output_path: Path) -> None:
        raise AssertionError("renderer should not run for invalid IR")

    monkeypatch.setattr(
        poc_web,
        "render_editable_docx_from_pdf_ir",
        unexpected_docx_renderer,
    )
    parser_output = {
        "schema_version": "document-ir/v0",
        "document": {
            "id": "sample-document-001",
            "title": "Invalid PDF IR",
            "source_type": "pdf",
        },
        "pages": [{"page_number": 1, "width": 100, "height": 100, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "paragraph",
                "text": "Outside page",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 90, "y": 10, "width": 20, "height": 12, "unit": "pt"},
                    "confidence": 0.95,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="pdf_to_word",
    )

    assert result["status"] == "blocked"
    assert result["validation"]["errors"] == ["blocks[0].bbox extends past page 1"]
    assert result["warnings"] == [
        "conversion mode pdf_to_word selected",
        "pdf_to_word reconstruction preserves editable text structure for review; exact PDF layout, fonts, coordinates, columns, footnotes, and OCR fidelity are not guaranteed",
        "primary artifact generation skipped: document IR validation failed",
    ]
    assert [artifact["id"] for artifact in result["artifacts"]] == ["debug-json"]
    downloaded = json.loads(result["download"]["content"].decode("utf-8"))
    assert downloaded["warnings"] == result["warnings"]


def test_pdf_to_word_primary_reconstructs_editable_docx_structures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_parse_text_pdf_to_document_ir(pdf_path: Path, *, document_id: str | None = None) -> dict:
        return {
            "schema_version": "document-ir/v0",
            "document": {
                "id": document_id or "sample",
                "title": pdf_path.name,
                "source_type": "pdf",
            },
            "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "pt"}],
            "blocks": [
                {
                    "id": "block-001",
                    "type": "heading",
                    "text": "Manufacturing summary",
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 12, "y": 18, "width": 180, "height": 16, "unit": "pt"},
                        "extractor": {"name": "test-text", "version": "test"},
                        "confidence": 0.97,
                    },
                },
                {
                    "id": "block-002",
                    "type": "paragraph",
                    "text": "Batch was inspected before release.",
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 12, "y": 44, "width": 220, "height": 16, "unit": "pt"},
                        "extractor": {"name": "test-text", "version": "test"},
                        "confidence": 0.95,
                    },
                },
                {
                    "id": "block-003",
                    "type": "table",
                    "text": "Lot\tResult\nA-001\tPass",
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 12, "y": 78, "width": 160, "height": 36, "unit": "pt"},
                        "extractor": {"name": "test-table", "version": "test"},
                        "confidence": 0.82,
                        "requires_review": True,
                    },
                },
            ],
        }

    monkeypatch.setattr(poc_web, "parse_text_pdf_to_document_ir", fake_parse_text_pdf_to_document_ir)

    result = convert_uploaded_document(
        filename="sample.pdf",
        content=b"%PDF-1.4\n%%EOF\n",
        conversion_mode="pdf_to_word",
    )

    assert result["status"] == "requires_review"
    assert result["audit"]["conversion_mode"] == "pdf_to_word"
    assert result["warnings"] == [
        "conversion mode pdf_to_word selected",
        "pdf_to_word reconstruction preserves editable text structure for review; exact PDF layout, fonts, coordinates, columns, footnotes, and OCR fidelity are not guaranteed",
        "blocks[2].parser marked block requires_review",
    ]

    primary_artifact = result["artifacts"][0]
    assert primary_artifact["format"] == "docx"
    assert primary_artifact["content_type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert primary_artifact["filename"] == "sample.veridoc-pdf-to-word.docx"
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])

    docx = extract_docx_structure(primary_path)
    assert [(block.kind, block.text, block.rows) for block in docx.blocks] == [
        ("heading", "sample.pdf", None),
        ("heading", "Manufacturing summary", None),
        ("paragraph", "Batch was inspected before release.", None),
        ("table", "Lot\tResult\nA-001\tPass", [["Lot", "Result"], ["A-001", "Pass"]]),
    ]


@pytest.mark.parametrize(
    ("conversion_mode", "source_type"),
    (
        ("pdf_to_excel", "pdf"),
        ("word_to_excel", "docx"),
    ),
)
def test_convert_uploaded_document_xlsx_primary_renders_table_blocks_as_grid(
    tmp_path: Path,
    conversion_mode: str,
    source_type: str,
) -> None:
    parser_output = {
        "source_type": source_type,
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Lot\tAssay\nA\t12.5",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode=conversion_mode,
    )

    primary_artifact = result["artifacts"][0]
    assert primary_artifact["format"] == "xlsx"
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Lot", "inline_string")
    assert cells["B4"] == ("Assay", "inline_string")
    assert cells["A5"] == ("A", "inline_string")
    assert cells["B5"] == ("12.5", "number")


def test_word_to_excel_docx_upload_preserves_structured_table_cells(tmp_path: Path) -> None:
    docx_path = tmp_path / "batch-report.docx"
    _write_docx(
        docx_path,
        """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Batch Report</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Lot</w:t><w:tab/><w:t>ID</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Assay %</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Comment</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>0007</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>12.5</w:t></w:r></w:p></w:tc>
        <w:tc><w:p/></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
""",
    )

    result = convert_uploaded_document(
        filename="batch-report.docx",
        content=docx_path.read_bytes(),
        conversion_mode="word_to_excel",
    )

    assert result["status"] == "requires_review"
    assert result["validation"]["errors"] == []
    primary_artifact = result["artifacts"][0]
    assert primary_artifact["format"] == "xlsx"
    assert primary_artifact["filename"] == "batch-report.veridoc-word-to-excel.xlsx"
    assert primary_artifact["content_type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])

    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A5"] == ("Lot\tID", "inline_string")
    assert cells["B5"] == ("Assay %", "inline_string")
    assert cells["C5"] == ("Comment", "inline_string")
    assert cells["A6"] == ("0007", "inline_string")
    assert cells["B6"] == ("12.5", "number")
    assert cells["C6"] == ("", "inline_string")


def test_word_to_excel_representative_docx_fixtures_render_xlsx_artifacts(
    tmp_path: Path,
) -> None:
    manifest = json.loads(FIXTURE_MANIFEST_PATH.read_text(encoding="utf-8"))
    fixtures = [
        fixture
        for fixture in manifest["fixtures"]
        if fixture["source_type"] == "word"
        and fixture.get("word_to_excel_representative") is True
    ]

    assert {fixture["id"] for fixture in fixtures} == {
        "word-to-excel-meeting-minutes",
        "word-to-excel-report",
        "word-to-excel-application",
    }

    for fixture in fixtures:
        fixture_path = REPO_ROOT / fixture["path"]
        result = convert_uploaded_document(
            filename=fixture_path.name,
            content=fixture_path.read_bytes(),
            conversion_mode="word_to_excel",
        )

        primary_artifact = result["artifacts"][0]
        assert primary_artifact["format"] == "xlsx", fixture["id"]
        assert primary_artifact["filename"].endswith(".veridoc-word-to-excel.xlsx")
        assert primary_artifact["content_type"] == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert primary_artifact["metadata"]["download"] == {
            "available": True,
            "field": "artifacts[0].content_base64",
        }

        primary_path = tmp_path / primary_artifact["filename"]
        primary_path.write_bytes(primary_artifact["content"])
        xlsx = extract_xlsx_structure(primary_path)
        cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}

        expectations = fixture["word_to_excel_expectations"]
        for ref, expected in expectations["cells"].items():
            assert cells[ref] == (expected["value"], expected["value_type"]), fixture["id"]

        assert xlsx.sheets[0].dimension == expectations["dimension"], fixture["id"]
        assert len({_cell_row_index(cell.ref) for cell in xlsx.sheets[0].cells}) == (
            expectations["row_count"]
        ), fixture["id"]
        assert len({_cell_column_label(cell.ref) for cell in xlsx.sheets[0].cells}) >= (
            expectations["min_column_count"]
        ), fixture["id"]
        for warning in expectations.get("warnings", []):
            assert warning in result["warnings"], fixture["id"]


def test_pdf_to_excel_representative_table_fixture_renders_xlsx_artifact(
    tmp_path: Path,
) -> None:
    require_pdf_eval_deps = os.environ.get("VERIDOC_REQUIRE_PDF_EVAL_DEPS") == "1"

    manifest = json.loads(FIXTURE_MANIFEST_PATH.read_text(encoding="utf-8"))
    fixtures = [
        fixture
        for fixture in manifest["fixtures"]
        if fixture["source_type"] == "text_pdf"
        and fixture.get("pdf_to_excel_representative") is True
    ]

    assert {fixture["id"] for fixture in fixtures} == {"pdf-to-excel-table-report"}

    for fixture in fixtures:
        fixture_path = REPO_ROOT / fixture["path"]
        report_path = REPO_ROOT / fixture["report_path"]
        report = json.loads(report_path.read_text(encoding="utf-8"))
        source_relative_path = Path(report["source_path"])
        assert not source_relative_path.is_absolute(), fixture["id"]
        assert _repo_tracks_path(source_relative_path), fixture["id"]
        source_path = REPO_ROOT / source_relative_path
        assert source_path.is_file(), fixture["id"]
        assert fixture_path == source_path, fixture["id"]
        selected_candidate = next(
            candidate
            for candidate in report["candidates"]
            if f"{candidate['extractor']}:{candidate['flavor']}" == report["selected_candidate"]
        )
        selected_table = selected_candidate["tables"][0]
        selected_cell_bboxes = selected_table["cell_bboxes"]
        assert all(
            cell["origin"] == "bottom-left"
            for row in selected_cell_bboxes
            for cell in row
        ), fixture["id"]
        _assert_pdf_fixture_has_ruled_table(source_path, selected_cell_bboxes)
        _assert_pdf_fixture_text_is_inside_bboxes(
            source_path,
            selected_table["rows"],
            selected_cell_bboxes,
        )

        result = convert_uploaded_document(
            filename=report_path.name,
            content=report_path.read_bytes(),
            conversion_mode="pdf_to_excel",
        )

        expectations = fixture["pdf_to_excel_expectations"]
        primary_artifact = result["artifacts"][0]
        assert primary_artifact["format"] == "xlsx", fixture["id"]
        assert primary_artifact["filename"].endswith(".veridoc-pdf-to-excel.xlsx")
        assert primary_artifact["content_type"] == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert primary_artifact["metadata"]["download"] == {
            "available": True,
            "field": "artifacts[0].content_base64",
        }

        primary_path = tmp_path / primary_artifact["filename"]
        primary_path.write_bytes(primary_artifact["content"])
        xlsx = extract_xlsx_structure(primary_path)
        cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}

        assert xlsx.sheets[0].dimension == "A1:D6", fixture["id"]
        report_table_refs = (
            "A4",
            "B4",
            "C4",
            "D4",
            "A5",
            "B5",
            "C5",
            "D5",
            "A6",
            "B6",
            "C6",
            "D6",
        )
        assert len(report_table_refs) == len(expectations["cells"]), fixture["id"]
        for ref, expected_ref in zip(report_table_refs, expectations["cells"]):
            expected = expectations["cells"][expected_ref]
            assert cells[ref] == (expected["value"], expected["value_type"]), fixture["id"]

        assert "conversion mode pdf_to_excel selected" in result["warnings"], fixture["id"]
        assert (
            "PDF table extraction candidate unavailable: pdfplumber:table; "
            "xlsx artifact requires review"
        ) in result["warnings"], fixture["id"]

        table_refs = list(expectations["cells"])
        assert len({_cell_row_index(ref) for ref in table_refs}) == (
            expectations["table_row_count"]
        ), fixture["id"]
        assert len({_cell_column_label(ref) for ref in table_refs}) == (
            expectations["table_column_count"]
        ), fixture["id"]

        comments_by_ref = _xlsx_comments_by_ref(primary_path)
        expected_comment_ref = "A4"
        assert expected_comment_ref in comments_by_ref, fixture["id"]
        source_comment = comments_by_ref[expected_comment_ref]
        for expected_text in expectations["source_comment"]["contains"]:
            assert expected_text in source_comment, fixture["id"]

        live_report = poc_web.compare_pdf_table_extractors(fixture_path).to_dict()
        if not _pdf_table_report_can_exercise_strict_live_fixture(
            live_report,
            expected_selected_candidate=str(report["selected_candidate"]),
        ):
            if require_pdf_eval_deps:
                pytest.fail(
                    "PDF eval dependencies were required, but live table extractors "
                    "did not produce a complete strict representative report."
                )
            continue
        assert live_report["selected_candidate"] == report["selected_candidate"], fixture["id"]
        live_selected_candidate = next(
            candidate
            for candidate in live_report["candidates"]
            if f"{candidate['extractor']}:{candidate['flavor']}"
            == live_report["selected_candidate"]
        )
        assert live_selected_candidate["status"] == "ok", fixture["id"]
        live_selected_table = live_selected_candidate["tables"][0]
        assert live_selected_table["rows"] == selected_table["rows"], fixture["id"]
        assert all(
            cell["origin"] == "bottom-left"
            for row in live_selected_table["cell_bboxes"]
            for cell in row
        ), fixture["id"]
        _assert_pdf_fixture_text_is_inside_bboxes(
            source_path,
            live_selected_table["rows"],
            live_selected_table["cell_bboxes"],
        )

        live_result = convert_uploaded_document(
            filename=fixture_path.name,
            content=fixture_path.read_bytes(),
            conversion_mode="pdf_to_excel",
        )

        live_primary_artifact = live_result["artifacts"][0]
        assert live_primary_artifact["format"] == "xlsx", fixture["id"]
        assert live_primary_artifact["filename"].endswith(".veridoc-pdf-to-excel.xlsx")
        assert live_primary_artifact["content_type"] == (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        assert live_primary_artifact["metadata"]["download"] == {
            "available": True,
            "field": "artifacts[0].content_base64",
        }

        live_primary_path = tmp_path / live_primary_artifact["filename"]
        live_primary_path.write_bytes(live_primary_artifact["content"])
        live_xlsx = extract_xlsx_structure(live_primary_path)
        live_cells = {
            cell.ref: (cell.value, cell.value_type) for cell in live_xlsx.sheets[0].cells
        }

        assert live_xlsx.sheets[0].dimension == expectations["dimension"], fixture["id"]
        for ref, expected in expectations["cells"].items():
            assert live_cells[ref] == (expected["value"], expected["value_type"]), fixture["id"]

        assert live_result["warnings"] == expectations["warnings"], fixture["id"]

        live_comments_by_ref = _xlsx_comments_by_ref(live_primary_path)
        live_expected_comment_ref = expectations["source_comment"]["cell"]
        assert live_expected_comment_ref in live_comments_by_ref, fixture["id"]
        live_source_comment = live_comments_by_ref[live_expected_comment_ref]
        for expected_text in expectations["source_comment"]["contains"]:
            assert expected_text in live_source_comment, fixture["id"]


def test_pdf_table_live_fixture_guard_rejects_partial_extractor_deps() -> None:
    report = {
        "selected_candidate": "camelot:lattice",
        "candidates": [
            {
                "extractor": "camelot",
                "flavor": "lattice",
                "status": "ok",
                "tables": [{"rows": [["Lot", "Assay"]]}],
            },
            {
                "extractor": "pdfplumber",
                "flavor": "table",
                "status": "failed",
                "tables": [],
            },
        ],
    }

    assert not _pdf_table_report_can_exercise_strict_live_fixture(
        report,
        expected_selected_candidate="camelot:lattice",
    )


def test_pdf_table_live_fixture_guard_accepts_complete_expected_candidate() -> None:
    report = {
        "selected_candidate": "camelot:lattice",
        "candidates": [
            {
                "extractor": "camelot",
                "flavor": "lattice",
                "status": "ok",
                "tables": [{"rows": [["Lot", "Assay"]]}],
            },
            {
                "extractor": "pdfplumber",
                "flavor": "table",
                "status": "ok",
                "tables": [{"rows": [["Lot", "Assay"]]}],
            },
        ],
    }

    assert _pdf_table_report_can_exercise_strict_live_fixture(
        report,
        expected_selected_candidate="camelot:lattice",
    )


def _pdf_table_report_can_exercise_strict_live_fixture(
    report: dict[str, object],
    *,
    expected_selected_candidate: str,
) -> bool:
    if report.get("selected_candidate") != expected_selected_candidate:
        return False
    candidates = report.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return False
    selected_candidate_has_table = False
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("status") != "ok":
            return False
        candidate_name = f"{candidate.get('extractor')}:{candidate.get('flavor')}"
        if candidate_name == expected_selected_candidate:
            tables = candidate.get("tables")
            selected_candidate_has_table = isinstance(tables, list) and bool(tables)
    return selected_candidate_has_table


def _xlsx_comments_by_ref(xlsx_path: Path) -> dict[str, str]:
    namespace = {"xlsx": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(xlsx_path) as archive:
        comments_xml = archive.read("xl/comments1.xml")
    root = ElementTree.fromstring(comments_xml)
    comments_by_ref: dict[str, str] = {}
    for comment in root.findall(".//xlsx:comment", namespace):
        ref = comment.attrib.get("ref")
        if ref is None:
            continue
        comments_by_ref[ref] = "".join(
            text_node.text or ""
            for text_node in comment.findall(".//xlsx:t", namespace)
        )
    return comments_by_ref


def _repo_tracks_path(path: Path) -> bool:
    if not (REPO_ROOT / ".git").exists():
        return True
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", path.as_posix()],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _pdf_fixture_content_stream_text(pdf_path: Path) -> str:
    raw_pdf = pdf_path.read_bytes()
    streams: list[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", raw_pdf, re.DOTALL):
        stream_data = match.group(1)
        try:
            stream_data = zlib.decompress(stream_data)
        except zlib.error:
            pass
        streams.append(stream_data.decode("latin-1", errors="replace"))
    assert streams, pdf_path
    return "\n".join(streams)


def _assert_pdf_fixture_has_ruled_table(
    pdf_path: Path,
    cell_bboxes: list[list[dict[str, object]]],
) -> None:
    content_stream = _pdf_fixture_content_stream_text(pdf_path)
    line_segments = {
        tuple(float(value) for value in match)
        for match in re.findall(
            r"([0-9.]+) ([0-9.]+) m ([0-9.]+) ([0-9.]+) l S",
            content_stream,
        )
    }
    left_edges = {
        float(cell["x"])
        for row in cell_bboxes
        for cell in row
    }
    right_edge = max(
        float(cell["x"]) + float(cell["width"])
        for row in cell_bboxes
        for cell in row
    )
    bottom_edges = {
        float(cell["y"])
        for row in cell_bboxes
        for cell in row
    }
    top_edge = max(
        float(cell["y"]) + float(cell["height"])
        for row in cell_bboxes
        for cell in row
    )
    min_x = min(left_edges)
    min_y = min(bottom_edges)
    for x in sorted({*left_edges, right_edge}):
        assert (x, min_y, x, top_edge) in line_segments
    for y in sorted({*bottom_edges, top_edge}):
        assert (min_x, y, right_edge, y) in line_segments


def _assert_pdf_fixture_text_is_inside_bboxes(
    pdf_path: Path,
    rows: list[list[str]],
    cell_bboxes: list[list[dict[str, object]]],
) -> None:
    content_stream = _pdf_fixture_content_stream_text(pdf_path)
    text_positions = {
        text: (float(x), float(y))
        for x, y, text in re.findall(
            r"1 0 0 1 ([0-9.]+) ([0-9.]+) Tm \(([^)]*)\) Tj",
            content_stream,
        )
    }
    assert text_positions, pdf_path
    for row_index, row in enumerate(rows):
        for column_index, text in enumerate(row):
            if not text:
                continue
            assert text in text_positions
            x, y = text_positions[text]
            bbox = cell_bboxes[row_index][column_index]
            left = float(bbox["x"])
            bottom = float(bbox["y"])
            right = left + float(bbox["width"])
            top = bottom + float(bbox["height"])
            assert left <= x <= right, text
            assert bottom <= y <= top, text


def _cell_row_index(cell_ref: str) -> int:
    match = re.fullmatch(r"[A-Z]+([0-9]+)", cell_ref)
    assert match is not None
    return int(match.group(1))


def _cell_column_label(cell_ref: str) -> str:
    match = re.fullmatch(r"([A-Z]+)[0-9]+", cell_ref)
    assert match is not None
    return match.group(1)


def test_word_to_excel_phase0_json_preserves_v0_table_block_rows(tmp_path: Path) -> None:
    parser_output = {
        "schema_version": "document-ir/v0",
        "extractor": "docx-table-parser",
        "document": {"id": "phase0-docx", "title": "phase0-docx.docx", "source_type": "docx"},
        "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "table",
                "text": "Lot\tID\tAssay\n0007\t12.5",
                "rows": [["Lot\tID", "Assay"], ["0007", "12.5"]],
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 10, "y": 20, "width": 120, "height": 32},
                    "extractor": {"name": "docx-table-parser", "version": "test"},
                    "confidence": 0.95,
                    "requires_review": False,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-docx.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="word_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Lot\tID", "inline_string")
    assert cells["B4"] == ("Assay", "inline_string")
    assert cells["A5"] == ("0007", "inline_string")
    assert cells["B5"] == ("12.5", "number")


def test_word_to_excel_phase0_json_preserves_v0_table_block_warnings(
    tmp_path: Path,
) -> None:
    warning = "DOCX table contains merged cells; xlsx artifact requires review"
    parser_output = {
        "schema_version": "document-ir/v0",
        "extractor": "docx-table-parser",
        "document": {"id": "phase0-docx", "title": "phase0-docx.docx", "source_type": "docx"},
        "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "table",
                "text": "Merged Header\nLot\t0007",
                "rows": [["Merged Header", ""], ["Lot", "0007"]],
                "warnings": [warning],
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 10, "y": 20, "width": 120, "height": 32},
                    "extractor": {"name": "docx-table-parser", "version": "test"},
                    "confidence": 0.95,
                    "requires_review": False,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-docx-warnings.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="word_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Merged Header", "inline_string")
    assert cells["A5"] == ("Lot", "inline_string")
    assert cells["B5"] == ("0007", "inline_string")
    assert result["status"] == "requires_review"
    assert warning in result["warnings"]
    assert result["document_ir"]["blocks"][0]["review"]["warnings"] == [warning]


def test_word_to_excel_phase0_json_preserves_top_level_table_warnings_with_existing_fragments(
    tmp_path: Path,
) -> None:
    warning = "DOCX table contains merged cells; xlsx artifact requires review"
    parser_output = {
        "schema_version": "document-ir/v0",
        "extractor": "docx-table-parser",
        "document": {"id": "phase0-docx", "title": "phase0-docx.docx", "source_type": "docx"},
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Merged\tHeader\tNotes\nLot\t0007\t",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
        "blocks": [
            {
                "id": "block-001",
                "type": "table",
                "text": "Merged\tHeader\tNotes\nLot\t0007\t",
                "rows": [["Merged\tHeader", "Notes"], ["Lot", "0007", ""]],
                "warnings": [warning],
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                    "extractor": {"name": "docx-table-parser", "version": "test"},
                    "confidence": 0.95,
                    "requires_review": False,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-docx-existing-fragment-warning.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="word_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Merged\tHeader", "inline_string")
    assert cells["B4"] == ("Notes", "inline_string")
    assert cells["A5"] == ("Lot", "inline_string")
    assert cells["B5"] == ("0007", "inline_string")
    assert result["status"] == "requires_review"
    assert warning in result["warnings"]
    assert result["document_ir"]["blocks"][0]["review"]["warnings"] == [warning]


def test_word_to_excel_json_table_rows_match_root_extractor_fallback(tmp_path: Path) -> None:
    parser_output = {
        "source_type": "docx",
        "extractor": "docx-root-parser",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Sample\tValue\tNotes\nA\t0012\t",
                        "rows": [["Sample\tValue", "Notes"], ["A", "0012"]],
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="root-extractor.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="word_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Sample\tValue", "inline_string")
    assert cells["B4"] == ("Notes", "inline_string")
    assert cells["A5"] == ("A", "inline_string")
    assert cells["B5"] == ("0012", "inline_string")


def test_word_to_excel_json_table_rows_match_root_extractor_object_fallback(
    tmp_path: Path,
) -> None:
    parser_output = {
        "source_type": "docx",
        "extractor": {"name": "docx-root-parser", "version": "2"},
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Sample\tValue\tNotes\nA\t0012\t",
                        "rows": [["Sample\tValue", "Notes"], ["A", "0012"]],
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="root-extractor-object.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="word_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Sample\tValue", "inline_string")
    assert cells["B4"] == ("Notes", "inline_string")
    assert cells["A5"] == ("A", "inline_string")
    assert cells["B5"] == ("0012", "inline_string")


def test_word_to_excel_json_top_level_rows_match_root_extractor_object_fallback(
    tmp_path: Path,
) -> None:
    parser_output = {
        "source_type": "docx",
        "extractor": {"name": "docx-root-parser", "version": "2"},
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Sample\tValue\tNotes\nA\t0012\t",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
        "blocks": [
            {
                "type": "table",
                "text": "Sample\tValue\tNotes\nA\t0012\t",
                "rows": [["Sample\tValue", "Notes"], ["A", "0012"]],
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                    "extractor": {"name": "docx-root-parser", "version": "legacy"},
                    "confidence": 0.95,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="root-extractor-object-top-level-rows.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="word_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Sample\tValue", "inline_string")
    assert cells["B4"] == ("Notes", "inline_string")
    assert cells["A5"] == ("A", "inline_string")
    assert cells["B5"] == ("0012", "inline_string")


def test_word_to_excel_json_top_level_rows_match_fragment_without_extractor(
    tmp_path: Path,
) -> None:
    parser_output = {
        "source_type": "docx",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Sample\tValue\tNotes\nA\t0012\t",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
        "blocks": [
            {
                "type": "table",
                "text": "Sample\tValue\tNotes\nA\t0012\t",
                "rows": [["Sample\tValue", "Notes"], ["A", "0012", ""]],
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                    "extractor": {"name": "docx-table-parser", "version": "legacy"},
                    "confidence": 0.95,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="metadata-extractor-top-level-rows.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="word_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Sample\tValue", "inline_string")
    assert cells["B4"] == ("Notes", "inline_string")
    assert cells["A5"] == ("A", "inline_string")
    assert cells["B5"] == ("0012", "inline_string")


def test_word_to_excel_json_table_rows_ignore_page_extractor_fallback(
    tmp_path: Path,
) -> None:
    parser_output = {
        "source_type": "docx",
        "pages": [
            {
                "page_number": 1,
                "extractor": "docx-page-parser",
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Sample\tValue\tNotes\nA\t0012\t",
                        "rows": [["Sample\tValue", "Notes"], ["A", "0012"]],
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="page-extractor.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="word_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Sample\tValue", "inline_string")
    assert cells["B4"] == ("Notes", "inline_string")
    assert cells["A5"] == ("A", "inline_string")
    assert cells["B5"] == ("0012", "inline_string")


def test_word_to_excel_json_page_fragment_rows_override_stale_top_level_rows(
    tmp_path: Path,
) -> None:
    parser_output = {
        "source_type": "docx",
        "extractor": "docx-root-parser",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Sample\tValue\tNotes\nA\t0012\t",
                        "rows": [["Sample\tValue", "Notes"], ["A", "0012"]],
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
        "blocks": [
            {
                "type": "table",
                "text": "Sample\tValue\tNotes\nA\t0012\t",
                "rows": [["stale", "grid"], ["wrong", "cells"]],
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                    "extractor": {"name": "docx-root-parser", "version": "legacy"},
                    "confidence": 0.95,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="stale-top-level.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="word_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Sample\tValue", "inline_string")
    assert cells["B4"] == ("Notes", "inline_string")
    assert cells["A5"] == ("A", "inline_string")
    assert cells["B5"] == ("0012", "inline_string")


def test_word_to_excel_json_table_rows_match_engine_fallback(
    tmp_path: Path,
) -> None:
    parser_output = {
        "source_type": "docx",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "engine": "docx-engine-parser",
                        "text": "Sample\tValue\tNotes\nA\t0012\t",
                        "rows": [["Sample\tValue", "Notes"], ["A", "0012", ""]],
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="engine-fallback.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="word_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Sample\tValue", "inline_string")
    assert cells["B4"] == ("Notes", "inline_string")
    assert cells["A5"] == ("A", "inline_string")
    assert cells["B5"] == ("0012", "inline_string")


def test_word_to_excel_json_table_rows_use_page_number_fallback(
    tmp_path: Path,
) -> None:
    parser_output = {
        "source_type": "docx",
        "extractor": "docx-root-parser",
        "pages": [
            {
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Sample\tValue\tNotes\nA\t0012\t",
                        "rows": [["Sample\tValue", "Notes"], ["A", "0012", ""]],
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="missing-page-number.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="word_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Sample\tValue", "inline_string")
    assert cells["B4"] == ("Notes", "inline_string")
    assert cells["A5"] == ("A", "inline_string")
    assert cells["B5"] == ("0012", "inline_string")


def test_word_to_excel_json_table_rows_treat_blank_extractor_as_missing(
    tmp_path: Path,
) -> None:
    parser_output = {
        "source_type": "docx",
        "extractor": "docx-root-parser",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "extractor": "",
                        "text": "Sample\tValue\tNotes\nA\t0012\t",
                        "rows": [["Sample\tValue", "Notes"], ["A", "0012", ""]],
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="blank-extractor.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="word_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Sample\tValue", "inline_string")
    assert cells["B4"] == ("Notes", "inline_string")
    assert cells["A5"] == ("A", "inline_string")
    assert cells["B5"] == ("0012", "inline_string")


def test_word_to_excel_json_table_rows_prefer_exact_extractor_match(
    tmp_path: Path,
) -> None:
    parser_output = {
        "source_type": "docx",
        "extractor": "docx-root-parser",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Sample\tValue\tNotes\nA\t0012\t",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
        "blocks": [
            {
                "type": "table",
                "text": "Sample\tValue\tNotes\nA\t0012\t",
                "rows": [["stale", "grid"], ["wrong", "cells"]],
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                    "extractor": {"name": "other-parser", "version": "legacy"},
                    "confidence": 0.95,
                },
            },
            {
                "type": "table",
                "text": "Sample\tValue\tNotes\nA\t0012\t",
                "rows": [["Sample\tValue", "Notes"], ["A", "0012", ""]],
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                    "extractor": {"name": "docx-root-parser", "version": "legacy"},
                    "confidence": 0.95,
                },
            },
        ],
    }

    result = convert_uploaded_document(
        filename="exact-extractor-match.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="word_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Sample\tValue", "inline_string")
    assert cells["B4"] == ("Notes", "inline_string")
    assert cells["A5"] == ("A", "inline_string")
    assert cells["B5"] == ("0012", "inline_string")


def test_word_to_excel_docx_upload_flags_merged_table_cells(tmp_path: Path) -> None:
    docx_path = tmp_path / "merged-report.docx"
    _write_docx(
        docx_path,
        """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:tbl>
      <w:tr>
        <w:tc>
          <w:tcPr><w:gridSpan w:val="2"/></w:tcPr>
          <w:p><w:r><w:t>Merged Header</w:t></w:r></w:p>
        </w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Lot</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>0007</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
""",
    )

    result = convert_uploaded_document(
        filename="merged-report.docx",
        content=docx_path.read_bytes(),
        conversion_mode="word_to_excel",
    )

    assert result["status"] == "requires_review"
    assert (
        "DOCX table contains merged cells; xlsx artifact requires review" in result["warnings"]
    )
    assert {
        "document_id": "merged-report",
        "block_id": "block-0001",
        "source_id": "merged-report:block-0001",
        "source_page": 1,
        "source_confidence": 0.0,
        "text": "Merged Header\nLot\t0007",
        "warnings": [
            "blocks[0].bbox missing; block marked requires_review",
            "blocks[0].parser marked block requires_review",
            "DOCX table contains merged cells; xlsx artifact requires review",
            "blocks[0].source metadata incomplete; original jump unavailable",
        ],
    } in result["review_items"]
    assert result["artifacts"][0]["format"] == "xlsx"


def test_pdf_to_excel_primary_uses_extracted_pdf_table_with_source_comment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_parse_text_pdf_to_document_ir(pdf_path: Path, *, document_id: str | None = None) -> dict:
        return {
            "schema_version": "document-ir/v0",
            "document": {"id": document_id or "sample", "title": pdf_path.name, "source_type": "pdf"},
            "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "pt"}],
            "blocks": [
                {
                    "id": "block-001",
                    "type": "paragraph",
                    "text": "Introductory PDF text",
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 12, "y": 18, "width": 180, "height": 16, "unit": "pt"},
                        "extractor": {"name": "test-text", "version": "test"},
                        "confidence": 0.95,
                    },
                }
            ],
        }

    table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot\nID", "Assay\t%"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=20, width=50, height=12),
                TableBBox(x=60, y=20, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=32, width=50, height=12),
                TableBBox(x=60, y=32, width=60, height=12),
            ],
        ],
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[table],
                notes="synthetic selected table",
            )
        ],
        mismatches=[],
        selected_candidate="camelot:lattice",
        notes="synthetic report",
    )
    monkeypatch.setattr(poc_web, "parse_text_pdf_to_document_ir", fake_parse_text_pdf_to_document_ir)
    monkeypatch.setattr(poc_web, "compare_pdf_table_extractors", lambda _path: report, raising=False)

    result = convert_uploaded_document(
        filename="sample.pdf",
        content=b"%PDF-1.4\n%%EOF\n",
        conversion_mode="pdf_to_excel",
    )

    primary_artifact = result["artifacts"][0]
    assert primary_artifact["format"] == "xlsx"
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A5"] == ("Lot\nID", "inline_string")
    assert cells["B5"] == ("Assay\t%", "inline_string")
    assert cells["A6"] == ("A-001", "inline_string")
    assert cells["B6"] == ("12.5", "number")
    with ZipFile(primary_path) as archive:
        comments_xml = archive.read("xl/comments1.xml").decode("utf-8")
    assert "PDF table extraction: camelot:lattice" in comments_xml
    assert "source_page=1" in comments_xml
    assert "bbox=10.0,196.0,110.0,24.0 pt" in comments_xml


def test_pdf_to_excel_no_selected_pdf_table_requires_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_parse_text_pdf_to_document_ir(
        pdf_path: Path, *, document_id: str | None = None
    ) -> dict:
        return {
            "schema_version": "document-ir/v0",
            "document": {
                "id": document_id or "sample",
                "title": pdf_path.name,
                "source_type": "pdf",
            },
            "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "pt"}],
            "blocks": [
                {
                    "id": "block-001",
                    "type": "paragraph",
                    "text": "Plain PDF text only",
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {
                            "x": 12,
                            "y": 18,
                            "width": 180,
                            "height": 16,
                            "unit": "pt",
                        },
                        "extractor": {"name": "test-text", "version": "test"},
                        "confidence": 0.95,
                    },
                }
            ],
        }

    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[],
        mismatches=[],
        selected_candidate=None,
        notes="no extractor selected",
    )
    monkeypatch.setattr(poc_web, "parse_text_pdf_to_document_ir", fake_parse_text_pdf_to_document_ir)
    monkeypatch.setattr(poc_web, "compare_pdf_table_extractors", lambda _path: report, raising=False)

    result = convert_uploaded_document(
        filename="sample.pdf",
        content=b"%PDF-1.4\n%%EOF\n",
        conversion_mode="pdf_to_excel",
    )

    assert result["status"] == "requires_review"
    assert result["warnings"] == [
        "PDF table extraction produced no selected table; xlsx artifact requires review",
        "conversion mode pdf_to_excel selected",
    ]
    assert result["review_items"] == [
        {
            "document_id": "sample",
            "block_id": "pdf-table-extraction",
            "source_id": "sample:pdf-table-extraction",
            "source_page": 1,
            "text": "PDF table extraction requires review",
            "warnings": [
                "PDF table extraction produced no selected table; xlsx artifact requires review"
            ],
        }
    ]
    assert result["artifacts"][0]["format"] == "xlsx"


def test_pdf_to_excel_unavailable_table_comparator_requires_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_parse_text_pdf_to_document_ir(
        pdf_path: Path, *, document_id: str | None = None
    ) -> dict:
        return {
            "schema_version": "document-ir/v0",
            "document": {
                "id": document_id or "sample",
                "title": pdf_path.name,
                "source_type": "pdf",
            },
            "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "pt"}],
        }

    table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=20, width=50, height=12),
                TableBBox(x=60, y=20, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=32, width=50, height=12),
                TableBBox(x=60, y=32, width=60, height=12),
            ],
        ],
    )
    unavailable = TableExtractionCandidate(
        extractor="pdfplumber",
        flavor="table",
        version=None,
        status="missing_dependency",
        tables=[],
        notes="pdfplumber unavailable",
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[table],
                notes="synthetic selected table",
            ),
            unavailable,
        ],
        mismatches=[],
        selected_candidate="camelot:lattice",
        notes="synthetic report",
    )
    monkeypatch.setattr(poc_web, "parse_text_pdf_to_document_ir", fake_parse_text_pdf_to_document_ir)
    monkeypatch.setattr(poc_web, "compare_pdf_table_extractors", lambda _path: report, raising=False)

    result = convert_uploaded_document(
        filename="sample.pdf",
        content=b"%PDF-1.4\n%%EOF\n",
        conversion_mode="pdf_to_excel",
    )

    assert result["status"] == "requires_review"
    assert result["warnings"] == [
        (
            "PDF table extraction candidate unavailable: pdfplumber:table; "
            "xlsx artifact requires review"
        ),
        "conversion mode pdf_to_excel selected",
    ]
    assert result["review_items"] == [
        {
            "document_id": "sample",
            "block_id": "pdf-table-extraction",
            "source_id": "sample:pdf-table-extraction",
            "source_page": 1,
            "text": "PDF table extraction requires review",
            "warnings": [
                (
                    "PDF table extraction candidate unavailable: pdfplumber:table; "
                    "xlsx artifact requires review"
                )
            ],
        }
    ]


def test_pdf_to_excel_json_table_report_warnings_require_review() -> None:
    table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=20, width=50, height=12),
                TableBBox(x=60, y=20, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=32, width=50, height=12),
                TableBBox(x=60, y=32, width=60, height=12),
            ],
        ],
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[table],
                notes="synthetic selected table",
            )
        ],
        mismatches=[],
        selected_candidate=None,
        notes="no extractor selected",
    )

    result = convert_uploaded_document(
        filename="sample.json",
        content=json.dumps(report.to_dict()).encode("utf-8"),
        conversion_mode="pdf_to_excel",
    )

    assert result["status"] == "requires_review"
    assert "PDF table extraction produced no selected table; xlsx artifact requires review" in result[
        "warnings"
    ]
    assert {
        "document_id": "sample",
        "block_id": "pdf-table-extraction",
        "source_id": "sample:pdf-table-extraction",
        "source_page": 1,
        "text": "PDF table extraction requires review",
        "warnings": ["PDF table extraction produced no selected table; xlsx artifact requires review"],
    } in result["review_items"]


def test_pdf_to_excel_json_table_report_preserves_structured_rows(tmp_path: Path) -> None:
    table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot\nID", "Assay\t%"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=20, width=50, height=12),
                TableBBox(x=60, y=20, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=32, width=50, height=12),
                TableBBox(x=60, y=32, width=60, height=12),
            ],
        ],
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[table],
                notes="synthetic selected table",
            )
        ],
        mismatches=[],
        selected_candidate="camelot:lattice",
        notes="synthetic report",
    )

    result = convert_uploaded_document(
        filename="sample.json",
        content=json.dumps(report.to_dict()).encode("utf-8"),
        conversion_mode="pdf_to_excel",
    )

    assert result["status"] == "converted"
    primary_artifact = result["artifacts"][0]
    assert primary_artifact["format"] == "xlsx"
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])
    xlsx = extract_xlsx_structure(primary_path)
    cells = {cell.ref: (cell.value, cell.value_type) for cell in xlsx.sheets[0].cells}
    assert cells["A4"] == ("Lot\nID", "inline_string")
    assert cells["B4"] == ("Assay\t%", "inline_string")
    assert cells["A5"] == ("A-001", "inline_string")
    assert cells["B5"] == ("12.5", "number")


def test_pdf_to_excel_json_table_report_grows_synthetic_page_for_selected_bboxes() -> None:
    table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=700, y=900, width=50, height=12),
                TableBBox(x=750, y=900, width=60, height=12),
            ],
            [
                TableBBox(x=700, y=912, width=50, height=12),
                TableBBox(x=750, y=912, width=60, height=12),
            ],
        ],
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[table],
                notes="synthetic selected table",
            )
        ],
        mismatches=[],
        selected_candidate="camelot:lattice",
        notes="synthetic report",
    )

    result = convert_uploaded_document(
        filename="sample.json",
        content=json.dumps(report.to_dict()).encode("utf-8"),
        conversion_mode="pdf_to_excel",
    )

    assert result["status"] == "converted"
    assert result["validation"]["errors"] == []
    page = result["document_ir"]["pages"][0]
    assert page["width"] == 810.0
    assert page["height"] == 924.0


def test_pdf_to_excel_multiple_selected_tables_without_mismatch_converts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_parse_text_pdf_to_document_ir(
        pdf_path: Path, *, document_id: str | None = None
    ) -> dict:
        return {
            "schema_version": "document-ir/v0",
            "document": {
                "id": document_id or "sample",
                "title": pdf_path.name,
                "source_type": "pdf",
            },
            "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "pt"}],
        }

    first_table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=20, width=50, height=12),
                TableBBox(x=60, y=20, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=32, width=50, height=12),
                TableBBox(x=60, y=32, width=60, height=12),
            ],
        ],
    )
    second_table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Batch", "Result"], ["B-002", "pass"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=80, width=50, height=12),
                TableBBox(x=60, y=80, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=92, width=50, height=12),
                TableBBox(x=60, y=92, width=60, height=12),
            ],
        ],
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[first_table, second_table],
                notes="synthetic selected tables",
            )
        ],
        mismatches=[],
        selected_candidate="camelot:lattice",
        notes="synthetic report",
    )
    monkeypatch.setattr(poc_web, "parse_text_pdf_to_document_ir", fake_parse_text_pdf_to_document_ir)
    monkeypatch.setattr(poc_web, "compare_pdf_table_extractors", lambda _path: report, raising=False)

    result = convert_uploaded_document(
        filename="sample.pdf",
        content=b"%PDF-1.4\n%%EOF\n",
        conversion_mode="pdf_to_excel",
    )

    assert result["status"] == "converted"
    assert result["warnings"] == ["conversion mode pdf_to_excel selected"]
    assert result["review_items"] == []


def test_pdf_table_extractor_requires_review_for_partial_table_bboxes() -> None:
    parser_output = {
        "document": {
            "id": "sample",
            "title": "sample.pdf",
            "source_type": "pdf",
        },
        "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "pt"}],
    }
    complete_table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=20, width=50, height=12),
                TableBBox(x=60, y=20, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=32, width=50, height=12),
                TableBBox(x=60, y=32, width=60, height=12),
            ],
        ],
    )
    partial_table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Batch", "Result"], ["B-002", "pass"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=80, width=50, height=12),
                None,
            ],
            [
                TableBBox(x=10, y=92, width=50, height=12),
                TableBBox(x=60, y=92, width=60, height=12),
            ],
        ],
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[complete_table, partial_table],
                notes="synthetic selected tables",
            )
        ],
        mismatches=[],
        selected_candidate="camelot:lattice",
        notes="synthetic report",
    )

    output = poc_web._parser_output_with_pdf_tables(parser_output, report)

    assert poc_web._pdf_table_warnings(report) == [
        (
            "PDF table extraction selected table has incomplete cell boundaries; "
            "xlsx artifact requires review"
        )
    ]
    fragments = output["pages"][0]["fragments"]
    assert fragments[0]["confidence"] == 0.9
    assert "requires_review" not in fragments[0]
    assert fragments[1]["confidence"] == 0.0
    assert fragments[1]["requires_review"] is True
    assert fragments[1]["missing_confidence"] is True
    assert "bbox" not in fragments[1]


def test_pdf_table_extractor_requires_review_for_malformed_table_bbox_dict() -> None:
    parser_output = {
        "document": {
            "id": "sample",
            "title": "sample.pdf",
            "source_type": "pdf",
        },
        "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "pt"}],
    }
    report = {
        "source_path": "sample.pdf",
        "candidates": [
            {
                "extractor": "camelot",
                "flavor": "lattice",
                "version": "test",
                "status": "ok",
                "tables": [
                    {
                        "extractor": "camelot",
                        "flavor": "lattice",
                        "page_number": 1,
                        "rows": [["Lot", "Assay"], ["A-001", "12.5"]],
                        "cell_bboxes": [
                            [
                                {"x": 10, "y": 20, "width": 50, "height": 12},
                                {"x": 60, "y": 20, "width": 60},
                            ],
                            [
                                {"x": 10, "y": 32, "width": 50, "height": 12},
                                {"x": 60, "y": 32, "width": 60, "height": 12},
                            ],
                        ],
                    }
                ],
                "notes": "synthetic selected table",
            }
        ],
        "mismatches": [],
        "selected_candidate": "camelot:lattice",
        "notes": "synthetic report",
    }

    output = poc_web._parser_output_with_pdf_tables(parser_output, report)

    assert poc_web._pdf_table_warnings(report) == [
        (
            "PDF table extraction selected table has incomplete cell boundaries; "
            "xlsx artifact requires review"
        )
    ]
    fragment = output["pages"][0]["fragments"][0]
    assert fragment["confidence"] == 0.0
    assert fragment["requires_review"] is True
    assert fragment["missing_confidence"] is True
    assert "bbox" not in fragment


def test_pdf_to_excel_does_not_duplicate_parser_table_blocks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_parse_text_pdf_to_document_ir(
        pdf_path: Path, *, document_id: str | None = None
    ) -> dict:
        return {
            "schema_version": "document-ir/v0",
            "document": {
                "id": document_id or "sample",
                "title": pdf_path.name,
                "source_type": "pdf",
            },
            "pages": [
                {
                    "page_number": 1,
                    "width": 320,
                    "height": 240,
                    "unit": "pt",
                    "fragments": [
                        {
                            "kind": "table",
                            "text": "Lot\tAssay\nA-001\t12.5",
                            "bbox": {
                                "x": 10,
                                "y": 20,
                                "width": 110,
                                "height": 24,
                                "unit": "pt",
                            },
                            "confidence": 0.72,
                            "requires_review": True,
                        }
                    ],
                }
            ],
        }

    table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=20, width=50, height=12),
                TableBBox(x=60, y=20, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=32, width=50, height=12),
                TableBBox(x=60, y=32, width=60, height=12),
            ],
        ],
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[table],
                notes="synthetic selected table",
            )
        ],
        mismatches=[],
        selected_candidate="camelot:lattice",
        notes="synthetic report",
    )
    monkeypatch.setattr(poc_web, "parse_text_pdf_to_document_ir", fake_parse_text_pdf_to_document_ir)
    monkeypatch.setattr(poc_web, "compare_pdf_table_extractors", lambda _path: report, raising=False)

    result = convert_uploaded_document(
        filename="sample.pdf",
        content=b"%PDF-1.4\n%%EOF\n",
        conversion_mode="pdf_to_excel",
    )

    primary_path = tmp_path / result["artifacts"][0]["filename"]
    primary_path.write_bytes(result["artifacts"][0]["content"])
    xlsx = extract_xlsx_structure(primary_path)
    values = [cell.value for cell in xlsx.sheets[0].cells]
    assert values.count("Lot") == 1
    assert values.count("Assay") == 1
    assert values.count("A-001") == 1
    assert values.count("12.5") == 1
    with ZipFile(primary_path) as archive:
        comments_xml = archive.read("xl/comments1.xml").decode("utf-8")
    assert "PDF table extraction: camelot:lattice" in comments_xml


def test_pdf_table_extractor_merges_existing_parser_table_block() -> None:
    parser_output = {
        "document": {
            "id": "sample",
            "title": "sample.pdf",
            "source_type": "pdf",
        },
        "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "table",
                "text": "Lot\tAssay\nA-001\t12.5",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 10, "y": 20, "width": 110, "height": 24, "unit": "pt"},
                    "extractor": {"name": "pymupdf-text-table-heuristic", "version": "test"},
                    "confidence": 0.6,
                    "requires_review": True,
                },
            }
        ],
    }
    table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=20, width=50, height=12),
                TableBBox(x=60, y=20, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=32, width=50, height=12),
                TableBBox(x=60, y=32, width=60, height=12),
            ],
        ],
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[table],
                notes="synthetic selected table",
            )
        ],
        mismatches=[],
        selected_candidate="camelot:lattice",
        notes="synthetic report",
    )

    output = poc_web._parser_output_with_pdf_tables(parser_output, report)

    fragments = output["pages"][0]["fragments"]
    assert len(fragments) == 1
    assert fragments[0]["text"] == "Lot\tAssay\nA-001\t12.5"
    assert fragments[0]["rows"] == [["Lot", "Assay"], ["A-001", "12.5"]]
    assert fragments[0]["extractor"] == "camelot:lattice"
    assert fragments[0]["confidence"] == 0.9
    assert fragments[0]["bbox"] == {
        "x": 10.0,
        "y": 196.0,
        "width": 110.0,
        "height": 24.0,
        "unit": "pt",
        "origin": "top-left",
    }
    assert "requires_review" not in fragments[0]


def test_pdf_table_extractor_clears_stale_bbox_when_merged_table_lacks_bboxes() -> None:
    parser_output = {
        "document": {
            "id": "sample",
            "title": "sample.pdf",
            "source_type": "pdf",
        },
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Lot\tAssay\nA-001\t12.5",
                        "bbox": {"x": 10, "y": 20, "width": 110, "height": 24, "unit": "pt"},
                        "confidence": 0.6,
                    }
                ],
            }
        ],
    }
    table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=20, width=50, height=12),
                None,
            ],
            [
                TableBBox(x=10, y=32, width=50, height=12),
                TableBBox(x=60, y=32, width=60, height=12),
            ],
        ],
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[table],
                notes="synthetic selected table",
            )
        ],
        mismatches=[],
        selected_candidate="camelot:lattice",
        notes="synthetic report",
    )

    output = poc_web._parser_output_with_pdf_tables(parser_output, report)

    fragments = output["pages"][0]["fragments"]
    assert len(fragments) == 1
    assert fragments[0]["confidence"] == 0.0
    assert fragments[0]["requires_review"] is True
    assert fragments[0]["missing_confidence"] is True
    assert "bbox" not in fragments[0]


def test_pdf_table_extractor_merges_one_existing_parser_fragment_per_table() -> None:
    parser_output = {
        "document": {
            "id": "sample",
            "title": "sample.pdf",
            "source_type": "pdf",
        },
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Lot\tAssay\nA-001\t12.5",
                        "bbox": {"x": 10, "y": 20, "width": 110, "height": 24, "unit": "pt"},
                        "confidence": 0.6,
                        "requires_review": True,
                    },
                    {
                        "kind": "table",
                        "text": "Lot\tAssay\nA-001\t12.5",
                        "bbox": {"x": 10, "y": 80, "width": 110, "height": 24, "unit": "pt"},
                        "confidence": 0.6,
                        "requires_review": True,
                    },
                ],
            }
        ],
    }
    first_table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=20, width=50, height=12),
                TableBBox(x=60, y=20, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=32, width=50, height=12),
                TableBBox(x=60, y=32, width=60, height=12),
            ],
        ],
    )
    second_table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=80, width=50, height=12),
                TableBBox(x=60, y=80, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=92, width=50, height=12),
                TableBBox(x=60, y=92, width=60, height=12),
            ],
        ],
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[first_table, second_table],
                notes="synthetic selected tables",
            )
        ],
        mismatches=[],
        selected_candidate="camelot:lattice",
        notes="synthetic report",
    )

    output = poc_web._parser_output_with_pdf_tables(parser_output, report)

    fragments = output["pages"][0]["fragments"]
    assert len(fragments) == 2
    assert [fragment["bbox"]["y"] for fragment in fragments] == [196.0, 136.0]
    assert all(fragment["extractor"] == "camelot:lattice" for fragment in fragments)
    assert all("requires_review" not in fragment for fragment in fragments)


def test_pdf_table_extractor_removes_unselected_duplicate_parser_fragments() -> None:
    parser_output = {
        "document": {
            "id": "sample",
            "title": "sample.pdf",
            "source_type": "pdf",
        },
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Lot\tAssay\nA-001\t12.5",
                        "bbox": {"x": 10, "y": 20, "width": 110, "height": 24, "unit": "pt"},
                        "confidence": 0.6,
                    },
                    {
                        "kind": "table",
                        "text": "Lot\tAssay\nA-001\t12.5",
                        "bbox": {"x": 10, "y": 80, "width": 110, "height": 24, "unit": "pt"},
                        "confidence": 0.6,
                    },
                ],
            }
        ],
    }
    table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=20, width=50, height=12),
                TableBBox(x=60, y=20, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=32, width=50, height=12),
                TableBBox(x=60, y=32, width=60, height=12),
            ],
        ],
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[table],
                notes="synthetic selected table",
            )
        ],
        mismatches=[],
        selected_candidate="camelot:lattice",
        notes="synthetic report",
    )

    output = poc_web._parser_output_with_pdf_tables(parser_output, report)

    fragments = output["pages"][0]["fragments"]
    assert len(fragments) == 1
    assert fragments[0]["extractor"] == "camelot:lattice"
    assert fragments[0]["bbox"]["y"] == 196.0


def test_pdf_table_extractor_ignores_non_rendered_top_level_blocks_for_merge() -> None:
    parser_output = {
        "document": {
            "id": "sample",
            "title": "sample.pdf",
            "source_type": "pdf",
        },
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Lot\tAssay\nA-001\t12.5",
                        "bbox": {"x": 10, "y": 20, "width": 110, "height": 24, "unit": "pt"},
                        "confidence": 0.6,
                    }
                ],
            }
        ],
        "blocks": [
            {
                "id": "block-001",
                "type": "table",
                "text": "Lot\tAssay\nA-001\t12.5",
                "page_number": 1,
            }
        ],
    }
    first_table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=20, width=50, height=12),
                TableBBox(x=60, y=20, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=32, width=50, height=12),
                TableBBox(x=60, y=32, width=60, height=12),
            ],
        ],
    )
    second_table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=80, width=50, height=12),
                TableBBox(x=60, y=80, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=92, width=50, height=12),
                TableBBox(x=60, y=92, width=60, height=12),
            ],
        ],
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[first_table, second_table],
                notes="synthetic selected tables",
            )
        ],
        mismatches=[],
        selected_candidate="camelot:lattice",
        notes="synthetic report",
    )

    output = poc_web._parser_output_with_pdf_tables(parser_output, report)

    fragments = output["pages"][0]["fragments"]
    assert len(fragments) == 2
    assert [fragment["bbox"]["y"] for fragment in fragments] == [196.0, 136.0]
    assert all(fragment["extractor"] == "camelot:lattice" for fragment in fragments)


def test_pdf_table_extractor_preserves_repeated_identical_selected_tables() -> None:
    parser_output = {
        "document": {
            "id": "sample",
            "title": "sample.pdf",
            "source_type": "pdf",
        },
        "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "pt"}],
    }
    first_table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=20, width=50, height=12),
                TableBBox(x=60, y=20, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=32, width=50, height=12),
                TableBBox(x=60, y=32, width=60, height=12),
            ],
        ],
    )
    second_table = ExtractedTable(
        extractor="camelot",
        flavor="lattice",
        page_number=1,
        rows=[["Lot", "Assay"], ["A-001", "12.5"]],
        cell_bboxes=[
            [
                TableBBox(x=10, y=80, width=50, height=12),
                TableBBox(x=60, y=80, width=60, height=12),
            ],
            [
                TableBBox(x=10, y=92, width=50, height=12),
                TableBBox(x=60, y=92, width=60, height=12),
            ],
        ],
    )
    report = TableExtractionReport(
        source_path="sample.pdf",
        candidates=[
            TableExtractionCandidate(
                extractor="camelot",
                flavor="lattice",
                version="test",
                status="ok",
                tables=[first_table, second_table],
                notes="synthetic selected tables",
            )
        ],
        mismatches=[],
        selected_candidate="camelot:lattice",
        notes="synthetic report",
    )

    output = poc_web._parser_output_with_pdf_tables(parser_output, report)

    fragments = output["pages"][0]["fragments"]
    assert len(fragments) == 2
    assert [fragment["text"] for fragment in fragments] == [
        "Lot\tAssay\nA-001\t12.5",
        "Lot\tAssay\nA-001\t12.5",
    ]
    assert [fragment["bbox"]["y"] for fragment in fragments] == [196.0, 136.0]


def test_convert_uploaded_document_passes_xlsx_render_plan_for_table_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capturing_xlsx_renderer(
        document_ir: dict,
        output_path: Path,
        *,
        conversion_plan: dict | None = None,
        render_plan: dict | None = None,
    ) -> None:
        captured["conversion_plan"] = conversion_plan
        captured["render_plan"] = render_plan
        output_path.write_bytes(b"xlsx fixture")

    monkeypatch.setattr(poc_web, "render_xlsx_from_ir", capturing_xlsx_renderer)
    parser_output = {
        "source_type": "pdf",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Lot\tAssay\nA\t12.5",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 32, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="pdf_to_excel",
    )

    assert result["status"] == "converted"
    assert result["artifacts"][0]["content"] == b"xlsx fixture"
    assert captured == {
        "conversion_plan": None,
        "render_plan": {"table_merges": []},
    }


@pytest.mark.parametrize(
    ("conversion_mode", "artifact_format"),
    (
        ("pdf_to_word", "docx"),
        ("pdf_to_excel", "xlsx"),
    ),
)
def test_convert_uploaded_document_manifest_preserves_artifact_suffixes_for_long_names(
    conversion_mode: str,
    artifact_format: str,
) -> None:
    parser_output = {
        "source_type": "pdf",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [{"text": "PDF text", "confidence": 0.95}],
            }
        ],
    }
    mode_slug = conversion_mode.replace("_", "-")

    result = convert_uploaded_document(
        filename=f"{'a' * 300}.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode=conversion_mode,
    )

    primary_filename = result["artifacts"][0]["filename"]
    debug_filename = result["artifacts"][1]["filename"]
    assert primary_filename.endswith(f".veridoc-{mode_slug}.{artifact_format}")
    assert len(primary_filename.encode("utf-8")) <= poc_web.MAX_DOWNLOAD_FILENAME_BYTES
    assert debug_filename.endswith(".veridoc-result.json")
    assert len(debug_filename.encode("utf-8")) <= poc_web.MAX_DOWNLOAD_FILENAME_BYTES
    assert result["download"]["filename"] == debug_filename


@pytest.mark.parametrize(
    ("conversion_mode", "artifact_format"),
    (
        ("pdf_to_word", "docx"),
        ("pdf_to_excel", "xlsx"),
    ),
)
def test_convert_uploaded_document_manifest_avoids_reserved_stems_after_trim(
    conversion_mode: str,
    artifact_format: str,
) -> None:
    parser_output = {
        "source_type": "pdf",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [{"text": "PDF text", "confidence": 0.95}],
            }
        ],
    }
    mode_slug = conversion_mode.replace("_", "-")

    result = convert_uploaded_document(
        filename="nested/CON-.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode=conversion_mode,
    )

    primary_filename = result["artifacts"][0]["filename"]
    debug_filename = result["artifacts"][1]["filename"]
    assert primary_filename == f"CON_.veridoc-{mode_slug}.{artifact_format}"
    assert debug_filename == "CON_.veridoc-result.json"
    assert result["download"]["filename"] == debug_filename


@pytest.mark.parametrize(
    ("conversion_mode", "artifact_format"),
    (
        ("pdf_to_word", "docx"),
        ("pdf_to_excel", "xlsx"),
    ),
)
def test_convert_uploaded_document_manifest_avoids_reserved_stems_after_truncation(
    conversion_mode: str,
    artifact_format: str,
) -> None:
    parser_output = {
        "source_type": "pdf",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [{"text": "PDF text", "confidence": 0.95}],
            }
        ],
    }
    mode_slug = conversion_mode.replace("_", "-")

    result = convert_uploaded_document(
        filename=f"CON{'-' * 233}A.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode=conversion_mode,
    )

    primary_filename = result["artifacts"][0]["filename"]
    debug_filename = result["artifacts"][1]["filename"]
    assert primary_filename == f"CON_.veridoc-{mode_slug}.{artifact_format}"
    assert debug_filename == "CON_.veridoc-result.json"
    assert result["download"]["filename"] == debug_filename


def test_convert_uploaded_document_records_selected_conversion_mode() -> None:
    parser_output = {
        "source_type": "pdf",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "text": "Lot: SAMPLE-001",
                        "bbox": {"x": 10, "y": 20, "width": 120, "height": 16, "unit": "pt"},
                        "confidence": 0.91,
                    }
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="pdf_to_word",
    )

    expected_warnings = [
        "conversion mode pdf_to_word selected",
        "pdf_to_word reconstruction preserves editable text structure for review; exact PDF layout, fonts, coordinates, columns, footnotes, and OCR fidelity are not guaranteed",
    ]
    assert result["warnings"] == expected_warnings
    assert result["audit"]["conversion_mode"] == "pdf_to_word"
    downloaded = json.loads(result["download"]["content"].decode("utf-8"))
    assert downloaded["warnings"] == expected_warnings
    assert downloaded["audit"]["conversion_mode"] == "pdf_to_word"


def test_convert_uploaded_document_rejects_unknown_conversion_mode() -> None:
    with pytest.raises(ValueError, match="unsupported conversion_mode"):
        convert_uploaded_document(
            filename="upload.txt",
            content=b"fallback text",
            conversion_mode="spreadsheet_magic",
        )


@pytest.mark.parametrize(
    ("field_name", "kwargs"),
    [
        ("use_llm", {"use_llm": "true"}),
        ("use_ocr", {"use_ocr": 1}),
    ],
)
def test_convert_uploaded_document_rejects_non_boolean_conversion_settings(
    field_name: str,
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match=f"{field_name} must be boolean"):
        convert_uploaded_document(
            filename="upload.txt",
            content=b"fallback text",
            **kwargs,
        )


def test_convert_uploaded_document_rejects_mismatched_conversion_mode() -> None:
    parser_output = {
        "source_type": "pdf",
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [{"text": "PDF text", "confidence": 0.95}],
            }
        ],
    }

    with pytest.raises(
        ValueError,
        match="conversion_mode word_to_excel requires docx input; got pdf",
    ):
        convert_uploaded_document(
            filename="phase0-output.json",
            content=json.dumps(parser_output).encode("utf-8"),
            conversion_mode="word_to_excel",
        )


def test_convert_uploaded_document_treats_unusable_review_bboxes_as_absent() -> None:
    parser_output = {
        "pages": [
            {
                "page_number": 1,
                "width": 100,
                "height": 100,
                "unit": "pt",
                "fragments": [
                    {
                        "text": "Missing bbox",
                        "confidence": 0.41,
                        "low_confidence": True,
                    },
                    {
                        "text": "Outside page",
                        "bbox": {"x": 90, "y": 10, "width": 20, "height": 12, "unit": "pt"},
                        "confidence": 0.41,
                        "low_confidence": True,
                    },
                ],
            }
        ]
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "blocked"
    assert result["validation"]["errors"] == ["blocks[1].bbox extends past page 1"]
    assert [item["block_id"] for item in result["review_items"]] == ["block-0001", "block-0002"]
    assert all("source_bbox" not in item for item in result["review_items"])
    assert all("source_page_geometry" not in item for item in result["review_items"])


def test_convert_uploaded_document_omits_synthetic_missing_bbox_from_review_item() -> None:
    parser_output = {
        "pages": [
            {
                "page_number": 1,
                "width": 100,
                "height": 100,
                "unit": "pt",
                "fragments": [
                    {
                        "text": "Missing bbox",
                        "confidence": 0.41,
                        "low_confidence": True,
                    },
                ],
            }
        ]
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "requires_review"
    assert result["validation"]["errors"] == []
    assert result["review_items"] == [
        {
            "document_id": "phase0-output",
            "block_id": "block-0001",
            "source_id": "phase0-output:block-0001",
            "source_page": 1,
            "source_confidence": 0.0,
            "text": "Missing bbox",
            "warnings": [
                "blocks[0].bbox missing; block marked requires_review",
                "blocks[0].low confidence; block marked requires_review",
                "blocks[0].source metadata incomplete; original jump unavailable",
            ],
        }
    ]


def test_convert_uploaded_document_warns_when_review_item_source_jump_is_incomplete() -> None:
    parser_output = {
        "schema_version": "document-ir/v0",
        "document": {
            "id": "sample-document-001",
            "title": "High Risk Missing Source Metadata",
            "source_type": "pdf",
        },
        "pages": [{"page_number": 1, "width": 612, "height": 792, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "paragraph",
                "text": "Assay result: 99.8%",
                "value_metadata": {
                    "source_page": 1,
                    "confidence": 0.98,
                    "requires_review": True,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "requires_review"
    assert result["validation"]["requires_review"] is True
    assert result["review_items"] == [
        {
            "document_id": "sample-document-001",
            "block_id": "block-0001",
            "source_id": "sample-document-001:block-0001",
            "source_page": 1,
            "source_confidence": 0.0,
            "text": "Assay result: 99.8%",
            "warnings": [
                "blocks[0].bbox missing; block marked requires_review",
                "blocks[0].parser marked block requires_review",
                "blocks[0].source metadata incomplete; original jump unavailable",
            ],
        }
    ]
    assert "source_bbox" not in result["review_items"][0]
    assert "source_page_geometry" not in result["review_items"][0]


def test_convert_uploaded_document_omits_unsupported_unit_review_bbox() -> None:
    parser_output = {
        "pages": [
            {
                "page_number": 1,
                "width": 100,
                "height": 100,
                "unit": "em",
                "fragments": [
                    {
                        "text": "Unsupported unit",
                        "bbox": {"x": 10, "y": 10, "width": 20, "height": 12, "unit": "em"},
                        "confidence": 0.41,
                        "low_confidence": True,
                    },
                ],
            }
        ]
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "blocked"
    assert result["validation"]["errors"] == [
        "pages[0].unit is unsupported: em",
        "blocks[0].bbox unit is unsupported: em",
    ]
    assert result["review_items"] == [
        {
            "document_id": "phase0-output",
            "block_id": "block-0001",
            "source_id": "phase0-output:block-0001",
            "source_page": 1,
            "source_confidence": 0.41,
            "text": "Unsupported unit",
            "warnings": [
                "blocks[0].low confidence; block marked requires_review",
                "blocks[0].source metadata incomplete; original jump unavailable",
            ],
        }
    ]


def test_convert_uploaded_docx_uses_real_parser_bytes(tmp_path: Path) -> None:
    docx_path = tmp_path / "batch-record.docx"
    _write_docx(docx_path, _sample_docx_xml())

    result = convert_uploaded_document(
        filename="batch-record.docx",
        content=docx_path.read_bytes(),
    )

    assert result["status"] == "requires_review"
    assert result["document_ir"]["document"]["source_type"] == "docx"
    assert [block["text"] for block in result["document_ir"]["blocks"]] == [
        "Batch Summary",
        "Lot SAMPLE-001 requires review",
    ]


def test_convert_uploaded_pdf_adapts_phase0_document_ir_v0_blocks(monkeypatch) -> None:
    def fake_parse_text_pdf_to_document_ir(upload_path: Path, *, document_id: str) -> dict:
        assert upload_path.read_bytes() == b"%PDF sample bytes"
        return {
            "schema_version": "document-ir/v0",
            "document": {
                "id": document_id,
                "title": upload_path.name,
                "source_type": "pdf",
            },
            "pages": [{"page_number": 1, "width": 612, "height": 792, "unit": "pt"}],
            "blocks": [
                {
                    "id": "block-001",
                    "type": "table",
                    "text": "Lot\tSAMPLE-001",
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 72, "y": 72, "width": 180, "height": 24, "unit": "pt"},
                        "extractor": {"name": "pymupdf-text-table-heuristic"},
                        "confidence": 0.6,
                        "requires_review": True,
                    },
                }
            ],
        }

    monkeypatch.setattr(poc_web, "parse_text_pdf_to_document_ir", fake_parse_text_pdf_to_document_ir)

    result = convert_uploaded_document(
        filename="batch-record.pdf",
        content=b"%PDF sample bytes",
    )

    assert result["status"] == "requires_review"
    assert result["document_ir"]["document"]["source_type"] == "pdf"
    assert result["document_ir"]["blocks"][0]["type"] == "table"
    assert result["document_ir"]["blocks"][0]["text"] == "Lot\tSAMPLE-001"
    assert result["document_ir"]["blocks"][0]["review"]["requires_review"] is True
    assert result["review_items"] == [
        {
            "document_id": "batch-record",
            "block_id": "block-0001",
            "source_id": "batch-record:block-0001",
            "source_page": 1,
            "source_confidence": 0.6,
            "source_bbox": {
                "x": 72.0,
                "y": 72.0,
                "width": 180.0,
                "height": 24.0,
                "unit": "pt",
                "origin": "top-left",
            },
            "source_page_geometry": {
                "page_number": 1,
                "width": 612.0,
                "height": 792.0,
                "unit": "pt",
            },
            "text": "Lot\tSAMPLE-001",
            "warnings": ["blocks[0].parser marked block requires_review"],
        }
    ]


def test_convert_uploaded_pdf_preserves_ocr_low_confidence_from_v0_metadata(
    monkeypatch,
) -> None:
    def fake_parse_text_pdf_to_document_ir(upload_path: Path, *, document_id: str) -> dict:
        assert upload_path.read_bytes() == b"%PDF scanned sample bytes"
        return {
            "schema_version": "document-ir/v0",
            "document": {
                "id": document_id,
                "title": upload_path.name,
                "source_type": "pdf",
            },
            "pages": [{"page_number": 1, "width": 320, "height": 240, "unit": "px"}],
            "blocks": [
                {
                    "id": "block-001",
                    "type": "field",
                    "text": "LOT-O0I",
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 10, "y": 20, "width": 70, "height": 18, "unit": "px"},
                        "extractor": {"name": "scanned_pdf_ocr", "version": "0.test"},
                        "confidence": 0.41,
                        "low_confidence": True,
                    },
                }
            ],
        }

    monkeypatch.setattr(poc_web, "parse_text_pdf_to_document_ir", fake_parse_text_pdf_to_document_ir)

    result = convert_uploaded_document(
        filename="scanned-batch-record.pdf",
        content=b"%PDF scanned sample bytes",
    )

    assert result["status"] == "requires_review"
    assert result["validation"]["errors"] == []
    assert result["audit"]["conversion_settings"]["use_ocr"] == {
        "requested": False,
        "enabled": False,
        "status": "disabled",
    }
    assert result["document_ir"]["blocks"][0]["extractor"]["name"] == "scanned_pdf_ocr"
    assert result["document_ir"]["blocks"][0]["review"]["requires_review"] is True
    assert result["review_items"] == [
        {
            "document_id": "scanned-batch-record",
            "block_id": "block-0001",
            "source_id": "scanned-batch-record:block-0001",
            "source_page": 1,
            "source_confidence": 0.41,
            "source_bbox": {
                "x": 10.0,
                "y": 20.0,
                "width": 70.0,
                "height": 18.0,
                "unit": "px",
                "origin": "top-left",
            },
            "source_page_geometry": {
                "page_number": 1,
                "width": 320.0,
                "height": 240.0,
                "unit": "px",
            },
            "text": "LOT-O0I",
            "warnings": ["blocks[0].low confidence; block marked requires_review"],
        }
    ]


def test_binary_pdf_parser_output_adapts_blocks_before_v1_conversion(monkeypatch) -> None:
    def fake_parse_text_pdf_to_document_ir(upload_path: Path, *, document_id: str) -> dict:
        assert upload_path.read_bytes() == b"%PDF sample bytes"
        return {
            "schema_version": "document-ir/v0",
            "document": {
                "id": document_id,
                "title": upload_path.name,
                "source_type": "pdf",
            },
            "pages": [{"page_number": 1, "width": 612, "height": 792, "unit": "pt"}],
            "blocks": [
                {
                    "id": "block-001",
                    "type": "paragraph",
                    "text": "Extracted PDF text",
                    "value_metadata": {
                        "source_page": 1,
                        "bbox": {"x": 72, "y": 72, "width": 180, "height": 24, "unit": "pt"},
                        "extractor": {"name": "pymupdf", "version": "1.2.3"},
                        "confidence": 0.9,
                    },
                }
            ],
        }

    monkeypatch.setattr(poc_web, "parse_text_pdf_to_document_ir", fake_parse_text_pdf_to_document_ir)

    parser_output = poc_web._parser_output_from_binary_upload(
        "batch-record.pdf",
        b"%PDF sample bytes",
        "pdf",
    )

    assert parser_output["pages"][0]["fragments"] == [
        {
            "kind": "paragraph",
            "text": "Extracted PDF text",
            "page_number": 1,
            "bbox": {"x": 72, "y": 72, "width": 180, "height": 24, "unit": "pt"},
            "confidence": 0.9,
            "extractor": {"name": "pymupdf", "version": "1.2.3"},
        }
    ]


def test_convert_uploaded_phase0_json_infers_docx_source_type_from_shape() -> None:
    parser_output = {
        "blocks": [
            {
                "kind": "paragraph",
                "text": "DOCX parser block",
                "bbox": {"x": 10, "y": 20, "width": 120, "height": 16, "unit": "pt"},
                "confidence": 0.95,
            }
        ]
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "converted"
    assert result["document_ir"]["document"]["source_type"] == "docx"
    assert result["document_ir"]["blocks"][0]["text"] == "DOCX parser block"


def test_convert_uploaded_phase0_json_preserves_explicit_unknown_source_type() -> None:
    parser_output = {
        "schema_version": "document-ir/v0",
        "document": {
            "id": "sample-document-001",
            "title": "Unknown source document",
            "source_type": "unknown",
        },
        "pages": [{"page_number": 1, "width": 612, "height": 792, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "paragraph",
                "text": "Explicit unknown source block",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 72, "y": 72, "width": 240, "height": 24, "unit": "pt"},
                    "confidence": 0.95,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "converted"
    assert result["document_ir"]["document"]["source_type"] == "unknown"
    assert result["document_ir"]["blocks"][0]["text"] == "Explicit unknown source block"


def test_convert_uploaded_phase0_json_preserves_document_metadata_and_v0_blocks() -> None:
    parser_output = {
        "schema_version": "document-ir/v0",
        "document": {
            "id": "sample-document-001",
            "title": "Original Phase0 Document",
            "source_type": "docx",
        },
        "pages": [{"page_number": 1, "width": 612, "height": 792, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "paragraph",
                "text": "Preserved Phase0 block",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 72, "y": 72, "width": 240, "height": 24, "unit": "pt"},
                    "extractor": {"name": "docx-parser", "version": "2.3.4"},
                    "confidence": 0.95,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "converted"
    assert result["document_ir"]["document"] == {
        "id": "sample-document-001",
        "title": "Original Phase0 Document",
        "source_type": "docx",
    }
    assert result["document_ir"]["blocks"][0]["text"] == "Preserved Phase0 block"
    assert result["document_ir"]["blocks"][0]["extractor"] == {
        "name": "docx-parser",
        "version": "2.3.4",
    }


def test_convert_uploaded_phase0_json_inherits_page_unit_for_v0_bbox() -> None:
    parser_output = {
        "schema_version": "document-ir/v0",
        "document": {
            "id": "sample-document-001",
            "title": "Pixel Coordinate Document",
            "source_type": "pdf",
        },
        "pages": [{"page_number": 1, "width": 1280, "height": 720, "unit": "px"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "paragraph",
                "text": "Pixel coordinate block",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 10, "y": 20, "width": 240, "height": 24},
                    "confidence": 0.95,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "converted"
    assert result["validation"]["errors"] == []
    assert result["document_ir"]["blocks"][0]["bbox"]["unit"] == "px"


def test_convert_uploaded_phase0_json_marks_missing_v0_confidence_for_review() -> None:
    parser_output = {
        "schema_version": "document-ir/v0",
        "document": {
            "id": "sample-document-001",
            "title": "Missing Confidence Document",
            "source_type": "docx",
        },
        "pages": [{"page_number": 1, "width": 612, "height": 792, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "paragraph",
                "text": "Missing confidence block",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 72, "y": 72, "width": 240, "height": 24, "unit": "pt"},
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "requires_review"
    assert result["validation"]["errors"] == []
    assert result["document_ir"]["blocks"][0]["confidence"] == 0.0
    assert result["review_items"][0]["source_confidence"] is None
    assert result["review_items"][0]["source_bbox"] == {
        "x": 72.0,
        "y": 72.0,
        "width": 240.0,
        "height": 24.0,
        "unit": "pt",
        "origin": "top-left",
    }
    assert result["review_items"][0]["warnings"] == [
        "blocks[0].confidence missing; block marked requires_review"
    ]


def test_convert_uploaded_phase0_json_marks_review_item_llm_involvement() -> None:
    parser_output = {
        "schema_version": "document-ir/v0",
        "document": {
            "id": "sample-document-001",
            "title": "LLM Assisted Document",
            "source_type": "docx",
        },
        "pages": [{"page_number": 1, "width": 612, "height": 792, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "paragraph",
                "text": "LLM assisted block",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 72, "y": 72, "width": 240, "height": 24, "unit": "pt"},
                    "extractor": {"name": "local-llm-conversion-plan", "version": "0.1.0"},
                    "confidence": 0.98,
                    "requires_review": True,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["review_items"][0]["llm_involved"] is True


@pytest.mark.parametrize(
    "extractor_name",
    ["standard", "Qwen/Qwen3-8B", "DeepSeek V4 Flash"],
)
def test_convert_uploaded_phase0_json_marks_configured_llm_extractors(
    extractor_name: str,
) -> None:
    parser_output = {
        "schema_version": "document-ir/v0",
        "document": {
            "id": "sample-document-001",
            "title": "Configured LLM Assisted Document",
            "source_type": "docx",
        },
        "pages": [{"page_number": 1, "width": 612, "height": 792, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "paragraph",
                "text": "Configured LLM assisted block",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 72, "y": 72, "width": 240, "height": 24, "unit": "pt"},
                    "extractor": {"name": extractor_name, "version": "0.1.0"},
                    "confidence": 0.98,
                    "requires_review": True,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["review_items"][0]["llm_involved"] is True


def test_convert_uploaded_phase0_json_does_not_mark_unknown_extractor_as_llm() -> None:
    parser_output = {
        "schema_version": "document-ir/v0",
        "document": {
            "id": "sample-document-001",
            "title": "Non LLM Document",
            "source_type": "docx",
        },
        "pages": [{"page_number": 1, "width": 612, "height": 792, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "paragraph",
                "text": "Non LLM reviewed block",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 72, "y": 72, "width": 240, "height": 24, "unit": "pt"},
                    "extractor": {"name": "rule-based-reviewer", "version": "0.1.0"},
                    "confidence": 0.98,
                    "requires_review": True,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert "llm_involved" not in result["review_items"][0]


def test_convert_uploaded_phase0_json_blocks_unsupported_v0_block_type() -> None:
    parser_output = {
        "schema_version": "document-ir/v0",
        "document": {
            "id": "sample-document-001",
            "title": "Unsupported Block Type Document",
            "source_type": "docx",
        },
        "pages": [{"page_number": 1, "width": 612, "height": 792, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "image",
                "text": "Unsupported block type",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 72, "y": 72, "width": 240, "height": 24, "unit": "pt"},
                    "confidence": 0.95,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "blocked"
    assert result["validation"]["errors"] == ["blocks[0].type is unsupported: image"]
    assert result["document_ir"]["blocks"][0]["type"] == "image"


def test_convert_uploaded_phase0_json_blocks_invalid_declared_source_type() -> None:
    parser_output = {
        "schema_version": "document-ir/v0",
        "document": {
            "id": "sample-document-001",
            "title": "Unsupported Source Document",
            "source_type": "pptx",
        },
        "pages": [{"page_number": 1, "width": 612, "height": 792, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "paragraph",
                "text": "Unsupported source type block",
                "value_metadata": {
                    "source_page": 1,
                    "bbox": {"x": 72, "y": 72, "width": 240, "height": 24, "unit": "pt"},
                    "confidence": 0.95,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "blocked"
    assert result["validation"]["errors"] == ["document.source_type is unsupported: pptx"]
    assert result["document_ir"]["document"]["source_type"] == "pptx"


def test_convert_uploaded_phase0_json_blocks_invalid_v0_source_page() -> None:
    parser_output = {
        "schema_version": "document-ir/v0",
        "document": {
            "id": "sample-document-001",
            "title": "Original Phase0 Document",
            "source_type": "docx",
        },
        "pages": [{"page_number": 1, "width": 612, "height": 792, "unit": "pt"}],
        "blocks": [
            {
                "id": "block-001",
                "type": "paragraph",
                "text": "Bad provenance block",
                "value_metadata": {
                    "bbox": {"x": 72, "y": 72, "width": 240, "height": 24, "unit": "pt"},
                    "confidence": 0.95,
                },
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "blocked"
    assert result["validation"]["errors"] == ["blocks[0].source_page must be >= 1"]
    assert result["document_ir"]["blocks"][0]["source_page"] == 0


def test_convert_uploaded_phase0_json_infers_xlsx_source_type_from_source_path() -> None:
    parser_output = {
        "source_path": "phase0-output.xlsx",
        "sheets": [
            {
                "name": "Document IR",
                "cells": [
                    {"ref": "A1", "value": "Lot"},
                    {"ref": "B1", "value": "SAMPLE-001"},
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["document_ir"]["document"]["source_type"] == "xlsx"
    assert result["document_ir"]["pages"]
    assert result["document_ir"]["blocks"][0]["text"] == (
        "Sheet: Document IR\nA1: Lot\nB1: SAMPLE-001"
    )
    assert result["document_ir"]["blocks"][0]["rows"] == [
        ["Sheet: Document IR"],
        ["Lot", "SAMPLE-001"],
    ]
    with tempfile.TemporaryDirectory() as temp_dir:
        document_path = Path(temp_dir) / "api-emitted-document-ir-v1.json"
        document_path.write_text(json.dumps(result["document_ir"]), encoding="utf-8")

        validation_result = subprocess.run(
            [
                sys.executable,
                "scripts/ci/validate_document_ir.py",
                "--schema",
                "core/ir/document-ir-v1.schema.json",
                "--document",
                str(document_path),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    assert validation_result.returncode == 0, validation_result.stderr


def test_convert_uploaded_phase0_json_preserves_xlsx_column_gaps() -> None:
    parser_output = {
        "source_path": "gapped-output.xlsx",
        "sheets": [
            {
                "name": "Gapped",
                "cells": [
                    {"ref": "A1", "value": "Left"},
                    {"ref": "C1", "value": "Right"},
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="gapped-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="excel_to_word",
    )

    assert result["document_ir"]["blocks"][0]["rows"] == [
        ["Sheet: Gapped"],
        ["Left", "", "Right"],
    ]


def test_convert_uploaded_phase0_json_preserves_leading_xlsx_offsets(
    tmp_path: Path,
) -> None:
    parser_output = {
        "source_path": "offset-table-output.xlsx",
        "sheets": [
            {
                "name": "Offset Table",
                "cells": [
                    {"ref": "B2", "value": "ID"},
                    {"ref": "C2", "value": "Task"},
                    {"ref": "B3", "value": "00123"},
                    {"ref": "C3", "value": "Template review"},
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="offset-table-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="excel_to_word",
    )

    expected_rows = [
        ["Sheet: Offset Table"],
        ["", "", ""],
        ["", "ID", "Task"],
        ["", "00123", "Template review"],
    ]
    assert result["document_ir"]["blocks"][0]["rows"] == expected_rows

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])

    docx = extract_docx_structure(primary_path)
    table_blocks = [block for block in docx.blocks if block.kind == "table"]
    assert table_blocks[0].rows == expected_rows


def test_convert_uploaded_phase0_json_accepts_lowercase_xlsx_refs(tmp_path: Path) -> None:
    parser_output = {
        "source_path": "lowercase-refs-output.xlsx",
        "sheets": [
            {
                "name": "Lowercase Refs",
                "cells": [
                    {"ref": "a1", "value": "Left"},
                    {"ref": "c1", "value": "Right"},
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="lowercase-refs-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="excel_to_word",
    )

    assert result["document_ir"]["blocks"][0]["rows"] == [
        ["Sheet: Lowercase Refs"],
        ["Left", "", "Right"],
    ]

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])

    docx = extract_docx_structure(primary_path)
    table_blocks = [block for block in docx.blocks if block.kind == "table"]
    assert table_blocks[0].rows[1] == ["Left", "", "Right"]


def test_convert_uploaded_phase0_json_preserves_reasonable_wide_column_gaps(
    tmp_path: Path,
) -> None:
    parser_output = {
        "source_path": "wide-gapped-output.xlsx",
        "sheets": [
            {
                "name": "Wide Gapped",
                "cells": [
                    {"ref": "A1", "value": "Left"},
                    {"ref": "BM1", "value": "Right"},
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="wide-gapped-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="excel_to_word",
    )

    row = result["document_ir"]["blocks"][0]["rows"][1]
    assert len(row) == 65
    assert row[0] == "Left"
    assert row[1:64] == [""] * 63
    assert row[64] == "Right"

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])

    docx = extract_docx_structure(primary_path)
    table_blocks = [block for block in docx.blocks if block.kind == "table"]
    rendered_row = table_blocks[0].rows[1]
    assert len(rendered_row) == 65
    assert rendered_row[0] == "Left"
    assert rendered_row[1:64] == [""] * 63
    assert rendered_row[64] == "Right"


def test_convert_uploaded_phase0_json_preserves_xlsx_row_gaps() -> None:
    parser_output = {
        "source_path": "row-gapped-output.xlsx",
        "sheets": [
            {
                "name": "Row Gapped",
                "cells": [
                    {"ref": "A1", "value": "Header"},
                    {"ref": "A3", "value": "Footer"},
                    {"ref": "C3", "value": "Total"},
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="row-gapped-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="excel_to_word",
    )

    assert result["document_ir"]["blocks"][0]["rows"] == [
        ["Sheet: Row Gapped"],
        ["Header", "", ""],
        ["", "", ""],
        ["Footer", "", "Total"],
    ]


def test_convert_uploaded_phase0_json_preserves_sparse_column_gaps_after_row_cap(
    tmp_path: Path,
) -> None:
    parser_output = {
        "source_path": "sparse-row-column-gap.xlsx",
        "sheets": [
            {
                "name": "Sparse Columns",
                "cells": [
                    {"ref": "A1", "value": "Top"},
                    {"ref": "C300", "value": "Total"},
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="sparse-row-column-gap.json",
        content=json.dumps(parser_output).encode("utf-8"),
        conversion_mode="excel_to_word",
    )

    expected_rows = [
        ["Sheet: Sparse Columns"],
        ["Top", "", ""],
    ]
    assert result["document_ir"]["blocks"][0]["rows"][:2] == expected_rows
    assert result["document_ir"]["blocks"][0]["rows"][2:300] == [["", "", ""]] * 298
    assert result["document_ir"]["blocks"][0]["rows"][300] == ["", "", "Total"]
    assert len(result["document_ir"]["blocks"][0]["rows"]) == 301

    primary_artifact = result["artifacts"][0]
    primary_path = tmp_path / primary_artifact["filename"]
    primary_path.write_bytes(primary_artifact["content"])

    docx = extract_docx_structure(primary_path)
    table_blocks = [block for block in docx.blocks if block.kind == "table"]
    assert table_blocks[0].rows[:2] == expected_rows
    assert table_blocks[0].rows[2:300] == [["", "", ""]] * 298
    assert table_blocks[0].rows[300] == ["", "", "Total"]
    assert len(table_blocks[0].rows) == 301


def test_convert_uploaded_xlsx_json_keeps_page_table_rows_over_sheet_records() -> None:
    parser_output = {
        "source_path": "mixed-parser.xlsx",
        "sheets": [
            {
                "name": "Sheet Source",
                "cells": [
                    {"ref": "A1", "value": "Sheet header"},
                    {"ref": "B1", "value": "Sheet value"},
                ],
            }
        ],
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "kind": "table",
                        "text": "Page header\tPage value",
                        "rows": [["Page header", "Page value"]],
                        "extractor": "xlsx",
                    }
                ],
            }
        ],
    }

    result = convert_uploaded_document(
        filename="mixed-parser.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["document_ir"]["document"]["source_type"] == "xlsx"
    assert result["document_ir"]["blocks"][0]["text"] == "Page header\tPage value"
    assert result["document_ir"]["blocks"][0]["rows"] == [["Page header", "Page value"]]


def test_convert_uploaded_document_serializes_invalid_numeric_values_as_strict_json() -> None:
    parser_output = {
        "pages": [
            {
                "page_number": 1,
                "width": 320,
                "height": 240,
                "unit": "pt",
                "fragments": [
                    {
                        "text": "Bad coordinate",
                        "bbox": {"x": "left", "y": 20, "width": 120, "height": 16, "unit": "pt"},
                        "confidence": 0.95,
                    }
                ],
            }
        ]
    }

    result = convert_uploaded_document(
        filename="phase0-output.json",
        content=json.dumps(parser_output).encode("utf-8"),
    )

    assert result["status"] == "blocked"
    download_text = result["download"]["content"].decode("utf-8")
    assert "NaN" not in download_text
    downloaded = json.loads(download_text)
    assert downloaded["document_ir"]["blocks"][0]["bbox"]["x"] is None
    assert "blocks[0].bbox values must be finite numbers" in downloaded["validation"]["errors"]


def test_poc_http_api_returns_json_safe_download_content() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "filename": "upload.txt",
                "content": "Unstructured OCR fallback text",
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 200
    assert body["status"] == "requires_review"
    assert body["review_items"][0]["warnings"] == [
        "blocks[0].low confidence; block marked requires_review"
    ]
    downloaded = json.loads(body["download"]["content_text"])
    assert downloaded["document_ir"]["document"]["id"] == "upload"


def test_poc_http_api_advertises_base64_artifact_payload_field() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        parser_output = {
            "source_type": "pdf",
            "pages": [
                {
                    "page_number": 1,
                    "width": 320,
                    "height": 240,
                    "unit": "pt",
                    "fragments": [
                        {
                            "text": "PDF text",
                            "bbox": {
                                "x": 10,
                                "y": 20,
                                "width": 120,
                                "height": 16,
                                "unit": "pt",
                            },
                            "confidence": 0.95,
                        }
                    ],
                }
            ],
        }
        payload = json.dumps(
            {
                "filename": "phase0-output.json",
                "content": json.dumps(parser_output),
                "conversion_mode": "pdf_to_word",
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 200
    primary_artifact = body["artifacts"][0]
    assert "content" not in primary_artifact
    assert base64.b64decode(primary_artifact["content_base64"]).startswith(b"PK")
    assert primary_artifact["metadata"]["download"]["field"] == "artifacts[0].content_base64"


def test_poc_http_api_blocks_primary_render_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_docx_renderer(document_ir: dict, output_path: Path) -> None:
        raise ValueError("docx renderer fixture failure")

    monkeypatch.setattr(
        poc_web,
        "render_editable_docx_from_pdf_ir",
        failing_docx_renderer,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        parser_output = {
            "source_type": "pdf",
            "pages": [
                {
                    "page_number": 1,
                    "width": 320,
                    "height": 240,
                    "unit": "pt",
                    "fragments": [
                        {
                            "text": "PDF text",
                            "bbox": {
                                "x": 10,
                                "y": 20,
                                "width": 120,
                                "height": 16,
                                "unit": "pt",
                            },
                            "confidence": 0.95,
                        }
                    ],
                }
            ],
        }
        payload = json.dumps(
            {
                "filename": "phase0-output.json",
                "content": json.dumps(parser_output),
                "conversion_mode": "pdf_to_word",
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 200
    assert body["status"] == "blocked"
    assert body["warnings"] == [
        "conversion mode pdf_to_word selected",
        "pdf_to_word reconstruction preserves editable text structure for review; exact PDF layout, fonts, coordinates, columns, footnotes, and OCR fidelity are not guaranteed",
        "primary artifact generation failed: docx renderer fixture failure",
    ]
    assert [artifact["id"] for artifact in body["artifacts"]] == ["debug-json"]


def test_poc_http_api_scopes_review_actions_by_conversion_role() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "filename": "upload.txt",
                "content": "Unstructured OCR fallback text",
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={
                "Authorization": "Bearer reviewer-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        reviewer_response = connection.getresponse()
        reviewer_body = json.loads(reviewer_response.read().decode("utf-8"))
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={
                "Authorization": "Bearer approver-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        approver_response = connection.getresponse()
        approver_body = json.loads(approver_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert reviewer_response.status == 200
    assert reviewer_body["available_review_actions"] == ["edit"]
    assert approver_response.status == 200
    assert approver_body["available_review_actions"] == ["edit", "approve"]


def test_poc_http_api_reads_local_auth_tokens_from_env_for_review_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "VERIDOC_LOCAL_AUTH_TOKENS",
        "reviewer:env-reviewer=env-reviewer-token",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        status, body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-env-auth",
                revised_text="Lot: SAMPLE-001 env corrected",
            ),
            role_token="env-reviewer-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert status == 202
    assert body["audit_event"]["actor"] == {
        "id": "local-principal:env-reviewer",
        "role": "reviewer",
    }
    assert [event["conversion_id"] for event in store.list_events()] == [
        "conversion-env-auth"
    ]


def test_poc_http_api_excludes_no_auth_approval_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(poc_web.LOCAL_AUTH_TOKENS_ENV, raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "filename": "upload.txt",
                "content": "Unstructured OCR fallback text",
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 200
    assert body["available_review_actions"] == ["edit"]


def test_poc_http_api_accepts_review_action_audit_event() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "audit_event": {
                    "event_type": "conversion_review.action_requested",
                    "action": "edit",
                    "document_id": "phase0-output",
                    "block_id": "block-0001",
                    "source_page": 1,
                    "source_bbox": {
                        "x": 10,
                        "y": 20,
                        "width": 120,
                        "height": 16,
                        "unit": "pt",
                        "origin": "top-left",
                    },
                    "original_text": "Lot: SAMPLE-001",
                    "revised_text": "Lot: SAMPLE-001 corrected",
                    "warnings": ["blocks[0].low confidence; block marked requires_review"],
                }
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/review-events",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 202
    assert body["accepted"] is True
    assert body["audit_event"]["action"] == "edit"
    assert body["audit_event"]["document_id"] == "phase0-output"
    assert body["audit_event"]["block_id"] == "block-0001"
    assert body["audit_event"]["source_bbox"]["origin"] == "top-left"


def test_poc_http_api_persists_review_action_audit_event_server_side() -> None:
    audit_event = _review_audit_event()

    status, body, events = _post_review_audit_event_with_store(audit_event)
    audit_event["revised_text"] = "mutated after request"

    assert status == 202
    assert events == [body["audit_event"]]
    assert events[0]["revised_text"] == "Lot: SAMPLE-001 corrected"


def test_review_audit_event_store_records_hash_chain_metadata() -> None:
    store = ReviewAuditEventStore()

    first = store.record(_review_audit_event(conversion_id="conversion-first"))
    second = store.record(_review_audit_event(conversion_id="conversion-second"))

    assert first["integrity_algorithm"] == "sha256-canonical-json-chain-v1"
    assert first["sequence"] == 1
    assert first["prev_event_hash"] is None
    assert re.fullmatch(r"[0-9a-f]{64}", first["event_hash"])
    assert second["sequence"] == 2
    assert second["prev_event_hash"] == first["event_hash"]
    assert store.verify_integrity() == {"ok": True, "errors": []}


def test_review_audit_event_store_detects_tampered_fixture() -> None:
    store = ReviewAuditEventStore()
    store.record(_review_audit_event())

    store._events[0]["revised_text"] = "tampered after append"  # noqa: SLF001

    assert store.verify_integrity() == {
        "ok": False,
        "errors": ["event[0] hash mismatch"],
    }


def test_review_audit_event_store_detects_tail_truncation() -> None:
    store = ReviewAuditEventStore()
    store.record(_review_audit_event(conversion_id="conversion-first"))
    store.record(_review_audit_event(conversion_id="conversion-second"))

    del store._events[-1]  # noqa: SLF001

    assert store.verify_integrity() == {
        "ok": False,
        "errors": [
            "audit log terminal sequence mismatch",
            "audit log head hash mismatch",
        ],
    }


def test_review_audit_event_store_rejects_append_after_tail_truncation() -> None:
    store = ReviewAuditEventStore()
    store.record(_review_audit_event(conversion_id="conversion-first"))
    store.record(_review_audit_event(conversion_id="conversion-second"))
    del store._events[-1]  # noqa: SLF001

    with pytest.raises(ValueError, match="audit log integrity violation"):
        store.record(_review_audit_event(conversion_id="conversion-third"))

    assert store.verify_integrity() == {
        "ok": False,
        "errors": [
            "audit log terminal sequence mismatch",
            "audit log head hash mismatch",
        ],
    }


def test_review_audit_event_store_rejects_validated_append_after_tail_truncation() -> None:
    store = ReviewAuditEventStore()
    store.record(_review_audit_event(conversion_id="conversion-first"))
    store.record(_review_audit_event(conversion_id="conversion-second"))
    del store._events[-1]  # noqa: SLF001

    def fail_if_validation_runs(
        audit_event: dict[str, object],
        stored_events: list[dict[str, object]],
    ) -> None:
        raise AssertionError("validation should not run for a truncated audit log")

    with pytest.raises(ValueError, match="audit log integrity violation"):
        store.record_validated(
            _review_audit_event(conversion_id="conversion-third"),
            fail_if_validation_runs,
        )

    assert store.verify_integrity() == {
        "ok": False,
        "errors": [
            "audit log terminal sequence mismatch",
            "audit log head hash mismatch",
        ],
    }


def test_job_audit_event_store_detects_tail_truncation() -> None:
    store = JobAuditEventStore()
    store.record(
        {
            "event_type": "job.lifecycle",
            "job_id": "job-first",
            "action": "conversion_completed",
        }
    )
    store.record(
        {
            "event_type": "job.lifecycle",
            "job_id": "job-second",
            "action": "retry_conversion",
        }
    )

    del store._events[-1]  # noqa: SLF001

    assert store.verify_integrity() == {
        "ok": False,
        "errors": [
            "audit log terminal sequence mismatch",
            "audit log head hash mismatch",
        ],
    }


def test_job_audit_event_store_rejects_append_after_tail_truncation() -> None:
    store = JobAuditEventStore()
    store.record(
        {
            "event_type": "job.lifecycle",
            "job_id": "job-first",
            "action": "conversion_completed",
        }
    )
    store.record(
        {
            "event_type": "job.lifecycle",
            "job_id": "job-second",
            "action": "retry_conversion",
        }
    )
    del store._events[-1]  # noqa: SLF001

    with pytest.raises(ValueError, match="audit log integrity violation"):
        store.record(
            {
                "event_type": "job.lifecycle",
                "job_id": "job-third",
                "action": "retry_conversion",
            }
        )

    assert store.verify_integrity() == {
        "ok": False,
        "errors": [
            "audit log terminal sequence mismatch",
            "audit log head hash mismatch",
        ],
    }


def test_poc_http_api_lists_server_side_review_action_audit_events() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.review_event_store = ReviewAuditEventStore()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        audit_event = _review_audit_event()
        payload = json.dumps({"audit_event": audit_event}).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/review-events",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        post_response = connection.getresponse()
        post_body = json.loads(post_response.read().decode("utf-8"))
        connection.request("GET", "/api/review-events")
        list_response = connection.getresponse()
        list_body = json.loads(list_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert post_response.status == 202
    assert list_response.status == 200
    assert list_body == {"review_events": [post_body["audit_event"]]}


def test_poc_http_api_filters_server_side_review_action_audit_events() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.review_event_store = ReviewAuditEventStore()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        first_status, first_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(conversion_id="conversion-current"),
            role_token=None,
        )
        second_status, _second_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                block_id="block-0002",
                conversion_id="conversion-other",
            ),
            role_token=None,
        )
        connection.request(
            "GET",
            "/api/review-events?document_id=phase0-output"
            "&block_id=block-0001&conversion_id=conversion-current",
        )
        list_response = connection.getresponse()
        list_body = json.loads(list_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert first_status == 202
    assert second_status == 202
    assert list_response.status == 200
    assert list_body == {"review_events": [first_body["audit_event"]]}


def test_poc_http_api_filters_review_action_audit_events_by_action() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.review_event_store = ReviewAuditEventStore()
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        edit_status, edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-current",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="reviewer-token",
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                conversion_id="conversion-current",
                original_text="Lot: SAMPLE-001 corrected",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="admin-token",
        )
        connection.request(
            "GET",
            "/api/review-events?action=approve",
            headers={"Authorization": "Bearer viewer-token"},
        )
        list_response = connection.getresponse()
        list_body = json.loads(list_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert edit_status == 202
    assert approve_status == 202
    assert edit_body["audit_event"]["action"] == "edit"
    assert list_response.status == 200
    assert list_body == {"review_events": [approve_body["audit_event"]]}


def test_poc_http_api_filters_review_events_before_copying_payloads() -> None:
    class CopyForbiddenText(str):
        def __deepcopy__(self, _memo: dict[object, object]) -> object:
            raise AssertionError("unrelated event payload was copied")

    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    unrelated_event = {
        **_review_audit_event(
            document_id="unrelated-document",
            block_id="block-unrelated",
            conversion_id="conversion-unrelated",
        ),
        "original_text": CopyForbiddenText(),
        "revised_text": CopyForbiddenText(),
        "actor": {"id": "local-principal:reviewer", "role": "reviewer"},
        "occurred_at": "2026-06-27T00:00:00Z",
    }
    with store._lock:
        unrelated_event["integrity_algorithm"] = poc_web.AUDIT_INTEGRITY_ALGORITHM
        unrelated_event["sequence"] = 1
        unrelated_event["prev_event_hash"] = None
        unrelated_event["event_hash"] = poc_web._audit_event_hash(unrelated_event)
        store._events.append(unrelated_event)
        store._integrity_checkpoint = poc_web._audit_event_integrity_checkpoint(  # noqa: SLF001
            store._events
        )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        first_status, first_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(conversion_id="conversion-current"),
            role_token=None,
        )
        connection.request(
            "GET",
            "/api/review-events?document_id=phase0-output"
            "&block_id=block-0001&conversion_id=conversion-current",
        )
        list_response = connection.getresponse()
        list_body = json.loads(list_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert first_status == 202
    assert list_response.status == 200
    assert list_body == {"review_events": [first_body["audit_event"]]}


def test_poc_http_api_does_not_persist_rejected_review_action_audit_event() -> None:
    audit_event = _review_audit_event(source_bbox=_review_bbox(origin="bottom-left"))

    status, body, events = _post_review_audit_event_with_store(audit_event)

    assert status == 400
    assert body == {
        "error": "invalid_review_event",
        "message": "audit_event.source_bbox.origin must be top-left",
    }
    assert events == []


def test_poc_http_api_rejects_review_approve_without_approver_role() -> None:
    audit_event = _review_audit_event(action="approve", revised_text="Lot: SAMPLE-001")

    status, body, events = _post_review_audit_event_with_store(
        audit_event,
        role_token="reviewer-token",
    )

    assert status == 403
    assert body == {
        "error": "forbidden",
        "message": "role reviewer cannot perform review_approve",
    }
    assert events == []


def test_poc_http_api_rejects_same_actor_approving_prior_review_edit() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"Authorization": "Bearer admin-token", "Content-Type": "application/json"}

        edit_payload = json.dumps({"audit_event": _review_audit_event()}).encode("utf-8")
        connection.request(
            "POST",
            "/api/review-events",
            body=edit_payload,
            headers={**headers, "Content-Length": str(len(edit_payload))},
        )
        edit_response = connection.getresponse()
        edit_body = json.loads(edit_response.read().decode("utf-8"))

        approve_payload = json.dumps(
            {
                "audit_event": _review_audit_event(
                    action="approve",
                    original_text="Lot: SAMPLE-001 corrected",
                    revised_text="Lot: SAMPLE-001 corrected",
                )
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/review-events",
            body=approve_payload,
            headers={**headers, "Content-Length": str(len(approve_payload))},
        )
        approve_response = connection.getresponse()
        approve_body = json.loads(approve_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert edit_response.status == 202
    assert edit_body["audit_event"]["actor"]["id"] == "local-principal:admin"
    assert edit_body["audit_event"]["actor"]["role"] == "admin"
    assert edit_body["audit_event"]["occurred_at"].endswith("Z")
    assert approve_response.status == 409
    assert approve_body == {
        "error": "review_conflict",
        "message": "review approval must be performed by a different actor",
    }
    assert [event["action"] for event in store.list_events()] == ["edit"]


def test_poc_http_api_scans_all_prior_review_edits_before_approval() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        admin_edit_status, _admin_edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(revised_text="Lot: SAMPLE-001 admin edit"),
            role_token="admin-token",
        )
        reviewer_edit_status, reviewer_edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                original_text="Lot: SAMPLE-001 admin edit",
                revised_text="Lot: SAMPLE-001 reviewer edit",
            ),
            role_token="reviewer-token",
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                original_text="Lot: SAMPLE-001 reviewer edit",
                revised_text="Lot: SAMPLE-001 reviewer edit",
            ),
            role_token="admin-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert admin_edit_status == 202
    assert reviewer_edit_status == 202
    assert reviewer_edit_body["audit_event"]["actor"]["id"] == "local-principal:reviewer"
    assert approve_status == 409
    assert approve_body == {
        "error": "review_conflict",
        "message": "review approval must be performed by a different actor",
    }
    assert [event["action"] for event in store.list_events()] == ["edit", "edit"]


def test_poc_http_api_scopes_approval_history_to_conversion_id() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        old_edit_status, _old_edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-old",
                revised_text="Lot: SAMPLE-001 old admin edit",
            ),
            role_token="admin-token",
        )
        new_edit_status, _new_edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-new",
                revised_text="Lot: SAMPLE-001 reviewer edit",
            ),
            role_token="reviewer-token",
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                conversion_id="conversion-new",
                original_text="Lot: SAMPLE-001 reviewer edit",
                revised_text="Lot: SAMPLE-001 reviewer edit",
            ),
            role_token="admin-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert old_edit_status == 202
    assert new_edit_status == 202
    assert approve_status == 202
    assert approve_body["audit_event"]["conversion_id"] == "conversion-new"
    assert [
        (event["action"], event["conversion_id"])
        for event in store.list_events()
    ] == [
        ("edit", "conversion-old"),
        ("edit", "conversion-new"),
        ("approve", "conversion-new"),
    ]


def test_poc_http_api_ignores_older_conversion_after_current_edit_match() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        old_edit_status, _old_edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-old",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="reviewer-token",
        )
        current_edit_status, _current_edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-current",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="reviewer-token",
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                conversion_id="conversion-current",
                original_text="Lot: SAMPLE-001",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="admin-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert old_edit_status == 202
    assert current_edit_status == 202
    assert approve_status == 202
    assert approve_body["audit_event"]["conversion_id"] == "conversion-current"
    assert [
        (event["action"], event["conversion_id"])
        for event in store.list_events()
    ] == [
        ("edit", "conversion-old"),
        ("edit", "conversion-current"),
        ("approve", "conversion-current"),
    ]


def test_poc_http_api_defers_cross_conversion_reuse_until_current_edit_known() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        current_edit_status, _current_edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-current",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="reviewer-token",
        )
        other_edit_status, _other_edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-other",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="reviewer-token",
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                conversion_id="conversion-current",
                original_text="Lot: SAMPLE-001",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="admin-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert current_edit_status == 202
    assert other_edit_status == 202
    assert approve_status == 202
    assert approve_body["audit_event"]["conversion_id"] == "conversion-current"
    assert [
        (event["action"], event["conversion_id"])
        for event in store.list_events()
    ] == [
        ("edit", "conversion-current"),
        ("edit", "conversion-other"),
        ("approve", "conversion-current"),
    ]


def test_poc_http_api_rejects_approval_with_unbound_conversion_id() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        edit_status, _edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-saved",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="reviewer-token",
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                conversion_id="conversion-forged",
                original_text="Lot: SAMPLE-001",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="admin-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert edit_status == 202
    assert approve_status == 409
    assert approve_body == {
        "error": "review_conflict",
        "message": "review approval must target latest edited text",
    }
    assert [event["action"] for event in store.list_events()] == ["edit"]


def test_poc_http_api_allows_unchanged_approval_with_other_scoped_edit() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        edit_status, _edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-old",
                revised_text="Lot: SAMPLE-001 old edit",
            ),
            role_token="reviewer-token",
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                conversion_id="conversion-current",
                original_text="Lot: SAMPLE-001",
                revised_text="Lot: SAMPLE-001",
            ),
            role_token="admin-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert edit_status == 202
    assert approve_status == 202
    assert approve_body["audit_event"]["conversion_id"] == "conversion-current"
    assert [
        (event["action"], event["conversion_id"])
        for event in store.list_events()
    ] == [
        ("edit", "conversion-old"),
        ("approve", "conversion-current"),
    ]


def test_poc_http_api_allows_unchanged_approval_matching_older_conversion_edit() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        old_edit_status, _old_edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-old",
                revised_text="Lot: SAMPLE-001",
            ),
            role_token="reviewer-token",
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                conversion_id="conversion-current",
                original_text="Lot: SAMPLE-001",
                revised_text="Lot: SAMPLE-001",
            ),
            role_token="admin-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert old_edit_status == 202
    assert approve_status == 202
    assert approve_body["audit_event"]["conversion_id"] == "conversion-current"
    assert [
        (event["action"], event["conversion_id"])
        for event in store.list_events()
    ] == [
        ("edit", "conversion-old"),
        ("approve", "conversion-current"),
    ]


def test_poc_http_api_checks_legacy_edit_when_approval_has_conversion_id() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        legacy_edit_status, _legacy_edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(revised_text="Lot: SAMPLE-001 legacy edit"),
            role_token="reviewer-token",
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                conversion_id="conversion-current",
                original_text="Lot: SAMPLE-001",
                revised_text="Lot: SAMPLE-001",
            ),
            role_token="admin-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert legacy_edit_status == 202
    assert approve_status == 409
    assert approve_body == {
        "error": "review_conflict",
        "message": "review approval must target latest edited text",
    }
    assert [event["action"] for event in store.list_events()] == ["edit"]


def test_poc_http_api_rejects_approval_for_stale_review_text() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        edit_status, _edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-current",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="reviewer-token",
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                conversion_id="conversion-current",
                original_text="Lot: SAMPLE-001",
                revised_text="Lot: SAMPLE-001",
            ),
            role_token="admin-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert edit_status == 202
    assert approve_status == 409
    assert approve_body == {
        "error": "review_conflict",
        "message": "review approval must target latest edited text",
    }
    assert [event["action"] for event in store.list_events()] == ["edit"]


def test_poc_http_api_rejects_changed_approval_without_saved_edit() -> None:
    audit_event = _review_audit_event(
        action="approve",
        revised_text="Lot: SAMPLE-001 corrected",
    )

    status, body, events = _post_review_audit_event_with_store(audit_event)

    assert status == 409
    assert body == {
        "error": "review_conflict",
        "message": "review approval must target latest edited text",
    }
    assert events == []


def test_poc_http_api_allows_approval_with_revised_text_target() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        edit_status, _edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-current",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="reviewer-token",
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                conversion_id="conversion-current",
                original_text="Lot: SAMPLE-001",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="admin-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert edit_status == 202
    assert approve_status == 202
    assert approve_body["audit_event"]["original_text"] == "Lot: SAMPLE-001"
    assert approve_body["audit_event"]["revised_text"] == "Lot: SAMPLE-001 corrected"
    assert [event["action"] for event in store.list_events()] == ["edit", "approve"]


def test_poc_http_api_uses_principal_id_for_review_separation() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = {
        "same-reviewer-token": {"role": "reviewer", "principal_id": "same-person"},
        "same-approver-token": {"role": "approver", "principal_id": "same-person"},
    }
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        edit_status, edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-current",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="same-reviewer-token",
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                conversion_id="conversion-current",
                original_text="Lot: SAMPLE-001 corrected",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="same-approver-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert edit_status == 202
    assert edit_body["audit_event"]["actor"]["id"] == "local-principal:same-person"
    assert approve_status == 409
    assert approve_body == {
        "error": "review_conflict",
        "message": "review approval must be performed by a different actor",
    }
    assert [event["action"] for event in store.list_events()] == ["edit"]


def test_poc_http_api_rejects_same_principal_approval_with_forged_conversion_id() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = {
        "same-reviewer-token": {"role": "reviewer", "principal_id": "same-person"},
        "same-approver-token": {"role": "approver", "principal_id": "same-person"},
    }
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        edit_status, _edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                conversion_id="conversion-saved",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="same-reviewer-token",
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                conversion_id="conversion-forged",
                original_text="Lot: SAMPLE-001 corrected",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token="same-approver-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert edit_status == 202
    assert approve_status == 409
    assert approve_body == {
        "error": "review_conflict",
        "message": "review approval must be performed by a different actor",
    }
    assert [event["action"] for event in store.list_events()] == ["edit"]


def test_review_event_store_validates_and_records_under_same_lock() -> None:
    store = ReviewAuditEventStore()
    store.record(
        {
            **_review_audit_event(revised_text="Lot: SAMPLE-001 reviewer edit"),
            "actor": {"id": "local-principal:reviewer", "role": "reviewer"},
            "occurred_at": "2026-06-27T00:00:00Z",
        }
    )
    approval_event = {
        **_review_audit_event(
            action="approve",
            original_text="Lot: SAMPLE-001 reviewer edit",
            revised_text="Lot: SAMPLE-001 reviewer edit",
        ),
        "actor": {"id": "local-principal:admin", "role": "admin"},
        "occurred_at": "2026-06-27T00:00:01Z",
    }
    concurrent_admin_edit = {
        **_review_audit_event(
            original_text="Lot: SAMPLE-001 reviewer edit",
            revised_text="Lot: SAMPLE-001 admin edit",
        ),
        "actor": {"id": "local-principal:admin", "role": "admin"},
        "occurred_at": "2026-06-27T00:00:02Z",
    }
    validator_entered = Event()
    release_validator = Event()
    edit_started = Event()
    thread_errors: list[BaseException] = []

    def slow_validate(
        audit_event: dict[str, object],
        stored_events: list[dict[str, object]],
    ) -> None:
        validator_entered.set()
        assert [
            event["actor"]["id"]  # type: ignore[index]
            for event in stored_events
        ] == ["local-principal:reviewer"]
        assert release_validator.wait(timeout=5)
        poc_web._validate_review_workflow_event(audit_event, stored_events)

    def record_approval() -> None:
        try:
            store.record_validated(approval_event, slow_validate)
        except BaseException as exc:  # pragma: no cover - surfaced below
            thread_errors.append(exc)

    def record_concurrent_edit() -> None:
        try:
            edit_started.set()
            store.record(concurrent_admin_edit)
        except BaseException as exc:  # pragma: no cover - surfaced below
            thread_errors.append(exc)

    approval_thread = Thread(target=record_approval)
    approval_thread.start()
    assert validator_entered.wait(timeout=5)

    edit_thread = Thread(target=record_concurrent_edit)
    edit_thread.start()
    assert edit_started.wait(timeout=5)
    edit_thread.join(timeout=0.05)
    assert edit_thread.is_alive()

    release_validator.set()
    approval_thread.join(timeout=5)
    edit_thread.join(timeout=5)

    assert not approval_thread.is_alive()
    assert not edit_thread.is_alive()
    assert thread_errors == []
    assert [
        (event["action"], event["actor"]["id"])  # type: ignore[index]
        for event in store.list_events()
    ] == [
        ("edit", "local-principal:reviewer"),
        ("approve", "local-principal:admin"),
        ("edit", "local-principal:admin"),
    ]


def test_poc_http_api_rejects_no_auth_review_approval_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(poc_web.LOCAL_AUTH_TOKENS_ENV, raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        edit_status, edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(revised_text="Lot: SAMPLE-001 corrected"),
            role_token=None,
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                original_text="Lot: SAMPLE-001 corrected",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            role_token=None,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert edit_status == 202
    assert edit_body["audit_event"]["actor"] == {"id": None, "role": None}
    assert approve_status == 403
    assert approve_body == {
        "error": "forbidden",
        "message": "review approval requires authenticated actor identity",
    }
    assert [event["action"] for event in store.list_events()] == ["edit"]


def test_poc_http_api_rejects_no_auth_approval_before_workflow_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(poc_web.LOCAL_AUTH_TOKENS_ENV, raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        edit_status, _edit_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(revised_text="Lot: SAMPLE-001 corrected"),
            role_token=None,
        )
        approve_status, approve_body = _post_review_event_on_connection(
            connection,
            _review_audit_event(
                action="approve",
                original_text="Lot: SAMPLE-001",
                revised_text="Lot: SAMPLE-001",
            ),
            role_token=None,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert edit_status == 202
    assert approve_status == 403
    assert approve_body == {
        "error": "forbidden",
        "message": "review approval requires authenticated actor identity",
    }
    assert [event["action"] for event in store.list_events()] == ["edit"]


def test_poc_http_api_requires_configured_local_auth_token_for_review_events() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(_review_audit_event()).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/review-events",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 401
    assert body == {
        "error": "auth_required",
        "message": "Authorization bearer token is required",
    }
    assert store.list_events() == []


def test_poc_http_api_rejects_role_token_without_principal_id() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = {"reviewer-token": "reviewer"}
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        status, body = _post_review_event_on_connection(
            connection,
            _review_audit_event(),
            role_token="reviewer-token",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert status == 401
    assert body == {
        "error": "auth_required",
        "message": "Authorization bearer token is invalid",
    }
    assert store.list_events() == []


def test_poc_http_api_authenticates_review_events_before_parsing_payload() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = b"{not valid json"
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/review-events",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 401
    assert body == {
        "error": "auth_required",
        "message": "Authorization bearer token is required",
    }
    assert store.list_events() == []


def test_poc_http_api_rejects_read_only_review_role_before_parsing_payload() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = b"{not valid json"
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/review-events",
            body=payload,
            headers={
                "Authorization": "Bearer viewer-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 403
    assert body == {
        "error": "forbidden",
        "message": "role viewer cannot perform review_edit",
    }
    assert store.list_events() == []


def test_poc_http_api_rejects_viewer_review_edit() -> None:
    audit_event = _review_audit_event()

    status, body, events = _post_review_audit_event_with_store(
        audit_event,
        role_token="viewer-token",
    )

    assert status == 403
    assert body == {
        "error": "forbidden",
        "message": "role viewer cannot perform review_edit",
    }
    assert events == []


def test_poc_http_api_rejects_malformed_review_action_audit_event() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "audit_event": {
                    "event_type": "conversion_review.action_requested",
                    "action": "approve",
                    "document_id": "phase0-output",
                    "block_id": "block-0001",
                    "source_page": 1,
                    "source_bbox": {
                        "x": 10,
                        "y": 20,
                        "width": 120,
                        "height": 16,
                        "unit": "pt",
                        "origin": "bottom-left",
                    },
                    "original_text": "Lot: SAMPLE-001",
                    "revised_text": "Lot: SAMPLE-001",
                    "warnings": [],
                }
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/review-events",
            body=payload,
            headers={
                "Authorization": "Bearer approver-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body == {
        "error": "invalid_review_event",
        "message": "audit_event.source_bbox.origin must be top-left",
    }


def test_poc_http_api_accepts_approve_review_action_without_duplicate_revised_text() -> None:
    audit_event = _review_audit_event(
        action="approve",
        original_text="Lot: SAMPLE-001",
    )
    del audit_event["revised_text"]

    status, body = _post_review_audit_event(audit_event)

    assert status == 202
    assert body["audit_event"]["action"] == "approve"
    assert body["audit_event"]["original_text"] == "Lot: SAMPLE-001"
    assert body["audit_event"]["revised_text"] == "Lot: SAMPLE-001"


def test_poc_http_api_accepts_review_action_without_source_bbox() -> None:
    audit_event = _review_audit_event(source_bbox=None)

    status, body = _post_review_audit_event(audit_event)

    assert status == 202
    assert body["audit_event"]["source_bbox"] is None


def test_poc_http_api_accepts_large_review_edit_event_above_upload_sized_cap() -> None:
    extracted_text_bytes = poc_web.MAX_UPLOAD_BYTES + (128 * 1024)
    original_text = '"' * extracted_text_bytes
    revised_text = '"' * extracted_text_bytes
    audit_event = _review_audit_event(
        action="edit",
        source_bbox=None,
        original_text=original_text,
        revised_text=revised_text,
    )
    payload_size = len(json.dumps({"audit_event": audit_event}).encode("utf-8"))
    upload_sized_review_event_cap = (poc_web.MAX_UPLOAD_BYTES * 4) + (64 * 1024)

    assert len(original_text.encode("utf-8")) > poc_web.MAX_UPLOAD_BYTES
    assert len(original_text.encode("utf-8")) <= poc_web.MAX_REVIEW_EVENT_TEXT_BYTES
    assert payload_size > upload_sized_review_event_cap
    assert payload_size < poc_web.MAX_REVIEW_EVENT_REQUEST_BYTES

    status, body = _post_review_audit_event(audit_event)

    assert status == 202
    assert body["audit_event"]["original_text"] == original_text
    assert body["audit_event"]["revised_text"] == revised_text


def test_poc_http_api_rejects_review_text_over_extracted_text_cap(monkeypatch) -> None:
    monkeypatch.setattr(poc_web, "MAX_REVIEW_EVENT_TEXT_BYTES", 16)
    audit_event = _review_audit_event(
        action="edit",
        source_bbox=None,
        original_text="x" * 17,
        revised_text="corrected",
    )

    status, body, events = _post_review_audit_event_with_store(audit_event)

    assert status == 400
    assert body == {
        "error": "invalid_review_event",
        "message": "audit_event.original_text exceeds review text limit",
    }
    assert events == []


def test_poc_http_api_normalizes_review_action_source_bbox_strings() -> None:
    audit_event = _review_audit_event(
        source_bbox=_review_bbox(unit="pt ", origin=" top-left")
    )

    status, body = _post_review_audit_event(audit_event)

    assert status == 202
    assert body["audit_event"]["source_bbox"] == {
        "x": 10,
        "y": 20,
        "width": 120,
        "height": 16,
        "unit": "pt",
        "origin": "top-left",
    }


def test_poc_http_api_rejects_huge_review_bbox_integer_without_crashing() -> None:
    audit_event = _review_audit_event(source_bbox=_review_bbox(x=10**400))

    status, body, events = _post_review_audit_event_with_store(audit_event)

    assert status == 400
    assert body == {
        "error": "invalid_review_event",
        "message": "audit_event.source_bbox.x must be finite",
    }
    assert events == []


def _review_audit_event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "event_type": "conversion_review.action_requested",
        "action": "edit",
        "document_id": "phase0-output",
        "block_id": "block-0001",
        "source_page": 1,
        "source_bbox": {
            "x": 10,
            "y": 20,
            "width": 120,
            "height": 16,
            "unit": "pt",
            "origin": "top-left",
        },
        "original_text": "Lot: SAMPLE-001",
        "revised_text": "Lot: SAMPLE-001 corrected",
        "warnings": [],
    }
    event.update(overrides)
    return event


def _review_bbox(**overrides: object) -> dict[str, object]:
    bbox: dict[str, object] = {
        "x": 10,
        "y": 20,
        "width": 120,
        "height": 16,
        "unit": "pt",
        "origin": "top-left",
    }
    bbox.update(overrides)
    return bbox


def _post_review_audit_event(
    audit_event: dict[str, object],
    *,
    role_token: Optional[str] = "auto",
) -> tuple[int, dict[str, object]]:
    status, body, _events = _post_review_audit_event_with_store(
        audit_event,
        role_token=role_token,
    )
    return status, body


def _post_review_event_on_connection(
    connection: HTTPConnection,
    audit_event: dict[str, object],
    *,
    role_token: Optional[str],
) -> tuple[int, dict[str, object]]:
    payload = json.dumps({"audit_event": audit_event}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
    }
    if role_token is not None:
        headers["Authorization"] = f"Bearer {role_token}"
    connection.request(
        "POST",
        "/api/review-events",
        body=payload,
        headers=headers,
    )
    response = connection.getresponse()
    body = json.loads(response.read().decode("utf-8"))
    return response.status, body


def _post_review_audit_event_with_store(
    audit_event: dict[str, object],
    *,
    role_token: Optional[str] = "auto",
) -> tuple[int, dict[str, object], list[dict[str, object]]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    store = ReviewAuditEventStore()
    server.review_event_store = store
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps({"audit_event": audit_event}).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        selected_token = role_token
        if selected_token == "auto" and audit_event.get("action") != "approve":
            selected_token = "reviewer-token"
        if selected_token == "auto" and audit_event.get("action") == "approve":
            selected_token = "approver-token"
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
        }
        if selected_token:
            headers["Authorization"] = f"Bearer {selected_token}"
        connection.request(
            "POST",
            "/api/review-events",
            body=payload,
            headers=headers,
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)
    return response.status, body, store.list_events()


def _local_auth_tokens() -> dict[str, dict[str, str]]:
    return {
        "viewer-token": {"role": "viewer", "principal_id": "viewer"},
        "reviewer-token": {"role": "reviewer", "principal_id": "reviewer"},
        "approver-token": {"role": "approver", "principal_id": "approver"},
        "admin-token": {"role": "admin", "principal_id": "admin"},
    }


@pytest.mark.parametrize(
    ("audit_event", "message"),
    [
        (
            _review_audit_event(source_page=True),
            "audit_event.source_page must be a positive integer",
        ),
        (
            _review_audit_event(action={"name": "approve"}),
            "audit_event.action is unsupported",
        ),
        (
            _review_audit_event(document_id={"id": "phase0"}),
            "audit_event.document_id is required",
        ),
        (
            _review_audit_event(block_id=True),
            "audit_event.block_id is required",
        ),
        (
            _review_audit_event(source_bbox=_review_bbox(unit="em")),
            "audit_event.source_bbox.unit must be one of mm, pt, px",
        ),
        (
            _review_audit_event(source_bbox=_review_bbox(x=-1)),
            "audit_event.source_bbox origin coordinates must be non-negative",
        ),
        (
            _review_audit_event(source_bbox=_review_bbox(y=-1)),
            "audit_event.source_bbox origin coordinates must be non-negative",
        ),
    ],
)
def test_poc_http_api_rejects_review_action_audit_event_boundary_drift(
    audit_event: dict[str, object],
    message: str,
) -> None:
    status, body = _post_review_audit_event(audit_event)

    assert status == 400
    assert body == {"error": "invalid_review_event", "message": message}


def test_poc_http_api_creates_idempotent_conversion_job() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "idempotency_key": "upload-1",
                "filename": "batch-record.pdf",
                "mode": "standard",
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/jobs",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        created_response = connection.getresponse()
        created = json.loads(created_response.read().decode("utf-8"))
        connection.request(
            "POST",
            "/api/jobs",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        duplicate_response = connection.getresponse()
        duplicate = json.loads(duplicate_response.read().decode("utf-8"))
        job_id = created["job"]["job_id"]
        connection.request("GET", f"/api/jobs/{job_id}")
        status_response = connection.getresponse()
        status = json.loads(status_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert created_response.status == 202
    assert duplicate_response.status == 202
    assert duplicate["job"]["job_id"] == job_id
    assert status_response.status == 200
    assert status["job"]["status"] == "queued"
    assert status["job"]["mode"] == "standard"


def test_poc_http_api_stores_uploaded_job_source_before_returning_reference() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uploaded_content = b"%PDF-1.7\nqueued source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    try:
        payload = json.dumps(
            {
                "idempotency_key": "upload-with-source",
                "filename": "batch-record.pdf",
                "content_type": "application/pdf",
                "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
                "size_bytes": len(uploaded_content),
                "source_sha256": source_sha256,
                "mode": "standard",
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/jobs",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        job = server.job_queue.get_job(body["job"]["job_id"])
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 202
    assert body["job"]["hashes"]["source_sha256"] == source_sha256
    assert body["job"]["hash_verification"]["source"] == {
        "status": "recorded",
        "sha256": source_sha256,
    }
    assert "source" not in body["job"]
    assert job.source == {
        "filename": "batch-record.pdf",
        "content_type": "application/pdf",
        "size_bytes": len(uploaded_content),
        "sha256": source_sha256,
        "content": uploaded_content,
    }
    assert server.job_event_store.list_events(filters={"job_id": job.job_id}) == []


def test_poc_http_api_rolls_back_desktop_upload_when_create_audit_fails() -> None:
    class RejectingJobAuditEventStore(JobAuditEventStore):
        def record_once(self, event: dict[str, object], *, dedupe: dict[str, object]) -> dict[str, object]:
            raise ValueError("audit log unavailable")

    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = RejectingJobAuditEventStore()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uploaded_content = b"%PDF-1.7\nqueued source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    try:
        payload = json.dumps(
            {
                "idempotency_key": "desktop-upload-audit-fails",
                "filename": "batch-record.pdf",
                "content_type": "application/pdf",
                "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
                "size_bytes": len(uploaded_content),
                "source_sha256": source_sha256,
                "mode": "standard",
                "desktop_upload_audit": True,
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/jobs",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        jobs = server.job_queue.list_jobs()
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body == {"error": "invalid_job_request", "message": "audit log unavailable"}
    assert jobs == []


def test_poc_http_api_rolls_back_sourceless_desktop_upload_audit_create() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    retry_content = b"%PDF-1.7\nqueued source"
    retry_sha256 = hashlib.sha256(retry_content).hexdigest()
    try:
        sourceless_payload = json.dumps(
            {
                "idempotency_key": "desktop-upload-sourceless",
                "filename": "batch-record.pdf",
                "mode": "standard",
                "desktop_upload_audit": True,
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/jobs",
            body=sourceless_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(sourceless_payload)),
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        jobs_after_rejection = server.job_queue.list_jobs()

        retry_payload = json.dumps(
            {
                "idempotency_key": "desktop-upload-sourceless",
                "filename": "batch-record.pdf",
                "content_type": "application/pdf",
                "content_base64": base64.b64encode(retry_content).decode("ascii"),
                "size_bytes": len(retry_content),
                "source_sha256": retry_sha256,
                "mode": "standard",
                "desktop_upload_audit": True,
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/jobs",
            body=retry_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(retry_payload)),
            },
        )
        retry_response = connection.getresponse()
        retry_body = json.loads(retry_response.read().decode("utf-8"))
        events = server.job_event_store.list_events()
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body == {
        "error": "invalid_job_request",
        "message": "desktop_upload requires stored job source",
    }
    assert jobs_after_rejection == []
    assert retry_response.status == 202
    assert retry_body["job"]["hashes"]["source_sha256"] == retry_sha256
    assert [event["action"] for event in events] == ["desktop_upload"]


def test_poc_http_api_does_not_expose_desktop_upload_job_before_create_audit() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    audit_started = Event()
    release_audit = Event()

    class RacingJobAuditEventStore(JobAuditEventStore):
        started_job_id: Optional[str] = None
        audit_job_id: Optional[str] = None

        def record_once(self, event: dict[str, object], *, dedupe: dict[str, object]) -> dict[str, object]:
            self.audit_job_id = str(event["job_id"])
            audit_started.set()
            release_audit.wait(timeout=5)
            started = server.job_queue.start_next_job()
            self.started_job_id = started.job_id if started is not None else None
            raise ValueError("audit log unavailable")

    audit_store = RacingJobAuditEventStore()
    server.job_event_store = audit_store
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uploaded_content = b"%PDF-1.7\nqueued source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    try:
        payload = json.dumps(
            {
                "idempotency_key": "desktop-upload-audit-race",
                "filename": "batch-record.pdf",
                "content_type": "application/pdf",
                "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
                "size_bytes": len(uploaded_content),
                "source_sha256": source_sha256,
                "mode": "standard",
                "desktop_upload_audit": True,
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Content-Length": str(len(payload))}
        result: dict[str, object] = {}

        def post_upload() -> None:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("POST", "/api/jobs", body=payload, headers=headers)
                response = connection.getresponse()
                result["status"] = response.status
                result["body"] = json.loads(response.read().decode("utf-8"))
            finally:
                connection.close()

        worker = Thread(target=post_upload)
        worker.start()
        assert audit_started.wait(timeout=5)

        list_connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        list_connection.request("GET", "/api/jobs")
        list_response = list_connection.getresponse()
        list_body = json.loads(list_response.read().decode("utf-8"))
        list_connection.close()

        detail_connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        detail_connection.request("GET", f"/api/jobs/{audit_store.audit_job_id}")
        detail_response = detail_connection.getresponse()
        detail_body = json.loads(detail_response.read().decode("utf-8"))
        detail_connection.close()

        replay_payload = json.dumps(
            {
                "idempotency_key": "desktop-upload-audit-race",
                "filename": "batch-record.pdf",
                "content_type": "application/pdf",
                "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
                "size_bytes": len(uploaded_content),
                "source_sha256": source_sha256,
                "mode": "standard",
            }
        ).encode("utf-8")
        replay_connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        replay_connection.request(
            "POST",
            "/api/jobs",
            body=replay_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(replay_payload)),
            },
        )
        replay_response = replay_connection.getresponse()
        replay_body = json.loads(replay_response.read().decode("utf-8"))
        replay_connection.close()

        release_audit.set()
        worker.join(timeout=10)
        jobs = server.job_queue.list_jobs()
    finally:
        release_audit.set()
        server.shutdown()
        thread.join(timeout=5)

    assert result == {
        "status": 400,
        "body": {"error": "invalid_job_request", "message": "audit log unavailable"},
    }
    assert list_response.status == 200
    assert list_body["jobs"] == []
    assert detail_response.status == 404
    assert detail_body == {"error": "job_not_found"}
    assert replay_response.status == 409
    assert replay_body == {"error": "job_conflict", "message": "job creation pending"}
    assert audit_store.started_job_id is None
    assert jobs == []


def test_poc_http_api_rejects_late_idempotent_desktop_upload_audit_create() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uploaded_content = b"%PDF-1.7\nqueued source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    request = {
        "idempotency_key": "desktop-upload-audit-late-replay",
        "filename": "batch-record.pdf",
        "content_type": "application/pdf",
        "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
        "size_bytes": len(uploaded_content),
        "source_sha256": source_sha256,
        "mode": "standard",
    }
    try:
        initial_payload = json.dumps(request).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/jobs",
            body=initial_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(initial_payload)),
            },
        )
        initial_response = connection.getresponse()
        initial_body = json.loads(initial_response.read().decode("utf-8"))
        replay_request = dict(request)
        replay_request["desktop_upload_audit"] = True
        replay_payload = json.dumps(replay_request).encode("utf-8")
        connection.request(
            "POST",
            "/api/jobs",
            body=replay_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(replay_payload)),
            },
        )
        replay_response = connection.getresponse()
        replay_body = json.loads(replay_response.read().decode("utf-8"))
        events = server.job_event_store.list_events(filters={"job_id": initial_body["job"]["job_id"]})
        jobs = server.job_queue.list_jobs()
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert initial_response.status == 202
    assert replay_response.status == 400
    assert replay_body == {
        "error": "invalid_job_request",
        "message": "desktop_upload audit cannot be added after idempotent job creation",
    }
    assert events == []
    assert len(jobs) == 1
    assert jobs[0].job_id == initial_body["job"]["job_id"]
    assert jobs[0].status == "queued"


@pytest.mark.parametrize("job_state", ["queued_not_pending", "running", "succeeded"])
def test_poc_http_api_rejects_existing_idempotent_desktop_upload_audit_create(
    job_state: str,
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    uploaded_content = b"%PDF-1.7\nqueued source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    request = {
        "idempotency_key": f"desktop-upload-existing-{job_state}",
        "filename": "batch-record.pdf",
        "content_type": "application/pdf",
        "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
        "size_bytes": len(uploaded_content),
        "source_sha256": source_sha256,
        "mode": "standard",
    }
    job = server.job_queue.create_job(
        idempotency_key=request["idempotency_key"],
        filename=request["filename"],
        mode=request["mode"],
        source={
            "filename": request["filename"],
            "content_type": request["content_type"],
            "size_bytes": request["size_bytes"],
            "sha256": request["source_sha256"],
            "content": uploaded_content,
        },
        enqueue=job_state != "queued_not_pending",
    )
    if job_state in {"running", "succeeded"}:
        running = server.job_queue.start_next_job()
        assert running is not None
    if job_state == "succeeded":
        server.job_queue.mark_succeeded(
            job.job_id,
            result={
                "status": "converted",
                "download": {
                    "filename": "batch-record.veridoc-result.json",
                    "content_type": "application/json",
                    "content": b'{"converted": true}',
                },
            },
        )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        replay_request = dict(request)
        replay_request["desktop_upload_audit"] = True
        replay_payload = json.dumps(replay_request).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/jobs",
            body=replay_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(replay_payload)),
            },
        )
        replay_response = connection.getresponse()
        replay_body = json.loads(replay_response.read().decode("utf-8"))
        events = server.job_event_store.list_events(filters={"job_id": job.job_id})
        stored_job = server.job_queue.get_job(job.job_id)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert replay_response.status == 400
    assert replay_body == {
        "error": "invalid_job_request",
        "message": "desktop_upload audit cannot be added after idempotent job creation",
    }
    assert events == []
    assert stored_job.status == ("queued" if job_state == "queued_not_pending" else job_state)


@pytest.mark.parametrize("job_state", ["running", "succeeded"])
def test_poc_http_api_rejects_late_idempotent_desktop_upload_audit_from_new_actor(
    job_state: str,
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    server.local_auth_tokens = _local_auth_tokens()
    uploaded_content = b"%PDF-1.7\nqueued source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    request = {
        "idempotency_key": f"desktop-upload-new-actor-{job_state}",
        "filename": "batch-record.pdf",
        "content_type": "application/pdf",
        "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
        "size_bytes": len(uploaded_content),
        "source_sha256": source_sha256,
        "mode": "standard",
        "desktop_upload_audit": True,
    }
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(request).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/jobs",
            body=payload,
            headers={
                "Authorization": "Bearer reviewer-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        create_response = connection.getresponse()
        create_body = json.loads(create_response.read().decode("utf-8"))
        job_id = create_body["job"]["job_id"]
        running = server.job_queue.start_next_job()
        assert running is not None
        if job_state == "succeeded":
            server.job_queue.mark_succeeded(
                job_id,
                result={
                    "status": "converted",
                    "download": {
                        "filename": "batch-record.veridoc-result.json",
                        "content_type": "application/json",
                        "content": b'{"converted": true}',
                    },
                },
            )

        connection.request(
            "POST",
            "/api/jobs",
            body=payload,
            headers={
                "Authorization": "Bearer approver-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        replay_response = connection.getresponse()
        replay_body = json.loads(replay_response.read().decode("utf-8"))
        events = server.job_event_store.list_events(filters={"job_id": job_id})
        stored_job = server.job_queue.get_job(job_id)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert create_response.status == 202
    assert replay_response.status == 400
    assert replay_body == {
        "error": "invalid_job_request",
        "message": "desktop_upload audit cannot be added after idempotent job creation",
    }
    assert [event["action"] for event in events] == ["desktop_upload"]
    assert events[0]["actor"] == {"id": "local-principal:reviewer", "role": "reviewer"}
    assert stored_job.status == job_state


def test_poc_http_api_records_desktop_upload_and_download_audit_events() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uploaded_content = b"%PDF-1.7\nqueued source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    try:
        payload = json.dumps(
            {
                "idempotency_key": "desktop-upload-download",
                "filename": "C:\\staged\\batch-record\r.pdf",
                "content_type": "application/pdf",
                "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
                "size_bytes": len(uploaded_content),
                "source_sha256": source_sha256,
                "mode": "standard",
                "desktop_upload_audit": True,
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/jobs",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        create_response = connection.getresponse()
        create_body = json.loads(create_response.read().decode("utf-8"))
        job_id = create_body["job"]["job_id"]
        upload_event_payload = json.dumps(
            {
                "job_id": job_id,
                "action": "desktop_upload",
                "audit_event": {
                    "event_type": "desktop.job_operation",
                    "job_id": job_id,
                    "job_status": "queued",
                    "action": "desktop_upload",
                    "filename": "batch-record.pdf",
                    "mode": "standard",
                    "source_sha256": source_sha256,
                    "size_bytes": len(uploaded_content),
                    "content_type": "application/pdf",
                },
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=upload_event_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(upload_event_payload)),
            },
        )
        upload_event_response = connection.getresponse()
        upload_event_body = json.loads(upload_event_response.read().decode("utf-8"))

        running = server.job_queue.start_next_job()
        assert running is not None
        server.job_queue.mark_succeeded(
            job_id,
            result={
                "status": "converted",
                "hashes": {
                    "source_sha256": source_sha256,
                    "output_sha256": hashlib.sha256(b'{"converted": true}').hexdigest(),
                },
                "download": {
                    "filename": "../batch-record\r\n.veridoc-result.json",
                    "content_type": "application/json; charset=utf-8",
                    "content": b'{"converted": true}',
                },
            },
        )

        connection.request(
            "POST",
            "/api/jobs",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        replay_response = connection.getresponse()
        replay_body = json.loads(replay_response.read().decode("utf-8"))
        connection.request("GET", f"/api/jobs/{job_id}/result")
        download_response = connection.getresponse()
        browser_download_proof = download_response.getheader(poc_web.DESKTOP_SAVE_PROOF_HEADER)
        download_body = download_response.read()
        browser_download_events = server.job_event_store.list_events(filters={"job_id": job_id})
        rejected_event_payload = json.dumps(
            {
                "job_id": job_id,
                "action": "desktop_result_download",
                "audit_event": {
                    "event_type": "desktop.job_operation",
                    "job_id": job_id,
                    "action": "desktop_result_download",
                    "download_filename": "batch-record.veridoc-result.json",
                    "output_sha256": hashlib.sha256(b'{"converted": true}').hexdigest(),
                },
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=rejected_event_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(rejected_event_payload)),
            },
        )
        rejected_event_response = connection.getresponse()
        rejected_event_body = json.loads(rejected_event_response.read().decode("utf-8"))
        rejected_events = server.job_event_store.list_events(filters={"job_id": job_id})
        connection.request(
            "GET",
            f"/api/jobs/{job_id}/result",
            headers={poc_web.DESKTOP_CLIENT_HEADER: poc_web.DESKTOP_CLIENT_HEADER_VALUE},
        )
        desktop_download_response = connection.getresponse()
        download_proof = desktop_download_response.getheader(poc_web.DESKTOP_SAVE_PROOF_HEADER)
        assert desktop_download_response.read() == b'{"converted": true}'
        event_payload = json.dumps(
            {
                "job_id": job_id,
                "action": "desktop_result_download",
                "audit_event": {
                    "event_type": "desktop.job_operation",
                    "job_id": job_id,
                    "action": "desktop_result_download",
                    "download_filename": "batch-record.veridoc-result.json",
                    "output_sha256": hashlib.sha256(b'{"converted": true}').hexdigest(),
                    "download_proof": download_proof,
                },
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=event_payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(event_payload))},
        )
        save_event_response = connection.getresponse()
        save_event_body = json.loads(save_event_response.read().decode("utf-8"))
        events = server.job_event_store.list_events(filters={"job_id": job_id})
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert create_response.status == 202
    assert create_body["audit_event"]["action"] == "desktop_upload"
    assert create_body["audit_event"]["source_sha256"] == source_sha256
    assert upload_event_response.status == 400
    assert upload_event_body == {
        "error": "invalid_job_event",
        "message": "desktop_upload audit must be recorded through the job create request",
    }
    assert replay_response.status == 202
    assert replay_body["job"]["job_id"] == job_id
    assert download_response.status == 200
    assert browser_download_proof is None
    assert download_body == b'{"converted": true}'
    assert [event["action"] for event in browser_download_events] == ["desktop_upload"]
    assert rejected_event_response.status == 400
    assert rejected_event_body == {
        "error": "invalid_job_event",
        "message": "audit_event.download_proof is required",
    }
    assert [event["action"] for event in rejected_events] == ["desktop_upload"]
    assert desktop_download_response.status == 200
    assert isinstance(download_proof, str) and download_proof
    assert save_event_response.status == 202
    assert [event["action"] for event in events] == [
        "desktop_upload",
        "desktop_result_download",
    ]
    assert events[0]["source_sha256"] == source_sha256
    assert events[0]["filename"] == "batch-record.pdf"
    assert save_event_body["audit_event"] == events[1]
    assert events[1]["filename"] == "batch-record.pdf"
    assert events[1]["output_sha256"] == hashlib.sha256(b'{"converted": true}').hexdigest()
    assert events[1]["download_filename"] == "batch-record.veridoc-result.json"


def test_poc_http_api_downloads_result_when_job_audit_log_is_tampered() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    uploaded_content = b"%PDF-1.7\nqueued source"
    result_content = b'{"converted": true}'
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    output_sha256 = hashlib.sha256(result_content).hexdigest()
    job = server.job_queue.create_job(
        idempotency_key="desktop-download-audit-tampered",
        filename="batch-record.pdf",
        mode="standard",
        source={
            "filename": "batch-record.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(uploaded_content),
            "sha256": source_sha256,
            "content": uploaded_content,
        },
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    server.job_queue.mark_succeeded(
        job.job_id,
        result={
            "status": "converted",
            "hashes": {
                "source_sha256": source_sha256,
                "output_sha256": output_sha256,
            },
            "download": {
                "filename": "batch-record.veridoc-result.json",
                "content_type": "application/json; charset=utf-8",
                "content": result_content,
            },
        },
    )
    server.job_event_store.record(
        {
            "event_type": "desktop.job_operation",
            "job_id": job.job_id,
            "action": "desktop_upload",
            "filename": "batch-record.pdf",
        }
    )
    server.job_event_store.record(
        {
            "event_type": "desktop.job_operation",
            "job_id": job.job_id,
            "action": "desktop_result_download",
            "download_filename": "batch-record.veridoc-result.json",
        }
    )
    del server.job_event_store._events[-1]  # noqa: SLF001
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "GET",
            f"/api/jobs/{job.job_id}/result",
            headers={poc_web.DESKTOP_CLIENT_HEADER: poc_web.DESKTOP_CLIENT_HEADER_VALUE},
        )
        download_response = connection.getresponse()
        download_body = download_response.read()
        event_payload = json.dumps(
            {
                "job_id": job.job_id,
                "action": "desktop_result_download",
                "audit_event": {
                    "event_type": "desktop.job_operation",
                    "job_id": job.job_id,
                    "action": "desktop_result_download",
                    "download_filename": "batch-record.veridoc-result.json",
                    "output_sha256": output_sha256,
                },
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=event_payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(event_payload))},
        )
        event_response = connection.getresponse()
        event_body = json.loads(event_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert download_response.status == 200
    assert download_body == result_content
    assert event_response.status == 400
    assert event_body["error"] == "invalid_job_event"
    assert "audit log integrity violation" in event_body["message"]
    assert [event["action"] for event in server.job_event_store.list_events()] == [
        "desktop_upload"
    ]
    assert server.job_event_store.verify_integrity() == {
        "ok": False,
        "errors": [
            "audit log terminal sequence mismatch",
            "audit log head hash mismatch",
        ],
    }


@pytest.mark.parametrize("saved_filename", ["CON", "a:b.json"])
def test_poc_http_api_rejects_unwritable_desktop_saved_filename(saved_filename: str) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    result_content = b'{"converted": true}'
    output_sha256 = hashlib.sha256(result_content).hexdigest()
    job = server.job_queue.create_job(
        idempotency_key=f"desktop-saved-filename-{saved_filename}",
        filename="batch-record.pdf",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    server.job_queue.mark_succeeded(
        job.job_id,
        result={
            "status": "converted",
            "hashes": {"output_sha256": output_sha256},
            "download": {
                "filename": "batch-record.veridoc-result.json",
                "content_type": "application/json; charset=utf-8",
                "content": result_content,
            },
        },
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "GET",
            f"/api/jobs/{job.job_id}/result",
            headers={poc_web.DESKTOP_CLIENT_HEADER: poc_web.DESKTOP_CLIENT_HEADER_VALUE},
        )
        download_response = connection.getresponse()
        download_proof = download_response.getheader(poc_web.DESKTOP_SAVE_PROOF_HEADER)
        download_response.read()
        event_payload = json.dumps(
            {
                "job_id": job.job_id,
                "action": "desktop_result_download",
                "audit_event": {
                    "event_type": "desktop.job_operation",
                    "job_id": job.job_id,
                    "action": "desktop_result_download",
                    "download_filename": "batch-record.veridoc-result.json",
                    "saved_filename": saved_filename,
                    "output_sha256": output_sha256,
                    "download_proof": download_proof,
                },
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=event_payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(event_payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert download_response.status == 200
    assert isinstance(download_proof, str) and download_proof
    assert response.status == 400
    assert body == {
        "error": "invalid_job_event",
        "message": "audit_event.saved_filename is invalid",
    }
    assert server.job_event_store.list_events(filters={"job_id": job.job_id}) == []


def test_poc_http_api_rejects_desktop_save_proof_from_different_token() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    server.desktop_save_proof_store = poc_web.DesktopSaveProofStore()
    server.local_auth_tokens = _local_auth_tokens()
    result_content = b'{"converted": true}'
    output_sha256 = hashlib.sha256(result_content).hexdigest()
    job = server.job_queue.create_job(
        idempotency_key="desktop-save-proof-token-binding",
        filename="batch-record.pdf",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    server.job_queue.mark_succeeded(
        job.job_id,
        result={
            "status": "converted",
            "hashes": {"output_sha256": output_sha256},
            "download": {
                "filename": "batch-record.veridoc-result.json",
                "content_type": "application/json",
                "content": result_content,
            },
        },
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "GET",
            f"/api/jobs/{job.job_id}/result",
            headers={
                "Authorization": "Bearer reviewer-token",
                poc_web.DESKTOP_CLIENT_HEADER: poc_web.DESKTOP_CLIENT_HEADER_VALUE,
            },
        )
        download_response = connection.getresponse()
        download_proof = download_response.getheader(poc_web.DESKTOP_SAVE_PROOF_HEADER)
        download_response.read()
        payload = json.dumps(
            {
                "job_id": job.job_id,
                "action": "desktop_result_download",
                "audit_event": {
                    "event_type": "desktop.job_operation",
                    "job_id": job.job_id,
                    "action": "desktop_result_download",
                    "download_filename": "batch-record.veridoc-result.json",
                    "output_sha256": output_sha256,
                    "download_proof": download_proof,
                },
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=payload,
            headers={
                "Authorization": "Bearer admin-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert download_response.status == 200
    assert isinstance(download_proof, str) and download_proof
    assert response.status == 400
    assert body == {
        "error": "invalid_job_event",
        "message": "audit_event.download_proof is invalid",
    }
    assert server.job_event_store.list_events(filters={"job_id": job.job_id}) == []


def test_poc_http_api_requires_create_permission_for_direct_desktop_upload_audit() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    server.local_auth_tokens = _local_auth_tokens()
    uploaded_content = b"%PDF-1.7\nqueued source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    job = server.job_queue.create_job(
        idempotency_key="desktop-upload-viewer-forgery",
        filename="batch-record.pdf",
        mode="standard",
        source={
            "filename": "batch-record.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(uploaded_content),
            "sha256": source_sha256,
            "content": uploaded_content,
        },
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "job_id": job.job_id,
                "action": "desktop_upload",
                "audit_event": {
                    "event_type": "desktop.job_operation",
                    "job_id": job.job_id,
                    "job_status": "queued",
                    "action": "desktop_upload",
                    "filename": "batch-record.pdf",
                    "mode": "standard",
                    "source_sha256": source_sha256,
                    "size_bytes": len(uploaded_content),
                    "content_type": "application/pdf",
                },
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/job-events",
            body=payload,
            headers={
                "Authorization": "Bearer viewer-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        events = server.job_event_store.list_events(filters={"job_id": job.job_id})
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 403
    assert body == {
        "error": "forbidden",
        "message": "role viewer cannot perform jobs_create",
    }
    assert events == []


def test_poc_http_api_rejects_queued_direct_desktop_upload_audit_backfill() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    uploaded_content = b"%PDF-1.7\nqueued source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    job = server.job_queue.create_job(
        idempotency_key="desktop-upload-queued-backfill",
        filename="batch-record.pdf",
        mode="standard",
        source={
            "filename": "batch-record.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(uploaded_content),
            "sha256": source_sha256,
            "content": uploaded_content,
        },
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "job_id": job.job_id,
                "action": "desktop_upload",
                "audit_event": {
                    "event_type": "desktop.job_operation",
                    "job_id": job.job_id,
                    "job_status": "queued",
                    "action": "desktop_upload",
                    "filename": "batch-record.pdf",
                    "mode": "standard",
                    "source_sha256": source_sha256,
                    "size_bytes": len(uploaded_content),
                    "content_type": "application/pdf",
                },
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/job-events",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        events = server.job_event_store.list_events(filters={"job_id": job.job_id})
        stored_job = server.job_queue.get_job(job.job_id)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body == {
        "error": "invalid_job_event",
        "message": "desktop_upload audit must be recorded through the job create request",
    }
    assert events == []
    assert stored_job.status == "queued"
    assert stored_job.attempts == 0


@pytest.mark.parametrize("terminal_state", ["running", "succeeded"])
def test_poc_http_api_rejects_late_direct_desktop_upload_audit(
    terminal_state: str,
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    uploaded_content = b"%PDF-1.7\nqueued source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    job = server.job_queue.create_job(
        idempotency_key=f"desktop-upload-late-{terminal_state}",
        filename="batch-record.pdf",
        mode="standard",
        source={
            "filename": "batch-record.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(uploaded_content),
            "sha256": source_sha256,
            "content": uploaded_content,
        },
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    if terminal_state == "succeeded":
        server.job_queue.mark_succeeded(
            job.job_id,
            result={
                "status": "converted",
                "download": {
                    "filename": "batch-record.veridoc-result.json",
                    "content_type": "application/json",
                    "content": b'{"converted": true}',
                },
            },
        )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "job_id": job.job_id,
                "action": "desktop_upload",
                "audit_event": {
                    "event_type": "desktop.job_operation",
                    "job_id": job.job_id,
                    "job_status": terminal_state,
                    "action": "desktop_upload",
                    "filename": "batch-record.pdf",
                    "mode": "standard",
                    "source_sha256": source_sha256,
                    "size_bytes": len(uploaded_content),
                    "content_type": "application/pdf",
                },
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/job-events",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        events = server.job_event_store.list_events(filters={"job_id": job.job_id})
        stored_job = server.job_queue.get_job(job.job_id)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body == {
        "error": "invalid_job_event",
        "message": "desktop_upload audit must be recorded before job starts",
    }
    assert events == []
    assert stored_job.status == terminal_state


def test_poc_http_api_rejects_retry_queued_direct_desktop_upload_audit() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    uploaded_content = b"%PDF-1.7\nqueued source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    job = server.job_queue.create_job(
        idempotency_key="desktop-upload-retry-queued",
        filename="batch-record.pdf",
        mode="standard",
        source={
            "filename": "batch-record.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(uploaded_content),
            "sha256": source_sha256,
            "content": uploaded_content,
        },
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    retry_queued = server.job_queue.mark_failed(job.job_id, error="temporary failure")
    assert retry_queued.status == "queued"
    assert retry_queued.attempts == 1
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "job_id": job.job_id,
                "action": "desktop_upload",
                "audit_event": {
                    "event_type": "desktop.job_operation",
                    "job_id": job.job_id,
                    "job_status": "queued",
                    "action": "desktop_upload",
                    "filename": "batch-record.pdf",
                    "mode": "standard",
                    "source_sha256": source_sha256,
                    "size_bytes": len(uploaded_content),
                    "content_type": "application/pdf",
                },
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/job-events",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        events = server.job_event_store.list_events(filters={"job_id": job.job_id})
        stored_job = server.job_queue.get_job(job.job_id)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body == {
        "error": "invalid_job_event",
        "message": "desktop_upload audit must be recorded before job starts",
    }
    assert events == []
    assert stored_job.status == "queued"
    assert stored_job.attempts == 1


def test_poc_http_api_accepts_desktop_save_audit_for_hashless_downloadable_result() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    job = server.job_queue.create_job(
        idempotency_key="desktop-hashless-result",
        filename="../legacy-record.pdf",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    download_content = b'{"converted": "legacy"}'
    server.job_queue.mark_succeeded(
        job.job_id,
        result={
            "status": "converted",
            "download": {
                "filename": "../legacy-record\r\n.veridoc-result.json",
                "content_type": "application/json",
                "content": download_content,
            },
        },
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "GET",
            f"/api/jobs/{job.job_id}/result",
            headers={poc_web.DESKTOP_CLIENT_HEADER: poc_web.DESKTOP_CLIENT_HEADER_VALUE},
        )
        download_response = connection.getresponse()
        download_proof = download_response.getheader(poc_web.DESKTOP_SAVE_PROOF_HEADER)
        download_body = download_response.read()
        payload = json.dumps(
            {
                "job_id": job.job_id,
                "action": "desktop_result_download",
                "audit_event": {
                    "event_type": "desktop.job_operation",
                    "job_id": job.job_id,
                    "action": "desktop_result_download",
                    "download_filename": "legacy-record.veridoc-result.json",
                    "output_sha256": hashlib.sha256(download_content).hexdigest(),
                    "download_proof": download_proof,
                },
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        events = server.job_event_store.list_events(filters={"job_id": job.job_id})
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert download_response.status == 200
    assert download_body == download_content
    assert isinstance(download_proof, str) and download_proof
    assert response.status == 202
    assert body["audit_event"] == events[0]
    assert events[0]["filename"] == "legacy-record.pdf"
    assert events[0]["download_filename"] == "legacy-record.veridoc-result.json"
    assert events[0]["output_sha256"] == hashlib.sha256(download_content).hexdigest()


def test_desktop_client_records_hashless_result_save_audit_with_computed_hash(tmp_path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    server.local_auth_tokens = _local_auth_tokens()
    job = server.job_queue.create_job(
        idempotency_key="desktop-client-hashless-result",
        filename="../legacy-record.pdf",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    download_content = b'{"converted": "legacy-client"}'
    server.job_queue.mark_succeeded(
        job.job_id,
        result={
            "status": "converted",
            "download": {
                "filename": "../legacy-record\r\n.veridoc-result.json",
                "content_type": "application/json",
                "content": download_content,
            },
        },
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = DesktopApiClient(
            DesktopApiClientConfig(base_url=f"http://127.0.0.1:{server.server_port}"),
            credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        )

        saved_path = client.save_job_result(job.job_id, tmp_path)
        events = server.job_event_store.list_events(filters={"job_id": job.job_id})
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert saved_path == tmp_path / "legacy-record.veridoc-result.json"
    assert saved_path.read_bytes() == download_content
    assert len(events) == 1
    assert events[0]["action"] == "desktop_result_download"
    assert events[0]["filename"] == "legacy-record.pdf"
    assert events[0]["download_filename"] == "legacy-record.veridoc-result.json"
    assert events[0]["saved_filename"] == "legacy-record.veridoc-result.json"
    assert events[0]["output_sha256"] == hashlib.sha256(download_content).hexdigest()
    assert events[0]["actor"] == {"id": "local-principal:reviewer", "role": "reviewer"}


def test_poc_http_api_records_one_desktop_upload_audit_for_concurrent_idempotent_uploads() -> None:
    lookup_barrier = Barrier(2)
    created_flags: list[bool] = []
    created_flags_lock = Lock()

    class RaceAmplifyingJobQueue(JobQueue):
        def get_or_create_job(self, **kwargs):
            lookup_barrier.wait(timeout=5)
            job, created = super().get_or_create_job(**kwargs)
            with created_flags_lock:
                created_flags.append(created)
            return job, created

        def get_idempotent_job(self, **kwargs):
            existing = super().get_idempotent_job(**kwargs)
            if existing is None:
                lookup_barrier.wait(timeout=5)
            return existing

    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = RaceAmplifyingJobQueue()
    server.job_event_store = JobAuditEventStore()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uploaded_content = b"%PDF-1.7\nqueued source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    payload = json.dumps(
        {
            "idempotency_key": "concurrent-desktop-upload",
            "filename": "batch-record.pdf",
            "content_type": "application/pdf",
            "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
            "size_bytes": len(uploaded_content),
            "source_sha256": source_sha256,
            "mode": "standard",
            "desktop_upload_audit": True,
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "Content-Length": str(len(payload))}
    responses: list[tuple[int, dict[str, object]]] = []
    errors: list[BaseException] = []
    events: list[dict[str, object]] = []

    def post_upload() -> None:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("POST", "/api/jobs", body=payload, headers=headers)
            response = connection.getresponse()
            responses.append((response.status, json.loads(response.read().decode("utf-8"))))
        except BaseException as exc:  # pragma: no cover - assertion below preserves the failure.
            errors.append(exc)
        finally:
            connection.close()

    try:
        workers = [Thread(target=post_upload), Thread(target=post_upload)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        if len(responses) == 2:
            job_ids = {body["job"]["job_id"] for _status, body in responses}
            if len(job_ids) == 1:
                job_id = str(job_ids.pop())
                events = server.job_event_store.list_events(filters={"job_id": job_id})
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert errors == []
    assert len(responses) == 2
    assert [status for status, _body in responses] == [202, 202]
    assert sorted(created_flags) == [False, True]
    job_ids = {body["job"]["job_id"] for _status, body in responses}
    assert len(job_ids) == 1
    assert [event["action"] for event in events] == ["desktop_upload"]
    assert events[0]["source_sha256"] == source_sha256
    assert {body["audit_event"]["event_hash"] for _status, body in responses} == {
        events[0]["event_hash"]
    }


def test_poc_http_api_reuses_desktop_upload_audit_for_same_actor_role_change() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    server.local_auth_tokens = {
        "reviewer-token": {"role": "reviewer", "principal_id": "same-person"},
        "admin-token": {"role": "admin", "principal_id": "same-person"},
    }
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uploaded_content = b"%PDF-1.7\nshared source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    request = {
        "idempotency_key": "same-actor-role-change-upload",
        "filename": "batch-record.pdf",
        "content_type": "application/pdf",
        "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
        "size_bytes": len(uploaded_content),
        "source_sha256": source_sha256,
        "mode": "standard",
        "desktop_upload_audit": True,
    }
    payload = json.dumps(request).encode("utf-8")
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/jobs",
            body=payload,
            headers={
                "Authorization": "Bearer reviewer-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        first_response = connection.getresponse()
        first_body = json.loads(first_response.read().decode("utf-8"))
        connection.request(
            "POST",
            "/api/jobs",
            body=payload,
            headers={
                "Authorization": "Bearer admin-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        second_response = connection.getresponse()
        second_body = json.loads(second_response.read().decode("utf-8"))
        events = server.job_event_store.list_events(
            filters={"job_id": first_body["job"]["job_id"]}
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert first_response.status == 202
    assert second_response.status == 202
    assert first_body["job"]["job_id"] == second_body["job"]["job_id"]
    assert [event["action"] for event in events] == ["desktop_upload"]
    assert events[0]["actor"] == {"id": "local-principal:same-person", "role": "reviewer"}
    assert events[0]["actor_id"] == "local-principal:same-person"
    assert first_body["audit_event"] == second_body["audit_event"] == events[0]


def test_poc_http_api_publishes_unpublished_desktop_upload_replay_after_audit() -> None:
    audit_recorded = Event()
    release_publish = Event()

    class BlockingPublishJobAuditEventStore(JobAuditEventStore):
        def record_once(
            self,
            event: dict[str, object],
            *,
            dedupe: dict[str, object],
        ) -> dict[str, object]:
            recorded = super().record_once(event, dedupe=dedupe)
            audit_recorded.set()
            release_publish.wait(timeout=5)
            return recorded

    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = BlockingPublishJobAuditEventStore()
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uploaded_content = b"%PDF-1.7\nshared source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    payload = json.dumps(
        {
            "idempotency_key": "unpublished-same-actor-upload",
            "filename": "batch-record.pdf",
            "content_type": "application/pdf",
            "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
            "size_bytes": len(uploaded_content),
            "source_sha256": source_sha256,
            "mode": "standard",
            "desktop_upload_audit": True,
        }
    ).encode("utf-8")
    headers = {
        "Authorization": "Bearer reviewer-token",
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
    }
    first_result: dict[str, object] = {}
    try:
        def post_first_upload() -> None:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("POST", "/api/jobs", body=payload, headers=headers)
                response = connection.getresponse()
                first_result["status"] = response.status
                first_result["body"] = json.loads(response.read().decode("utf-8"))
            finally:
                connection.close()

        worker = Thread(target=post_first_upload)
        worker.start()
        assert audit_recorded.wait(timeout=5)

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("POST", "/api/jobs", body=payload, headers=headers)
        replay_response = connection.getresponse()
        replay_body = json.loads(replay_response.read().decode("utf-8"))
        job_id = replay_body["job"]["job_id"]
        connection.request(
            "GET",
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer reviewer-token"},
        )
        detail_response = connection.getresponse()
        detail_body = json.loads(detail_response.read().decode("utf-8"))
        connection.close()

        release_publish.set()
        worker.join(timeout=10)
        events = server.job_event_store.list_events(filters={"job_id": job_id})
    finally:
        release_publish.set()
        server.shutdown()
        thread.join(timeout=5)

    assert first_result["status"] == 202
    assert replay_response.status == 202
    assert first_result["body"]["job"]["job_id"] == job_id
    assert detail_response.status == 200
    assert detail_body["job"]["job_id"] == job_id
    assert [event["action"] for event in events] == ["desktop_upload"]
    assert first_result["body"]["audit_event"] == replay_body["audit_event"] == events[0]


def test_poc_http_api_creator_publish_is_idempotent_after_replay_starts_job() -> None:
    audit_recorded = Event()
    release_creator_publish = Event()

    class BlockingPublishJobAuditEventStore(JobAuditEventStore):
        def record_once(
            self,
            event: dict[str, object],
            *,
            dedupe: dict[str, object],
        ) -> dict[str, object]:
            recorded = super().record_once(event, dedupe=dedupe)
            audit_recorded.set()
            release_creator_publish.wait(timeout=5)
            return recorded

    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = BlockingPublishJobAuditEventStore()
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uploaded_content = b"%PDF-1.7\nreplay started source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    payload = json.dumps(
        {
            "idempotency_key": "replay-started-before-creator-publish",
            "filename": "batch-record.pdf",
            "content_type": "application/pdf",
            "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
            "size_bytes": len(uploaded_content),
            "source_sha256": source_sha256,
            "mode": "standard",
            "desktop_upload_audit": True,
        }
    ).encode("utf-8")
    headers = {
        "Authorization": "Bearer reviewer-token",
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
    }
    first_result: dict[str, object] = {}
    errors: list[BaseException] = []

    def post_first_upload() -> None:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("POST", "/api/jobs", body=payload, headers=headers)
            response = connection.getresponse()
            first_result["status"] = response.status
            first_result["body"] = json.loads(response.read().decode("utf-8"))
        except BaseException as exc:  # pragma: no cover - assertion below preserves the failure.
            errors.append(exc)
        finally:
            connection.close()

    try:
        worker = Thread(target=post_first_upload)
        worker.start()
        assert audit_recorded.wait(timeout=5)

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("POST", "/api/jobs", body=payload, headers=headers)
        replay_response = connection.getresponse()
        replay_body = json.loads(replay_response.read().decode("utf-8"))
        connection.close()

        running = server.job_queue.start_next_job()
        release_creator_publish.set()
        worker.join(timeout=10)
        job_id = replay_body["job"]["job_id"]
        events = server.job_event_store.list_events(filters={"job_id": job_id})
    finally:
        release_creator_publish.set()
        server.shutdown()
        thread.join(timeout=5)

    assert errors == []
    assert running is not None
    assert running.job_id == replay_body["job"]["job_id"]
    assert running.status == "running"
    assert first_result["status"] == 202
    assert replay_response.status == 202
    assert first_result["body"]["job"]["job_id"] == replay_body["job"]["job_id"]
    assert first_result["body"]["job"]["status"] == "running"
    assert [event["action"] for event in events] == ["desktop_upload"]
    assert first_result["body"]["audit_event"] == replay_body["audit_event"] == events[0]


def test_poc_http_api_keeps_in_flight_desktop_upload_replay_idempotent() -> None:
    audit_recording_started = Event()
    release_audit_recording = Event()
    replay_waiting_for_publish = Event()

    class SlowJobAuditEventStore(JobAuditEventStore):
        def record_once(
            self,
            event: dict[str, object],
            *,
            dedupe: dict[str, object],
        ) -> dict[str, object]:
            audit_recording_started.set()
            release_audit_recording.wait(timeout=5)
            return super().record_once(event, dedupe=dedupe)

    class ObservedJobQueue(JobQueue):
        def wait_until_published(self, job_id: str, *, timeout: float = 5.0):
            replay_waiting_for_publish.set()
            return super().wait_until_published(job_id, timeout=timeout)

    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = ObservedJobQueue()
    server.job_event_store = SlowJobAuditEventStore()
    server.local_auth_tokens = _local_auth_tokens()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uploaded_content = b"%PDF-1.7\nin-flight shared source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    payload = json.dumps(
        {
            "idempotency_key": "in-flight-same-actor-upload",
            "filename": "batch-record.pdf",
            "content_type": "application/pdf",
            "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
            "size_bytes": len(uploaded_content),
            "source_sha256": source_sha256,
            "mode": "standard",
            "desktop_upload_audit": True,
        }
    ).encode("utf-8")
    headers = {
        "Authorization": "Bearer reviewer-token",
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
    }
    first_result: dict[str, object] = {}
    replay_result: dict[str, object] = {}
    errors: list[BaseException] = []

    def post_upload(result: dict[str, object]) -> None:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request("POST", "/api/jobs", body=payload, headers=headers)
            response = connection.getresponse()
            result["status"] = response.status
            result["body"] = json.loads(response.read().decode("utf-8"))
        except BaseException as exc:  # pragma: no cover - assertion below preserves the failure.
            errors.append(exc)
        finally:
            connection.close()

    first_worker = Thread(target=post_upload, args=(first_result,))
    replay_worker = Thread(target=post_upload, args=(replay_result,))
    try:
        first_worker.start()
        assert audit_recording_started.wait(timeout=5)

        replay_worker.start()
        assert replay_waiting_for_publish.wait(timeout=5)

        release_audit_recording.set()
        first_worker.join(timeout=10)
        replay_worker.join(timeout=10)
        job_id = first_result["body"]["job"]["job_id"]
        events = server.job_event_store.list_events(filters={"job_id": job_id})

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "GET",
            f"/api/jobs/{job_id}",
            headers={"Authorization": "Bearer reviewer-token"},
        )
        detail_response = connection.getresponse()
        detail_body = json.loads(detail_response.read().decode("utf-8"))
        connection.close()
    finally:
        release_audit_recording.set()
        server.shutdown()
        thread.join(timeout=5)

    assert errors == []
    assert first_result["status"] == 202
    assert replay_result["status"] == 202
    assert first_result["body"]["job"]["job_id"] == replay_result["body"]["job"]["job_id"]
    assert detail_response.status == 200
    assert detail_body["job"]["job_id"] == job_id
    assert [event["action"] for event in events] == ["desktop_upload"]
    assert first_result["body"]["audit_event"] == replay_result["body"]["audit_event"] == events[0]


def test_poc_http_api_rejects_unpublished_desktop_upload_replay_from_different_actor() -> None:
    audit_recorded = Event()
    release_publish = Event()

    class BlockingPublishJobAuditEventStore(JobAuditEventStore):
        def record_once(
            self,
            event: dict[str, object],
            *,
            dedupe: dict[str, object],
        ) -> dict[str, object]:
            recorded = super().record_once(event, dedupe=dedupe)
            audit_recorded.set()
            release_publish.wait(timeout=5)
            return recorded

    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = BlockingPublishJobAuditEventStore()
    server.local_auth_tokens = {
        "reviewer-one-token": {"role": "reviewer", "principal_id": "reviewer-one"},
        "reviewer-two-token": {"role": "reviewer", "principal_id": "reviewer-two"},
    }
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uploaded_content = b"%PDF-1.7\nshared source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    payload = json.dumps(
        {
            "idempotency_key": "unpublished-different-actor-upload",
            "filename": "batch-record.pdf",
            "content_type": "application/pdf",
            "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
            "size_bytes": len(uploaded_content),
            "source_sha256": source_sha256,
            "mode": "standard",
            "desktop_upload_audit": True,
        }
    ).encode("utf-8")
    first_headers = {
        "Authorization": "Bearer reviewer-one-token",
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
    }
    second_headers = {
        "Authorization": "Bearer reviewer-two-token",
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
    }
    first_result: dict[str, object] = {}
    try:
        def post_first_upload() -> None:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("POST", "/api/jobs", body=payload, headers=first_headers)
                response = connection.getresponse()
                first_result["status"] = response.status
                first_result["body"] = json.loads(response.read().decode("utf-8"))
            finally:
                connection.close()

        worker = Thread(target=post_first_upload)
        worker.start()
        assert audit_recorded.wait(timeout=5)

        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("POST", "/api/jobs", body=payload, headers=second_headers)
        replay_response = connection.getresponse()
        replay_body = json.loads(replay_response.read().decode("utf-8"))
        connection.close()

        release_publish.set()
        worker.join(timeout=10)
        job_id = first_result["body"]["job"]["job_id"]
        events = server.job_event_store.list_events(filters={"job_id": job_id})
    finally:
        release_publish.set()
        server.shutdown()
        thread.join(timeout=5)

    assert first_result["status"] == 202
    assert replay_response.status == 400
    assert replay_body == {
        "error": "invalid_job_request",
        "message": "desktop_upload audit cannot be added after idempotent job creation",
    }
    assert [event["action"] for event in events] == ["desktop_upload"]
    assert events[0]["actor_id"] == "local-principal:reviewer-one"


def test_poc_http_api_rejects_distinct_desktop_upload_audits_for_different_actors() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.job_event_store = JobAuditEventStore()
    server.local_auth_tokens = {
        "reviewer-one-token": {"role": "reviewer", "principal_id": "reviewer-one"},
        "reviewer-two-token": {"role": "reviewer", "principal_id": "reviewer-two"},
    }
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    uploaded_content = b"%PDF-1.7\nshared source"
    source_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    request = {
        "idempotency_key": "shared-desktop-upload",
        "filename": "batch-record.pdf",
        "content_type": "application/pdf",
        "content_base64": base64.b64encode(uploaded_content).decode("ascii"),
        "size_bytes": len(uploaded_content),
        "source_sha256": source_sha256,
        "mode": "standard",
        "desktop_upload_audit": True,
    }
    payload = json.dumps(request).encode("utf-8")
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/jobs",
            body=payload,
            headers={
                "Authorization": "Bearer reviewer-one-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        first_response = connection.getresponse()
        first_body = json.loads(first_response.read().decode("utf-8"))
        connection.request(
            "POST",
            "/api/jobs",
            body=payload,
            headers={
                "Authorization": "Bearer reviewer-two-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        second_response = connection.getresponse()
        second_body = json.loads(second_response.read().decode("utf-8"))
        events = server.job_event_store.list_events(filters={"job_id": first_body["job"]["job_id"]})
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert first_response.status == 202
    assert second_response.status == 400
    assert second_body == {
        "error": "invalid_job_request",
        "message": "desktop_upload audit cannot be added after idempotent job creation",
    }
    assert [event["action"] for event in events] == ["desktop_upload"]
    assert events[0]["actor"]["id"] == "local-principal:reviewer-one"
    assert events[0]["source_sha256"] == source_sha256
    assert first_body["audit_event"] == events[0]


def test_poc_http_api_rejects_non_string_job_upload_base64_content() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "idempotency_key": "upload-non-string-base64",
                "filename": "batch-record.pdf",
                "content_type": "application/pdf",
                "content_base64": 1234,
                "mode": "standard",
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/jobs",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        jobs = server.job_queue.list_jobs()
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body == {
        "error": "invalid_job_request",
        "message": "content_base64 must be a string",
    }
    assert jobs == []


def test_template_store_accepts_schema_only_mapping_without_legacy_content() -> None:
    store = poc_web.TemplateStore()
    mapping = {
        "format": "json",
        "root_key": "template_result",
        "field_map": [
            {"field_id": f"field_{index}", "output_key": f"result.field_{index}"}
            for index in range(300)
        ],
    }

    created = store.register_template(
        {
            "template_id": "schema-only",
            "name": "Schema Only",
            "category": "manufacturing",
            "document_type": "batch_record",
            "fields": [
                {"field_id": "lot_number", "label": "Lot number", "required": True},
            ],
            "output_mapping": mapping,
            "change_reason": "Initial schema-only template registration",
            "actor": {"principal_id": "qa-author", "role": "admin"},
        }
    )

    version = created["versions"][0]
    assert created["current_version"] == 1
    assert version["output_mapping"] == mapping
    assert version["content"] == ""
    assert version["fields"] == [
        {"field_id": "lot_number", "label": "Lot number", "required": True}
    ]


def test_template_store_records_version_change_history_and_rejects_missing_context() -> None:
    store = poc_web.TemplateStore()
    base_request = {
        "template_id": "audit-template",
        "name": "Audit Template",
        "category": "manufacturing",
        "fields": [{"field_id": "lot_number", "label": "Lot number", "required": True}],
        "change_reason": "Initial controlled template release",
        "actor": {"principal_id": "qa-author", "role": "admin"},
    }

    created = store.register_template(base_request)
    updated = store.register_template(
        {
            **base_request,
            "fields": [
                {"field_id": "lot_number", "label": "Lot number", "required": True},
                {"field_id": "reviewer", "label": "Reviewer", "required": False},
            ],
            "change_reason": "Add optional QA reviewer capture",
            "actor": {"principal_id": "qa-maintainer", "role": "admin"},
            "approved_by": {"principal_id": "qa-approver", "role": "approver"},
        }
    )

    assert created["change_history"] == [
        {
            "event_type": "template.change_recorded",
            "action": "created",
            "template_id": "audit-template",
            "version": 1,
            "change_reason": "Initial controlled template release",
            "actor": {"principal_id": "qa-author", "role": "admin"},
            "approval": {"status": "unapproved", "approved_by": None},
            "recorded_at": created["versions"][0]["created_at"],
        }
    ]
    assert updated["current_version"] == 2
    assert updated["versions"][1]["change_history"][0] == updated["change_history"][1]
    assert updated["change_history"][1]["action"] == "versioned"
    assert updated["change_history"][1]["version"] == 2
    assert updated["change_history"][1]["change_reason"] == "Add optional QA reviewer capture"
    assert updated["change_history"][1]["actor"] == {"principal_id": "qa-maintainer", "role": "admin"}
    assert updated["change_history"][1]["approval"] == {
        "status": "approved",
        "approved_by": {"principal_id": "qa-approver", "role": "approver"},
    }

    for missing_field in ("change_reason", "actor"):
        invalid_request = {
            **base_request,
            "name": "Rejected Update",
            "fields": [{"field_id": "lot_number", "label": "Lot number", "required": False}],
            missing_field: "",
        }
        if missing_field == "actor":
            invalid_request["actor"] = None
        with pytest.raises(ValueError, match=missing_field):
            store.register_template(invalid_request)

    assert store.get_template("audit-template")["current_version"] == 2

    disabled = store.register_template(
        {
            **base_request,
            "status": "inactive",
            "fields": [{"field_id": "lot_number", "label": "Lot number", "required": True}],
            "change_reason": "Disable superseded controlled template",
            "actor": {"principal_id": "qa-maintainer", "role": "admin"},
        }
    )
    preserved = store.register_template(
        {
            **base_request,
            "fields": [{"field_id": "lot_number", "label": "Lot number", "required": False}],
            "change_reason": "Update inactive template metadata",
            "actor": {"principal_id": "qa-maintainer", "role": "admin"},
        }
    )
    null_status_preserved = store.register_template(
        {
            **base_request,
            "status": None,
            "fields": [{"field_id": "lot_number", "label": "Lot number", "required": True}],
            "change_reason": "Update inactive template with null status",
            "actor": {"principal_id": "qa-maintainer", "role": "admin"},
        }
    )
    enabled = store.register_template(
        {
            **base_request,
            "status": "active",
            "fields": [{"field_id": "lot_number", "label": "Lot number", "required": False}],
            "change_reason": "Return template to active use",
            "actor": {"principal_id": "qa-maintainer", "role": "admin"},
        }
    )

    assert disabled["status"] == "inactive"
    assert disabled["change_history"][2]["action"] == "disabled"
    assert preserved["status"] == "inactive"
    assert preserved["versions"][3]["status"] == "inactive"
    assert preserved["change_history"][3]["action"] == "versioned"
    assert null_status_preserved["status"] == "inactive"
    assert null_status_preserved["versions"][4]["status"] == "inactive"
    assert null_status_preserved["change_history"][4]["action"] == "versioned"
    assert enabled["status"] == "active"
    assert enabled["change_history"][5]["action"] == "enabled"


def test_poc_http_api_registers_template_versions_and_jobs_keep_version_snapshot() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    server.template_store = poc_web.TemplateStore()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        create_template_payload = json.dumps(
            {
                "template_id": "batch-record",
                "name": "Batch Record",
                "category": "manufacturing",
                "document_type": "batch_record",
                "anchors": [
                    {
                        "anchor_id": "batch-header",
                        "kind": "heading",
                        "text": "Batch Production Record",
                        "match": "normalized",
                    }
                ],
                "fields": [
                    {
                        "field_id": "lot_number",
                        "label": "Lot number",
                        "required": True,
                        "risk_level": "high",
                        "output_key": "batch.lot_number",
                    },
                    {"field_id": "operator", "label": "Operator", "required": False},
                ],
                "tables": [
                    {
                        "table_id": "yield_summary",
                        "anchor_id": "batch-header",
                        "required_columns": ["step", "actual_yield"],
                        "output_key": "batch.yield_summary",
                    }
                ],
                "risk_rank": {
                    "default_level": "medium",
                    "levels": [{"level": "high", "rank": 3}],
                    "review_required_levels": ["high"],
                },
                "validation_rules": [
                    {
                        "rule_id": "lot-required",
                        "target": "lot_number",
                        "rule_type": "required",
                        "severity": "error",
                        "message": "Lot number is required.",
                    }
                ],
                "output_mapping": {
                    "format": "json",
                    "root_key": "template_result",
                    "field_map": [{"field_id": "lot_number", "output_key": "batch.lot_number"}],
                    "table_map": [
                        {"table_id": "yield_summary", "output_key": "batch.yield_summary"}
                    ],
                },
                "change_reason": "Initial controlled template release",
                "actor": {"principal_id": "qa-author", "role": "admin"},
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/templates",
            body=create_template_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(create_template_payload)),
            },
        )
        created_response = connection.getresponse()
        created = json.loads(created_response.read().decode("utf-8"))

        update_template_payload = json.dumps(
            {
                "template_id": "batch-record",
                "name": "Batch Record",
                "category": "manufacturing",
                "fields": [
                    {"name": "lot_number", "label": "Lot number", "required": True},
                    {"name": "operator", "label": "Operator", "required": True},
                    {"name": "approver", "label": "Approver", "required": False},
                ],
                "content": "Lot {{lot_number}} reviewed by {{operator}} and {{approver}}",
                "change_reason": "Add optional approver capture",
                "actor": {"principal_id": "qa-maintainer", "role": "admin"},
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/templates",
            body=update_template_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(update_template_payload)),
            },
        )
        updated_response = connection.getresponse()
        updated = json.loads(updated_response.read().decode("utf-8"))

        create_job_payload = json.dumps(
            {
                "idempotency_key": "upload-with-template",
                "filename": "batch-record.pdf",
                "mode": "standard",
                "template_id": "batch-record",
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/jobs",
            body=create_job_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(create_job_payload)),
            },
        )
        job_response = connection.getresponse()
        job = json.loads(job_response.read().decode("utf-8"))

        third_template_payload = json.dumps(
            {
                "template_id": "batch-record",
                "name": "Batch Record",
                "category": "manufacturing",
                "fields": [
                    {"name": "lot_number", "label": "Lot number", "required": True},
                    {"name": "operator", "label": "Operator", "required": True},
                    {"name": "approver", "label": "Approver", "required": True},
                ],
                "content": "Final {{lot_number}} / {{operator}} / {{approver}}",
                "status": "inactive",
                "change_reason": "Disable superseded template draft",
                "actor": {"principal_id": "qa-maintainer", "role": "admin"},
                "approved_by": {"principal_id": "qa-approver", "role": "approver"},
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/templates",
            body=third_template_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(third_template_payload)),
            },
        )
        third_response = connection.getresponse()
        third = json.loads(third_response.read().decode("utf-8"))

        connection.request("GET", "/api/templates/batch-record")
        detail_response = connection.getresponse()
        detail = json.loads(detail_response.read().decode("utf-8"))

        connection.request("GET", f"/api/jobs/{job['job']['job_id']}")
        refreshed_job_response = connection.getresponse()
        refreshed_job = json.loads(refreshed_job_response.read().decode("utf-8"))

        rejected_job_payload = json.dumps(
            {
                "idempotency_key": "new-upload-with-inactive-template",
                "filename": "batch-record.pdf",
                "mode": "standard",
                "template_id": "batch-record",
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/jobs",
            body=rejected_job_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(rejected_job_payload)),
            },
        )
        rejected_job_response = connection.getresponse()
        rejected_job = json.loads(rejected_job_response.read().decode("utf-8"))

        connection.request(
            "POST",
            "/api/jobs",
            body=create_job_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(create_job_payload)),
            },
        )
        replayed_job_response = connection.getresponse()
        replayed_job = json.loads(replayed_job_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert created_response.status == 201
    assert created["template"]["current_version"] == 1
    assert updated_response.status == 201
    assert updated["template"]["current_version"] == 2
    assert job_response.status == 202
    assert job["job"]["template"] == {
        "template_id": "batch-record",
        "template_version": 2,
        "name": "Batch Record",
    }
    assert third_response.status == 201
    assert third["template"]["current_version"] == 3
    assert third["template"]["change_history"][2]["action"] == "disabled"
    assert third["template"]["change_history"][2]["approval"]["status"] == "approved"
    assert detail_response.status == 200
    assert [version["version"] for version in detail["template"]["versions"]] == [1, 2, 3]
    first_version = detail["template"]["versions"][0]
    second_version = detail["template"]["versions"][1]
    assert first_version["document_type"] == "batch_record"
    assert first_version["anchors"][0]["anchor_id"] == "batch-header"
    assert first_version["fields"][0]["field_id"] == "lot_number"
    assert "name" not in first_version["fields"][0]
    assert first_version["fields"][0]["risk_level"] == "high"
    assert first_version["tables"][0]["table_id"] == "yield_summary"
    assert first_version["risk_rank"]["review_required_levels"] == ["high"]
    assert first_version["validation_rules"][0]["rule_id"] == "lot-required"
    assert first_version["output_mapping"]["field_map"][0]["output_key"] == "batch.lot_number"
    assert second_version["document_type"] == "batch_record"
    assert second_version["anchors"] == first_version["anchors"]
    assert second_version["tables"] == first_version["tables"]
    assert second_version["risk_rank"] == first_version["risk_rank"]
    assert second_version["validation_rules"] == first_version["validation_rules"]
    assert second_version["output_mapping"] == first_version["output_mapping"]
    assert second_version["fields"][0]["name"] == "lot_number"
    assert refreshed_job_response.status == 200
    assert refreshed_job["job"]["template"]["template_version"] == 2
    assert rejected_job_response.status == 400
    assert rejected_job == {
        "error": "invalid_job_request",
        "message": "template_id is inactive",
    }
    assert replayed_job_response.status == 202
    assert replayed_job["job"]["job_id"] == job["job"]["job_id"]
    assert replayed_job["job"]["template"]["template_version"] == 2


def test_poc_http_api_derives_template_audit_actor_from_local_auth() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.local_auth_tokens = _local_auth_tokens()
    server.template_store = poc_web.TemplateStore()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "template_id": "trusted-actor-template",
                "name": "Trusted Actor Template",
                "category": "manufacturing",
                "fields": [{"field_id": "lot_number", "label": "Lot number", "required": True}],
                "change_reason": "Register through authenticated API",
                "actor": {"principal_id": "spoofed-author", "role": "viewer"},
                "approved_by": {"principal_id": "spoofed-approver", "role": "approver"},
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/templates",
            body=payload,
            headers={
                "Authorization": "Bearer admin-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        null_approval_payload = json.dumps(
            {
                "template_id": "null-approval-template",
                "name": "Null Approval Template",
                "category": "manufacturing",
                "fields": [{"field_id": "lot_number", "label": "Lot number", "required": True}],
                "change_reason": "Register with explicit null approval",
                "actor": {"principal_id": "spoofed-author", "role": "viewer"},
                "approved_by": None,
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/templates",
            body=null_approval_payload,
            headers={
                "Authorization": "Bearer admin-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(null_approval_payload)),
            },
        )
        null_approval_response = connection.getresponse()
        null_approval_body = json.loads(null_approval_response.read().decode("utf-8"))
        malformed_approval_responses = []
        for template_id, approved_by in (
            ("malformed-empty-approval-template", {}),
            ("malformed-bool-approval-template", True),
        ):
            malformed_approval_payload = json.dumps(
                {
                    "template_id": template_id,
                    "name": "Malformed Approval Template",
                    "category": "manufacturing",
                    "fields": [
                        {"field_id": "lot_number", "label": "Lot number", "required": True}
                    ],
                    "change_reason": "Reject malformed local approval payload",
                    "actor": {"principal_id": "spoofed-author", "role": "viewer"},
                    "approved_by": approved_by,
                }
            ).encode("utf-8")
            connection.request(
                "POST",
                "/api/templates",
                body=malformed_approval_payload,
                headers={
                    "Authorization": "Bearer admin-token",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(malformed_approval_payload)),
                },
            )
            malformed_response = connection.getresponse()
            malformed_body = json.loads(malformed_response.read().decode("utf-8"))
            connection.request(
                "GET",
                f"/api/templates/{template_id}",
                headers={"Authorization": "Bearer admin-token"},
            )
            lookup_response = connection.getresponse()
            lookup_body = json.loads(lookup_response.read().decode("utf-8"))
            malformed_approval_responses.append(
                (malformed_response, malformed_body, lookup_response, lookup_body)
            )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 201
    change = body["template"]["change_history"][0]
    assert change["actor"] == {"principal_id": "local-principal:admin", "role": "admin"}
    assert change["approval"] == {
        "status": "approved",
        "approved_by": {"principal_id": "local-principal:admin", "role": "admin"},
    }
    null_approval_change = null_approval_body["template"]["change_history"][0]
    assert null_approval_response.status == 201
    assert null_approval_change["actor"] == {
        "principal_id": "local-principal:admin",
        "role": "admin",
    }
    assert null_approval_change["approval"] == {"status": "unapproved", "approved_by": None}
    for malformed_response, malformed_body, lookup_response, lookup_body in malformed_approval_responses:
        assert malformed_response.status == 400
        assert malformed_body["error"] == "invalid_template_request"
        assert malformed_body["message"].startswith("approved_by")
        assert lookup_response.status == 404
        assert lookup_body == {"error": "template_not_found"}


def test_poc_http_api_lists_representative_seed_templates() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.template_store = poc_web.TemplateStore.with_representative_defaults()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/api/templates")
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 200
    assert len(body["templates"]) == 4
    assert {template["template_id"] for template in body["templates"]} == {
        "batch-record",
        "deviation-report",
        "coa-summary",
        "validation-checklist",
    }


def test_poc_http_api_lists_conversion_jobs_filtered_by_status() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue(max_attempts=1)
    failed_job = server.job_queue.create_job(
        idempotency_key="failed-1",
        filename="failed-record.docx",
        mode="standard",
    )
    queued_job = server.job_queue.create_job(
        idempotency_key="queued-1",
        filename="queued-record.pdf",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    assert running.job_id == failed_job.job_id
    server.job_queue.mark_failed(failed_job.job_id, error="parser unavailable")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/api/jobs?status=queued")
        queued_response = connection.getresponse()
        queued_body = json.loads(queued_response.read().decode("utf-8"))
        connection.request("GET", "/api/jobs?status=failed")
        failed_response = connection.getresponse()
        failed_body = json.loads(failed_response.read().decode("utf-8"))
        retry_action = next(
            action
            for action in failed_body["jobs"][0]["available_actions"]
            if action["action"] == "retry_conversion"
        )
        event_payload = json.dumps(
            {
                "job_id": failed_job.job_id,
                "action": "retry_conversion",
                "audit_event": retry_action["audit_event"],
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=event_payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(event_payload))},
        )
        event_response = connection.getresponse()
        event_body = json.loads(event_response.read().decode("utf-8"))
        mismatched_payload = json.dumps(
            {
                "job_id": queued_job.job_id,
                "action": "retry_conversion",
                "audit_event": retry_action["audit_event"],
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=mismatched_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(mismatched_payload)),
            },
        )
        mismatched_response = connection.getresponse()
        mismatched_body = json.loads(mismatched_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert queued_response.status == 200
    assert [job["job_id"] for job in queued_body["jobs"]] == [queued_job.job_id]
    assert queued_body["jobs"][0]["status"] == "queued"
    assert [action["action"] for action in queued_body["jobs"][0]["available_actions"]] == [
        "open_detail"
    ]
    assert failed_response.status == 200
    assert [job["job_id"] for job in failed_body["jobs"]] == [failed_job.job_id]
    assert failed_body["jobs"][0]["status"] == "failed"
    assert event_response.status == 202
    assert event_body["audit_event"]["job_id"] == failed_job.job_id
    assert event_body["audit_event"]["action"] == "retry_conversion"
    assert event_body["job"]["job_id"] == failed_job.job_id
    assert event_body["job"]["status"] == "queued"
    assert event_body["job"]["error"] is None
    assert [action["action"] for action in event_body["job"]["available_actions"]] == [
        "open_detail"
    ]
    assert mismatched_response.status == 400
    assert mismatched_body["error"] == "invalid_job_event"
    next_job = server.job_queue.start_next_job()
    retried_job = server.job_queue.start_next_job()
    assert next_job is not None
    assert next_job.job_id == queued_job.job_id
    assert retried_job is not None
    assert retried_job.job_id == failed_job.job_id


def test_poc_http_api_persists_and_filters_job_audit_events() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue(max_attempts=1)
    failed_job = server.job_queue.create_job(
        idempotency_key="failed-1",
        filename="failed-record.docx",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    server.job_queue.mark_failed(failed_job.job_id, error="parser unavailable")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/api/jobs?status=failed")
        failed_response = connection.getresponse()
        failed_body = json.loads(failed_response.read().decode("utf-8"))
        retry_action = next(
            action
            for action in failed_body["jobs"][0]["available_actions"]
            if action["action"] == "retry_conversion"
        )
        event_payload = json.dumps(
            {
                "job_id": failed_job.job_id,
                "action": "retry_conversion",
                "audit_event": retry_action["audit_event"],
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=event_payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(event_payload))},
        )
        event_response = connection.getresponse()
        event_body = json.loads(event_response.read().decode("utf-8"))
        connection.request(
            "GET",
            f"/api/job-events?job_id={failed_job.job_id}&action=retry_conversion",
        )
        list_response = connection.getresponse()
        list_body = json.loads(list_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert failed_response.status == 200
    assert event_response.status == 202
    assert list_response.status == 200
    assert list_body == {"job_events": [event_body["audit_event"]]}


def test_poc_http_api_checks_job_audit_integrity_before_retrying_job() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue(max_attempts=1)
    server.job_event_store = JobAuditEventStore()
    failed_job = server.job_queue.create_job(
        idempotency_key="failed-1",
        filename="failed-record.docx",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    server.job_queue.mark_failed(failed_job.job_id, error="parser unavailable")
    server.job_event_store.record(
        {
            "event_type": "job.lifecycle",
            "job_id": "job-first",
            "action": "conversion_completed",
        }
    )
    server.job_event_store.record(
        {
            "event_type": "job.lifecycle",
            "job_id": "job-second",
            "action": "retry_conversion",
        }
    )
    del server.job_event_store._events[-1]  # noqa: SLF001
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/api/jobs?status=failed")
        failed_response = connection.getresponse()
        failed_body = json.loads(failed_response.read().decode("utf-8"))
        retry_action = next(
            action
            for action in failed_body["jobs"][0]["available_actions"]
            if action["action"] == "retry_conversion"
        )
        event_payload = json.dumps(
            {
                "job_id": failed_job.job_id,
                "action": "retry_conversion",
                "audit_event": retry_action["audit_event"],
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=event_payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(event_payload))},
        )
        event_response = connection.getresponse()
        event_body = json.loads(event_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert failed_response.status == 200
    assert event_response.status == 400
    assert event_body["error"] == "invalid_job_event"
    assert "audit log integrity violation" in event_body["message"]
    assert server.job_queue.get_job(failed_job.job_id).status == "failed"
    assert server.job_event_store.verify_integrity() == {
        "ok": False,
        "errors": [
            "audit log terminal sequence mismatch",
            "audit log head hash mismatch",
        ],
    }


def test_poc_http_api_requires_admin_role_for_retry_job_event() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue(max_attempts=1)
    server.local_auth_tokens = _local_auth_tokens()
    failed_job = server.job_queue.create_job(
        idempotency_key="failed-1",
        filename="failed-record.docx",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    server.job_queue.mark_failed(failed_job.job_id, error="parser unavailable")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "GET",
            "/api/jobs?status=failed",
            headers={"Authorization": "Bearer reviewer-token"},
        )
        failed_response = connection.getresponse()
        failed_body = json.loads(failed_response.read().decode("utf-8"))
        connection.request(
            "GET",
            f"/api/jobs/{failed_job.job_id}",
            headers={"Authorization": "Bearer reviewer-token"},
        )
        reviewer_detail_response = connection.getresponse()
        reviewer_detail_body = json.loads(reviewer_detail_response.read().decode("utf-8"))
        forged_retry_event = {
            "event_type": "conversion_job.action_requested",
            "job_id": failed_job.job_id,
            "job_status": "failed",
            "action": "retry_conversion",
        }
        event_payload = json.dumps(
            {
                "job_id": failed_job.job_id,
                "action": "retry_conversion",
                "audit_event": forged_retry_event,
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=event_payload,
            headers={
                "Authorization": "Bearer reviewer-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(event_payload)),
            },
        )
        reviewer_response = connection.getresponse()
        reviewer_body = json.loads(reviewer_response.read().decode("utf-8"))
        assert server.job_queue.get_job(failed_job.job_id).status == "failed"
        connection.request(
            "GET",
            "/api/jobs?status=failed",
            headers={"Authorization": "Bearer admin-token"},
        )
        admin_failed_response = connection.getresponse()
        admin_failed_body = json.loads(admin_failed_response.read().decode("utf-8"))
        retry_action = next(
            action
            for action in admin_failed_body["jobs"][0]["available_actions"]
            if action["action"] == "retry_conversion"
        )
        admin_event_payload = json.dumps(
            {
                "job_id": failed_job.job_id,
                "action": "retry_conversion",
                "audit_event": retry_action["audit_event"],
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=admin_event_payload,
            headers={
                "Authorization": "Bearer admin-token",
                "Content-Type": "application/json",
                "Content-Length": str(len(admin_event_payload)),
            },
        )
        admin_response = connection.getresponse()
        admin_body = json.loads(admin_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert failed_response.status == 200
    assert [action["action"] for action in failed_body["jobs"][0]["available_actions"]] == [
        "open_detail"
    ]
    assert reviewer_detail_response.status == 200
    assert [
        action["action"] for action in reviewer_detail_body["job"]["available_actions"]
    ] == ["open_detail"]
    assert reviewer_response.status == 403
    assert reviewer_body == {
        "error": "forbidden",
        "message": "role reviewer cannot perform jobs_retry",
    }
    assert admin_failed_response.status == 200
    assert admin_response.status == 202
    assert admin_body["job"]["status"] == "queued"
    assert server.job_queue.get_job(failed_job.job_id).status == "queued"


def test_poc_http_api_authenticates_job_events_before_parsing_payload() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue(max_attempts=1)
    server.local_auth_tokens = _local_auth_tokens()
    failed_job = server.job_queue.create_job(
        idempotency_key="failed-1",
        filename="failed-record.docx",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    server.job_queue.mark_failed(failed_job.job_id, error="parser unavailable")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = b"{not valid json"
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/job-events",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 401
    assert body == {
        "error": "auth_required",
        "message": "Authorization bearer token is required",
    }
    assert server.job_queue.get_job(failed_job.job_id).status == "failed"


def test_bundled_web_ui_plumbs_local_auth_token_into_api_fetches() -> None:
    html = (Path(__file__).resolve().parents[1] / "apps" / "web" / "index.html").read_text()

    assert '<script type="module">' in html
    assert "<script>" not in html
    assert 'id="auth-token"' in html
    assert 'let savedAuthToken = "";' in html
    assert "savedAuthToken = token" in html
    assert "savedAuthToken = \"\"" in html
    assert "function activeAuthToken()" in html
    assert "return savedAuthToken;" in html
    assert "const token = activeAuthToken();" in html
    assert "sessionStorage" not in html
    assert "localStorage" not in html
    assert "function apiFetch" in html
    assert "headers.Authorization = `Bearer ${token}`" in html
    assert "fetch(\"/api/convert\"" not in html
    assert "fetch(`/api/jobs${query}`)" not in html
    assert "fetch(\"/api/job-events\"" not in html
    assert "fetch(\"/api/review-events\"" not in html


def test_bundled_web_ui_exposes_audit_log_search_and_export() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    assert 'id="audit-title">Audit log' in html
    assert 'id="audit-job-id"' in html
    assert 'id="audit-document-id"' in html
    assert 'id="audit-action"' in html
    assert 'id="refresh-audit"' in html
    assert 'id="export-audit"' in html
    assert "async function refreshAuditLog()" in html
    assert "function exportAuditLog()" in html
    assert "function queryString(values)" in html
    assert "apiFetch(`/api/job-events${queryString({ job_id: jobId, action })}`)" in html
    assert (
        "apiFetch(`/api/review-events${queryString({ document_id: documentId, action })}`)"
        in html
    )
    assert "JSON.stringify({ audit_events: state.auditEvents }, null, 2)" in html
    assert 'link.download = "veridoc-audit-log.json"' in html
    assert "clearAuditLog()" in html


def test_bundled_web_ui_exposes_template_management_and_job_binding() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    assert 'id="templates-title">Templates' in html
    assert 'id="job-template"' in html
    assert 'id="template-id"' in html
    assert 'id="template-fields"' in html
    assert 'id="template-document-type"' in html
    assert 'id="template-status"' in html
    assert 'id="template-change-reason"' in html
    assert 'id="template-actor"' in html
    assert 'id="template-anchors"' in html
    assert 'id="template-tables"' in html
    assert 'id="template-risk-rank"' in html
    assert 'id="template-validation-rules"' in html
    assert 'id="template-output-mapping"' in html
    assert 'id="template-version-select"' in html
    assert 'id="template-detail-raw"' in html
    assert 'id="save-template"' in html
    assert "async function loadTemplates()" in html
    assert "async function saveTemplateVersion()" in html
    assert "async function loadTemplateDetail(templateId)" in html
    assert "function renderTemplateDetail(template)" in html
    assert 'apiFetch("/api/templates")' in html
    assert 'apiFetch("/api/templates", {' in html
    assert "document_type: templateDocumentType.value" in html
    assert "status: templateStatus.value" in html
    assert "change_reason: templateChangeReason.value" in html
    assert 'actor: { principal_id: templateActor.value, role: "admin" }' in html
    assert "anchors: parseTemplateJson(templateAnchors, \"anchors\")" in html
    assert "risk_rank: parseTemplateJson(templateRiskRank, \"risk_rank\")" in html
    assert "validation_rules: parseTemplateJson(templateValidationRules, \"validation_rules\")" in html
    assert "output_mapping: parseTemplateJson(templateOutputMapping, \"output_mapping\")" in html
    assert "template_id: jobTemplate.value || undefined" in html
    assert "job.template.template_version" in html
    assert "clearTemplateState()" in html


def test_bundled_web_ui_clears_credential_bound_state_when_auth_token_changes() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    save_handler = re.search(
        r'saveAuthToken\.addEventListener\("click", \(\) => \{(?P<body>.*?)\n      \}\);',
        html,
        re.DOTALL,
    )
    clear_handler = re.search(
        r'clearAuthToken\.addEventListener\("click", \(\) => \{(?P<body>.*?)\n      \}\);',
        html,
        re.DOTALL,
    )
    credential_clear = re.search(
        r"function clearCredentialBoundState\(\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )
    review_clear = re.search(
        r"function clearReviewResult\(\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )
    preview_clear = re.search(
        r"function clearPreview\(\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )

    assert save_handler is not None
    assert clear_handler is not None
    assert credential_clear is not None
    assert review_clear is not None
    assert preview_clear is not None
    save_body = save_handler.group("body")
    clear_body = clear_handler.group("body")
    credential_clear_body = credential_clear.group("body")
    review_clear_body = review_clear.group("body")
    preview_clear_body = preview_clear.group("body")
    assert save_body.index("clearCredentialBoundState()") < save_body.index(
        "savedAuthToken = token"
    )
    assert save_body.index("savedAuthToken = token") < save_body.index("authToken.value = \"\"")
    assert clear_body.index("savedAuthToken = \"\"") < clear_body.index(
        "clearCredentialBoundState()"
    )
    assert clear_body.index("clearCredentialBoundState()") < clear_body.index("loadJobs()")
    assert "state.authGeneration += 1" in credential_clear_body
    assert "state.directConversionToken += 1" in credential_clear_body
    assert "button.disabled = false" in credential_clear_body
    assert "createJob.disabled = false" in credential_clear_body
    assert "clearJobState()" in credential_clear_body
    assert "clearSourcePreview()" in credential_clear_body
    assert "clearReviewResult()" in credential_clear_body
    assert "reviewList.replaceChildren()" in review_clear_body
    assert "rawResult.textContent = \"\"" in review_clear_body
    assert "clearDownload()" in review_clear_body
    assert "state.latestResult = null" in preview_clear_body
    assert "state.availableReviewActions = []" in preview_clear_body


def test_bundled_web_ui_guards_credential_bound_job_responses() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    def js_between(start: str, end: str) -> str:
        start_index = html.index(start)
        return html[start_index:html.index(end, start_index)]

    create_job_body = js_between(
        "async function createConversionJob()",
        "async function loadJobDetail(jobId)",
    )
    load_detail_body = js_between(
        "async function loadJobDetail(jobId)",
        "async function sendJobAction(",
    )
    send_action_body = js_between(
        "async function sendJobAction(",
        "async function downloadSelectedJobResult()",
    )
    download_selected_body = js_between(
        "async function downloadSelectedJobResult()",
        "async function retrySelectedConversion()",
    )
    retry_body = js_between(
        "async function retrySelectedConversion()",
        "async function downloadJobResult(",
    )
    download_result_body = js_between(
        "async function downloadJobResult(",
        "function downloadFilename(response, job)",
    )

    assert create_job_body.index("await loadJobs();") < create_job_body.index(
        "if (!isActiveCredentialRequest(requestAuthToken, requestAuthGeneration)) return;",
        create_job_body.index("await loadJobs();"),
    ) < create_job_body.index("renderDetail(body.job);", create_job_body.index("await loadJobs();"))
    assert "const requestAuthToken = activeAuthToken();" in create_job_body
    assert "const requestAuthToken = activeAuthToken();" in load_detail_body
    assert load_detail_body.index("const body = await response.json();") < load_detail_body.index(
        "if (!isActiveCredentialRequest(requestAuthToken, requestAuthGeneration)) return;"
    ) < load_detail_body.index("renderDetail(body.job);")
    assert send_action_body.index("const body = await response.json();") < send_action_body.index(
        "if (!isActiveCredentialRequest(requestAuthToken, requestAuthGeneration)) return null;"
    ) < send_action_body.index("if (!response.ok)")
    assert '"download_result",\n            requestAuthToken,\n            requestAuthGeneration' in download_selected_body
    assert "const downloaded = await downloadJobResult(" in download_selected_body
    assert "if (!downloaded || !isActiveCredentialRequest" in download_selected_body
    assert '"retry_conversion",\n            requestAuthToken,\n            requestAuthGeneration' in retry_body
    assert retry_body.index("await loadJobs();") < retry_body.index(
        "if (!isActiveCredentialRequest(requestAuthToken, requestAuthGeneration)) return;",
        retry_body.index("await loadJobs();"),
    ) < retry_body.index("renderDetail(body.job);", retry_body.index("await loadJobs();"))
    assert "return false;" in download_result_body
    assert "return true;" in download_result_body


def test_bundled_web_ui_scopes_review_actions_from_api_permissions() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    render_result = re.search(
        r"function renderResult\(result\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )
    render_item = re.search(
        r"function renderReviewItem\(item\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )
    pending_handler = re.search(
        r"function setReviewActionPending\(item, isPending\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )

    assert render_result is not None
    assert render_item is not None
    assert pending_handler is not None
    assert "result.available_review_actions" in render_result.group("body")
    assert 'approve.dataset.reviewActionName = "approve"' in render_item.group("body")
    assert 'approve.disabled = !reviewActionAvailable(item, "approve")' in render_item.group("body")
    assert 'requestEdit.dataset.reviewActionName = "edit"' in render_item.group("body")
    assert 'requestEdit.disabled = !reviewActionAvailable(item, "edit")' in render_item.group("body")
    assert "control.dataset.reviewActionName" in pending_handler.group("body")


def test_bundled_web_ui_lists_review_items_with_operator_summary() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    render_result = re.search(
        r"function renderResult\(result\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )
    render_items = re.search(
        r"function renderReviewItems\(items\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )
    render_item = re.search(
        r"function renderReviewItem\(item\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )

    assert render_result is not None
    assert render_items is not None
    assert render_item is not None
    assert "renderReviewItems(result.review_items || [])" in render_result.group("body")
    assert "自動検証上の要確認なし" in render_items.group("body")
    assert "安全を確定するものではありません" in render_items.group("body")
    assert "review-empty" in render_items.group("body")
    assert "sourcePageLabel(item)" in render_item.group("body")
    assert "formatSourceBbox(item.source_bbox)" in render_item.group("body")
    assert "formatSourceConfidence(item.source_confidence)" in render_item.group("body")
    assert "sourceAvailabilityLabel(item)" in render_item.group("body")
    assert "item.source_id || \"unknown source\"" in render_item.group("body")
    assert "item.block_id || \"unknown block\"" in render_item.group("body")
    assert "descriptor.code" in render_item.group("body")
    assert "descriptor.severity" in render_item.group("body")
    assert "warningBadgeDescriptor" in render_item.group("body")
    assert "function sourceAvailabilityLabel(item)" in html
    assert "出典不明" in html
    assert "function formatSourceBbox(bbox)" in html
    assert "function formatSourceConfidence(value)" in html
    assert "function sourceConfidenceAvailable(value)" in html
    assert "sourceConfidenceAvailable(confidence)" in html
    assert "sourceConfidenceAvailable(value)" in html
    assert "value >= 0 && value <= 1" in html
    assert "review-item-meta" in html
    assert "review-item-target" in html


def test_poc_http_api_sanitizes_succeeded_job_result_and_downloads_result() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    created = server.job_queue.create_job(
        idempotency_key="converted-1",
        filename="converted-record.pdf",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    server.job_queue.mark_succeeded(
        created.job_id,
        result={
            "status": "converted",
            "document_ir": {"document": {"title": "stored conversion payload"}},
            "download": {
                "filename": "nested\\invoice📄\r\nX-Test: 1.pdf",
                "content_type": "application/json; charset=utf-8",
                "content": b'{"converted": true}',
            },
        },
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/api/jobs")
        list_response = connection.getresponse()
        list_body = json.loads(list_response.read().decode("utf-8"))
        connection.request("GET", f"/api/jobs/{created.job_id}")
        detail_response = connection.getresponse()
        detail_body = json.loads(detail_response.read().decode("utf-8"))
        connection.request("GET", f"/api/jobs/{created.job_id}/result")
        download_response = connection.getresponse()
        download_content_type = download_response.getheader("Content-Type")
        download_disposition = download_response.getheader("Content-Disposition")
        injected_header = download_response.getheader("X-Test")
        download_body = download_response.read()
    finally:
        server.shutdown()
        thread.join(timeout=5)

    list_job = list_body["jobs"][0]
    detail_job = detail_body["job"]
    assert list_response.status == 200
    assert detail_response.status == 200
    assert "result" not in list_job
    assert "result" not in detail_job
    assert list_job["has_result"] is True
    assert detail_job["has_result"] is True
    assert [action["action"] for action in detail_job["available_actions"]] == [
        "open_detail",
        "download_result",
    ]
    assert "stored conversion payload" not in json.dumps(list_body)
    assert "stored conversion payload" not in json.dumps(detail_body)
    assert download_response.status == 200
    assert download_content_type == "application/json; charset=utf-8"
    assert download_disposition == 'attachment; filename="invoiceX-Test: 1.pdf"'
    assert injected_header is None
    assert download_body == b'{"converted": true}'


def test_poc_http_api_summarizes_job_progress_without_exposing_result_payload() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    created = server.job_queue.create_job(
        idempotency_key="review-required-summary-1",
        filename="review-record.pdf",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    server.job_queue.mark_succeeded(
        created.job_id,
        result={
            "status": "requires_review",
            "document_ir": {"document": {"title": "hidden review payload"}},
            "warnings": ["low confidence", "bbox missing"],
        },
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/api/jobs")
        list_response = connection.getresponse()
        list_body = json.loads(list_response.read().decode("utf-8"))
        connection.request("GET", f"/api/jobs/{created.job_id}")
        detail_response = connection.getresponse()
        detail_body = json.loads(detail_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    list_job = list_body["jobs"][0]
    detail_job = detail_body["job"]
    assert list_response.status == 200
    assert detail_response.status == 200
    assert list_job["display_status"] == "review_required"
    assert detail_job["display_status"] == "review_required"
    assert detail_job["progress_percent"] == 100
    assert detail_job["warning_count"] == 2
    assert "result" not in list_job
    assert "result" not in detail_job
    assert "hidden review payload" not in json.dumps(list_body)
    assert "hidden review payload" not in json.dumps(detail_body)


def test_poc_http_api_preserves_blocked_conversion_job_display_status() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    created = server.job_queue.create_job(
        idempotency_key="blocked-summary-1",
        filename="blocked-record.pdf",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    server.job_queue.mark_succeeded(
        created.job_id,
        result={
            "status": "blocked",
            "document_ir": {"document": {"title": "hidden blocked payload"}},
            "warnings": ["unsupported block type"],
        },
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/api/jobs")
        list_response = connection.getresponse()
        list_body = json.loads(list_response.read().decode("utf-8"))
        connection.request("GET", f"/api/jobs/{created.job_id}")
        detail_response = connection.getresponse()
        detail_body = json.loads(detail_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    list_job = list_body["jobs"][0]
    detail_job = detail_body["job"]
    assert list_response.status == 200
    assert detail_response.status == 200
    assert list_job["display_status"] == "blocked"
    assert detail_job["display_status"] == "blocked"
    assert detail_job["progress_percent"] == 100
    assert detail_job["warning_count"] == 1
    assert "result" not in list_job
    assert "result" not in detail_job
    assert "hidden blocked payload" not in json.dumps(list_body)
    assert "hidden blocked payload" not in json.dumps(detail_body)


def test_poc_http_api_detects_output_hash_mismatch_and_blocks_redownload() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    created = server.job_queue.create_job(
        idempotency_key="converted-hash-mismatch-1",
        filename="converted-record.pdf",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    original_output = b'{"converted": true}'
    tampered_output = b'{"converted": false}'
    server.job_queue.mark_succeeded(
        created.job_id,
        result={
            "status": "converted",
            "hashes": {
                "source_sha256": hashlib.sha256(b"original source").hexdigest(),
                "output_sha256": hashlib.sha256(original_output).hexdigest(),
            },
            "download": {
                "filename": "converted-record.veridoc-result.json",
                "content_type": "application/json; charset=utf-8",
                "content": tampered_output,
            },
        },
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/api/jobs")
        list_response = connection.getresponse()
        list_body = json.loads(list_response.read().decode("utf-8"))
        connection.request("GET", f"/api/jobs/{created.job_id}")
        detail_response = connection.getresponse()
        detail_body = json.loads(detail_response.read().decode("utf-8"))
        connection.request("GET", f"/api/jobs/{created.job_id}/result")
        download_response = connection.getresponse()
        download_body = json.loads(download_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    list_job = list_body["jobs"][0]
    detail_job = detail_body["job"]
    assert list_response.status == 200
    assert detail_response.status == 200
    assert list_job["has_result"] is False
    assert detail_job["has_result"] is False
    assert [action["action"] for action in detail_job["available_actions"]] == [
        "open_detail"
    ]
    assert list_job["hash_verification"]["output"]["status"] == "mismatch"
    assert detail_job["hash_verification"]["output"]["expected_sha256"] == hashlib.sha256(
        original_output
    ).hexdigest()
    assert detail_job["hash_verification"]["output"]["actual_sha256"] == hashlib.sha256(
        tampered_output
    ).hexdigest()
    assert detail_job["hashes"]["source_sha256"] == hashlib.sha256(b"original source").hexdigest()
    assert download_response.status == 409
    assert download_body == {
        "error": "job_result_integrity_mismatch",
        "message": "job result output hash does not match stored content",
    }


def test_poc_http_api_detects_source_hash_mismatch_and_blocks_redownload() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    uploaded_content = b"%PDF-1.7\noriginal source"
    uploaded_sha256 = hashlib.sha256(uploaded_content).hexdigest()
    created = server.job_queue.create_job(
        idempotency_key="converted-source-mismatch-1",
        filename="converted-record.pdf",
        mode="standard",
        source={
            "filename": "converted-record.pdf",
            "content_type": "application/pdf",
            "size_bytes": len(uploaded_content),
            "sha256": uploaded_sha256,
            "content": uploaded_content,
        },
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    download_content = b'{"converted": true}'
    worker_source_sha256 = hashlib.sha256(b"different source").hexdigest()
    output_sha256 = hashlib.sha256(download_content).hexdigest()
    server.job_queue.mark_succeeded(
        created.job_id,
        result={
            "status": "converted",
            "hashes": {
                "source_sha256": worker_source_sha256,
                "output_sha256": output_sha256,
            },
            "download": {
                "filename": "converted-record.veridoc-result.json",
                "content_type": "application/json; charset=utf-8",
                "content": download_content,
            },
        },
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/api/jobs")
        list_response = connection.getresponse()
        list_body = json.loads(list_response.read().decode("utf-8"))
        connection.request("GET", f"/api/jobs/{created.job_id}")
        detail_response = connection.getresponse()
        detail_body = json.loads(detail_response.read().decode("utf-8"))
        connection.request("GET", f"/api/jobs/{created.job_id}/result")
        download_response = connection.getresponse()
        download_body = json.loads(download_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    list_job = list_body["jobs"][0]
    detail_job = detail_body["job"]
    assert list_response.status == 200
    assert detail_response.status == 200
    assert list_job["has_result"] is False
    assert detail_job["has_result"] is False
    assert list_job["hash_verification"]["source"] == {
        "status": "mismatch",
        "expected_sha256": uploaded_sha256,
        "actual_sha256": worker_source_sha256,
    }
    assert list_job["hashes"]["source_sha256"] == uploaded_sha256
    assert detail_job["hashes"]["source_sha256"] == uploaded_sha256
    assert detail_job["hash_verification"]["output"]["status"] == "match"
    assert [action["action"] for action in detail_job["available_actions"]] == [
        "open_detail"
    ]
    assert download_response.status == 409
    assert download_body == {
        "error": "job_result_integrity_mismatch",
        "message": "job result source hash does not match uploaded source",
    }


def test_poc_http_api_rejects_malformed_download_content_type() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    created = server.job_queue.create_job(
        idempotency_key="converted-bad-content-type-1",
        filename="converted-record.pdf",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    server.job_queue.mark_succeeded(
        created.job_id,
        result={
            "status": "converted",
            "download": {
                "filename": "converted-record.veridoc-result.json",
                "content_type": "application/json\r\nX-Test: 1",
                "content": b'{"converted": true}',
            },
        },
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/api/jobs")
        list_response = connection.getresponse()
        list_body = json.loads(list_response.read().decode("utf-8"))
        connection.request("GET", f"/api/jobs/{created.job_id}")
        detail_response = connection.getresponse()
        detail_body = json.loads(detail_response.read().decode("utf-8"))
        connection.request("GET", f"/api/jobs/{created.job_id}/result")
        download_response = connection.getresponse()
        injected_header = download_response.getheader("X-Test")
        download_body = json.loads(download_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert list_response.status == 200
    assert detail_response.status == 200
    assert download_response.status == 400
    assert injected_header is None
    assert download_body["error"] == "job_result_unavailable"
    assert list_body["jobs"][0]["has_result"] is False
    assert detail_body["job"]["has_result"] is False
    assert [action["action"] for action in detail_body["job"]["available_actions"]] == [
        "open_detail"
    ]


def test_download_content_type_rejects_crlf_before_parameter_separator() -> None:
    with pytest.raises(ValueError, match="content type is invalid"):
        poc_web._download_content_type("application/json\r\n;charset=utf-8")


def test_poc_http_api_hides_download_action_without_download_payload() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    created = server.job_queue.create_job(
        idempotency_key="converted-no-download-1",
        filename="converted-record.pdf",
        mode="standard",
    )
    running = server.job_queue.start_next_job()
    assert running is not None
    server.job_queue.mark_succeeded(created.job_id, result={"status": "converted"})
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/api/jobs")
        list_response = connection.getresponse()
        list_body = json.loads(list_response.read().decode("utf-8"))
        connection.request("GET", f"/api/jobs/{created.job_id}")
        detail_response = connection.getresponse()
        detail_body = json.loads(detail_response.read().decode("utf-8"))
        connection.request("GET", f"/api/jobs/{created.job_id}/result")
        download_response = connection.getresponse()
        download_body = json.loads(download_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert list_response.status == 200
    assert detail_response.status == 200
    assert download_response.status == 400
    assert download_body["error"] == "job_result_unavailable"
    assert list_body["jobs"][0]["has_result"] is False
    assert detail_body["job"]["has_result"] is False
    assert [action["action"] for action in detail_body["job"]["available_actions"]] == [
        "open_detail"
    ]


def test_poc_http_api_hides_high_quality_retry_while_another_high_quality_job_is_active() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue(max_attempts=1)
    failed_job = server.job_queue.create_job(
        idempotency_key="failed-hq-1",
        filename="failed-record.pdf",
        mode="high_quality",
    )
    failed_running = server.job_queue.start_next_job()
    assert failed_running is not None
    server.job_queue.mark_failed(failed_job.job_id, error="parser unavailable")
    active_job = server.job_queue.create_job(
        idempotency_key="active-hq-1",
        filename="active-record.pdf",
        mode="high_quality",
    )
    active_running = server.job_queue.start_next_job()
    assert active_running is not None
    assert active_running.job_id == active_job.job_id
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/api/jobs?status=failed")
        failed_response = connection.getresponse()
        failed_body = json.loads(failed_response.read().decode("utf-8"))
        event_payload = json.dumps(
            {
                "job_id": failed_job.job_id,
                "action": "retry_conversion",
                "audit_event": {
                    "event_type": "conversion_job.action_requested",
                    "job_id": failed_job.job_id,
                    "job_status": "failed",
                    "action": "retry_conversion",
                },
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/job-events",
            body=event_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(event_payload)),
            },
        )
        event_response = connection.getresponse()
        event_body = json.loads(event_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert failed_response.status == 200
    assert [action["action"] for action in failed_body["jobs"][0]["available_actions"]] == [
        "open_detail"
    ]
    assert event_response.status == 400
    assert event_body["error"] == "invalid_job_event"
    assert server.job_queue.get_job(failed_job.job_id).status == "failed"


def test_poc_http_api_rejects_second_active_high_quality_job() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        first_payload = json.dumps(
            {
                "idempotency_key": "hq-1",
                "filename": "batch-record.pdf",
                "mode": "high_quality",
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/jobs",
            body=first_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(first_payload)),
            },
        )
        created_response = connection.getresponse()
        created_response.read()
        server.job_queue.start_next_job()
        second_payload = json.dumps(
            {
                "idempotency_key": "hq-2",
                "filename": "batch-record.pdf",
                "mode": "high_quality",
            }
        ).encode("utf-8")
        connection.request(
            "POST",
            "/api/jobs",
            body=second_payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(second_payload)),
            },
        )
        conflict_response = connection.getresponse()
        conflict = json.loads(conflict_response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert created_response.status == 202
    assert conflict_response.status == 409
    assert conflict == {
        "error": "job_conflict",
        "message": "high_quality job already active",
    }


def test_poc_http_api_accepts_base64_docx_upload(tmp_path: Path) -> None:
    docx_path = tmp_path / "batch-record.docx"
    _write_docx(docx_path, _sample_docx_xml())
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "filename": "batch-record.docx",
                "content_base64": base64.b64encode(docx_path.read_bytes()).decode("ascii"),
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 200
    assert body["document_ir"]["document"]["source_type"] == "docx"
    assert body["document_ir"]["blocks"][0]["text"] == "Batch Summary"


def test_poc_http_api_rejects_unsupported_non_utf8_binary_upload() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "filename": "upload.bin",
                "content_base64": base64.b64encode(b"\xff\xfe\x00\x01").decode("ascii"),
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body == {
        "error": "invalid_upload",
        "message": "unsupported binary upload; use .pdf, .docx, .xlsx, or UTF-8 JSON/text",
    }


def test_poc_http_api_rejects_unknown_conversion_mode() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "filename": "upload.txt",
                "content": "Unstructured OCR fallback text",
                "conversion_mode": "spreadsheet_magic",
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body == {
        "error": "invalid_upload",
        "message": "unsupported conversion_mode: spreadsheet_magic",
    }


def test_poc_http_api_reflects_unavailable_llm_and_unsupported_ocr_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VERIDOC_STANDARD_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("VERIDOC_HIGH_QUALITY_OPENAI_BASE_URL", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "filename": "phase0-output.json",
                "content": json.dumps(
                    {
                        "pages": [
                            {
                                "page_number": 1,
                                "width": 320,
                                "height": 240,
                                "unit": "pt",
                                "fragments": [{"text": "Lot: SAMPLE-001", "confidence": 0.95}],
                            }
                        ]
                    }
                ),
                "conversion_mode": "auto",
                "use_llm": True,
                "use_ocr": True,
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 200
    assert body["audit"]["conversion_settings"] == {
        "use_llm": {
            "requested": True,
            "enabled": False,
            "status": "blocked",
            "reason": "missing_configured_profile",
        },
        "use_ocr": {"requested": True, "enabled": False, "status": "unsupported"},
    }
    assert (
        "LLM conversion plan fallback llm_fallback_unavailable: "
        "LLM conversion plan unavailable: no configured local LLM profile; "
        "deterministic conversion used; requires review"
    ) in body["warnings"]
    assert "OCR conversion setting is not implemented in the local PoC API" in body["warnings"]
    assert body["audit"]["conversion_plan"] == {
        "requested": True,
        "status": "fallback",
        "adopted": False,
        "schema_version": 1,
        "plan_hash": None,
        "reason": "missing_configured_profile",
        "warning_code": "llm_fallback_unavailable",
    }


def test_poc_http_api_rejects_external_llm_endpoint_before_conversion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERIDOC_STANDARD_OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("VERIDOC_STANDARD_MODEL", "cloud-model")
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "filename": "phase0-output.json",
                "content": json.dumps(
                    {
                        "pages": [
                            {
                                "page_number": 1,
                                "width": 320,
                                "height": 240,
                                "unit": "pt",
                                "fragments": [
                                    {
                                        "text": "DO-NOT-SEND-DOCUMENT-BODY",
                                        "confidence": 0.95,
                                    }
                                ],
                            }
                        ]
                    }
                ),
                "conversion_mode": "auto",
                "use_llm": True,
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body_text = response.read().decode("utf-8")
        body = json.loads(body_text)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body["error"] == "llm_configuration_rejected"
    assert body["audit"]["conversion_settings"]["use_llm"] == {
        "requested": True,
        "enabled": False,
        "status": "blocked",
        "reason": "non_local_endpoint",
    }
    assert body["warnings"] == [
        "LLM conversion blocked: configured endpoint must be local-only"
    ]
    assert "api.openai.com" not in body_text
    assert "DO-NOT-SEND-DOCUMENT-BODY" not in body_text


def test_poc_http_api_rejects_placeholder_llm_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERIDOC_STANDARD_OPENAI_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("VERIDOC_STANDARD_MODEL", "local-json-model")
    monkeypatch.setenv("VERIDOC_STANDARD_OPENAI_API_KEY", "fake-api-key")
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "filename": "phase0-output.json",
                "content": json.dumps(
                    {
                        "pages": [
                            {
                                "page_number": 1,
                                "width": 320,
                                "height": 240,
                                "unit": "pt",
                                "fragments": [{"text": "Lot: SAMPLE-001", "confidence": 0.95}],
                            }
                        ]
                    }
                ),
                "conversion_mode": "auto",
                "use_llm": True,
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body_text = response.read().decode("utf-8")
        body = json.loads(body_text)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body["error"] == "llm_configuration_rejected"
    assert body["audit"]["conversion_settings"]["use_llm"]["reason"] == "placeholder_api_key"
    assert body["warnings"] == [
        "LLM conversion blocked: configured API key is not trusted"
    ]
    assert "fake-api-key" not in body_text


def test_poc_http_api_allows_configured_local_llm_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERIDOC_STANDARD_OPENAI_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("VERIDOC_STANDARD_MODEL", "local-json-model")
    plan = {
        "schema_version": 1,
        "source_kind": "synthetic_text",
        "operations": [
            {
                "id": "extract-lot",
                "action": "extract_field",
                "inputs": ["Lot: SAMPLE-001"],
                "output": "lot_number",
                "rationale": "The lot value is explicitly present in the source text.",
            }
        ],
        "constraints": {"external_transmission": False},
    }

    class FakeLocalLLMAdapter:
        def create_conversion_plan(self, synthetic_text: str) -> dict[str, object]:
            return plan

    monkeypatch.setattr(
        poc_web,
        "_configured_llm_conversion_plan_adapter",
        lambda: (FakeLocalLLMAdapter(), None),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "filename": "phase0-output.json",
                "content": json.dumps(
                    {
                        "pages": [
                            {
                                "page_number": 1,
                                "width": 320,
                                "height": 240,
                                "unit": "pt",
                                "fragments": [{"text": "Lot: SAMPLE-001", "confidence": 0.95}],
                            }
                        ]
                    }
                ),
                "conversion_mode": "auto",
                "use_llm": True,
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 200
    assert body["audit"]["conversion_settings"]["use_llm"] == {
        "requested": True,
        "enabled": True,
        "status": "enabled",
    }
    assert body["audit"]["conversion_plan"]["status"] == "adopted"
    assert body["audit"]["conversion_plan"]["plan"] == plan


def test_poc_http_api_rejects_non_boolean_conversion_settings() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "filename": "upload.txt",
                "content": "Unstructured OCR fallback text",
                "use_llm": "true",
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body == {"error": "invalid_upload", "message": "use_llm must be boolean"}


def test_poc_http_api_rejects_too_long_binary_filename_as_json_error() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "filename": f"{'a' * 300}.pdf",
                "content_base64": base64.b64encode(b"%PDF sample bytes").decode("ascii"),
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body == {
        "error": "invalid_upload",
        "message": "PDF parser failed; upload requires a valid PDF file",
    }


def test_poc_http_api_surfaces_missing_pdf_dependency(monkeypatch) -> None:
    def missing_pdf_parser(upload_path: Path, *, document_id: str) -> dict:
        raise poc_web.MissingPdfExtractorDependency("pymupdf unavailable")

    monkeypatch.setattr(poc_web, "parse_text_pdf_to_document_ir", missing_pdf_parser)
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps(
            {
                "filename": "upload.pdf",
                "content_base64": base64.b64encode(b"%PDF sample bytes").decode("ascii"),
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 500
    assert body == {
        "error": "server_dependency_unavailable",
        "message": "PDF parser dependency is unavailable; install requirements-pdf-eval.txt",
    }


def test_poc_http_api_rejects_non_object_json_root() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps([]).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 400
    assert body == {"error": "invalid_upload", "message": "request JSON root must be an object"}


def test_poc_http_api_returns_strict_json_for_blocked_numeric_output() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        parser_output = {
            "pages": [
                {
                    "page_number": 1,
                    "width": 320,
                    "height": 240,
                    "unit": "pt",
                    "fragments": [
                        {
                            "text": "Bad coordinate",
                            "bbox": {
                                "x": "left",
                                "y": 20,
                                "width": 120,
                                "height": 16,
                                "unit": "pt",
                            },
                            "confidence": 0.95,
                        }
                    ],
                }
            ]
        }
        payload = json.dumps(
            {
                "filename": "phase0-output.json",
                "content": json.dumps(parser_output),
            }
        ).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request(
            "POST",
            "/api/convert",
            body=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        )
        response = connection.getresponse()
        response_text = response.read().decode("utf-8")
        body = json.loads(response_text)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert response.status == 200
    assert "NaN" not in response_text
    assert body["status"] == "blocked"
    assert body["document_ir"]["blocks"][0]["bbox"]["x"] is None
    downloaded = json.loads(body["download"]["content_text"])
    assert downloaded["document_ir"]["blocks"][0]["bbox"]["x"] is None


def test_poc_web_script_entrypoint_can_bootstrap_repo_imports() -> None:
    result = subprocess.run(
        [sys.executable, "services/api/poc_web.py", "--check"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_readme_documents_local_poc_api_startup_and_smoke_contract() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    expected_snippets = [
        "## Local PoC API startup and smoke checks",
        "python3 services/api/poc_web.py --check",
        "Expected result: the command exits with status `0` and prints no output.",
        "python3 services/api/poc_web.py",
        "http://127.0.0.1:8788",
        "GET /",
        "POST /api/convert",
        '"conversion_mode": "auto"',
        '"use_llm": False',
        '"use_ocr": False',
        "PoC API smoke check passed",
        "`pdf_to_excel`",
        "`pdf_to_word`",
        "`word_to_excel`",
        "`excel_to_word`",
        "`audit.conversion_settings`",
        "not yet implemented in the local PoC API",
        "Renderer-backed DOCX and XLSX primary artifacts are returned",
        "metadata.download.field",
        "exact PDF layout, fonts, coordinates, columns, footnotes,",
    ]

    for snippet in expected_snippets:
        assert snippet in readme

    assert str(poc_web.MAX_UPLOAD_BYTES // (1024 * 1024)) + " MiB" in readme
    for mode in poc_web.CONVERSION_MODE_SOURCE_TYPES:
        assert f"`{mode}`" in readme


def test_web_upload_preserves_file_bytes() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    assert "file.arrayBuffer()" in html
    assert "content_base64" in html
    assert "file.text()" not in html


def test_web_direct_convert_selects_and_posts_conversion_mode() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    assert 'id="direct-conversion-mode"' in html
    assert '<option value="auto">auto</option>' in html
    assert '<option value="pdf_to_excel">pdf_to_excel</option>' in html
    assert '<option value="pdf_to_word">pdf_to_word</option>' in html
    assert '<option value="word_to_excel">word_to_excel</option>' in html
    assert '<option value="excel_to_word">excel_to_word</option>' in html
    assert "const directConversionMode = document.querySelector(\"#direct-conversion-mode\")" in html
    assert "conversion_mode: directConversionMode.value" in html


def test_web_direct_convert_selects_and_posts_llm_ocr_settings() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")
    parser = _PocUiRegionParser()
    parser.feed(html)

    assert 'id="direct-use-llm"' in html
    assert 'id="direct-use-ocr"' in html
    assert "const directUseLlm = document.querySelector(\"#direct-use-llm\")" in html
    assert "const directUseOcr = document.querySelector(\"#direct-use-ocr\")" in html
    assert "use_llm: directUseLlm.checked" in html
    assert "use_ocr: directUseOcr.checked" in html
    assert "use_llm" in parser.region_fields["conversion-settings"]
    assert "use_ocr" in parser.region_fields["conversion-settings"]


def test_web_upload_settings_exposes_phase10_input_and_conversion_controls() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")
    parser = _PocUiRegionParser()
    parser.feed(html)
    click_handler = re.search(
        r'button\.addEventListener\("click", async \(\) => \{(?P<body>.*?)\n      \}\);',
        html,
        re.DOTALL,
    )

    expected_controls = [
        'id="upload-dropzone"',
        'id="upload-format-status"',
        'id="direct-output-format"',
        'id="direct-template"',
        'id="direct-use-ocr"',
        'id="direct-use-llm"',
        'id="gmp-setting-notice"',
        'id="unsupported-settings-note"',
    ]
    for control in expected_controls:
        assert control in html

    assert 'accept=".pdf,.docx,.xlsx,.json,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/json"' in html
    assert '<option value="json">Audit/debug JSON</option>' in html
    assert '<option value="docx">DOCX</option>' in html
    assert '<option value="xlsx">XLSX</option>' in html
    assert '<option value="">No template</option>' in html
    assert "PDF / DOCX / XLSX" in html
    assert "JSON is retained for audit/debug output, not required for choosing conversion settings." in html

    assert "conversion-settings" in parser.region_fields
    for field in [
        "conversion_mode",
        "output_format",
        "template_id",
        "use_llm",
        "use_ocr",
        "gmp_notice",
        "unsupported_settings",
    ]:
        assert field in parser.region_fields["conversion-settings"]

    assert click_handler is not None
    click_body = click_handler.group("body")
    assert "output_format: directOutputFormat.value" in click_body
    assert "template_id: directTemplate.value || undefined" in click_body
    assert "updateUploadFormatStatus(file)" in html


def test_web_direct_convert_defines_phase6_review_information_architecture() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")
    readme = Path("README.md").read_text(encoding="utf-8")
    parser = _PocUiRegionParser()
    parser.feed(html)

    expected_regions = {
        "upload": ["content_base64", "document_ir"],
        "conversion-settings": ["conversion_mode"],
        "review": ["review_items", "warnings", "document_ir"],
        "artifact-downloads": ["artifacts[]", "download", "audit"],
        "detail-json": ["document_ir", "review_items", "warnings", "artifacts[]", "audit"],
    }

    for region, fields in expected_regions.items():
        marker = f'data-poc-ui-region="{region}"'
        assert marker in html
        for field in fields:
            assert field in parser.region_fields[region]

    for element_id in [
        "top-level-warnings",
        "review-list",
        "review-action-status",
        "pdf-preview-panel",
        "bbox-layer",
    ]:
        assert "review" in parser.element_regions[element_id]
    assert 'function renderTopLevelWarnings(warnings)' in html
    assert "renderTopLevelWarnings(result.warnings || [])" in html
    for element_id in [
        "artifact-summary",
        "download-link",
        "debug-download-link",
    ]:
        assert "artifact-downloads" in parser.element_regions[element_id]
    assert "detail-json" in parser.element_regions["raw-result"]

    assert html.index('data-poc-ui-region="review"') < html.index(
        'data-poc-ui-region="detail-json"'
    )
    assert "JSON is for detail and audit inspection, not the primary review workflow." in html
    assert (
        "Review decisions are made from warnings, review items, source locations, and artifact actions."
        in html
    )

    for snippet in [
        "## PoC review UI information architecture",
        "Upload",
        "Conversion settings",
        "Review",
        "Artifact downloads",
        "Detail JSON",
        "JSON is retained for detail and audit inspection, not as the primary review workflow.",
        "`document_ir`",
        "`review_items`",
        "`warnings`",
        "`artifacts[]`",
        "`audit`",
    ]:
        assert snippet in readme


def test_web_app_shell_defines_phase10_navigation_and_screen_frames() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")
    parser = _PocUiRegionParser()
    parser.feed(html)

    expected_screens = {
        "dashboard": "Dashboard",
        "upload": "Upload",
        "jobs": "Jobs",
        "review": "Review",
        "templates": "Templates",
        "audit": "Audit",
        "admin": "Admin",
    }

    for screen, label in expected_screens.items():
        assert f'data-nav-target="{screen}"' in html
        assert f'data-app-screen="{screen}"' in html
        assert label in html

    assert 'id="app-shell"' in html
    assert 'id="app-navigation"' in html
    assert "window.addEventListener(\"hashchange\", routeFromHash)" in html
    assert "function routeFromHash()" in html
    assert "function activateScreen(screenId)" in html
    assert "location.hash" in html

    preserved_regions = {
        "upload": ["content_base64", "document_ir"],
        "conversion-settings": ["conversion_mode", "use_llm", "use_ocr"],
        "review": ["review_items", "warnings", "document_ir"],
        "artifact-downloads": ["artifacts[]", "download", "audit"],
        "detail-json": ["document_ir", "review_items", "warnings", "artifacts[]", "audit"],
    }
    for region, fields in preserved_regions.items():
        assert region in parser.region_fields
        for field in fields:
            assert field in parser.region_fields[region]


def test_web_phase10_screens_own_migrated_poc_capabilities() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")
    parser = _PocUiRegionParser()
    parser.feed(html)

    expected_screen_regions = {
        "jobs": [
            "job_id",
            "idempotency_key",
            "filename",
            "status",
            "display_status",
            "progress_percent",
            "warning_count",
            "mode",
            "created_at",
            "updated_at",
            "attempts",
            "error",
            "template_id",
            "template",
            "template.template_id",
            "template.name",
            "template.template_version",
            "hashes",
            "hashes.source_sha256",
            "hashes.output_sha256",
            "hash_verification",
            "hash_verification.source",
            "hash_verification.source.status",
            "hash_verification.source.sha256",
            "hash_verification.source.expected_sha256",
            "hash_verification.source.actual_sha256",
            "hash_verification.output",
            "hash_verification.output.status",
            "hash_verification.output.expected_sha256",
            "hash_verification.output.actual_sha256",
            "has_result",
            "available_actions",
            "available_actions.action",
            "available_actions.label",
            "available_actions.enabled",
            "available_actions.audit_event",
            "available_actions.audit_event.event_type",
            "available_actions.audit_event.job_id",
            "available_actions.audit_event.job_status",
            "available_actions.audit_event.action",
            "open_detail",
            "download_result",
            "retry_conversion",
            "action",
            "audit_event",
            "audit_event.event_type",
            "audit_event.job_id",
            "audit_event.job_status",
            "audit_event.action",
        ],
        "templates": [
            "template_id",
            "name",
            "category",
            "current_version",
            "field_count",
            "version_count",
            "versions",
            "versions.version",
            "versions.document_type",
            "versions.status",
            "versions.fields",
            "versions.fields.field_id",
            "versions.fields.name",
            "versions.fields.label",
            "versions.fields.required",
            "versions.fields.risk_level",
            "versions.fields.output_key",
            "versions.anchors",
            "versions.anchors.anchor_id",
            "versions.anchors.kind",
            "versions.anchors.text",
            "versions.anchors.match",
            "versions.tables",
            "versions.tables.table_id",
            "versions.tables.anchor_id",
            "versions.tables.required_columns",
            "versions.tables.output_key",
            "versions.risk_rank",
            "versions.risk_rank.default_level",
            "versions.risk_rank.levels",
            "versions.risk_rank.levels.level",
            "versions.risk_rank.levels.rank",
            "versions.risk_rank.review_required_levels",
            "versions.validation_rules",
            "versions.validation_rules.rule_id",
            "versions.validation_rules.target",
            "versions.validation_rules.rule_type",
            "versions.validation_rules.severity",
            "versions.validation_rules.message",
            "versions.output_mapping",
            "versions.output_mapping.format",
            "versions.output_mapping.root_key",
            "versions.output_mapping.field_map",
            "versions.output_mapping.field_map.field_id",
            "versions.output_mapping.field_map.output_key",
            "versions.output_mapping.table_map",
            "versions.output_mapping.table_map.table_id",
            "versions.output_mapping.table_map.output_key",
            "versions.content",
            "versions.created_at",
            "versions.change_history",
            "versions.change_history.event_type",
            "versions.change_history.action",
            "versions.change_history.template_id",
            "versions.change_history.version",
            "versions.change_history.change_reason",
            "versions.change_history.actor",
            "versions.change_history.actor.principal_id",
            "versions.change_history.actor.role",
            "versions.change_history.approval",
            "versions.change_history.approval.status",
            "versions.change_history.approval.approved_by",
            "versions.change_history.approval.approved_by.principal_id",
            "versions.change_history.approval.approved_by.role",
            "versions.change_history.recorded_at",
            "document_type",
            "status",
            "change_reason",
            "actor",
            "actor.principal_id",
            "actor.role",
            "fields",
            "fields.field_id",
            "fields.name",
            "fields.label",
            "fields.required",
            "fields.risk_level",
            "fields.output_key",
            "anchors",
            "anchors.anchor_id",
            "anchors.kind",
            "anchors.text",
            "anchors.match",
            "tables",
            "tables.table_id",
            "tables.anchor_id",
            "tables.required_columns",
            "tables.output_key",
            "risk_rank",
            "risk_rank.default_level",
            "risk_rank.levels",
            "risk_rank.levels.level",
            "risk_rank.levels.rank",
            "risk_rank.review_required_levels",
            "validation_rules",
            "validation_rules.rule_id",
            "validation_rules.target",
            "validation_rules.rule_type",
            "validation_rules.severity",
            "validation_rules.message",
            "output_mapping",
            "output_mapping.format",
            "output_mapping.root_key",
            "output_mapping.field_map",
            "output_mapping.field_map.field_id",
            "output_mapping.field_map.output_key",
            "output_mapping.table_map",
            "output_mapping.table_map.table_id",
            "output_mapping.table_map.output_key",
            "change_history",
            "change_history.event_type",
            "change_history.action",
            "change_history.template_id",
            "change_history.version",
            "change_history.change_reason",
            "change_history.actor",
            "change_history.actor.principal_id",
            "change_history.actor.role",
            "change_history.approval",
            "change_history.approval.status",
            "change_history.approval.approved_by",
            "change_history.approval.approved_by.principal_id",
            "change_history.approval.approved_by.role",
            "change_history.recorded_at",
            "updated_at",
        ],
        "audit": [
            "job_id",
            "document_id",
            "block_id",
            "action",
            "job_events",
            "event_type",
            "job_status",
            "source_sha256",
            "output_sha256",
            "filename",
            "saved_filename",
            "download_filename",
            "mode",
            "size_bytes",
            "content_type",
            "review_events",
            "occurred_at",
            "actor",
            "actor.role",
            "actor.id",
            "actor_id",
            "audit_events",
            "audit_events.job_id",
            "audit_events.document_id",
            "audit_events.block_id",
            "audit_events.action",
            "audit_events.event_type",
            "audit_events.job_status",
            "audit_events.source_sha256",
            "audit_events.output_sha256",
            "audit_events.filename",
            "audit_events.saved_filename",
            "audit_events.download_filename",
            "audit_events.mode",
            "audit_events.size_bytes",
            "audit_events.content_type",
            "audit_events.occurred_at",
            "audit_events.actor",
            "audit_events.actor.role",
            "audit_events.actor.id",
            "audit_events.actor_id",
            "audit_events.audit_kind",
            "audit_events.sequence",
            "audit_events.event_hash",
            "audit_events.prev_event_hash",
            "audit_events.integrity_algorithm",
            "audit_events.conversion_id",
            "audit_events.source_page",
            "audit_events.source_bbox",
            "audit_events.source_bbox.x",
            "audit_events.source_bbox.y",
            "audit_events.source_bbox.width",
            "audit_events.source_bbox.height",
            "audit_events.source_bbox.unit",
            "audit_events.source_bbox.origin",
            "audit_events.original_text",
            "audit_events.revised_text",
            "audit_events.warnings",
            "audit_kind",
            "sequence",
            "event_hash",
            "prev_event_hash",
            "integrity_algorithm",
            "conversion_id",
            "source_page",
            "source_bbox",
            "source_bbox.x",
            "source_bbox.y",
            "source_bbox.width",
            "source_bbox.height",
            "source_bbox.unit",
            "source_bbox.origin",
            "original_text",
            "revised_text",
            "warnings",
        ],
    }

    for region, fields in expected_screen_regions.items():
        assert region in parser.region_fields
        for field in fields:
            assert field in parser.region_fields[region]

    unexpected_screen_region_fields = {
        "jobs": ["hash_verification.output.sha256"],
        "templates": ["approval", "version", "content", "created_at", "recorded_at"],
    }
    for region, fields in unexpected_screen_region_fields.items():
        assert region in parser.region_fields
        for field in fields:
            assert field not in parser.region_fields[region]

    expected_region_elements = {
        "jobs": [
            "refresh-jobs",
            "create-job",
            "jobs-body",
            "detail-open",
            "detail-download",
            "detail-retry",
        ],
        "templates": [
            "refresh-templates",
            "save-template",
            "template-list",
            "template-detail-raw",
        ],
        "audit": ["refresh-audit", "export-audit", "audit-body", "audit-empty"],
    }
    for region, element_ids in expected_region_elements.items():
        for element_id in element_ids:
            assert region in parser.element_regions[element_id]


def test_web_direct_convert_download_uses_primary_artifact_before_debug_json() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    render_result = re.search(
        r"function renderResult\(result\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )

    assert render_result is not None
    body = render_result.group("body")
    assert "primaryDownloadArtifact(result)" in body
    assert "artifact.content_base64" in html
    assert "Download primary DOCX/XLSX" in html
    assert "Download audit/debug JSON" in html
    assert "JSON is retained for validation and audit details." in html


def test_web_direct_convert_artifact_region_lists_metadata_without_payloads() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")
    parser = _PocUiRegionParser()
    parser.feed(html)
    renderer = re.search(
        r"function renderArtifactListItem\(artifact\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )

    assert "artifact-downloads" in parser.element_regions["artifact-list"]
    assert 'id="artifact-list"' in html
    assert "renderArtifactList(result.artifacts || [])" in html
    assert "function renderArtifactList(artifacts)" in html
    assert renderer is not None
    renderer_body = renderer.group("body")
    assert "artifact.metadata?.role" in renderer_body
    assert "artifact.filename" in renderer_body
    assert "artifact.content_type" in renderer_body
    assert "artifact.kind" in renderer_body
    assert "artifact.format" in renderer_body
    assert "artifact.size_bytes" in renderer_body
    assert "content_base64" not in renderer_body
    assert re.search(r"artifact\.content(?!_type)", renderer_body) is None
    assert "content_base64" in html
    assert "artifactForDetail" in html


def test_web_direct_convert_renders_sanitized_error_region() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")
    parser = _PocUiRegionParser()
    parser.feed(html)
    click_handler = re.search(
        r'button\.addEventListener\("click", async \(\) => \{(?P<body>.*?)\n      \}\);',
        html,
        re.DOTALL,
    )
    error_renderer = re.search(
        r"function renderDirectConvertError\(errorPayload\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )

    assert "review" in parser.element_regions["direct-convert-error"]
    assert 'id="direct-convert-error"' in html
    assert 'role="alert"' in html
    assert 'data-api-fields="error message warnings artifacts[] download"' in html
    assert click_handler is not None
    assert "throw new Error" not in click_handler.group("body")
    assert "renderDirectConvertError(result)" in click_handler.group("body")
    assert error_renderer is not None
    renderer_body = error_renderer.group("body")
    assert "sanitizedUserMessage" in renderer_body
    assert "renderTopLevelWarnings(warnings)" in renderer_body
    assert "renderArtifactList(errorPayload.artifacts || [])" in renderer_body
    assert "clearDownload()" in renderer_body
    assert "rawPanel.hidden = true" in renderer_body
    assert "errorPayload.stack" not in renderer_body
    assert "function containsInternalDetail(message)" in html
    assert "function hasLocalPathPrefix(message)" in html
    assert 'homeRoots = ["Users", "home", "private"]' in html
    assert "/Users/" not in html
    assert r"\/Users\/" not in html
    assert r"\\/Users\\/" not in html


def test_web_job_detail_actions_perform_download_and_retry_side_effects() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    action_handler = re.search(
        r"async function sendJobAction\(\s*actionName,.*?\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )
    selected_download_handler = re.search(
        r"async function downloadSelectedJobResult\(\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )
    selected_retry_handler = re.search(
        r"async function retrySelectedConversion\(\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )
    download_handler = re.search(
        r"async function downloadJobResult\(\s*job,.*?\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )

    assert action_handler is not None
    assert selected_download_handler is not None
    assert selected_retry_handler is not None
    assert download_handler is not None
    action_body = action_handler.group("body")
    selected_download_body = selected_download_handler.group("body")
    selected_retry_body = selected_retry_handler.group("body")
    download_body = download_handler.group("body")

    assert 'detailDownload.addEventListener("click", () => downloadSelectedJobResult())' in html
    assert 'detailRetry.addEventListener("click", () => retrySelectedConversion())' in html
    assert 'apiFetch("/api/job-events"' in action_body
    assert 'sendJobAction(\n            "download_result"' in selected_download_body
    assert "await downloadJobResult(\n            accepted.job" in selected_download_body
    assert 'sendJobAction(\n            "retry_conversion"' in selected_retry_body
    assert "await loadJobs()" in selected_retry_body
    assert "renderDetail(body.job)" in selected_retry_body
    assert 'apiFetch(`/api/jobs/${encodeURIComponent(job.job_id)}/result`)' in download_body
    assert "await response.blob()" in download_body
    assert "URL.createObjectURL(blob)" in download_body
    assert "link.click()" in download_body


def test_web_job_list_refresh_updates_selected_detail_snapshot() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    load_jobs_handler = re.search(
        r"async function loadJobs\(\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )

    assert load_jobs_handler is not None
    load_jobs_body = load_jobs_handler.group("body")
    assert "const refreshedSelection = state.selectedJob" in load_jobs_body
    assert (
        "state.jobs.find((job) => job.job_id === state.selectedJob.job_id)"
        in load_jobs_body
    )
    assert "renderDetail(refreshedSelection)" in load_jobs_body
    assert "clearDetail()" in load_jobs_body


def test_web_job_list_refresh_ignores_stale_auth_token_responses() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    load_jobs_handler = re.search(
        r"async function loadJobs\(\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )
    create_job_handler = re.search(
        r"async function createConversionJob\(\) \{(?P<body>.*?)\n      \}",
        html,
        re.DOTALL,
    )

    assert load_jobs_handler is not None
    assert create_job_handler is not None
    load_jobs_body = load_jobs_handler.group("body")
    create_job_body = create_job_handler.group("body")
    assert "const requestAuthToken = activeAuthToken()" in load_jobs_body
    assert "const requestAuthGeneration = state.authGeneration" in load_jobs_body
    assert (
        "if (!isActiveCredentialRequest(requestAuthToken, requestAuthGeneration)) return;"
        in load_jobs_body
    )
    assert (
        load_jobs_body.index("const body = await response.json()")
        < load_jobs_body.index(
            "if (!isActiveCredentialRequest(requestAuthToken, requestAuthGeneration)) return;"
        )
        < load_jobs_body.index("if (!response.ok) throw")
        < load_jobs_body.index("state.jobs = body.jobs")
    )
    assert (
        load_jobs_body.rindex(
            "if (!isActiveCredentialRequest(requestAuthToken, requestAuthGeneration)) return;"
        )
        < load_jobs_body.index("setPageStatus(error.message, true)")
    )
    assert "const requestAuthToken = activeAuthToken()" in create_job_body
    assert "const requestAuthGeneration = state.authGeneration" in create_job_body
    assert (
        create_job_body.index("const body = await response.json()")
        < create_job_body.index(
            "if (!isActiveCredentialRequest(requestAuthToken, requestAuthGeneration)) return;"
        )
        < create_job_body.index("if (!response.ok) throw")
        < create_job_body.index("state.selectedJob = body.job")
    )
    assert (
        create_job_body.rindex(
            "if (!isActiveCredentialRequest(requestAuthToken, requestAuthGeneration)) return;"
        )
        < create_job_body.index("setPageStatus(error.message, true)")
    )
    assert "function isActiveCredentialRequest(requestAuthToken, requestAuthGeneration)" in html
    assert "state.authGeneration === requestAuthGeneration" in html
    assert "activeAuthToken() === requestAuthToken" in html
