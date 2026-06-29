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
        self.payload = payload or {"jobs": []}

    def __call__(self, request: Request, timeout: float):
        self.requests.append(request)
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
