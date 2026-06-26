import base64
import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
from threading import Thread
from zipfile import ZIP_DEFLATED, ZipFile

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
            "block_id": "block-0001",
            "source_page": 1,
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
            "block_id": "block-0001",
            "source_page": 1,
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
    assert mismatched_response.status == 400
    assert mismatched_body["error"] == "invalid_job_event"


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
