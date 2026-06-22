import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import subprocess
import sys
from threading import Thread

from services.api.poc_web import PocWebRequestHandler, convert_uploaded_document


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
