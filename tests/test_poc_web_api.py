import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
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
