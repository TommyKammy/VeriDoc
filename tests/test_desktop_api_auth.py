from __future__ import annotations

import json
import socket
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request

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
        assert port is None
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
        assert port is None
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
    assert request.host == "127.0.0.1:8765"
    assert request.get_header("Authorization") == "Bearer reviewer-token"


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
