import base64
import json
import re
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
from threading import Thread
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import services.api.poc_web as poc_web
from services.api.job_queue import JobQueue
from services.api.poc_web import PocWebRequestHandler, convert_uploaded_document


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
    assert result["validation"]["requires_review"] is True
    assert result["review_items"] == [
        {
            "document_id": "phase0-output",
            "block_id": "block-0001",
            "source_page": 1,
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
    assert downloaded["validation"]["requires_review"] is True
    assert downloaded["document_ir"]["blocks"][0]["review"]["requires_review"] is True


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
            "source_page": 1,
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
    assert result["review_items"][0]["warnings"] == [
        "blocks[0].confidence missing; block marked requires_review"
    ]


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
    assert "A1: Lot" in result["document_ir"]["blocks"][0]["text"]


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


def test_poc_http_api_rejects_malformed_review_action_audit_event() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
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
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
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


def _post_review_audit_event(audit_event: dict[str, object]) -> tuple[int, dict[str, object]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps({"audit_event": audit_event}).encode("utf-8")
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
    return response.status, body


@pytest.mark.parametrize(
    ("audit_event", "message"),
    [
        (
            _review_audit_event(source_page=True),
            "audit_event.source_page must be a positive integer",
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
        (
            _review_audit_event(
                action="approve",
                original_text="Lot: SAMPLE-001",
                revised_text="Lot: SAMPLE-001 corrected",
            ),
            "audit_event.revised_text must match original_text for approve",
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


def test_web_upload_preserves_file_bytes() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    assert "file.arrayBuffer()" in html
    assert "content_base64" in html
    assert "file.text()" not in html


def test_web_job_detail_actions_perform_download_and_retry_side_effects() -> None:
    html = Path("apps/web/index.html").read_text(encoding="utf-8")

    action_handler = re.search(
        r"async function sendJobAction\(actionName\) \{(?P<body>.*?)\n      \}",
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
        r"async function downloadJobResult\(job\) \{(?P<body>.*?)\n      \}",
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
    assert 'fetch("/api/job-events"' in action_body
    assert 'sendJobAction("download_result")' in selected_download_body
    assert "await downloadJobResult(accepted.job)" in selected_download_body
    assert 'sendJobAction("retry_conversion")' in selected_retry_body
    assert "await loadJobs()" in selected_retry_body
    assert "renderDetail(body.job)" in selected_retry_body
    assert 'fetch(`/api/jobs/${encodeURIComponent(job.job_id)}/result`)' in download_body
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
