from __future__ import annotations

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
from pathlib import Path
import re
from typing import Any

from core.ir.document_ir_v1 import DocumentIRV1, from_parser_output, validate_document_ir_v1

WEB_ROOT = Path(__file__).resolve().parents[2] / "apps" / "web"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788
MAX_UPLOAD_BYTES = 2 * 1024 * 1024


def convert_uploaded_document(*, filename: str, content: bytes) -> dict[str, Any]:
    """Convert one uploaded PoC document into IR, review details, and download bytes."""
    safe_filename = _safe_filename(filename)
    parser_output, input_warnings = _parser_output_from_upload(safe_filename, content)
    document_ir = from_parser_output(
        parser_output,
        document_id=_document_id(safe_filename),
        title=safe_filename,
        source_type=_source_type(safe_filename),
    )
    validation = validate_document_ir_v1(document_ir)
    review_items = _review_items(document_ir)
    payload = {
        "document_ir": document_ir.to_dict(),
        "validation": asdict(validation),
        "review_items": review_items,
        "warnings": [*input_warnings, *validation.warnings],
    }
    download_content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    return {
        "status": _status(validation.ok, validation.requires_review),
        **payload,
        "download": {
            "filename": f"{Path(safe_filename).stem}.veridoc-result.json",
            "content_type": "application/json; charset=utf-8",
            "content": download_content,
        },
    }


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Run the stdlib PoC API server."""
    server = ThreadingHTTPServer((host, port), PocWebRequestHandler)
    print(f"VeriDoc PoC web API listening on http://{host}:{port}")
    server.serve_forever()


class PocWebRequestHandler(BaseHTTPRequestHandler):
    server_version = "VeriDocPoC/0.1"

    def do_GET(self) -> None:
        if self.path in {"/", "/index.html"}:
            self._send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            return
        self._send_json({"error": "not_found"}, status=404)

    def do_POST(self) -> None:
        if self.path != "/api/convert":
            self._send_json({"error": "not_found"}, status=404)
            return
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            self._send_json({"error": "content_length_required"}, status=411)
            return
        byte_count = int(length)
        if byte_count > MAX_UPLOAD_BYTES:
            self._send_json({"error": "upload_too_large"}, status=413)
            return
        try:
            request = json.loads(self.rfile.read(byte_count).decode("utf-8"))
            filename = str(request.get("filename") or "upload.txt")
            content = _decode_request_content(request)
            result = convert_uploaded_document(filename=filename, content=content)
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json({"error": "invalid_upload", "message": str(exc)}, status=400)
            return
        self._send_json(_http_result(result))

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._send_json({"error": "web_asset_missing"}, status=500)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _parser_output_from_upload(filename: str, content: bytes) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    text = content.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        warnings.append("upload was treated as plain text; parser confidence requires review")
        return _plain_text_parser_output(text), warnings
    if not isinstance(parsed, dict):
        warnings.append("JSON upload root is not an object; content requires review")
        return _plain_text_parser_output(text), warnings
    return parsed, warnings


def _plain_text_parser_output(text: str) -> dict[str, Any]:
    normalized = text.strip() or "Uploaded document contained no readable text."
    return {
        "pages": [
            {
                "page_number": 1,
                "width": 612,
                "height": 792,
                "unit": "pt",
                "fragments": [
                    {
                        "text": normalized,
                        "bbox": {"x": 72, "y": 72, "width": 468, "height": 24, "unit": "pt"},
                        "confidence": 0.5,
                        "low_confidence": True,
                    }
                ],
            }
        ]
    }


def _review_items(document_ir: DocumentIRV1) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for block in document_ir.blocks:
        if not block.review.requires_review and not block.review.warnings:
            continue
        items.append(
            {
                "block_id": block.id,
                "source_page": block.source_page,
                "text": block.text,
                "warnings": block.review.warnings,
            }
        )
    return items


def _http_result(result: dict[str, Any]) -> dict[str, Any]:
    download = dict(result["download"])
    content = download.pop("content")
    return {
        **{key: value for key, value in result.items() if key != "download"},
        "download": {
            **download,
            "content_text": content.decode("utf-8"),
        },
    }


def _decode_request_content(request: dict[str, Any]) -> bytes:
    if "content_base64" in request:
        return base64.b64decode(str(request["content_base64"]), validate=True)
    if "content" in request:
        return str(request["content"]).encode("utf-8")
    raise ValueError("content or content_base64 is required")


def _source_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    if suffix == ".xlsx":
        return "xlsx"
    return "unknown"


def _safe_filename(filename: str) -> str:
    candidate = Path(filename).name.strip()
    return candidate or "upload.txt"


def _document_id(filename: str) -> str:
    stem = Path(filename).stem.lower()
    document_id = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
    return document_id or "upload"


def _status(ok: bool, requires_review: bool) -> str:
    if not ok:
        return "blocked"
    if requires_review:
        return "requires_review"
    return "converted"


if __name__ == "__main__":
    run()
