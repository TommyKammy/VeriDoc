from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import ipaddress
import json
import math
from numbers import Real
import socket
import ssl
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import SplitResult, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class MissingApiTokenError(RuntimeError):
    """Raised when the desktop client has no trusted API token available."""


class InvalidApiTokenError(RuntimeError):
    """Raised when the configured token is an obvious placeholder or sample."""


class DesktopApiError(RuntimeError):
    """Raised when the local API returns a non-auth client error."""


@dataclass(frozen=True)
class DesktopConnectionHealthResult:
    ok: bool
    status: str
    message: str
    base_url: str = ""


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

    def _request_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
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
    value = float(timeout_seconds)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("timeout_seconds must be finite and greater than 0")
    return value


def _validate_jobs_response(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload.get("jobs"), list):
        raise DesktopApiError("API jobs response must include a jobs array")
    return payload


def _urlopen_transport(request: Request, *, timeout: float, tls_server_name: str | None = None) -> Any:
    if tls_server_name is not None:
        return _pinned_https_transport(request, timeout=timeout, tls_server_name=tls_server_name)
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    return opener.open(request, timeout=timeout)


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


def _looks_like_placeholder_token(token: str) -> bool:
    normalized = token.strip().lower()
    if normalized.startswith("<") and normalized.endswith(">"):
        return True
    placeholder_fragments = ("todo", "placeholder", "sample", "example", "fake")
    return any(fragment in normalized for fragment in placeholder_fragments)
