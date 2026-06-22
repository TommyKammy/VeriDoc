from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

from core.llm.conversion_plan import (
    CONVERSION_PLAN_SCHEMA,
    CONVERSION_TASK_PROMPTS,
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
    assert set(CONVERSION_TASK_PROMPTS) == {
        "text_pdf",
        "scanned_pdf_ocr",
        "word_document",
        "excel_workbook",
    }
    messages = payload["messages"]
    assert isinstance(messages, list)
    system_message = messages[0]
    assert system_message["role"] == "system"
    system_prompt = system_message["content"]
    assert "text_pdf" in system_prompt
    assert "scanned_pdf_ocr" in system_prompt
    assert "word_document" in system_prompt
    assert "excel_workbook" in system_prompt


def test_schema_incompatible_conversion_plan_fails_closed() -> None:
    invalid_plan = _valid_plan()
    invalid_plan["constraints"] = {"external_transmission": True}

    with pytest.raises(ConversionPlanValidationError, match="external_transmission must be false"):
        validate_conversion_plan(invalid_plan)


def test_adapter_repairs_schema_invalid_plan_once() -> None:
    captured_payloads: list[dict[str, object]] = []
    invalid_plan = _valid_plan()
    invalid_plan["constraints"] = {"external_transmission": True}

    def transport(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        captured_payloads.append(payload)
        content = invalid_plan if len(captured_payloads) == 1 else _valid_plan()
        return {"choices": [{"message": {"content": content}}]}

    adapter = LocalLLMConversionPlanAdapter(
        base_url="http://127.0.0.1:8000/v1",
        model="local-json-model",
        transport=transport,
    )

    assert adapter.create_conversion_plan("Lot: ABC-123") == _valid_plan()
    assert len(captured_payloads) == 2
    repair_messages = captured_payloads[1]["messages"]
    assert isinstance(repair_messages, list)
    assert repair_messages[-1]["role"] == "user"
    assert "Repair the previous JSON" in repair_messages[-1]["content"]
    assert "$.constraints.external_transmission must be false" in repair_messages[-1]["content"]


def test_adapter_rejects_when_repaired_plan_remains_invalid() -> None:
    invalid_plan = _valid_plan()
    invalid_plan["constraints"] = {"external_transmission": True}

    def transport(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        return {"choices": [{"message": {"content": invalid_plan}}]}

    adapter = LocalLLMConversionPlanAdapter(
        base_url="http://127.0.0.1:8000/v1",
        model="local-json-model",
        transport=transport,
    )

    with pytest.raises(ConversionPlanValidationError, match="external_transmission must be false"):
        adapter.create_conversion_plan("Lot: ABC-123")


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_adapter_rejects_unclean_llm_finish_reasons(finish_reason: str) -> None:
    call_count = 0

    def transport(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        return {
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {"content": _valid_plan()},
                }
            ]
        }

    adapter = LocalLLMConversionPlanAdapter(
        base_url="http://127.0.0.1:8000/v1",
        model="local-json-model",
        transport=transport,
    )

    with pytest.raises(ConversionPlanValidationError, match=f"finish_reason={finish_reason}"):
        adapter.create_conversion_plan("Lot: ABC-123")
    assert call_count == 1


def test_adapter_wraps_malformed_choice_entries_as_validation_errors() -> None:
    call_count = 0

    def transport(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        nonlocal call_count
        call_count += 1
        return {"choices": [None]}

    adapter = LocalLLMConversionPlanAdapter(
        base_url="http://127.0.0.1:8000/v1",
        model="local-json-model",
        transport=transport,
    )

    with pytest.raises(ConversionPlanValidationError, match=r"choices\[0\]\.message\.content"):
        adapter.create_conversion_plan("Lot: ABC-123")
    assert call_count == 1


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


def test_adapter_rejects_ipv6_ec2_metadata_base_url_before_transport_call() -> None:
    with pytest.raises(LocalLLMConfigurationError, match="local-only"):
        LocalLLMConversionPlanAdapter(
            base_url="http://[fd00:ec2::254]/v1",
            model="local-json-model",
        )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:70000/v1",
        "http://127.0.0.1:0/v1",
        "http://localhost:not-a-port/v1",
        "http://[::1/v1",
    ],
)
def test_adapter_rejects_invalid_local_base_url_ports(base_url: str) -> None:
    with pytest.raises(LocalLLMConfigurationError, match="local-only"):
        LocalLLMConversionPlanAdapter(
            base_url=base_url,
            model="local-json-model",
        )


def test_adapter_resolves_and_pins_localhost_names(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def getaddrinfo(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert host == "localhost"
        assert port == 8000
        assert kwargs == {"type": socket.SOCK_STREAM}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8000))]

    def transport(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        captured["url"] = url
        captured["headers"] = headers
        return {"choices": [{"message": {"content": _valid_plan()}}]}

    monkeypatch.setattr("core.llm.conversion_plan.socket.getaddrinfo", getaddrinfo)

    adapter = LocalLLMConversionPlanAdapter(
        base_url="http://localhost:8000/v1",
        model="local-json-model",
        transport=transport,
    )

    plan = adapter.create_conversion_plan("Lot: ABC-123")

    assert plan == _valid_plan()
    assert captured["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "Host": "localhost:8000",
    }


def test_adapter_rejects_localhost_names_resolving_outside_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert host == "localhost"
        assert port == 8000
        assert kwargs == {"type": socket.SOCK_STREAM}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.25", 8000))]

    monkeypatch.setattr("core.llm.conversion_plan.socket.getaddrinfo", getaddrinfo)

    with pytest.raises(LocalLLMConfigurationError, match="local-only"):
        LocalLLMConversionPlanAdapter(
            base_url="http://localhost:8000/v1",
            model="local-json-model",
        )


def test_adapter_accepts_localhost_subdomain_after_loopback_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert host == "llm.localhost"
        assert port == 8000
        assert kwargs == {"type": socket.SOCK_STREAM}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8000))]

    monkeypatch.setattr("core.llm.conversion_plan.socket.getaddrinfo", getaddrinfo)

    adapter = LocalLLMConversionPlanAdapter(
        base_url="http://llm.localhost:8000/v1",
        model="local-json-model",
        transport=lambda _url, _payload, _headers, _timeout: {"choices": [{"message": {"content": _valid_plan()}}]},
    )

    assert adapter.base_url == "http://llm.localhost:8000/v1"


def test_adapter_revalidates_localhost_subdomain_before_transport_call(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved_addresses = [("127.0.0.1", 8000), ("8.8.8.8", 8000)]

    def getaddrinfo(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert host == "llm.localhost"
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
        base_url="http://llm.localhost:8000/v1",
        model="local-json-model",
        transport=transport,
    )

    with pytest.raises(LocalLLMConfigurationError, match="local-only"):
        adapter.create_conversion_plan("Lot: ABC-123")
    assert resolved_addresses == []


def test_adapter_pins_localhost_subdomain_to_validated_address_for_request(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def getaddrinfo(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert host == "llm.localhost"
        assert port == 8000
        assert kwargs == {"type": socket.SOCK_STREAM}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8000))]

    def transport(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> dict[str, object]:
        captured["url"] = url
        captured["headers"] = headers
        return {"choices": [{"message": {"content": _valid_plan()}}]}

    monkeypatch.setattr("core.llm.conversion_plan.socket.getaddrinfo", getaddrinfo)

    adapter = LocalLLMConversionPlanAdapter(
        base_url="http://llm.localhost:8000/v1",
        model="local-json-model",
        transport=transport,
    )

    plan = adapter.create_conversion_plan("Lot: ABC-123")

    assert plan == _valid_plan()
    assert captured["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "Host": "llm.localhost:8000",
    }


def test_adapter_tries_all_validated_localhost_subdomain_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_urls: list[str] = []

    def getaddrinfo(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert host == "llm.localhost"
        assert port == 8000
        assert kwargs == {"type": socket.SOCK_STREAM}
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 8000, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8000)),
        ]

    def urllib_transport(
        url: str,
        payload: dict[str, object],
        headers: dict[str, str],
        timeout_seconds: float,
        *,
        tls_server_name: str | None = None,
    ) -> dict[str, object]:
        captured_urls.append(url)
        if len(captured_urls) == 1:
            raise RuntimeError("local LLM request failed: connection refused")
        return {"choices": [{"message": {"content": _valid_plan()}}]}

    monkeypatch.setattr("core.llm.conversion_plan.socket.getaddrinfo", getaddrinfo)
    monkeypatch.setattr("core.llm.conversion_plan._urllib_transport", urllib_transport)

    adapter = LocalLLMConversionPlanAdapter(
        base_url="http://llm.localhost:8000/v1",
        model="local-json-model",
    )

    plan = adapter.create_conversion_plan("Lot: ABC-123")

    assert plan == _valid_plan()
    assert captured_urls == [
        "http://[::1]:8000/v1/chat/completions",
        "http://127.0.0.1:8000/v1/chat/completions",
    ]


def test_adapter_preserves_tls_server_name_when_pinning_https_localhost_subdomain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def getaddrinfo(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        assert host == "llm.localhost"
        assert port == 8000
        assert kwargs == {"type": socket.SOCK_STREAM}
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 8000))]

    class FakeResponse:
        status = 200
        reason = "OK"
        msg: dict[str, str] = {}

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": _valid_plan()},
                        }
                    ]
                }
            ).encode("utf-8")

    class FakePinnedHTTPSConnection:
        def __init__(self, connect_host: str, port: int, tls_server_name: str, timeout: float) -> None:
            captured["connect_host"] = connect_host
            captured["port"] = port
            captured["tls_server_name"] = tls_server_name
            captured["timeout"] = timeout

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            captured["headers"] = headers

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr("core.llm.conversion_plan.socket.getaddrinfo", getaddrinfo)
    monkeypatch.setattr("core.llm.conversion_plan._PinnedHTTPSConnection", FakePinnedHTTPSConnection)

    adapter = LocalLLMConversionPlanAdapter(
        base_url="https://llm.localhost:8000/v1",
        model="local-json-model",
        timeout_seconds=10,
    )

    plan = adapter.create_conversion_plan("Lot: ABC-123")

    assert plan == _valid_plan()
    assert captured["connect_host"] == "127.0.0.1"
    assert captured["port"] == 8000
    assert captured["tls_server_name"] == "llm.localhost"
    assert captured["timeout"] == 10
    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/chat/completions"
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "Host": "llm.localhost:8000",
    }
    assert captured["closed"] is True


@pytest.mark.parametrize("base_url", ["https://api.openai.com/v1", "http://dwarfstar:8000/v1"])
def test_adapter_rejects_arbitrary_dns_hostname_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    called = False

    def getaddrinfo(
        host: str,
        port: int | None,
        *args: object,
        **kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        nonlocal called
        called = True
        raise AssertionError("public DNS hostnames must fail before resolution")

    monkeypatch.setattr("core.llm.conversion_plan.socket.getaddrinfo", getaddrinfo)

    with pytest.raises(LocalLLMConfigurationError, match="local-only"):
        LocalLLMConversionPlanAdapter(
            base_url=base_url,
            model="local-json-model",
        )

    assert called is False


def test_adapter_rejects_placeholder_api_key() -> None:
    with pytest.raises(LocalLLMConfigurationError, match="placeholder"):
        LocalLLMConversionPlanAdapter(
            base_url="http://127.0.0.1:8000/v1",
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
            base_url="http://127.0.0.1:8000/v1",
            model="local-json-model",
            api_key=api_key,
        )


def test_adapter_repr_redacts_api_key() -> None:
    adapter = LocalLLMConversionPlanAdapter(
        base_url="http://127.0.0.1:8000/v1",
        model="local-json-model",
        api_key="operator-runtime-token",
    )

    rendered = repr(adapter)

    assert "operator-runtime-token" not in rendered
    assert "api_key" not in rendered


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


@pytest.mark.parametrize("response_body", ["not-json", '{"choices": ['])
def test_urllib_transport_wraps_malformed_json_response_body(
    monkeypatch: pytest.MonkeyPatch,
    response_body: str,
) -> None:
    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def read(self) -> bytes:
            return response_body.encode("utf-8")

    class FakeOpener:
        def open(self, request: urllib.request.Request, timeout: float) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr("core.llm.conversion_plan.urllib.request.build_opener", lambda *handlers: FakeOpener())

    with pytest.raises(ConversionPlanValidationError, match="response body is not valid JSON"):
        _urllib_transport("http://127.0.0.1:8000/v1/chat/completions", {"model": "local"}, {}, 5)


def test_pinned_https_transport_wraps_malformed_json_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status = 200
        reason = "OK"
        msg: dict[str, str] = {}

        def read(self) -> bytes:
            return b"not-json"

    class FakePinnedHTTPSConnection:
        def __init__(self, connect_host: str, port: int, tls_server_name: str, timeout: float) -> None:
            pass

        def request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> None:
            pass

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass

    monkeypatch.setattr("core.llm.conversion_plan._PinnedHTTPSConnection", FakePinnedHTTPSConnection)

    with pytest.raises(ConversionPlanValidationError, match="response body is not valid JSON"):
        _urllib_transport(
            "https://127.0.0.1:8000/v1/chat/completions",
            {"model": "local"},
            {},
            5,
            tls_server_name="localhost",
        )


def test_no_redirect_handler_rejects_redirects() -> None:
    request = urllib.request.Request("http://127.0.0.1:8000/v1/chat/completions")

    with pytest.raises(urllib.error.HTTPError, match="redirects are disabled"):
        _NoRedirectHandler().redirect_request(request, None, 302, "Found", {}, "https://api.example.com/v1")
