from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Protocol
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


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
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("base_url must be an HTTP(S) URL")
        object.__setattr__(self, "base_url", normalized.rstrip("/") + "/")


Transport = Callable[[Request, float], Any]


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
        self._transport = transport or urlopen

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
            with self._transport(request, self.config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise PermissionError("API authentication failed") from exc
            raise DesktopApiError(f"API request failed with HTTP {exc.code}") from exc


def _looks_like_placeholder_token(token: str) -> bool:
    normalized = token.strip().lower()
    if normalized.startswith("<") and normalized.endswith(">"):
        return True
    placeholder_fragments = ("todo", "placeholder", "sample", "example", "fake")
    return any(fragment in normalized for fragment in placeholder_fragments)
