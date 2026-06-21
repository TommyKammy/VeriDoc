from __future__ import annotations

import pytest

from core.llm.conversion_plan import (
    CONVERSION_PLAN_SCHEMA,
    ConversionPlanValidationError,
    LocalLLMConfigurationError,
    LocalLLMConversionPlanAdapter,
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


def test_adapter_rejects_placeholder_api_key() -> None:
    with pytest.raises(LocalLLMConfigurationError, match="placeholder"):
        LocalLLMConversionPlanAdapter(
            base_url="http://localhost:8000/v1",
            model="local-json-model",
            api_key="TODO",
        )
