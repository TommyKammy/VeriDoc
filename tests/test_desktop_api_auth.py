from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from apps.desktop.api_client import (
    ApiCredentialStore,
    DesktopApiClient,
    DesktopApiClientConfig,
    InvalidApiTokenError,
    MissingApiTokenError,
)


class RecordingTransport:
    def __init__(self, *, payload: dict[str, object] | None = None) -> None:
        self.requests: list[Request] = []
        self.timeouts: list[float] = []
        self.payload = payload or {"jobs": []}

    def __call__(self, request: Request, timeout: float):
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
    def unauthorized_transport(request: Request, timeout: float):
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


def test_desktop_api_client_default_transport_passes_timeout_as_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def fake_urlopen(request: Request, data: object | None = None, *, timeout: float | None = None):
        calls.append({"request": request, "data": data, "timeout": timeout})
        return JsonResponse({"jobs": []})

    monkeypatch.setattr("apps.desktop.api_client.urlopen", fake_urlopen)
    client = DesktopApiClient(
        DesktopApiClientConfig(base_url="http://127.0.0.1:8765", timeout_seconds=2.5),
        credential_store=ApiCredentialStore(read_token=lambda: "reviewer-token"),
    )

    assert client.list_jobs() == {"jobs": []}
    assert len(calls) == 1
    assert calls[0]["data"] is None
    assert calls[0]["timeout"] == 2.5
    request = calls[0]["request"]
    assert isinstance(request, Request)
    assert request.get_header("Authorization") == "Bearer reviewer-token"


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
