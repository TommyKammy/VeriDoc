from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
import errno
import hashlib
import http.client
import ipaddress
import json
import logging
import math
import mimetypes
from numbers import Real
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import sys
import threading
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, quote, unquote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class MissingApiTokenError(RuntimeError):
    """Raised when the desktop client has no trusted API token available."""


class InvalidApiTokenError(RuntimeError):
    """Raised when the configured token is an obvious placeholder or sample."""


class DesktopApiError(RuntimeError):
    """Raised when the local API returns a non-auth client error."""


class DesktopUploadValidationError(ValueError):
    """Raised when a selected or dropped file cannot be uploaded."""


class DesktopTemporaryCleanupError(RuntimeError):
    """Raised when one or more desktop-owned temporary files cannot be removed."""

    def __init__(self, failures: tuple[Path, ...]) -> None:
        self.failures = failures
        super().__init__(f"temporary cleanup failed for {len(failures)} file(s)")


MAX_DESKTOP_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_FILENAME_BYTES = 255
DOWNLOAD_FILENAME_FALLBACK = "veridoc-result.json"
DESKTOP_TEMP_WORK_DIR = "work"
DESKTOP_TEMP_TOKEN_BYTES = 16
DESKTOP_TEMP_DIR_MODE = 0o700
DESKTOP_TEMP_FILE_MODE = 0o600
WINDOWS_RESERVED_DOWNLOAD_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

LOGGER = logging.getLogger(__name__)
ALLOWED_UPLOAD_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@dataclass(frozen=True)
class DesktopConnectionHealthResult:
    ok: bool
    status: str
    message: str
    base_url: str = ""


@dataclass(frozen=True)
class DesktopJobDisplayState:
    job_id: str
    api_status: str
    display_status: str
    progress_percent: int
    warning_count: int
    error_message: str | None
    is_terminal: bool


class TokenReader(Protocol):
    def __call__(self) -> str | None:
        ...


@dataclass(frozen=True)
class ApiCredentialStore:
    """Thin adapter for an OS credential-store read operation.

    The desktop shell should wire `read_token` to the platform credential store.
    Tests may inject an in-memory callable, but the token is intentionally not
    part of DesktopApiClientConfig or any serializable endpoint settings.
    """

    read_token: TokenReader

    def require_token(self) -> str:
        token = (self.read_token() or "").strip()
        if not token:
            raise MissingApiTokenError("API token is required before calling the local API")
        if _looks_like_placeholder_token(token):
            raise InvalidApiTokenError("API token must come from a trusted credential source")
        return token


@dataclass(frozen=True)
class DesktopApiClientConfig:
    base_url: str
    timeout_seconds: float = 10.0
    _request_base_urls: tuple[str, ...] = field(init=False, repr=False)
    _host_header: str | None = field(init=False, repr=False, default=None)
    _tls_server_name: str | None = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        timeout_seconds = _validate_timeout_seconds(self.timeout_seconds)
        if not isinstance(self.base_url, str):
            raise ValueError("base_url must be a string")
        normalized = self.base_url.strip()
        if not normalized:
            raise ValueError("base_url is required")
        parsed = _split_valid_base_url(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not include embedded credentials")
        parsed = _normalize_api_base_path(parsed)
        local_hosts = _validated_local_api_hosts(parsed.hostname, parsed.port)
        if local_hosts is None:
            raise ValueError("base_url must point to a local API endpoint")
        request_base_urls = tuple(_replace_url_host(parsed, host).geturl().rstrip("/") + "/" for host in local_hosts)
        base_url = request_base_urls[0]
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "timeout_seconds", timeout_seconds)
        object.__setattr__(self, "_request_base_urls", request_base_urls)
        if parsed.hostname and parsed.hostname.lower() == "localhost":
            object.__setattr__(self, "_host_header", _host_header(parsed.hostname.lower(), parsed.port))
            if parsed.scheme == "https":
                object.__setattr__(self, "_tls_server_name", parsed.hostname.lower())


@dataclass(frozen=True)
class DesktopConnectionSettings:
    """User-configurable API endpoint settings for the desktop shell."""

    api_base_url: str
    timeout_seconds: float = 10.0
    require_https: bool = False

    def to_client_config(self) -> DesktopApiClientConfig:
        config = DesktopApiClientConfig(
            base_url=self.api_base_url,
            timeout_seconds=self.timeout_seconds,
        )
        if self.require_https and not config.base_url.startswith("https://"):
            raise ValueError("HTTPS is required for the configured API endpoint")
        return config

    def build_client(
        self,
        *,
        credential_store: ApiCredentialStore,
        transport: Transport | None = None,
    ) -> "DesktopApiClient":
        return DesktopApiClient(
            self.to_client_config(),
            credential_store=credential_store,
            transport=transport,
        )


class DesktopTemporaryFileManager:
    """Owns desktop-local staging files and removes them at operation end.

    Use this for upload/download/intermediate files created by the thin client.
    Files explicitly selected as final save targets should be registered with
    `register_explicit_artifact` and are not cleanup candidates.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._owned_paths: list[Path] = []
        self._explicit_artifacts: set[Path] = set()
        self._closed = False
        self.root.mkdir(parents=True, exist_ok=True, mode=DESKTOP_TEMP_DIR_MODE)
        _harden_private_path(self.root, DESKTOP_TEMP_DIR_MODE)
        self._ensure_work_dir()

    def __enter__(self) -> "DesktopTemporaryFileManager":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        try:
            self.cleanup()
        except DesktopTemporaryCleanupError:
            if exc_type is None:
                raise
        return False

    @property
    def _work_dir(self) -> Path:
        return self.root / DESKTOP_TEMP_WORK_DIR

    def create_staging_file(self, filename: str, content: bytes = b"") -> Path:
        if self._closed:
            raise RuntimeError("temporary file manager is already closed")
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        token = secrets.token_hex(DESKTOP_TEMP_TOKEN_BYTES)
        prefix = f"tmp-{token}-"
        safe_filename = _sanitize_download_filename(
            filename,
            max_bytes=MAX_DOWNLOAD_FILENAME_BYTES - len(prefix.encode("utf-8")),
        )
        path = self._work_dir / f"{prefix}{safe_filename}"
        self._require_owned_staging_path(path)
        fd = -1
        created_path = False
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, DESKTOP_TEMP_FILE_MODE)
            created_path = True
            _harden_private_path(path, DESKTOP_TEMP_FILE_MODE)
            self._owned_paths.append(path)
            output = os.fdopen(fd, "wb")
            fd = -1
            with output:
                output.write(content)
        except Exception:
            if fd != -1:
                os.close(fd)
                fd = -1
            if created_path and path not in self._owned_paths:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    LOGGER.error("temporary cleanup failed for %s: %s", path, exc)
            raise
        finally:
            if fd != -1:
                os.close(fd)
        return path

    def register_explicit_artifact(self, path: str | Path) -> Path:
        explicit_path = Path(path).expanduser().resolve()
        self._explicit_artifacts.add(explicit_path)
        return explicit_path

    def cancel(self) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        failures: list[Path] = []
        for path in reversed(self._owned_paths):
            if self._is_explicit_artifact(path):
                continue
            try:
                self._require_owned_staging_path(path)
                path.unlink()
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                LOGGER.error("temporary cleanup failed for %s: %s", path, exc)
                failures.append(path)
        self._closed = True
        if failures:
            raise DesktopTemporaryCleanupError(tuple(failures))

    def _ensure_work_dir(self) -> None:
        work_dir = self._work_dir
        if os.path.lexists(work_dir) and work_dir.is_symlink():
            raise ValueError("temporary work directory must not be a symlink")
        work_dir.mkdir(parents=True, exist_ok=True, mode=DESKTOP_TEMP_DIR_MODE)
        if work_dir.is_symlink():
            raise ValueError("temporary work directory must not be a symlink")
        _require_path_inside_root(work_dir.resolve(strict=True), self.root)
        _harden_private_path(work_dir, DESKTOP_TEMP_DIR_MODE)

    def _require_owned_staging_path(self, path: Path) -> None:
        _require_path_inside_root(path, self.root)
        if path.parent != self._work_dir:
            raise ValueError("temporary path escapes storage root")
        if self._work_dir.is_symlink():
            raise ValueError("temporary work directory must not be a symlink")
        _require_path_inside_root(self._work_dir.resolve(strict=True), self.root)

    def _is_explicit_artifact(self, path: Path) -> bool:
        return path in self._explicit_artifacts


class Transport(Protocol):
    def __call__(self, request: Request, *, timeout: float) -> Any:
        ...


class DesktopApiClient:
    def __init__(
        self,
        config: DesktopApiClientConfig,
        *,
        credential_store: ApiCredentialStore,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self._credential_store = credential_store
        self._transport = transport

    def list_jobs(self) -> dict[str, Any]:
        payload = self._request_json("GET", "/api/jobs")
        return _validate_jobs_response(payload)

    def get_job_progress(self, job_id: str) -> DesktopJobDisplayState:
        job_id = _validate_job_id(job_id)
        payload = self._request_json("GET", f"/api/jobs/{quote(job_id, safe='')}")
        job = _validate_job_response(payload)
        return _job_display_state(job)

    def upload_document_file(
        self,
        file_path: str | Path,
        *,
        content_type: str | None = None,
        mode: str = "standard",
        template_id: str | None = None,
    ) -> dict[str, Any]:
        request = _upload_request_from_file(
            file_path,
            content_type=content_type,
            mode=mode,
            template_id=template_id,
        )
        request["desktop_upload_audit"] = True
        payload = self._request_json("POST", "/api/jobs", request)
        job_ref = _validate_job_response(payload)
        _validate_desktop_upload_audit_response(payload, job_ref)
        return job_ref

    def upload_document_files(
        self,
        file_paths: list[str | Path] | tuple[str | Path, ...],
        *,
        content_types: list[str | None] | tuple[str | None, ...] | None = None,
        mode: str = "standard",
        template_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not file_paths:
            raise DesktopUploadValidationError("at least one file is required")
        if content_types is not None and len(content_types) != len(file_paths):
            raise DesktopUploadValidationError("content_types length must match file_paths")
        requests = [
            _upload_request_from_file(
                file_path,
                content_type=None if content_types is None else content_types[index],
                mode=mode,
                template_id=template_id,
            )
            for index, file_path in enumerate(file_paths)
        ]
        job_refs: list[dict[str, Any]] = []
        for request in requests:
            request["desktop_upload_audit"] = True
            payload = self._request_json("POST", "/api/jobs", request)
            job_ref = _validate_job_response(payload)
            _validate_desktop_upload_audit_response(payload, job_ref)
            job_refs.append(job_ref)
        return job_refs

    def save_job_result(self, job_id: str, destination_dir: str | Path) -> Path:
        job_id = _validate_job_id(job_id)
        destination = Path(destination_dir)
        if not destination.is_dir():
            raise ValueError("destination_dir must be an existing directory")
        body, headers = self._request_bytes("GET", f"/api/jobs/{quote(job_id, safe='')}/result")
        filename = _download_filename_from_headers(headers) or f"{job_id}.veridoc-result.json"
        api_filename = _api_download_filename(filename)
        safe_filename = _sanitize_download_filename(api_filename)
        for _ in range(1000):
            save_path = _available_destination_path(destination, safe_filename)
            try:
                _write_download_file(save_path, body, safe_filename)
            except FileExistsError:
                continue
            try:
                audit_status = self._record_desktop_result_download(
                    job_id,
                    {
                        "event_type": "desktop.job_operation",
                        "job_id": job_id,
                        "action": "desktop_result_download",
                        "download_filename": api_filename,
                        "saved_filename": save_path.name,
                        "output_sha256": hashlib.sha256(body).hexdigest(),
                    },
                )
            except (MissingApiTokenError, InvalidApiTokenError, PermissionError):
                _remove_download_file(save_path)
                raise
            if audit_status == "rejected":
                _remove_download_file(save_path)
                raise DesktopApiError("API did not accept the desktop result download audit event")
            if audit_status == "unconfirmed":
                raise DesktopApiError("desktop result download audit outcome is unconfirmed")
            return save_path
        raise DesktopApiError("downloaded result filename has too many collisions")

    def _record_desktop_result_download(
        self,
        job_id: str,
        audit_event: dict[str, Any],
    ) -> Literal["accepted", "rejected", "unconfirmed"]:
        try:
            payload = self._request_json_pre_connection_retry(
                "POST",
                "/api/job-events",
                {
                    "job_id": job_id,
                    "action": "desktop_result_download",
                    "audit_event": audit_event,
                },
            )
        except DesktopApiError as exc:
            if str(exc) in {
                "API response must be a JSON object",
                "API response must be valid JSON",
                "API response body was incomplete",
                "API response transport failed",
            }:
                return "unconfirmed"
            return "rejected"
        except URLError:
            return "unconfirmed"
        audit_event = payload.get("audit_event")
        if payload.get("accepted") is not True or not isinstance(audit_event, dict):
            return "rejected"
        if audit_event.get("job_id") != job_id or audit_event.get("action") != "desktop_result_download":
            return "rejected"
        return "accepted"

    def _request_json_single_attempt(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request_json(method, path, body, retry_on_url_error=False)

    def _request_json_pre_connection_retry(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request_json(
            method,
            path,
            body,
            retry_on_url_error=_is_pre_connection_url_error,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        retry_on_url_error: bool | Callable[[URLError], bool] = True,
    ) -> dict[str, Any]:
        token = self._credential_store.require_token()
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request_urls = tuple(urljoin(base_url, path) for base_url in self.config._request_base_urls)
        last_url_error: URLError | None = None
        for index, request_url in enumerate(request_urls):
            request_headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            }
            if self.config._host_header:
                request_headers["Host"] = self.config._host_header
            request = Request(request_url, data=payload, method=method, headers=request_headers)
            if payload is not None:
                request.add_header("Content-Type", "application/json")

            try:
                with self._open(request) as response:
                    decoded = json.loads(response.read().decode("utf-8"))
                    if not isinstance(decoded, dict):
                        raise DesktopApiError("API response must be a JSON object")
                    return decoded
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise PermissionError("API authentication failed") from exc
                raise DesktopApiError(f"API request failed with HTTP {exc.code}") from exc
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise DesktopApiError("API response must be valid JSON") from exc
            except http.client.IncompleteRead as exc:
                raise DesktopApiError("API response body was incomplete") from exc
            except http.client.HTTPException as exc:
                raise DesktopApiError("API response transport failed") from exc
            except URLError as exc:
                last_url_error = exc
                should_retry = (
                    retry_on_url_error(exc)
                    if callable(retry_on_url_error)
                    else retry_on_url_error
                )
                if should_retry and index + 1 < len(request_urls):
                    continue
                raise

        assert last_url_error is not None
        raise last_url_error

    def _request_bytes(self, method: str, path: str) -> tuple[bytes, dict[str, str]]:
        token = self._credential_store.require_token()
        request_urls = tuple(urljoin(base_url, path) for base_url in self.config._request_base_urls)
        last_url_error: URLError | None = None
        for index, request_url in enumerate(request_urls):
            request_headers = {
                "Accept": "application/octet-stream",
                "Authorization": f"Bearer {token}",
            }
            if self.config._host_header:
                request_headers["Host"] = self.config._host_header
            request = Request(request_url, method=method, headers=request_headers)

            try:
                with self._open(request) as response:
                    return response.read(), _response_headers(response)
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise PermissionError("API authentication failed") from exc
                raise DesktopApiError(f"API request failed with HTTP {exc.code}") from exc
            except http.client.IncompleteRead as exc:
                raise DesktopApiError("API response body was incomplete") from exc
            except http.client.HTTPException as exc:
                raise DesktopApiError("API response transport failed") from exc
            except URLError as exc:
                last_url_error = exc
                if index + 1 < len(request_urls):
                    continue
                raise

        assert last_url_error is not None
        raise last_url_error

    def _open(self, request: Request) -> Any:
        if self._transport is not None:
            return self._transport(request, timeout=self.config.timeout_seconds)
        return _urlopen_transport(
            request,
            timeout=self.config.timeout_seconds,
            tls_server_name=self.config._tls_server_name,
        )


def check_desktop_api_connection(
    settings: DesktopConnectionSettings,
    *,
    credential_store: ApiCredentialStore,
    transport: Transport | None = None,
) -> DesktopConnectionHealthResult:
    try:
        config = settings.to_client_config()
    except ValueError as exc:
        message = str(exc)
        if "HTTPS is required" in message:
            status = "https_required"
        elif "timeout_seconds" in message:
            status = "invalid_timeout"
        else:
            status = "invalid_url"
        return DesktopConnectionHealthResult(
            ok=False,
            status=status,
            message=f"API接続先設定エラー: {exc}",
        )

    client = DesktopApiClient(
        config,
        credential_store=credential_store,
        transport=transport,
    )
    try:
        client.list_jobs()
    except (MissingApiTokenError, InvalidApiTokenError, PermissionError) as exc:
        return DesktopConnectionHealthResult(
            ok=False,
            status="authentication_failed",
            message=f"API認証に失敗しました: {exc}",
            base_url=config.base_url,
        )
    except DesktopApiError as exc:
        return DesktopConnectionHealthResult(
            ok=False,
            status="request_failed",
            message=f"API接続確認に失敗しました: {exc}",
            base_url=config.base_url,
        )
    except http.client.HTTPException as exc:
        return DesktopConnectionHealthResult(
            ok=False,
            status="request_failed",
            message=f"API接続確認に失敗しました: {exc}",
            base_url=config.base_url,
        )
    except (OSError, URLError) as exc:
        return DesktopConnectionHealthResult(
            ok=False,
            status="connection_failed",
            message=f"API接続に失敗しました: {exc}",
            base_url=config.base_url,
        )
    return DesktopConnectionHealthResult(
        ok=True,
        status="connected",
        message="API接続に成功しました。",
        base_url=config.base_url,
    )


def _validate_timeout_seconds(timeout_seconds: object) -> float:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, Real):
        raise ValueError("timeout_seconds must be a finite positive number")
    try:
        value = float(timeout_seconds)
    except (OverflowError, ValueError) as exc:
        raise ValueError("timeout_seconds must be a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout_seconds must be finite and greater than 0")
    if value > threading.TIMEOUT_MAX:
        raise ValueError("timeout_seconds must not exceed the platform timeout maximum")
    return value


def _validate_jobs_response(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload.get("jobs"), list):
        raise DesktopApiError("API jobs response must include a jobs array")
    return payload


def _validate_job_response(payload: dict[str, Any]) -> dict[str, Any]:
    job = payload.get("job")
    if not isinstance(job, dict):
        raise DesktopApiError("API job response must include a job object")
    if not isinstance(job.get("job_id"), str) or not job["job_id"].strip():
        raise DesktopApiError("API job response must include a job_id")
    return job


def _validate_desktop_upload_audit_response(
    payload: dict[str, Any],
    job_ref: dict[str, Any],
) -> None:
    audit_event = payload.get("audit_event")
    if not isinstance(audit_event, dict):
        raise DesktopApiError("API did not accept the desktop upload audit event")
    if audit_event.get("job_id") != job_ref["job_id"] or audit_event.get("action") != "desktop_upload":
        raise DesktopApiError("API accepted a mismatched desktop upload audit event")


def _validate_job_id(job_id: str) -> str:
    if not isinstance(job_id, str):
        raise ValueError("job_id must be a string")
    normalized = job_id.strip()
    if not normalized:
        raise ValueError("job_id is required")
    return normalized


def _job_display_state(job: dict[str, Any]) -> DesktopJobDisplayState:
    job_id = _validate_job_id(job["job_id"])
    api_status = _string_field(job, "status")
    if api_status not in {"queued", "running", "succeeded", "failed"}:
        raise DesktopApiError("API job response includes an unsupported job status")
    display_status = _job_display_status(job, api_status)
    return DesktopJobDisplayState(
        job_id=job_id,
        api_status=api_status,
        display_status=display_status,
        progress_percent=_progress_percent(job, api_status),
        warning_count=_warning_count(job),
        error_message=_optional_string(job.get("error")),
        is_terminal=display_status in {"review_required", "completed", "failed", "blocked"},
    )


def _job_display_status(job: dict[str, Any], api_status: str) -> str:
    raw_display_status = job.get("display_status")
    if isinstance(raw_display_status, str) and raw_display_status.strip():
        display_status = raw_display_status.strip()
    elif api_status == "succeeded":
        display_status = "completed"
    else:
        display_status = api_status
    if display_status == "requires_review":
        display_status = "review_required"
    if display_status not in {"queued", "running", "review_required", "completed", "failed", "blocked"}:
        raise DesktopApiError("API job response includes an unsupported display status")
    return display_status


def _progress_percent(job: dict[str, Any], api_status: str) -> int:
    raw_progress = job.get("progress_percent")
    if raw_progress is None:
        return {"queued": 0, "running": 50, "succeeded": 100, "failed": 100}.get(api_status, 0)
    if isinstance(raw_progress, bool) or not isinstance(raw_progress, int):
        raise DesktopApiError("API job response progress_percent must be an integer")
    if raw_progress < 0 or raw_progress > 100:
        raise DesktopApiError("API job response progress_percent must be between 0 and 100")
    return raw_progress


def _warning_count(job: dict[str, Any]) -> int:
    raw_warning_count = job.get("warning_count", 0)
    if isinstance(raw_warning_count, bool) or not isinstance(raw_warning_count, int):
        raise DesktopApiError("API job response warning_count must be an integer")
    if raw_warning_count < 0:
        raise DesktopApiError("API job response warning_count must not be negative")
    return raw_warning_count


def _string_field(mapping: dict[str, Any], field_name: str) -> str:
    value = mapping.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise DesktopApiError(f"API job response must include {field_name}")
    return value.strip()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DesktopApiError("API job response error must be a string or null")
    return value


def _upload_request_from_file(
    file_path: str | Path,
    *,
    content_type: str | None,
    mode: str,
    template_id: str | None,
) -> dict[str, Any]:
    path = Path(file_path)
    filename = path.name
    if not filename:
        raise DesktopUploadValidationError("filename is required")
    expected_content_type = _expected_upload_content_type(path)
    resolved_content_type = _validated_upload_content_type(
        filename=filename,
        expected_content_type=expected_content_type,
        supplied_content_type=content_type,
    )
    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        raise DesktopUploadValidationError(f"selected file cannot be read: {filename}") from exc
    if size_bytes > MAX_DESKTOP_UPLOAD_BYTES:
        raise DesktopUploadValidationError("selected file exceeds the upload size limit")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise DesktopUploadValidationError(f"selected file cannot be read: {filename}") from exc
    content_sha256 = hashlib.sha256(content).hexdigest()
    request: dict[str, Any] = {
        "filename": filename,
        "content_type": resolved_content_type,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "size_bytes": size_bytes,
        "source_sha256": content_sha256,
        "idempotency_key": _upload_idempotency_key(
            source_sha256=content_sha256,
            filename=filename,
            content_type=resolved_content_type,
            size_bytes=size_bytes,
            mode=mode,
            template_id=template_id,
        ),
        "mode": mode,
    }
    if template_id is not None:
        request["template_id"] = template_id
    return request


def _upload_idempotency_key(
    *,
    source_sha256: str,
    filename: str,
    content_type: str,
    size_bytes: int,
    mode: str,
    template_id: str | None,
) -> str:
    key_material = {
        "content_type": content_type,
        "filename": filename,
        "mode": mode,
        "size_bytes": size_bytes,
        "source_sha256": source_sha256,
        "template_id": template_id,
    }
    digest = hashlib.sha256(
        json.dumps(key_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"upload:{digest}"


def _expected_upload_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    expected = ALLOWED_UPLOAD_CONTENT_TYPES.get(suffix)
    if expected is None:
        allowed = ", ".join(sorted(ALLOWED_UPLOAD_CONTENT_TYPES))
        raise DesktopUploadValidationError(f"unsupported file type; allowed extensions: {allowed}")
    guessed, _encoding = mimetypes.guess_type(path.name)
    if guessed is not None and guessed != expected:
        raise DesktopUploadValidationError("selected file MIME type does not match its extension")
    return expected


def _validated_upload_content_type(
    *,
    filename: str,
    expected_content_type: str,
    supplied_content_type: str | None,
) -> str:
    if supplied_content_type is None or not supplied_content_type.strip():
        return expected_content_type
    normalized = supplied_content_type.split(";", 1)[0].strip().lower()
    if normalized != expected_content_type:
        raise DesktopUploadValidationError(f"selected file MIME type is not allowed: {filename}")
    return normalized


def _response_headers(response: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    raw_headers = getattr(response, "headers", None)
    if raw_headers is not None:
        for name in ("Content-Disposition", "Content-Type"):
            value = raw_headers.get(name)
            if isinstance(value, str):
                headers[name.lower()] = value
    for name in ("Content-Disposition", "Content-Type"):
        getheader = getattr(response, "getheader", None)
        if callable(getheader):
            value = getheader(name)
            if isinstance(value, str):
                headers[name.lower()] = value
    return headers


def _download_filename_from_headers(headers: dict[str, str]) -> str | None:
    content_disposition = headers.get("content-disposition", "")
    if not content_disposition:
        return None
    filename_star = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, flags=re.IGNORECASE)
    if filename_star:
        return unquote(filename_star.group(1).strip().strip('"'))
    filename = re.search(r'filename="([^"]+)"|filename=([^;]+)', content_disposition, flags=re.IGNORECASE)
    if filename:
        return (filename.group(1) or filename.group(2) or "").strip()
    return None


def _api_download_filename(filename: str) -> str:
    basename = filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1].strip()
    safe = re.sub(r'[\x00-\x1f\x7f"\\]', "", basename)
    safe = "".join(char for char in safe if 0x20 <= ord(char) <= 0x7E).strip()
    return safe or DOWNLOAD_FILENAME_FALLBACK


def _sanitize_download_filename(filename: str, *, max_bytes: int = MAX_DOWNLOAD_FILENAME_BYTES) -> str:
    leaf = filename.replace("\\", "/").rsplit("/", maxsplit=1)[-1].strip()
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", leaf)
    sanitized = re.sub(r"\s+", " ", sanitized).strip(" .-")
    if not sanitized:
        return DOWNLOAD_FILENAME_FALLBACK
    sanitized = _avoid_windows_reserved_download_filename(sanitized)
    return _fit_download_filename(sanitized, max_bytes=max_bytes)


def _write_download_file(save_path: Path, body: bytes, safe_filename: str) -> None:
    try:
        with save_path.open("xb") as output:
            output.write(body)
    except FileExistsError:
        raise
    except OSError as exc:
        try:
            save_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        raise DesktopApiError(f"downloaded result could not be saved: {safe_filename}") from exc


def _remove_download_file(save_path: Path) -> None:
    try:
        save_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise DesktopApiError("downloaded result audit failed and saved file could not be removed") from exc


def _require_path_inside_root(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("temporary path escapes storage root") from exc


def _harden_private_path(path: Path, posix_mode: int) -> None:
    if sys.platform == "win32":
        _harden_windows_private_acl(path)
        return
    path.chmod(posix_mode)


def _harden_windows_private_acl(path: Path) -> None:
    script = r"""
$ErrorActionPreference = 'Stop'
$target = Get-Item -LiteralPath $env:VERIDOC_PRIVATE_ACL_PATH -Force
$acl = Get-Acl -LiteralPath $target.FullName
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) {
    [void] $acl.RemoveAccessRuleAll($rule)
}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$accessRule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $identity,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    [System.Security.AccessControl.AccessControlType]::Allow
)
$acl.SetAccessRule($accessRule)
Set-Acl -LiteralPath $target.FullName -AclObject $acl
"""
    env = os.environ.copy()
    env["VERIDOC_PRIVATE_ACL_PATH"] = os.fspath(path)
    try:
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise OSError(f"failed to secure Windows temporary path ACL: {path}") from exc


def _avoid_windows_reserved_download_filename(filename: str) -> str:
    first_segment, separator, remainder = filename.partition(".")
    if first_segment.rstrip(" .").upper() not in WINDOWS_RESERVED_DOWNLOAD_STEMS:
        return filename
    return f"{first_segment.rstrip(' .')}_{separator}{remainder}"


def _available_destination_path(destination_dir: Path, filename: str) -> Path:
    for index in range(1000):
        candidate_name = filename if index == 0 else _fit_download_filename(filename, insertion=f" ({index})")
        candidate = destination_dir / candidate_name
        if not os.path.lexists(candidate):
            return candidate
    raise DesktopApiError("downloaded result filename has too many collisions")


def _split_collision_suffix(filename: str) -> tuple[str, str]:
    compound_suffix = ".veridoc-result.json"
    if filename.lower().endswith(compound_suffix):
        return filename[: -len(compound_suffix)], filename[-len(compound_suffix) :]
    path = Path(filename)
    if path.suffix:
        return filename[: -len(path.suffix)], path.suffix
    return filename, ""


def _fit_download_filename(
    filename: str,
    *,
    insertion: str = "",
    max_bytes: int = MAX_DOWNLOAD_FILENAME_BYTES,
) -> str:
    stem, suffix = _split_collision_suffix(filename)
    reserved = f"{insertion}{suffix}"
    available_stem_bytes = max_bytes - len(reserved.encode("utf-8"))
    if available_stem_bytes > 0:
        fitted_stem = _truncate_utf8_bytes(stem, available_stem_bytes).strip(" .-")
        if fitted_stem:
            return f"{fitted_stem}{reserved}"

    fitted = _truncate_utf8_bytes(f"{stem}{insertion}{suffix}", max_bytes).strip(" .-")
    return fitted or DOWNLOAD_FILENAME_FALLBACK


def _truncate_utf8_bytes(value: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _urlopen_transport(request: Request, *, timeout: float, tls_server_name: str | None = None) -> Any:
    if tls_server_name is not None:
        return _pinned_https_transport(request, timeout=timeout, tls_server_name=tls_server_name)
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    return opener.open(request, timeout=timeout)


def _is_pre_connection_url_error(exc: URLError) -> bool:
    reason = exc.reason
    if isinstance(reason, ConnectionRefusedError):
        return True
    if isinstance(reason, OSError):
        return reason.errno in {errno.ECONNREFUSED, errno.EHOSTUNREACH, errno.ENETUNREACH}
    return False


def _split_valid_base_url(base_url: str) -> SplitResult:
    parsed = urlsplit(base_url)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("base_url port must be a valid TCP port") from exc
    return parsed


def _normalize_api_base_path(parsed: SplitResult) -> SplitResult:
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not include query or fragment")
    path = parsed.path or "/"
    if path in {"", "/"}:
        return parsed._replace(path="/")
    if path in {"/api", "/api/"}:
        return parsed._replace(path="/api/")
    raise ValueError("base_url path must be empty or /api")


class _NoRedirectHandler(HTTPRedirectHandler):
    def http_error_302(self, req: Request, fp: Any, code: int, msg: str, headers: Any) -> Any:
        raise HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def _replace_url_host(parsed: SplitResult, hostname: str) -> SplitResult:
    host = _format_url_host(hostname)
    port = parsed.port
    netloc = host if port is None else f"{host}:{port}"
    return parsed._replace(netloc=netloc)


def _host_header(hostname: str, port: int | None) -> str:
    return hostname if port is None else f"{hostname}:{port}"


def _format_url_host(hostname: str) -> str:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname
    if address.version == 6:
        return f"[{address.compressed}]"
    return address.compressed


def _validated_local_api_hosts(hostname: str | None, port: int | None) -> tuple[str, ...] | None:
    if hostname is None:
        return None
    try:
        address = ipaddress.ip_address(hostname)
        return (address.compressed,) if address.is_loopback else None
    except ValueError:
        pass
    if hostname.lower() != "localhost":
        return None
    try:
        results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    if not results:
        return None
    loopback_addresses: list[str] = []
    seen_addresses: set[str] = set()
    for result in results:
        address = str(result[4][0]).split("%", maxsplit=1)[0]
        try:
            parsed_address = ipaddress.ip_address(address)
            if not parsed_address.is_loopback:
                return None
            compressed = parsed_address.compressed
            if compressed not in seen_addresses:
                loopback_addresses.append(compressed)
                seen_addresses.add(compressed)
        except ValueError:
            return None
    if not loopback_addresses:
        return None
    return tuple(loopback_addresses)


def _pinned_https_transport(request: Request, *, timeout: float, tls_server_name: str) -> Any:
    parsed = urlsplit(request.full_url)
    if parsed.scheme != "https" or parsed.hostname is None:
        raise ValueError("TLS server name pinning requires an HTTPS request URL")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    connection = _PinnedHTTPSConnection(
        connect_host=parsed.hostname,
        port=parsed.port or 443,
        tls_server_name=tls_server_name,
        timeout=timeout,
    )
    try:
        connection.request(
            request.get_method(),
            path,
            body=request.data,
            headers=dict(request.header_items()),
        )
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise HTTPError(
                request.full_url,
                response.status,
                f"local API redirects are disabled: {response.reason}",
                response.msg,
                response,
            )
        if response.status >= 400:
            raise HTTPError(request.full_url, response.status, response.reason, response.msg, response)
        return _PinnedHTTPSResponse(connection, response)
    except HTTPError:
        connection.close()
        raise
    except (OSError, http.client.HTTPException) as exc:
        connection.close()
        raise URLError(exc) from exc


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, connect_host: str, port: int, tls_server_name: str, timeout: float) -> None:
        super().__init__(
            tls_server_name,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._connect_host = connect_host
        self._tls_server_name = tls_server_name

    def connect(self) -> None:
        sock = socket.create_connection((self._connect_host, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._tls_server_name)


class _PinnedHTTPSResponse:
    def __init__(self, connection: _PinnedHTTPSConnection, response: http.client.HTTPResponse) -> None:
        self._connection = connection
        self._response = response

    def __enter__(self) -> _PinnedHTTPSResponse:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._connection.close()

    def read(self) -> bytes:
        return self._response.read()

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._response.getheader(name, default)

    @property
    def headers(self) -> Any:
        return self._response.msg


def _looks_like_placeholder_token(token: str) -> bool:
    normalized = token.strip().lower()
    if normalized.startswith("<") and normalized.endswith(">"):
        return True
    placeholder_fragments = ("todo", "placeholder", "sample", "example", "fake")
    return any(fragment in normalized for fragment in placeholder_fragments)
