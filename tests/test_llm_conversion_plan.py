from __future__ import annotations

import socket
import urllib.error
import urllib.request

import pytest

from core.llm.conversion_plan import (
    CONVERSION_PLAN_SCHEMA,
    ConversionPlanValidationError,
    LocalLLMConfigurationError,
    LocalLLMConversionPlanAdapter,
    _NoRedirectHandler,
    _urllib_transport,
    validate_conversion_plan,
)


def _valid_plan() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_kind": "synthetic_text",
        "operations": [
            {
                "id": "extract-lot-number",
                "action": "extract_field",
                "inputs": ["Lot: ABC-123"],
                "output": "lot_number",
                "rationale": "Synthetic lot field is explicitly labelled.",
            }
        ],
        "constraints": {
            "external_transmission": False,
        },
    }


def test_adapter_returns_schema_valid_conversion_plan_with_temperature_zero() -> None:
    captured_payloads: list[dict[str, object]] = []

    def transport(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        captured_payloads.append(payload)
        assert url == "http://127.0.0.1:8000/v1/chat/completions"
        assert "Authorization" not in headers
        assert timeout_seconds == 10
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"schema_version":1,"source_kind":"synthetic_text",'
                            '"operations":[{"id":"extract-lot-number","action":"extract_field",'
                            '"inputs":["Lot: ABC-123"],"output":"lot_number",'
                            '"rationale":"Synthetic lot field is explicitly labelled."}],'
                            '"constraints":{"external_transmission":false}}'
                        )
                    }
                }
            ]
        }

    adapter = LocalLLMConversionPlanAdapter(
        base_url="http://127.0.0.1:8000/v1",
        model="local-json-model",
        timeout_seconds=10,
        transport=transport,
    )

    plan = adapter.create_conversion_plan("Lot: ABC-123")

    assert plan == _valid_plan()
    assert len(captured_payloads) == 1
    payload = captured_payloads[0]
    assert payload["temperature"] == 0
    assert payload["stream"] is False
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "veridoc_conversion_plan",
            "strict": True,
            "schema": CONVERSION_PLAN_SCHEMA,
        },
    }


def test_schema_incompatible_conversion_plan_fails_closed() -> None:
    invalid_plan = _valid_plan()
    invalid_plan["constraints"] = {"external_transmission": True}

    with pytest.raises(ConversionPlanValidationError, match="external_transmission must be false"):
        validate_conversion_plan(invalid_plan)


def test_boolean_schema_version_fails_closed() -> None:
    invalid_plan = _valid_plan()
    invalid_plan["schema_version"] = True

    with pytest.raises(ConversionPlanValidationError, match="schema_version must be 1"):
        validate_conversion_plan(invalid_plan)


def test_non_string_operation_action_fails_closed() -> None:
    invalid_plan = _valid_plan()
    operations = invalid_plan["operations"]
    assert isinstance(operations, list)
    operation = operations[0]
    assert isinstance(operation, dict)
    operation["action"] = ["extract_field"]

    with pytest.raises(ConversionPlanValidationError, match=r"operations\[0\]\.action is not supported"):
        validate_conversion_plan(invalid_plan)


def test_adapter_rejects_non_local_base_url_before_transport_call() -> None:
    called = False

    def transport(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        nonlocal called
        called = True
        raise AssertionError("transport should not be called")

    with pytest.raises(LocalLLMConfigurationError, match="local-only"):
        LocalLLMConversionPlanAdapter(
            base_url="https://api.example.com/v1",
            model="local-json-model",
            transport=transport,
        )

    assert called is False


def test_adapter_rejects_link_local_base_url_before_transport_call() -> None:
    with pytest.raises(LocalLLMConfigurationError, match="local-only"):
        LocalLLMConversionPlanAdapter(
            base_url="http://169.254.169.254/v1",
            model="local-json-model",
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:70000/v1",
        "http://127.0.0.1:0/v1",
        "http://localhost:not-a-port/v1",
    ],
)
def test_adapter_rejects_invalid_local_base_url_ports(base_url: str) -> None:
    with pytest.raises(LocalLLMConfigurationError, match="local-only"):
        LocalLLMConversionPlanAdapter(
            base_url=base_url,
            model="local-json-model",
        )


def test_adapter_accepts_dns_hostname_after_local_address_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert host == "dwarfstar"
        assert port == 8000
        assert kwargs == {"type": socket.SOCK_STREAM}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.25", 8000))]

    monkeypatch.setattr("core.llm.conversion_plan.socket.getaddrinfo", getaddrinfo)

    adapter = LocalLLMConversionPlanAdapter(
        base_url="http://dwarfstar:8000/v1",
        model="local-json-model",
        transport=lambda _url, _payload, _headers, _timeout: {"choices": [{"message": {"content": _valid_plan()}}]},
    )

    assert adapter.base_url == "http://dwarfstar:8000/v1"


def test_adapter_revalidates_dns_hostname_before_transport_call(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved_addresses = [("10.0.0.25", 8000), ("8.8.8.8", 8000)]

    def getaddrinfo(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert host == "dwarfstar"
        assert port == 8000
        assert kwargs == {"type": socket.SOCK_STREAM}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", resolved_addresses.pop(0))]

    def transport(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        raise AssertionError("transport should not be called after DNS revalidation fails")

    monkeypatch.setattr("core.llm.conversion_plan.socket.getaddrinfo", getaddrinfo)

    adapter = LocalLLMConversionPlanAdapter(
        base_url="http://dwarfstar:8000/v1",
        model="local-json-model",
        transport=transport,
    )

    with pytest.raises(LocalLLMConfigurationError, match="local-only"):
        adapter.create_conversion_plan("Lot: ABC-123")
    assert resolved_addresses == []


def test_adapter_rejects_dns_hostname_with_public_resolved_address(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert host == "llm.example.test"
        assert port == 8000
        assert kwargs == {"type": socket.SOCK_STREAM}
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.25", 8000)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 8000)),
        ]

    monkeypatch.setattr("core.llm.conversion_plan.socket.getaddrinfo", getaddrinfo)

    with pytest.raises(LocalLLMConfigurationError, match="local-only"):
        LocalLLMConversionPlanAdapter(
            base_url="http://llm.example.test:8000/v1",
            model="local-json-model",
        )


def test_adapter_rejects_placeholder_api_key() -> None:
    with pytest.raises(LocalLLMConfigurationError, match="placeholder"):
        LocalLLMConversionPlanAdapter(
            base_url="http://localhost:8000/v1",
            model="local-json-model",
            api_key="TODO",
        )


@pytest.mark.parametrize(
    "api_key",
    [
        "sample-secret",
        "fake_api_key",
        "example-token",
        "please-change-me",
    ],
)
def test_adapter_rejects_placeholder_api_key_variants(api_key: str) -> None:
    with pytest.raises(LocalLLMConfigurationError, match="placeholder"):
        LocalLLMConversionPlanAdapter(
            base_url="http://localhost:8000/v1",
            model="local-json-model",
            api_key=api_key,
        )


def test_urllib_transport_bypasses_ambient_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok": true}'

    class FakeOpener:
        def open(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

    def build_opener(*handlers: object) -> FakeOpener:
        captured["handlers"] = handlers
        return FakeOpener()

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8080")
    monkeypatch.setattr("core.llm.conversion_plan.urllib.request.build_opener", build_opener)

    result = _urllib_transport("http://127.0.0.1:8000/v1/chat/completions", {"model": "local"}, {}, 5)

    assert result == {"ok": True}
    assert captured["timeout"] == 5
    handlers = captured["handlers"]
    assert isinstance(handlers, tuple)
    proxy_handlers = [handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}
    assert any(isinstance(handler, _NoRedirectHandler) for handler in handlers)


def test_no_redirect_handler_rejects_redirects() -> None:
    request = urllib.request.Request("http://127.0.0.1:8000/v1/chat/completions")

    with pytest.raises(urllib.error.HTTPError, match="redirects are disabled"):
        _NoRedirectHandler().redirect_request(request, None, 302, "Found", {}, "https://api.example.com/v1")
