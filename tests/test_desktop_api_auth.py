from __future__ import annotations

import base64
import hashlib
import http.client
import json
import socket
import threading
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request

import pytest

from apps.desktop.api_client import (
    ApiCredentialStore,
    DesktopConnectionSettings,
    DesktopApiClient,
    DesktopApiClientConfig,
    DesktopJobDisplayState,
    DesktopUploadValidationError,
    InvalidApiTokenError,
    MAX_DOWNLOAD_FILENAME_BYTES,
    MAX_DESKTOP_UPLOAD_BYTES,
    MissingApiTokenError,
    check_desktop_api_connection,
)


class RecordingTransport:
    def __init__(self, *, payload: dict[str, object] | None = None) -> None:
        self.requests: list[Request] = []
        self.timeouts: list[float] = []
        self.payload = payload or {"jobs": []}

    def __call__(self, request: Request, *, timeout: float):
        self.requests.append(request)
        self.timeouts.append(timeout)
        return JsonResponse(self.payload)


class JsonResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class RawResponse:
    def __init__(self, body: bytes, *, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self.body

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)


class IncompleteResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        raise http.client.IncompleteRead(partial=b"{", expected=128)


class SequenceTransport:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.requests: list[Request] = []
        self.payloads = list(payloads)

    def __call__(self, request: Request, *, timeout: float):
        self.requests.append(request)
        return JsonResponse(self.payloads.pop(0))


def test_desktop_api_client_attaches_bearer_token_from_credential_store() -> None:
    transport = RecordingTransport()
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    assert client.list_jobs() == {"jobs": []}

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.full_url == "http://127.0.0.1:8765/api/jobs"
    assert request.get_header("Authorization") == "Bearer reviewer-token"
    assert transport.timeouts == [10.0]
    assert "reviewer-token" not in repr(client.config)


def test_desktop_api_client_uploads_selected_pdf_and_returns_job_reference(tmp_path) -> None:
    selected_file = tmp_path / "batch-record.pdf"
    selected_file.write_bytes(b"%PDF-1.7\nfocused upload")
    transport = RecordingTransport(
        payload={
            "job": {
                "job_id": "job-upload-1",
                "status": "queued",
                "filename": "batch-record.pdf",
            }
        }
    )
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    job_ref = client.upload_document_file(selected_file)

    assert job_ref["job_id"] == "job-upload-1"
    assert job_ref["status"] == "queued"
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.full_url == "http://127.0.0.1:8765/api/jobs"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer reviewer-token"
    assert request.get_header("Content-type") == "application/json"
    body = json.loads(request.data.decode("utf-8"))
    source_sha256 = hashlib.sha256(b"%PDF-1.7\nfocused upload").hexdigest()
    idempotency_material = {
        "content_type": "application/pdf",
        "filename": "batch-record.pdf",
        "mode": "standard",
        "size_bytes": selected_file.stat().st_size,
        "source_sha256": source_sha256,
        "template_id": None,
    }
    idempotency_digest = hashlib.sha256(
        json.dumps(idempotency_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert body == {
        "filename": "batch-record.pdf",
        "content_type": "application/pdf",
        "content_base64": base64.b64encode(b"%PDF-1.7\nfocused upload").decode("ascii"),
        "size_bytes": selected_file.stat().st_size,
        "source_sha256": source_sha256,
        "idempotency_key": f"upload:{idempotency_digest}",
        "mode": "standard",
    }


def test_desktop_api_client_uploads_multiple_selected_files_in_order(tmp_path) -> None:
    pdf_file = tmp_path / "batch-record.pdf"
    xlsx_file = tmp_path / "batch-metrics.xlsx"
    pdf_file.write_bytes(b"%PDF-1.7\nfocused upload")
    xlsx_file.write_bytes(b"PK\x03\x04xlsx")
    transport = SequenceTransport(
        [
            {"job": {"job_id": "job-pdf", "status": "queued", "filename": pdf_file.name}},
            {"job": {"job_id": "job-xlsx", "status": "queued", "filename": xlsx_file.name}},
        ]
    )
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    job_refs = client.upload_document_files([pdf_file, xlsx_file])

    assert [job_ref["job_id"] for job_ref in job_refs] == ["job-pdf", "job-xlsx"]
    assert len(transport.requests) == 2
    assert [json.loads(request.data.decode("utf-8"))["filename"] for request in transport.requests] == [
        "batch-record.pdf",
        "batch-metrics.xlsx",
    ]


def test_desktop_api_client_upload_key_includes_job_parameters(tmp_path) -> None:
    selected_file = tmp_path / "batch-record.pdf"
    selected_file.write_bytes(b"%PDF-1.7\nfocused upload")
    transport = SequenceTransport(
        [
            {"job": {"job_id": "job-standard", "status": "queued", "filename": selected_file.name}},
            {"job": {"job_id": "job-template", "status": "queued", "filename": selected_file.name}},
        ]
    )
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    client.upload_document_file(selected_file, mode="standard")
    client.upload_document_file(selected_file, mode="high_quality", template_id="batch-record")

    first_body = json.loads(transport.requests[0].data.decode("utf-8"))
    second_body = json.loads(transport.requests[1].data.decode("utf-8"))
    assert first_body["source_sha256"] == second_body["source_sha256"]
    assert first_body["idempotency_key"] != second_body["idempotency_key"]
    assert second_body["mode"] == "high_quality"
    assert second_body["template_id"] == "batch-record"


def test_desktop_api_client_validates_full_batch_before_dispatch(tmp_path) -> None:
    pdf_file = tmp_path / "batch-record.pdf"
    txt_file = tmp_path / "notes.txt"
    pdf_file.write_bytes(b"%PDF-1.7\nfocused upload")
    txt_file.write_text("not an accepted desktop upload")
    transport = SequenceTransport(
        [
            {"job": {"job_id": "job-pdf", "status": "queued", "filename": pdf_file.name}},
        ]
    )
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    with pytest.raises(DesktopUploadValidationError, match="unsupported file type"):
        client.upload_document_files([pdf_file, txt_file])

    assert transport.requests == []


def test_desktop_api_client_rejects_unsupported_upload_type_before_dispatch(tmp_path) -> None:
    selected_file = tmp_path / "notes.txt"
    selected_file.write_text("not an accepted desktop upload")
    transport = RecordingTransport()
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    with pytest.raises(DesktopUploadValidationError, match="unsupported file type"):
        client.upload_document_file(selected_file)

    assert transport.requests == []


def test_desktop_api_client_rejects_mismatched_upload_mime_before_dispatch(tmp_path) -> None:
    selected_file = tmp_path / "batch-record.pdf"
    selected_file.write_bytes(b"%PDF-1.7\nfocused upload")
    transport = RecordingTransport()
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    with pytest.raises(DesktopUploadValidationError, match="MIME"):
        client.upload_document_file(selected_file, content_type="text/plain")

    assert transport.requests == []


def test_desktop_api_client_rejects_oversized_upload_before_dispatch(tmp_path) -> None:
    selected_file = tmp_path / "large-record.pdf"
    selected_file.write_bytes(b"0" * (MAX_DESKTOP_UPLOAD_BYTES + 1))
    transport = RecordingTransport()
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    with pytest.raises(DesktopUploadValidationError, match="size limit"):
        client.upload_document_file(selected_file)

    assert transport.requests == []


def test_desktop_api_client_upload_maps_unauthorized_api_response_fail_closed(tmp_path) -> None:
    selected_file = tmp_path / "batch-record.pdf"
    selected_file.write_bytes(b"%PDF-1.7\nfocused upload")

    def unauthorized_transport(request: Request, *, timeout: float):
        raise HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=None,
        )

    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "wrong-token"),
        transport=unauthorized_transport,
    )

    with pytest.raises(PermissionError, match="API authentication failed"):
        client.upload_document_file(selected_file)


def test_desktop_api_client_saves_completed_job_result_to_selected_folder_with_sanitized_collision_name(
    tmp_path,
) -> None:
    destination_dir = tmp_path / "selected-output"
    destination_dir.mkdir()
    existing = destination_dir / "unsafe-name.veridoc-result.json"
    existing.write_text("existing result", encoding="utf-8")
    download_body = b'{"document_ir":{"document_id":"job-result"}}'

    def download_transport(request: Request, *, timeout: float):
        return RawResponse(
            download_body,
            headers={
                "Content-Disposition": 'attachment; filename="../unsafe:name.veridoc-result.json"',
                "Content-Type": "application/json",
            },
        )

    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=download_transport,
    )

    saved_path = client.save_job_result("job-complete-1", destination_dir)

    assert saved_path == destination_dir / "unsafe-name (1).veridoc-result.json"
    assert saved_path.read_bytes() == download_body
    assert existing.read_text(encoding="utf-8") == "existing result"


def test_desktop_api_client_result_save_uses_authenticated_download_api(tmp_path) -> None:
    requests: list[Request] = []

    def download_transport(request: Request, *, timeout: float):
        requests.append(request)
        return RawResponse(
            b"{}",
            headers={"Content-Disposition": 'attachment; filename="result.json"'},
        )

    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=download_transport,
    )

    assert client.save_job_result("job-complete-1", tmp_path) == tmp_path / "result.json"

    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "http://127.0.0.1:8765/api/jobs/job-complete-1/result"
    assert request.get_method() == "GET"
    assert request.get_header("Authorization") == "Bearer reviewer-token"
    assert request.get_header("Accept") == "application/octet-stream"


def test_desktop_api_client_clamps_long_result_download_names(tmp_path) -> None:
    long_filename = f"{'a' * 300}.veridoc-result.json"

    def download_transport(request: Request, *, timeout: float):
        return RawResponse(
            b'{"document_ir":{}}',
            headers={"Content-Disposition": f'attachment; filename="{long_filename}"'},
        )

    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=download_transport,
    )

    first_path = client.save_job_result("job-complete-1", tmp_path)
    second_path = client.save_job_result("job-complete-1", tmp_path)

    assert first_path.name.endswith(".veridoc-result.json")
    assert second_path.name.endswith(".veridoc-result.json")
    assert " (1)" in second_path.name
    assert first_path != second_path
    assert len(first_path.name.encode("utf-8")) <= MAX_DOWNLOAD_FILENAME_BYTES
    assert len(second_path.name.encode("utf-8")) <= MAX_DOWNLOAD_FILENAME_BYTES
    assert first_path.read_bytes() == b'{"document_ir":{}}'
    assert second_path.read_bytes() == b'{"document_ir":{}}'


def test_desktop_api_client_fetches_job_progress_display_state() -> None:
    transport = RecordingTransport(
        payload={
            "job": {
                "job_id": "job-progress-1",
                "status": "succeeded",
                "display_status": "review_required",
                "progress_percent": 100,
                "warning_count": 2,
                "error": None,
            }
        }
    )
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    state = client.get_job_progress("job-progress-1")

    assert state == DesktopJobDisplayState(
        job_id="job-progress-1",
        api_status="succeeded",
        display_status="review_required",
        progress_percent=100,
        warning_count=2,
        error_message=None,
        is_terminal=True,
    )
    assert len(transport.requests) == 1
    assert transport.requests[0].full_url == "http://127.0.0.1:8765/api/jobs/job-progress-1"


def test_desktop_api_client_accepts_blocked_job_progress_display_state() -> None:
    transport = RecordingTransport(
        payload={
            "job": {
                "job_id": "job-blocked-1",
                "status": "succeeded",
                "display_status": "blocked",
                "progress_percent": 100,
                "warning_count": 1,
                "error": None,
            }
        }
    )
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    state = client.get_job_progress("job-blocked-1")

    assert state == DesktopJobDisplayState(
        job_id="job-blocked-1",
        api_status="succeeded",
        display_status="blocked",
        progress_percent=100,
        warning_count=1,
        error_message=None,
        is_terminal=True,
    )


@pytest.mark.parametrize(
    ("api_status", "display_status", "error_message", "is_terminal"),
    [
        ("queued", "queued", None, False),
        ("running", "running", None, False),
        ("succeeded", "completed", None, True),
        ("failed", "failed", "parser unavailable", True),
    ],
)
def test_desktop_api_client_derives_job_terminal_state_without_display_hint(
    api_status: str,
    display_status: str,
    error_message: str | None,
    is_terminal: bool,
) -> None:
    transport = RecordingTransport(
        payload={
            "job": {
                "job_id": "job-progress-1",
                "status": api_status,
                "error": error_message,
            }
        }
    )
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    state = client.get_job_progress("job-progress-1")

    assert state.display_status == display_status
    assert state.error_message == error_message
    assert state.is_terminal is is_terminal
    assert state.progress_percent == (100 if is_terminal else {"queued": 0, "running": 50}[api_status])


def test_desktop_connection_settings_feed_later_api_client_config() -> None:
    settings = DesktopConnectionSettings(
        api_base_url="http://127.0.0.1:8765/api",
        timeout_seconds=2.5,
    )

    config = settings.to_client_config()
    client = settings.build_client(
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=RecordingTransport(),
    )

    assert config.base_url == "http://127.0.0.1:8765/api/"
    assert config.timeout_seconds == 2.5
    assert client.config == config


def test_desktop_connection_health_check_reports_success() -> None:
    transport = RecordingTransport(payload={"jobs": [{"job_id": "job-1"}]})
    settings = DesktopConnectionSettings(api_base_url="http://127.0.0.1:8765")

    result = check_desktop_api_connection(
        settings,
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    assert result.ok is True
    assert result.status == "connected"
    assert result.base_url == "http://127.0.0.1:8765/"
    assert "接続" in result.message
    assert len(transport.requests) == 1
    assert transport.requests[0].full_url == "http://127.0.0.1:8765/api/jobs"


@pytest.mark.parametrize(
    ("base_url", "status", "message"),
    [
        ("not a url", "invalid_url", "HTTP(S) URL"),
        ("http://203.0.113.10:8765", "invalid_url", "local API endpoint"),
    ],
)
def test_desktop_connection_health_check_reports_invalid_url_without_network_dispatch(
    base_url: str,
    status: str,
    message: str,
) -> None:
    transport = RecordingTransport()
    result = check_desktop_api_connection(
        DesktopConnectionSettings(api_base_url=base_url),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    assert result.ok is False
    assert result.status == status
    assert message in result.message
    assert transport.requests == []


@pytest.mark.parametrize("base_url", [None, 123, True, {"url": "http://127.0.0.1:8765"}])
def test_desktop_connection_health_check_rejects_non_string_url_without_network_dispatch(
    base_url: object,
) -> None:
    transport = RecordingTransport()
    result = check_desktop_api_connection(
        DesktopConnectionSettings(api_base_url=base_url),  # type: ignore[arg-type]
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    assert result.ok is False
    assert result.status == "invalid_url"
    assert "base_url" in result.message
    assert transport.requests == []


def test_desktop_connection_settings_can_require_https() -> None:
    settings = DesktopConnectionSettings(
        api_base_url="http://127.0.0.1:8765",
        require_https=True,
    )

    result = check_desktop_api_connection(
        settings,
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=RecordingTransport(),
    )

    assert result.ok is False
    assert result.status == "https_required"
    assert "HTTPS" in result.message


def test_desktop_connection_health_check_reports_connection_failure() -> None:
    def failing_transport(request: Request, *, timeout: float):
        raise URLError("connection refused")

    result = check_desktop_api_connection(
        DesktopConnectionSettings(api_base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=failing_transport,
    )

    assert result.ok is False
    assert result.status == "connection_failed"
    assert "connection refused" in result.message


@pytest.mark.parametrize("timeout_seconds", [0.0, -1.0, 10**400, 1e308, float("nan"), float("inf"), float("-inf")])
def test_desktop_connection_health_check_rejects_invalid_timeout_without_network_dispatch(
    timeout_seconds: float,
) -> None:
    transport = RecordingTransport()

    result = check_desktop_api_connection(
        DesktopConnectionSettings(api_base_url="http://127.0.0.1:8765", timeout_seconds=timeout_seconds),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    assert result.ok is False
    assert result.status == "invalid_timeout"
    assert "timeout_seconds" in result.message
    assert transport.requests == []


@pytest.mark.parametrize("timeout_seconds", ["", "5 seconds", None, True])
def test_desktop_connection_health_check_rejects_nonnumeric_timeout_without_network_dispatch(
    timeout_seconds: object,
) -> None:
    transport = RecordingTransport()

    result = check_desktop_api_connection(
        DesktopConnectionSettings(api_base_url="http://127.0.0.1:8765", timeout_seconds=timeout_seconds),  # type: ignore[arg-type]
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    assert result.ok is False
    assert result.status == "invalid_timeout"
    assert "timeout_seconds" in result.message
    assert transport.requests == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"not json", "valid JSON"),
        (b"[]", "JSON object"),
    ],
)
def test_desktop_connection_health_check_reports_malformed_api_response(
    body: bytes,
    message: str,
) -> None:
    def malformed_transport(request: Request, *, timeout: float):
        return RawResponse(body)

    result = check_desktop_api_connection(
        DesktopConnectionSettings(api_base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=malformed_transport,
    )

    assert result.ok is False
    assert result.status == "request_failed"
    assert message in result.message


def test_desktop_connection_health_check_reports_incomplete_api_response() -> None:
    def incomplete_transport(request: Request, *, timeout: float):
        return IncompleteResponse()

    result = check_desktop_api_connection(
        DesktopConnectionSettings(api_base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=incomplete_transport,
    )

    assert result.ok is False
    assert result.status == "request_failed"
    assert "incomplete" in result.message


def test_desktop_connection_health_check_reports_http_protocol_error() -> None:
    def malformed_transport(request: Request, *, timeout: float):
        raise http.client.BadStatusLine("not-http")

    result = check_desktop_api_connection(
        DesktopConnectionSettings(api_base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=malformed_transport,
    )

    assert result.ok is False
    assert result.status == "request_failed"
    assert "transport" in result.message


def test_desktop_connection_health_check_reports_real_truncated_http_response() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve_truncated_response() -> None:
        with server:
            connection, _address = server.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 128\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                    b"{"
                )

    thread = threading.Thread(target=serve_truncated_response)
    thread.start()
    try:
        result = check_desktop_api_connection(
            DesktopConnectionSettings(api_base_url=f"http://127.0.0.1:{port}", timeout_seconds=1.0),
            credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        )
    finally:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert result.ok is False
    assert result.status == "request_failed"
    assert "incomplete" in result.message


def test_desktop_connection_health_check_rejects_wrong_shape_job_list_response() -> None:
    result = check_desktop_api_connection(
        DesktopConnectionSettings(api_base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=RecordingTransport(payload={"ok": True}),
    )

    assert result.ok is False
    assert result.status == "request_failed"
    assert "jobs" in result.message


@pytest.mark.parametrize("token", [None, "", "   "])
def test_desktop_api_client_fails_closed_without_api_token(token: str | None) -> None:
    transport = RecordingTransport()
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: token),
        transport=transport,
    )

    with pytest.raises(MissingApiTokenError):
        client.list_jobs()

    assert transport.requests == []


@pytest.mark.parametrize("token", ["<viewer-token>", "TODO", "placeholder-token", "sample-secret"])
def test_desktop_api_client_rejects_placeholder_tokens(token: str) -> None:
    transport = RecordingTransport()
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: token),
        transport=transport,
    )

    with pytest.raises(InvalidApiTokenError):
        client.list_jobs()

    assert transport.requests == []


def test_desktop_api_client_maps_unauthorized_api_response_fail_closed() -> None:
    def unauthorized_transport(request: Request, *, timeout: float):
        raise HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=None,
        )

    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "wrong-token"),
        transport=unauthorized_transport,
    )

    with pytest.raises(PermissionError, match="API authentication failed"):
        client.list_jobs()


def test_desktop_api_client_default_transport_disables_proxy_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    handlers: list[object] = []

    class FakeOpener:
        def open(self, request: Request, *, timeout: float):
            calls.append({"request": request, "timeout": timeout})
            return JsonResponse({"jobs": []})

    def fake_build_opener(*configured_handlers: object) -> FakeOpener:
        handlers.extend(configured_handlers)
        return FakeOpener()

    monkeypatch.setattr("apps.desktop.api_client.build_opener", fake_build_opener)
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765", timeout_seconds=2.5),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
    )

    assert client.list_jobs() == {"jobs": []}
    assert len(calls) == 1
    assert calls[0]["timeout"] == 2.5
    request = calls[0]["request"]
    assert isinstance(request, Request)
    assert request.get_header("Authorization") == "Bearer reviewer-token"
    assert any(isinstance(handler, ProxyHandler) and handler.proxies == {} for handler in handlers)
    assert any(handler.__class__.__name__ == "_NoRedirectHandler" for handler in handlers)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://example.com",
        "http://127.0.0.1.example.com:8765",
    ],
)
def test_desktop_api_client_config_rejects_non_local_api_endpoints(base_url: str) -> None:
    with pytest.raises(ValueError, match="local API endpoint"):
        DesktopApiClientConfig(base_url=base_url)


def test_desktop_api_client_config_accepts_localhost_resolved_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(host: str, port: object, *, type: int):
        assert host == "localhost"
        assert port == 8765
        assert type == socket.SOCK_STREAM
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 0, 0, 0)),
        ]

    monkeypatch.setattr("apps.desktop.api_client.socket.getaddrinfo", fake_getaddrinfo)

    config = DesktopApiClientConfig(base_url="http://localhost:8765")

    assert config.base_url == "http://127.0.0.1:8765/"


def test_desktop_api_client_pins_validated_localhost_before_sending_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def trusted_getaddrinfo(host: str, port: object, *, type: int):
        assert host == "localhost"
        assert port == 8765
        assert type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

    monkeypatch.setattr("apps.desktop.api_client.socket.getaddrinfo", trusted_getaddrinfo)
    config = DesktopApiClientConfig(base_url="http://localhost:8765")

    def rebound_getaddrinfo(host: str, port: object, *, type: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("203.0.113.10", 0))]

    monkeypatch.setattr("apps.desktop.api_client.socket.getaddrinfo", rebound_getaddrinfo)
    transport = RecordingTransport()
    client = DesktopApiClient(
        config,
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    assert client.list_jobs() == {"jobs": []}

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.full_url == "http://127.0.0.1:8765/api/jobs"
    assert request.get_header("Host") == "localhost:8765"
    assert request.get_header("Authorization") == "Bearer reviewer-token"


def test_desktop_api_client_tries_all_validated_localhost_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(host: str, port: object, *, type: int):
        assert host == "localhost"
        assert port == 8765
        assert type == socket.SOCK_STREAM
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0)),
        ]

    requests: list[Request] = []

    def transport(request: Request, *, timeout: float):
        requests.append(request)
        if len(requests) == 1:
            raise URLError("connection refused")
        return JsonResponse({"jobs": []})

    monkeypatch.setattr("apps.desktop.api_client.socket.getaddrinfo", fake_getaddrinfo)
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://localhost:8765"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    assert client.list_jobs() == {"jobs": []}

    assert [request.full_url for request in requests] == [
        "http://[::1]:8765/api/jobs",
        "http://127.0.0.1:8765/api/jobs",
    ]
    assert [request.get_header("Host") for request in requests] == [
        "localhost:8765",
        "localhost:8765",
    ]
    assert all(request.get_header("Authorization") == "Bearer reviewer-token" for request in requests)


def test_desktop_api_client_preserves_https_localhost_tls_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_getaddrinfo(host: str, port: object, *, type: int):
        assert host == "localhost"
        assert port == 8765
        assert type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

    class FakePinnedHTTPSConnection:
        def __init__(self, connect_host: str, port: int, tls_server_name: str, timeout: float) -> None:
            captured["connect_host"] = connect_host
            captured["port"] = port
            captured["tls_server_name"] = tls_server_name
            captured["timeout"] = timeout

        def request(self, method: str, path: str, body: bytes | None, headers: dict[str, str]) -> None:
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            captured["headers"] = headers

        def getresponse(self):
            class FakeResponse:
                status = 200
                reason = "OK"
                msg: dict[str, str] = {}

                def read(self) -> bytes:
                    return json.dumps({"jobs": []}).encode("utf-8")

            return FakeResponse()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr("apps.desktop.api_client.socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("apps.desktop.api_client._PinnedHTTPSConnection", FakePinnedHTTPSConnection)
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="https://localhost:8765", timeout_seconds=2.5),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
    )

    assert client.list_jobs() == {"jobs": []}

    assert captured["connect_host"] == "127.0.0.1"
    assert captured["port"] == 8765
    assert captured["tls_server_name"] == "localhost"
    assert captured["timeout"] == 2.5
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/jobs"
    assert captured["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer reviewer-token",
        "Host": "localhost:8765",
    }
    assert captured["closed"] is True


def test_desktop_api_client_preserves_pinned_https_result_download_headers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}

    def fake_getaddrinfo(host: str, port: object, *, type: int):
        assert host == "localhost"
        assert port == 8765
        assert type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 0))]

    class FakePinnedHTTPSConnection:
        def __init__(self, connect_host: str, port: int, tls_server_name: str, timeout: float) -> None:
            captured["connect_host"] = connect_host
            captured["port"] = port
            captured["tls_server_name"] = tls_server_name
            captured["timeout"] = timeout

        def request(self, method: str, path: str, body: bytes | None, headers: dict[str, str]) -> None:
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            captured["headers"] = headers

        def getresponse(self):
            class FakeResponse:
                status = 200
                reason = "OK"
                msg = {
                    "Content-Disposition": 'attachment; filename="pinned-result.json"',
                    "Content-Type": "application/json",
                }

                def read(self) -> bytes:
                    return b'{"document_ir":{}}'

                def getheader(self, name: str, default: str | None = None) -> str | None:
                    return self.msg.get(name, default)

            return FakeResponse()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr("apps.desktop.api_client.socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("apps.desktop.api_client._PinnedHTTPSConnection", FakePinnedHTTPSConnection)
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="https://localhost:8765", timeout_seconds=2.5),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
    )

    saved_path = client.save_job_result("job-complete-1", tmp_path)

    assert saved_path == tmp_path / "pinned-result.json"
    assert saved_path.read_bytes() == b'{"document_ir":{}}'
    assert captured["connect_host"] == "127.0.0.1"
    assert captured["tls_server_name"] == "localhost"
    assert captured["path"] == "/api/jobs/job-complete-1/result"
    assert captured["headers"] == {
        "Accept": "application/octet-stream",
        "Authorization": "Bearer reviewer-token",
        "Host": "localhost:8765",
    }
    assert captured["closed"] is True


def test_desktop_api_client_uses_single_api_path_for_api_root_base_url() -> None:
    transport = RecordingTransport()
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765/api"),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
        transport=transport,
    )

    assert client.list_jobs() == {"jobs": []}

    assert len(transport.requests) == 1
    assert transport.requests[0].full_url == "http://127.0.0.1:8765/api/jobs"


def test_desktop_api_client_config_rejects_unexpected_base_url_paths() -> None:
    with pytest.raises(ValueError, match="empty or /api"):
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765/app")


@pytest.mark.parametrize(
    "resolved_address",
    [
        "203.0.113.10",
        "not-an-address",
    ],
)
def test_desktop_api_client_config_rejects_localhost_unless_resolution_is_loopback(
    monkeypatch: pytest.MonkeyPatch,
    resolved_address: str,
) -> None:
    def fake_getaddrinfo(host: str, port: object, *, type: int):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (resolved_address, 0))]

    monkeypatch.setattr("apps.desktop.api_client.socket.getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="local API endpoint"):
        DesktopApiClientConfig(base_url="http://localhost:8765")


def test_desktop_api_client_config_rejects_localhost_when_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(host: str, port: object, *, type: int):
        raise socket.gaierror("blocked")

    monkeypatch.setattr("apps.desktop.api_client.socket.getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="local API endpoint"):
        DesktopApiClientConfig(base_url="http://localhost:8765")


@pytest.mark.parametrize(
    "base_url",
    [
        "http://token@127.0.0.1:8788",
        "http://viewer:secret@localhost:8788",
    ],
)
def test_desktop_api_client_config_rejects_embedded_url_credentials(base_url: str) -> None:
    with pytest.raises(ValueError, match="embedded credentials"):
        DesktopApiClientConfig(base_url=base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:99999",
        "http://127.0.0.1:not-a-port",
    ],
)
def test_desktop_api_client_config_rejects_invalid_api_ports(base_url: str) -> None:
    with pytest.raises(ValueError, match="valid TCP port"):
        DesktopApiClientConfig(base_url=base_url)
