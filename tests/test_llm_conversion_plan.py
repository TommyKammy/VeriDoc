from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

from core.llm.conversion_plan import (
    CONVERSION_PLAN_SCHEMA,
    CONVERSION_TASK_PROMPTS,
    build_conversion_audit_log,
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


def test_build_conversion_audit_log_records_hashes_metadata_and_redacts_secrets() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "temperature": 0,
            "max_tokens": 1024,
            "api_key": "operator-runtime-token",
            "apiKey": "operator-runtime-api-key",
            "credentials": "operator-runtime-credentials",
            "accessToken": "operator-runtime-access-token",
            "nested": {
                "authorization": "Bearer operator-runtime-token",
                "refreshToken": "operator-runtime-refresh-token",
                "clientSecret": "operator-runtime-client-secret",
            },
        },
    )

    assert audit_log == {
        "schema_version": "veridoc-conversion-audit-log/v0",
        "source_sha256": "b0ebacea0dcf186fd6c9fd36fd9bb1fc087e1b75fee337a3ebef432143529558",
        "output_sha256": "46401c81d6eb7e1cc2ad9f82df8b95c5aabab04143d90a15c8868e9767e208ca",
        "model": "local-json-model",
        "prompt": {
            "id": "veridoc_conversion_plan",
            "version": "poc-08",
        },
        "ir_version": "document-ir-v1",
        "parameters": {
            "temperature": 0,
            "max_tokens": 1024,
            "api_key": "[REDACTED]",
            "apiKey": "[REDACTED]",
            "credentials": "[REDACTED]",
            "accessToken": "[REDACTED]",
            "nested": {
                "authorization": "[REDACTED]",
                "refreshToken": "[REDACTED]",
                "clientSecret": "[REDACTED]",
            },
        },
    }
    rendered = json.dumps(audit_log, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "operator-runtime-api-key" not in rendered
    assert "operator-runtime-credentials" not in rendered
    assert "operator-runtime-access-token" not in rendered
    assert "operator-runtime-refresh-token" not in rendered
    assert "operator-runtime-client-secret" not in rendered
    assert "Bearer" not in rendered


@pytest.mark.parametrize(
    "parameter_key",
    [
        "credentials",
        "serviceCredentials",
        "googleCredentialsJson",
        "auth",
        "authentication",
        "clientAuthentication",
        "basicAuth",
        "accessKey",
        "accountKey",
        "storageAccountKey",
        "subscriptionKey",
        "Ocp-Apim-Subscription-Key",
        "x-functions-key",
        "connectionString",
        "accessToken",
        "accessTokens",
        "refresh_tokens",
        "passwords",
        "githubTokenFile",
        "refreshToken",
        "clientSecret",
        "awsSecretAccessKey",
        "privateKey",
        "signingPrivateKeyPem",
        "Cookie",
        "Set-Cookie",
        "session",
        "jwt",
    ],
)
def test_build_conversion_audit_log_redacts_review_thread_credential_keys(parameter_key: str) -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={parameter_key: "operator-runtime-sensitive-value"},
    )

    assert audit_log["parameters"] == {parameter_key: "[REDACTED]"}
    assert "operator-runtime-sensitive-value" not in json.dumps(audit_log, sort_keys=True)


@pytest.mark.parametrize(
    "parameters",
    [
        {"X-Amz-Signature": "operator-runtime-signature"},
        {"sig": "operator-runtime-signature"},
        {"sharedAccessSignature": "operator-runtime-signature"},
        {"endpoint": "https://example.invalid/blob?sv=1&sig=operator-runtime-signature"},
        {"callback_url": "https://example.invalid/cb#access_token=operator-runtime-token"},
    ],
)
def test_build_conversion_audit_log_redacts_signature_credentials(
    parameters: dict[str, object],
) -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters=parameters,
    )

    rendered = json.dumps(audit_log, sort_keys=True)
    assert "operator-runtime-signature" not in rendered
    assert "operator-runtime-token" not in rendered


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({"messages": [{"role": "user", "content": "Lot: ABC-123"}]}, r"parameters\.messages"),
        ({"body": "Lot: ABC-123"}, r"parameters\.body"),
        ({"requestBody": "Lot: ABC-123"}, r"parameters\.requestBody"),
        ({"payload": "Lot: ABC-123"}, r"parameters\.payload"),
        ({"generation": {"previous_response": {"choices": []}}}, r"parameters\.generation\.previous_response"),
        ({"tools": [{"content": "Lot: ABC-123"}]}, r"parameters\.tools\[0\]\.content"),
        ({"previousResponse": {"choices": []}}, r"parameters\.previousResponse"),
        ({"source": "Lot: ABC-123"}, r"parameters\.source"),
        ({"raw_source": "Lot: ABC-123"}, r"parameters\.raw_source"),
        ({"rawOutput": '{"lot_number":"ABC-123"}'}, r"parameters\.rawOutput"),
        ({"output": '{"lot_number":"ABC-123"}'}, r"parameters\.output"),
        ({"input": "Lot: ABC-123"}, r"parameters\.input"),
        ({"instructions": "Use the source document exactly."}, r"parameters\.instructions"),
        ({"prompt": "Lot: ABC-123"}, r"parameters\.prompt"),
        ({"attachment": "Lot: ABC-123"}, r"parameters\.attachment"),
        ({"upload": "Lot: ABC-123"}, r"parameters\.upload"),
        ({"userPrompt": "Lot: ABC-123"}, r"parameters\.userPrompt"),
        ({"system_prompt": "Lot: ABC-123"}, r"parameters\.system_prompt"),
        ({"inputText": "Lot: ABC-123"}, r"parameters\.inputText"),
        ({"text": "Lot: ABC-123"}, r"parameters\.text"),
        ({"documents": ["Lot: ABC-123"]}, r"parameters\.documents"),
        ({"prompts": ["Lot: ABC-123"]}, r"parameters\.prompts"),
        ({"synthetic_text": "Lot: ABC-123"}, r"parameters\.synthetic_text"),
        ({"document": "Lot: ABC-123"}, r"parameters\.document"),
        ({"sourceDocument": "Lot: ABC-123"}, r"parameters\.sourceDocument"),
        ({"document_bytes": b"Lot: ABC-123\n"}, r"parameters\.document_bytes"),
        ({"source_bytes": b"Lot: ABC-123\n"}, r"parameters\.source_bytes"),
        ({"output_bytes": b'{"lot_number":"ABC-123"}\n'}, r"parameters\.output_bytes"),
        ({"sourceBytes": b"Lot: ABC-123\n"}, r"parameters\.sourceBytes"),
        ({"outputBytes": b'{"lot_number":"ABC-123"}\n'}, r"parameters\.outputBytes"),
        ({"source": {"bytes": b"Lot: ABC-123\n"}}, r"parameters\.source"),
        ({"output": {"bytes": b'{"lot_number":"ABC-123"}\n'}}, r"parameters\.output"),
        ({"source": [("bytes", b"Lot: ABC-123\n")]}, r"parameters\.source"),
        ({"extra": [["prompt", "Lot: ABC-123"]]}, r"parameters\.extra\[0\]\.prompt"),
        ({"extra": [["raw%5Fsource", "Lot: ABC-123"]]}, r"parameters\.extra\[0\]\.raw%5Fsource"),
        ({"blob": b"Lot: ABC-123\n"}, r"parameters\.blob"),
        ({"file": "Lot: ABC-123"}, r"parameters\.file"),
        ({"files": ["Lot: ABC-123"]}, r"parameters\.files\[0\]"),
        ({"files": [("upload", b"Lot: ABC-123\n")]}, r"parameters\.files\[0\]\.upload"),
        ({"files": [("upload", "Lot: ABC-123")]}, r"parameters\.files\[0\]\.upload"),
        (
            {"files": [{"key": "upload", "value": "Lot: ABC-123"}]},
            r"parameters\.files\[0\]\.upload",
        ),
        (
            {"files": [{"filename": "source.pdf", "sha256": "source-sha256"}]},
            r"parameters\.files\[0\]",
        ),
        (
            {"callback_url": "https://example.invalid/cb?prompt=Lot%3A+ABC-123"},
            r"parameters\.callback_url",
        ),
        (
            {"callback_url": "https://example.invalid/cb?version=1;prompt=Lot%3A+ABC-123"},
            r"parameters\.callback_url",
        ),
        (
            {"image_url": "data:application/pdf;base64,TG90OiBBQkMtMTIz"},
            r"parameters\.image_url",
        ),
    ],
)
def test_build_conversion_audit_log_rejects_content_bearing_parameters(
    parameters: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters=parameters,
        )


def test_build_conversion_audit_log_allows_scalar_prompt_token_limits() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={"max_prompt_tokens": 4096},
    )

    assert audit_log["parameters"] == {"max_prompt_tokens": 4096}


def test_build_conversion_audit_log_redacts_tuple_parameter_entries() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "headers": [("Authorization", "Bearer operator-runtime-token")],
            "options": [("max_prompt_tokens", 4096)],
        },
    )

    assert audit_log["parameters"] == {
        "headers": [["Authorization", "[REDACTED]"]],
        "options": [["max_prompt_tokens", 4096]],
    }
    assert "operator-runtime-token" not in json.dumps(audit_log, sort_keys=True)
    assert "Bearer" not in json.dumps(audit_log, sort_keys=True)


def test_build_conversion_audit_log_preserves_two_string_generation_parameter_lists() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "metadata": ["prompt", "END"],
            "stop": ["prompt", "END"],
        },
    )

    assert audit_log["parameters"] == {
        "metadata": ["prompt", "END"],
        "stop": ["prompt", "END"],
    }


def test_build_conversion_audit_log_sanitizes_list_key_value_parameter_entries() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "headers": [["Authorization", "Bearer operator-runtime-token"]],
            "options": [["max_prompt_tokens", 4096]],
            "extra": [["privateKey", "operator-runtime-private-key"]],
        },
    )

    assert audit_log["parameters"] == {
        "headers": [["Authorization", "[REDACTED]"]],
        "options": [["max_prompt_tokens", 4096]],
        "extra": [["privateKey", "[REDACTED]"]],
    }
    rendered = json.dumps(audit_log, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "operator-runtime-private-key" not in rendered
    assert "Bearer" not in rendered


def test_build_conversion_audit_log_sanitizes_mapping_key_value_parameter_entries() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "headers": [{"name": "Authorization", "value": "Bearer operator-runtime-token"}],
            "options": [{"key": "max_prompt_tokens", "value": 4096}],
            "extra_headers": [{"header": "Authorization", "value": "Bearer operator-runtime-token-2"}],
            "query_params": [{"parameter": "api_key", "value": "operator-runtime-api-key"}],
        },
    )

    assert audit_log["parameters"] == {
        "headers": [{"name": "Authorization", "value": "[REDACTED]"}],
        "options": [{"key": "max_prompt_tokens", "value": 4096}],
        "extra_headers": [{"header": "Authorization", "value": "[REDACTED]"}],
        "query_params": [{"parameter": "api_key", "value": "[REDACTED]"}],
    }
    rendered = json.dumps(audit_log, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "operator-runtime-token-2" not in rendered
    assert "operator-runtime-api-key" not in rendered
    assert "Bearer" not in rendered


def test_build_conversion_audit_log_normalizes_mapping_key_value_field_labels() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "headers": [{"Name": "Authorization", "Value": "Bearer operator-runtime-token"}],
            "options": [{"Key": "max_prompt_tokens", "Value": 4096}],
        },
    )

    assert audit_log["parameters"] == {
        "headers": [{"Name": "Authorization", "Value": "[REDACTED]"}],
        "options": [{"Key": "max_prompt_tokens", "Value": 4096}],
    }
    rendered = json.dumps(audit_log, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "Bearer" not in rendered


def test_build_conversion_audit_log_sanitizes_extra_header_and_cookie_pair_containers() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "extra_headers": [("Authorization", "Bearer operator-runtime-token")],
            "extra_cookies": [("sessionToken", "operator-runtime-session")],
            "cookies": "theme=light; session=operator-runtime-session",
            "customCookies": "csrftoken=operator-runtime-csrf; theme=light",
            "http_headers": [["Ocp-Apim-Subscription-Key", "operator-runtime-subscription"]],
        },
    )

    assert audit_log["parameters"] == {
        "extra_headers": [["Authorization", "[REDACTED]"]],
        "extra_cookies": [["sessionToken", "[REDACTED]"]],
        "cookies": "theme=light; session=[REDACTED]",
        "customCookies": "csrftoken=[REDACTED]; theme=light",
        "http_headers": [["Ocp-Apim-Subscription-Key", "[REDACTED]"]],
    }
    rendered = json.dumps(audit_log, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "operator-runtime-session" not in rendered
    assert "operator-runtime-csrf" not in rendered
    assert "operator-runtime-subscription" not in rendered
    assert "Bearer" not in rendered


def test_build_conversion_audit_log_redacts_raw_header_line_parameter_values() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "headers": ["Authorization: Bearer operator-runtime-token"],
            "extra_headers": ["Ocp-Apim-Subscription-Key: operator-runtime-subscription"],
            "request_headers": ["X-Api-Key=operator-runtime-api-key"],
        },
    )

    assert audit_log["parameters"] == {
        "headers": ["Authorization: [REDACTED]"],
        "extra_headers": ["Ocp-Apim-Subscription-Key: [REDACTED]"],
        "request_headers": ["X-Api-Key=[REDACTED]"],
    }
    rendered = json.dumps(audit_log, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "operator-runtime-subscription" not in rendered
    assert "operator-runtime-api-key" not in rendered
    assert "Bearer" not in rendered


def test_build_conversion_audit_log_redacts_multi_entry_raw_parameter_strings() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "query_params": "version=1&api%5Fkey=operator-runtime-api-key",
            "params_semicolon": "version=1;token=not-a-container",
            "params": [
                "callback=https://example.invalid/callback?sig=operator-runtime-signature",
                "?key=operator-runtime-query-key",
                "code=operator-runtime-function-code",
                "version=1;api_key=operator-runtime-api-key-2",
            ],
            "headers": "X-Test: ok\nAuthorization: Bearer operator-runtime-token",
            "extra_headers": [
                "Authorization=Bearer operator-runtime-token-with:colon",
            ],
            "cookies": "theme=light; session=operator-runtime-session:with-colon",
        },
    )

    assert audit_log["parameters"] == {
        "query_params": "version=1&api%5Fkey=[REDACTED]",
        "params_semicolon": "version=1;token=not-a-container",
        "params": ["callback=[REDACTED]", "?key=[REDACTED]", "code=[REDACTED]", "version=1;api_key=[REDACTED]"],
        "headers": "X-Test: ok\nAuthorization: [REDACTED]",
        "extra_headers": ["Authorization=[REDACTED]"],
        "cookies": "theme=light; session=[REDACTED]",
    }
    rendered = json.dumps(audit_log, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "operator-runtime-api-key-2" not in rendered
    assert "operator-runtime-query-key" not in rendered
    assert "operator-runtime-function-code" not in rendered
    assert "operator-runtime-signature" not in rendered
    assert "operator-runtime-token-with" not in rendered
    assert "operator-runtime-session" not in rendered
    assert "operator-runtime-token" not in rendered
    assert "Bearer" not in rendered


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        (
            {"query_params": "callback=https://example.invalid/cb?prompt=Lot%3A+ABC-123"},
            r"parameters\.query_params\.callback",
        ),
        (
            {"headers": "Referer: https://example.invalid/cb?prompt=Lot%3A+ABC-123"},
            r"parameters\.headers\.Referer",
        ),
        (
            {"customHeaders": "Prompt: Lot: ABC-123"},
            r"parameters\.customHeaders\.Prompt",
        ),
    ],
)
def test_build_conversion_audit_log_rejects_raw_content_url_parameter_values(
    parameters: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters=parameters,
        )


def test_build_conversion_audit_log_rejects_multi_entry_raw_content_parameters() -> None:
    with pytest.raises(ValueError, match=r"parameters\.query_params\.prompt"):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters={"query_params": "version=1&prompt=Lot: ABC-123"},
        )


def test_build_conversion_audit_log_rejects_semicolon_raw_query_content_parameters() -> None:
    with pytest.raises(ValueError, match=r"parameters\.query_params\.prompt"):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters={"query_params": "version=1;prompt=Lot: ABC-123"},
        )


def test_build_conversion_audit_log_allows_content_type_header_metadata() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "headers": {
                "Content-Type": "application/json",
                "Content-Length": "123",
                "Content-Encoding": "gzip",
                "Content-MD5": "checksum",
                "X-Amz-Content-Sha256": "sha256-checksum",
            },
            "extra_headers": [
                ("Content-Type", "application/json"),
                "Content-Type: application/json",
                "Content-Length: 123",
            ],
        },
    )

    assert audit_log["parameters"] == {
        "headers": {
            "Content-Type": "application/json",
            "Content-Length": "123",
            "Content-Encoding": "gzip",
            "Content-MD5": "checksum",
            "X-Amz-Content-Sha256": "sha256-checksum",
        },
        "extra_headers": [
            ["Content-Type", "application/json"],
            "Content-Type: application/json",
            "Content-Length: 123",
        ],
    }


def test_build_conversion_audit_log_redacts_credential_bearing_url_values() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "base_url": "https://operator:operator-runtime-password@example.invalid/v1",
            "callback_url": (
                "https://example.invalid/callback?api_key=operator-runtime-api-key"
            ),
            "redirect_url": (
                "https://example.invalid/callback?"
                "next=https%3A%2F%2Fnested.invalid%2F%3Fkey%3Doperator-runtime-query-key"
            ),
            "function_url": "https://example.invalid/api/convert?code=operator-runtime-function-code",
            "webhook_url": (
                "https://example.invalid/callback?version=1;api_key=operator-runtime-api-key-2"
            ),
            "metadata_url": "https://example.invalid/metadata",
        },
    )

    assert audit_log["parameters"] == {
        "base_url": "[REDACTED]",
        "callback_url": "[REDACTED]",
        "redirect_url": "[REDACTED]",
        "function_url": "[REDACTED]",
        "webhook_url": "[REDACTED]",
        "metadata_url": "https://example.invalid/metadata",
    }
    rendered = json.dumps(audit_log, sort_keys=True)
    assert "operator-runtime-password" not in rendered
    assert "operator-runtime-api-key" not in rendered
    assert "operator-runtime-api-key-2" not in rendered
    assert "operator-runtime-query-key" not in rendered
    assert "operator-runtime-function-code" not in rendered


def test_build_conversion_audit_log_redacts_header_suffixed_parameter_containers() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "default_headers": [("Authorization", "Bearer operator-runtime-token")],
            "customHeaders": ["X-Api-Key=operator-runtime-api-key"],
        },
    )

    assert audit_log["parameters"] == {
        "default_headers": [["Authorization", "[REDACTED]"]],
        "customHeaders": ["X-Api-Key=[REDACTED]"],
    }
    rendered = json.dumps(audit_log, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "operator-runtime-api-key" not in rendered
    assert "Bearer" not in rendered


def test_build_conversion_audit_log_rejects_mapping_key_value_content_parameter_entries() -> None:
    with pytest.raises(ValueError, match=r"parameters\.options\[0\]\.prompt"):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters={"options": [{"key": "prompt", "value": "Lot: ABC-123"}]},
        )


def test_build_conversion_audit_log_rejects_normalized_mapping_key_value_content_parameter_entries() -> None:
    with pytest.raises(ValueError, match=r"parameters\.options\[0\]\.prompt"):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters={"options": [{"Name": "prompt", "Value": "Lot: ABC-123"}]},
        )


def test_build_conversion_audit_log_rejects_list_key_value_content_parameter_entries() -> None:
    with pytest.raises(ValueError, match=r"parameters\.options\.prompt"):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters={"options": ["prompt", "Lot: ABC-123"]},
        )


def test_build_conversion_audit_log_rejects_parameters_key_value_content_entries() -> None:
    with pytest.raises(ValueError, match=r"parameters\.parameters\.prompt"):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters={"parameters": ["prompt", "Lot: ABC-123"]},
        )


def test_build_conversion_audit_log_rejects_raw_key_value_content_parameter_lines() -> None:
    with pytest.raises(ValueError, match=r"parameters\.headers\[0\]\.Prompt"):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters={"headers": ["Prompt: Lot: ABC-123"]},
        )


def test_build_conversion_audit_log_allows_response_format_schema_property_names() -> None:
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "veridoc_conversion_plan",
            "schema": {
                "type": "object",
                "properties": {
                    "output": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string"},
                            "token": {"type": "string"},
                            "password": {"type": "string"},
                            "api_key": {"type": "string"},
                            "attachment": {
                                "type": "string",
                                "contentMediaType": "application/pdf",
                                "contentEncoding": "base64",
                            },
                        },
                    },
                },
            },
        },
    }

    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={"response_format": response_format},
    )

    assert audit_log["parameters"] == {"response_format": response_format}


def test_build_conversion_audit_log_allows_response_format_schema_defs_names() -> None:
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "veridoc_conversion_plan",
            "schema": {
                "type": "object",
                "$defs": {
                    "output": {"type": "string"},
                    "input": {"type": "string"},
                    "content": {"type": "string"},
                    "signature": {"type": "string"},
                },
                "properties": {
                    "result": {"$ref": "#/$defs/output"},
                },
            },
        },
    }

    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={"response_format": response_format},
    )

    assert audit_log["parameters"] == {"response_format": response_format}


@pytest.mark.parametrize("schema_key", ["default", "const", "examples"])
def test_build_conversion_audit_log_rejects_schema_content_values(
    schema_key: str,
) -> None:
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "veridoc_conversion_plan",
            "schema": {
                "type": "object",
                "properties": {
                    "output": {
                        "type": "string",
                        schema_key: "Lot: ABC-123",
                    },
                },
            },
        },
    }

    with pytest.raises(
        ValueError,
        match=rf"parameters\.response_format\.json_schema\.schema\.properties\.output\.{schema_key}",
    ):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters={"response_format": response_format},
        )


@pytest.mark.parametrize(
    ("field_name", "schema_key", "schema_value"),
    [
        ("lot_number", "const", "ABC-123"),
        ("lot_number", "default", 123),
        ("patient_name", "examples", ["Jane Doe"]),
    ],
)
def test_build_conversion_audit_log_rejects_domain_schema_values(
    field_name: str,
    schema_key: str,
    schema_value: object,
) -> None:
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "veridoc_conversion_plan",
            "schema": {
                "type": "object",
                "properties": {
                    field_name: {
                        "type": "string",
                        schema_key: schema_value,
                    },
                },
            },
        },
    }

    with pytest.raises(
        ValueError,
        match=rf"parameters\.response_format\.json_schema\.schema\.properties\.{field_name}\.{schema_key}",
    ):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters={"response_format": response_format},
        )


def test_build_conversion_audit_log_rejects_scalar_schema_field_entries() -> None:
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "veridoc_conversion_plan",
            "schema": {
                "type": "object",
                "properties": {
                    "output": "Lot: ABC-123",
                },
            },
        },
    }

    with pytest.raises(
        ValueError,
        match=r"parameters\.response_format\.json_schema\.schema\.properties\.output",
    ):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters={"response_format": response_format},
        )


def test_build_conversion_audit_log_rejects_schema_secret_defaults() -> None:
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "veridoc_conversion_plan",
            "schema": {
                "type": "object",
                "properties": {
                    "token": {
                        "type": "string",
                        "default": "operator-runtime-token",
                    },
                },
            },
        },
    }

    with pytest.raises(
        ValueError,
        match=r"parameters\.response_format\.json_schema\.schema\.properties\.token\.default",
    ):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters={"response_format": response_format},
        )


def test_build_conversion_audit_log_allows_tool_function_schema_property_names() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "extract_field",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input": {
                            "type": "string",
                            "contentMediaType": "text/plain",
                        },
                        "content": {"type": "string"},
                        "token": {"type": "string"},
                    },
                },
            },
        }
    ]

    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={"tools": tools},
    )

    assert audit_log["parameters"] == {"tools": tools}


def test_build_conversion_audit_log_rejects_direct_response_format_content_values() -> None:
    with pytest.raises(ValueError, match=r"parameters\.response_format\.output"):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters={"response_format": {"output": '{"lot_number":"ABC-123"}'}},
        )


def test_build_conversion_audit_log_redacts_sequence_valued_secret_parameters() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "token": ["operator-runtime-token", "metadata"],
            "headers": {
                "Cookie": "session=operator-runtime-cookie",
                "Set-Cookie": "session=operator-runtime-set-cookie",
            },
        },
    )

    assert audit_log["parameters"] == {
        "token": "[REDACTED]",
        "headers": {
            "Cookie": "[REDACTED]",
            "Set-Cookie": "[REDACTED]",
        },
    }
    rendered = json.dumps(audit_log, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "operator-runtime-cookie" not in rendered
    assert "operator-runtime-set-cookie" not in rendered
    assert "session=" not in rendered


def test_build_conversion_audit_log_rejects_tuple_content_parameter_entries() -> None:
    with pytest.raises(ValueError, match=r"parameters\.options\[0\]\.prompt"):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters={"options": [("prompt", "Lot: ABC-123")]},
        )


def test_build_conversion_audit_log_preserves_parent_keys_for_nested_secret_paths() -> None:
    audit_log = build_conversion_audit_log(
        source_bytes=b"Lot: ABC-123\n",
        output_bytes=b'{"lot_number":"ABC-123"}\n',
        model="local-json-model",
        prompt_id="veridoc_conversion_plan",
        prompt_version="poc-08",
        ir_version="document-ir-v1",
        parameters={
            "api": {"key": "operator-runtime-api-key"},
            "private": {"key": "operator-runtime-private-key"},
            "safe": {"key": "non-secret-key-name"},
        },
    )

    assert audit_log["parameters"] == {
        "api": {"key": "[REDACTED]"},
        "private": {"key": "[REDACTED]"},
        "safe": {"key": "non-secret-key-name"},
    }
    rendered = json.dumps(audit_log, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "operator-runtime-private-key" not in rendered


def test_build_conversion_audit_log_rejects_openai_request_payload_parameters() -> None:
    request_payload = {
        "model": "local-json-model",
        "temperature": 0,
        "max_tokens": 1024,
        "input": "Lot: ABC-123",
        "messages": [
            {"role": "system", "content": "Return only JSON."},
            {"role": "user", "content": "Lot: ABC-123"},
        ],
        "previous_response": {"choices": [{"message": {"content": "{}"}}]},
    }

    with pytest.raises(ValueError, match=r"parameters\.input"):
        build_conversion_audit_log(
            source_bytes=b"Lot: ABC-123\n",
            output_bytes=b'{"lot_number":"ABC-123"}\n',
            model="local-json-model",
            prompt_id="veridoc_conversion_plan",
            prompt_version="poc-08",
            ir_version="document-ir-v1",
            parameters=request_payload,
        )


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
