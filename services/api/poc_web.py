from __future__ import annotations

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import binascii
import json
import math
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.ir.document_ir_v1 import (
    DocumentIRV1,
    adapt_pdf_document_ir_v0_blocks,
    from_parser_output,
    validate_document_ir_v1,
)
from core.parsers.docx_extraction import extract_docx_structure
from core.parsers.pdf_text_extraction import parse_text_pdf_to_document_ir
from core.parsers.xlsx_extraction import extract_xlsx_structure

WEB_ROOT = REPO_ROOT / "apps" / "web"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_REQUEST_BYTES = (MAX_UPLOAD_BYTES * 4 // 3) + 4096
SOURCE_TYPES = {"pdf", "docx", "xlsx", "unknown"}
KNOWN_SOURCE_TYPES = SOURCE_TYPES - {"unknown"}


def convert_uploaded_document(*, filename: str, content: bytes) -> dict[str, Any]:
    """Convert one uploaded PoC document into IR, review details, and download bytes."""
    safe_filename = _safe_filename(filename)
    parser_output, input_warnings = _parser_output_from_upload(safe_filename, content)
    document_ir = from_parser_output(
        parser_output,
        document_id=_document_id(safe_filename),
        title=safe_filename,
        source_type=_source_type(safe_filename, parser_output),
    )
    validation = validate_document_ir_v1(document_ir)
    review_items = _review_items(document_ir)
    payload = {
        "document_ir": document_ir.to_dict(),
        "validation": asdict(validation),
        "review_items": review_items,
        "warnings": [*input_warnings, *validation.warnings],
    }
    download_content = _strict_json_bytes(payload, indent=2)
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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--check"]:
        return 0
    run()
    return 0


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
        if byte_count > MAX_UPLOAD_REQUEST_BYTES:
            self._send_json({"error": "upload_too_large"}, status=413)
            return
        try:
            request = json.loads(self.rfile.read(byte_count).decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request JSON root must be an object")
            filename = str(request.get("filename") or "upload.txt")
            content = _decode_request_content(request)
            if len(content) > MAX_UPLOAD_BYTES:
                self._send_json({"error": "upload_too_large"}, status=413)
                return
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
        body = _strict_json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _parser_output_from_upload(filename: str, content: bytes) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    source_type = _source_type_from_path(filename)
    if source_type in KNOWN_SOURCE_TYPES:
        return _parser_output_from_binary_upload(filename, content, source_type), warnings

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "unsupported binary upload; use .pdf, .docx, .xlsx, or UTF-8 JSON/text"
        ) from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        warnings.append("upload was treated as plain text; parser confidence requires review")
        return _plain_text_parser_output(text), warnings
    if not isinstance(parsed, dict):
        warnings.append("JSON upload root is not an object; content requires review")
        return _plain_text_parser_output(text), warnings
    return parsed, warnings


def _parser_output_from_binary_upload(
    filename: str, content: bytes, source_type: str
) -> dict[str, Any]:
    with TemporaryDirectory(prefix="veridoc-poc-upload-") as temp_dir:
        upload_path = Path(temp_dir) / filename
        upload_path.write_bytes(content)
        try:
            if source_type == "docx":
                return extract_docx_structure(upload_path).to_dict()
            if source_type == "xlsx":
                return extract_xlsx_structure(upload_path).to_dict()
            if source_type == "pdf":
                return adapt_pdf_document_ir_v0_blocks(
                    parse_text_pdf_to_document_ir(
                        upload_path,
                        document_id=_document_id(filename),
                    )
                )
        except Exception as exc:
            raise ValueError(
                f"{source_type.upper()} parser failed; upload requires a valid {source_type.upper()} file"
            ) from exc
    raise ValueError("unsupported binary upload")


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
        try:
            return base64.b64decode(str(request["content_base64"]), validate=True)
        except binascii.Error as exc:
            raise ValueError("content_base64 must be valid base64") from exc
    if "content" in request:
        return str(request["content"]).encode("utf-8")
    raise ValueError("content or content_base64 is required")


def _source_type(filename: str, parser_output: dict[str, Any] | None = None) -> str:
    filename_source_type = _source_type_from_path(filename)
    if filename_source_type != "unknown":
        return filename_source_type

    data = parser_output if isinstance(parser_output, dict) else {}
    explicit_source_type = str(data.get("source_type") or "")
    if explicit_source_type in KNOWN_SOURCE_TYPES:
        return explicit_source_type

    document = data.get("document")
    if isinstance(document, dict):
        document_source_type = str(document.get("source_type") or "")
        if document_source_type in KNOWN_SOURCE_TYPES:
            return document_source_type

    source_path_type = _source_type_from_path(str(data.get("source_path") or ""))
    if source_path_type != "unknown":
        return source_path_type

    if isinstance(data.get("blocks"), list):
        return "docx"
    if isinstance(data.get("sheets"), list):
        return "xlsx"
    if isinstance(data.get("candidates"), list):
        return "pdf"
    return "unknown"


def _source_type_from_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
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


def _strict_json_bytes(payload: Any, *, indent: int | None = None) -> bytes:
    return json.dumps(
        _json_safe(payload),
        allow_nan=False,
        ensure_ascii=False,
        indent=indent,
        sort_keys=True,
    ).encode("utf-8")


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
