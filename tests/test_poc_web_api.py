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
                        "extractor": {"name": "pymupdf"},
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
            "extractor": "pymupdf",
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
