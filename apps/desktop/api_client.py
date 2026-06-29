from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import socket
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import SplitResult, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class MissingApiTokenError(RuntimeError):
    """Raised when the desktop client has no trusted API token available."""


class InvalidApiTokenError(RuntimeError):
    """Raised when the configured token is an obvious placeholder or sample."""


class DesktopApiError(RuntimeError):
    """Raised when the local API returns a non-auth client error."""


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

    def __post_init__(self) -> None:
        normalized = self.base_url.strip()
        if not normalized:
            raise ValueError("base_url is required")
        parsed = _split_valid_base_url(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("base_url must not include embedded credentials")
        local_host = _validated_local_api_host(parsed.hostname)
        if local_host is None:
            raise ValueError("base_url must point to a local API endpoint")
        base_url = _replace_url_host(parsed, local_host).geturl().rstrip("/") + "/"
        object.__setattr__(self, "base_url", base_url)


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
        self._transport = transport or _urlopen_transport

    def list_jobs(self) -> dict[str, Any]:
        return self._request_json("GET", "api/jobs")

    def _request_json(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._credential_store.require_token()
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(
            urljoin(self.config.base_url, path),
            data=payload,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        if payload is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with self._transport(request, timeout=self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise PermissionError("API authentication failed") from exc
            raise DesktopApiError(f"API request failed with HTTP {exc.code}") from exc


def _urlopen_transport(request: Request, *, timeout: float) -> Any:
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    return opener.open(request, timeout=timeout)


def _split_valid_base_url(base_url: str) -> SplitResult:
    parsed = urlsplit(base_url)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("base_url port must be a valid TCP port") from exc
    return parsed


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


def _format_url_host(hostname: str) -> str:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname
    if address.version == 6:
        return f"[{address.compressed}]"
    return address.compressed


def _validated_local_api_host(hostname: str | None) -> str | None:
    if hostname is None:
        return None
    try:
        address = ipaddress.ip_address(hostname)
        return address.compressed if address.is_loopback else None
    except ValueError:
        pass
    if hostname.lower() != "localhost":
        return None
    try:
        results = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    if not results:
        return None
    pinned_address: str | None = None
    for result in results:
        address = result[4][0]
        try:
            parsed_address = ipaddress.ip_address(address)
            if not parsed_address.is_loopback:
                return None
            if pinned_address is None:
                pinned_address = parsed_address.compressed
        except ValueError:
            return None
    return pinned_address


def _looks_like_placeholder_token(token: str) -> bool:
    normalized = token.strip().lower()
    if normalized.startswith("<") and normalized.endswith(">"):
        return True
    placeholder_fragments = ("todo", "placeholder", "sample", "example", "fake")
    return any(fragment in normalized for fragment in placeholder_fragments)
