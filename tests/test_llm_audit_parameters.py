from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from core.llm.audit_parameters import sanitize_audit_parameters


class _BytesPathLike(os.PathLike[bytes]):
    def __fspath__(self) -> bytes:
        return b"fixtures/source.pdf"


class _StringPathLike(os.PathLike[str]):
    def __init__(self, value: str) -> None:
        self._value = value

    def __fspath__(self) -> str:
        return self._value


def test_sanitize_audit_parameters_returns_json_safe_parameter_values() -> None:
    parameters = sanitize_audit_parameters(
        {
            "metadata": {
                "path": Path("fixtures") / "source.pdf",
                "ratio": Decimal("0.25"),
                "labels": {"safe", "metadata"},
            },
        }
    )

    assert parameters == {
        "metadata": {
            "path": "fixtures/source.pdf",
            "ratio": "0.25",
            "labels": ["metadata", "safe"],
        },
    }
    json.dumps(parameters, sort_keys=True)


def test_sanitize_audit_parameters_decodes_bytes_pathlike_metadata_values() -> None:
    parameters = sanitize_audit_parameters({"metadata": {"path": _BytesPathLike()}})

    assert parameters == {"metadata": {"path": "fixtures/source.pdf"}}
    json.dumps(parameters, sort_keys=True)


def test_sanitize_audit_parameters_redacts_pathlike_url_credentials_after_decoding() -> None:
    parameters = sanitize_audit_parameters(
        {
            "metadata_url": _StringPathLike(
                "https://example.invalid/callback?api_key=operator-runtime-api-key"
            ),
        }
    )

    assert parameters == {"metadata_url": "[REDACTED]"}
    rendered = json.dumps(parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "https:" not in rendered


def test_sanitize_audit_parameters_rejects_pathlike_content_url_after_decoding() -> None:
    with pytest.raises(ValueError, match=r"parameters\.metadata_url"):
        sanitize_audit_parameters(
            {
                "metadata_url": _StringPathLike(
                    "https://example.invalid/callback?prompt=Lot%3A+ABC-123"
                ),
            }
        )


def test_sanitize_audit_parameters_rejects_unsupported_non_json_metadata_values() -> None:
    with pytest.raises(TypeError, match=r"parameters\.metadata\.opaque"):
        sanitize_audit_parameters({"metadata": {"opaque": object()}})


@pytest.mark.parametrize(
    ("container_key", "container_value"),
    [
        ("file", Path("fixtures") / "source.pdf"),
        ("files", [Path("fixtures") / "source.pdf"]),
    ],
)
def test_sanitize_audit_parameters_rejects_pathlike_file_container_values(
    container_key: str,
    container_value: object,
) -> None:
    with pytest.raises(ValueError, match=rf"parameters\.{container_key}"):
        sanitize_audit_parameters({container_key: container_value})


def test_sanitize_audit_parameters_allows_schema_json_string_root_metadata() -> None:
    schema_json = json.dumps(
        {
            "title": "Extraction schema",
            "type": "object",
            "required": ["prompt"],
        }
    )

    assert sanitize_audit_parameters({"schema_json": schema_json}) == {
        "schema_json": schema_json
    }


@pytest.mark.parametrize(
    "schema_json",
    [
        {"type": "string", "enum": ["pending", "complete"]},
        {"const": 1},
    ],
)
def test_sanitize_audit_parameters_allows_safe_schema_json_literal_constraints(
    schema_json: dict[str, object],
) -> None:
    assert sanitize_audit_parameters({"schema_json": schema_json}) == {
        "schema_json": schema_json
    }


@pytest.mark.parametrize(
    "schema_json",
    [
        {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pending", "complete"]},
            },
        },
        {
            "type": "object",
            "properties": {
                "status": {"type": "string", "default": "pending"},
            },
        },
        {
            "type": "object",
            "properties": {
                "status": {"type": "string", "const": "complete"},
            },
        },
    ],
)
def test_sanitize_audit_parameters_allows_safe_schema_json_property_literal_constraints(
    schema_json: dict[str, object],
) -> None:
    assert sanitize_audit_parameters({"schema_json": schema_json}) == {
        "schema_json": schema_json
    }


@pytest.mark.parametrize(
    ("schema_json", "message"),
    [
        ({"default": "operator-runtime-token"}, r"parameters\.schema_json\.default"),
        ({"enum": ["sk-proj-secret"]}, r"parameters\.schema_json\.enum"),
    ],
)
def test_sanitize_audit_parameters_rejects_sensitive_schema_json_literal_constraints(
    schema_json: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        sanitize_audit_parameters({"schema_json": schema_json})


@pytest.mark.parametrize(
    ("schema_json", "message"),
    [
        ({"default": 12345}, r"parameters\.schema_json\.default"),
        ({"enum": [12345]}, r"parameters\.schema_json\.enum"),
    ],
)
def test_sanitize_audit_parameters_rejects_numeric_schema_json_identifier_literals(
    schema_json: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        sanitize_audit_parameters({"schema_json": schema_json})


def test_sanitize_audit_parameters_allows_real_parameters_schema_json() -> None:
    schema_json = {"type": "object"}

    assert sanitize_audit_parameters({"parameters": {"schema_json": schema_json}}) == {
        "parameters": {
            "schema_json": schema_json,
        },
    }


@pytest.mark.parametrize("schema_json", ["true", "false"])
def test_sanitize_audit_parameters_allows_schema_json_string_boolean_roots(
    schema_json: str,
) -> None:
    assert sanitize_audit_parameters({"schema_json": schema_json}) == {
        "schema_json": schema_json
    }


@pytest.mark.parametrize(
    ("schema_json", "message"),
    [
        (
            '{"items":["Lot: ABC-123"]}',
            r"parameters\.schema_json\.items\[0\]",
        ),
        (
            '{"properties":["Lot: ABC-123"]}',
            r"parameters\.schema_json\.properties",
        ),
        (
            '{"additionalProperties":["Lot: ABC-123"]}',
            r"parameters\.schema_json\.additionalProperties",
        ),
        (
            '{"unevaluatedItems":["Lot: ABC-123"]}',
            r"parameters\.schema_json\.unevaluatedItems",
        ),
        (
            '{"not":["Lot: ABC-123"]}',
            r"parameters\.schema_json\.not",
        ),
        (
            '{"anyOf":["Lot: ABC-123"]}',
            r"parameters\.schema_json\.anyOf\[0\]",
        ),
        (
            '{"patternProperties":{".*":["Lot: ABC-123"]}}',
            r"parameters\.schema_json\.patternProperties\.\.\*",
        ),
        (
            '{"dependentSchemas":{"foo":["Lot: ABC-123"]}}',
            r"parameters\.schema_json\.dependentSchemas\.foo",
        ),
    ],
)
def test_sanitize_audit_parameters_rejects_malformed_schema_json_scalars(
    schema_json: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        sanitize_audit_parameters({"schema_json": schema_json})


@pytest.mark.parametrize(
    "schema_json",
    [
        123,
        None,
        [123],
    ],
)
def test_sanitize_audit_parameters_rejects_decoded_non_schema_json_roots(
    schema_json: object,
) -> None:
    with pytest.raises(ValueError, match=r"parameters\.schema_json"):
        sanitize_audit_parameters({"schema_json": schema_json})


def test_sanitize_audit_parameters_sanitizes_extra_header_and_cookie_pair_containers() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "extra_headers": [("Authorization", "Bearer operator-runtime-token")],
            "extra_cookies": [("sessionToken", "operator-runtime-session")],
            "cookies": "theme=light; session=operator-runtime-session",
            "customCookies": "csrftoken=operator-runtime-csrf; theme=light",
            "http_headers": [
                ["Ocp-Apim-Subscription-Key", "operator-runtime-subscription"]
            ],
        }
    )

    assert sanitized_parameters == {
        "extra_headers": [["Authorization", "[REDACTED]"]],
        "extra_cookies": [["sessionToken", "[REDACTED]"]],
        "cookies": "theme=light; session=[REDACTED]",
        "customCookies": "csrftoken=[REDACTED]; theme=light",
        "http_headers": [["Ocp-Apim-Subscription-Key", "[REDACTED]"]],
    }
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "operator-runtime-session" not in rendered
    assert "operator-runtime-csrf" not in rendered
    assert "operator-runtime-subscription" not in rendered
    assert "Bearer" not in rendered


def test_sanitize_audit_parameters_redacts_raw_header_line_parameter_values() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "headers": ["Authorization: Bearer operator-runtime-token"],
            "extra_headers": [
                "Ocp-Apim-Subscription-Key: operator-runtime-subscription"
            ],
            "request_headers": ["X-Api-Key=operator-runtime-api-key"],
        }
    )

    assert sanitized_parameters == {
        "headers": ["Authorization: [REDACTED]"],
        "extra_headers": ["Ocp-Apim-Subscription-Key: [REDACTED]"],
        "request_headers": ["X-Api-Key=[REDACTED]"],
    }
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "operator-runtime-subscription" not in rendered
    assert "operator-runtime-api-key" not in rendered
    assert "Bearer" not in rendered


def test_sanitize_audit_parameters_redacts_multi_entry_raw_parameter_strings() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "query_params": "version=1&api%5Fkey=operator-runtime-api-key",
            "default_parameters": "version=1&code=operator-runtime-code&key=operator-runtime-key",
            "requestParameters": "version=1&api_key=operator-runtime-request-api-key",
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
        }
    )

    assert sanitized_parameters == {
        "query_params": "version=1&api%5Fkey=[REDACTED]",
        "default_parameters": "version=1&code=[REDACTED]&key=[REDACTED]",
        "requestParameters": "version=1&api_key=[REDACTED]",
        "params_semicolon": "version=1;token=not-a-container",
        "params": [
            "callback=[REDACTED]",
            "?key=[REDACTED]",
            "code=[REDACTED]",
            "version=1;api_key=[REDACTED]",
        ],
        "headers": "X-Test: ok\nAuthorization: [REDACTED]",
        "extra_headers": ["Authorization=[REDACTED]"],
        "cookies": "theme=light; session=[REDACTED]",
    }
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "operator-runtime-api-key-2" not in rendered
    assert "operator-runtime-request-api-key" not in rendered
    assert "operator-runtime-code" not in rendered
    assert "operator-runtime-key" not in rendered
    assert "operator-runtime-query-key" not in rendered
    assert "operator-runtime-function-code" not in rendered
    assert "operator-runtime-signature" not in rendered
    assert "operator-runtime-token-with" not in rendered
    assert "operator-runtime-session" not in rendered
    assert "operator-runtime-token" not in rendered
    assert "Bearer" not in rendered


def test_sanitize_audit_parameters_redacts_raw_provider_parameter_strings() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "provider_parameters": "version=1&api_key=operator-runtime-api-key",
            "providerParams": "version=1&token=operator-runtime-token",
        }
    )

    assert sanitized_parameters == {
        "provider_parameters": "version=1&api_key=[REDACTED]",
        "providerParams": "version=1&token=[REDACTED]",
    }
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "operator-runtime-token" not in rendered


def test_sanitize_audit_parameters_preserves_recursive_raw_parameter_redaction() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": "version=1&params=api_key%3Doperator-runtime-api-key",
        }
    )

    assert sanitized_parameters == {
        "queryParameters": "version=1&params=api_key=[REDACTED]",
    }
    assert "operator-runtime-api-key" not in json.dumps(
        sanitized_parameters, sort_keys=True
    )


def test_sanitize_audit_parameters_redacts_nested_raw_query_entry_credentials() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": "next=api_key=operator-runtime-key",
            "provider_parameters": "next=api_key%3Doperator-runtime-provider-key",
        }
    )

    assert sanitized_parameters == {
        "queryParameters": "next=[REDACTED]",
        "provider_parameters": "next=[REDACTED]",
    }
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-key" not in rendered
    assert "operator-runtime-provider-key" not in rendered


def test_sanitize_audit_parameters_redacts_structured_nested_raw_query_entry_credentials() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": [
                ["next", "api_key=operator-runtime-key"],
                {"key": "next", "value": "api_key=operator-runtime-map-key"},
            ],
        }
    )

    assert sanitized_parameters == {
        "queryParameters": [
            ["next", "[REDACTED]"],
            {"key": "next", "value": "[REDACTED]"},
        ],
    }
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-key" not in rendered
    assert "operator-runtime-map-key" not in rendered


def test_sanitize_audit_parameters_redacts_structured_encoded_entry_names() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "provider_parameters": [["api%255Fkey", "operator-runtime-key"]],
        }
    )

    assert sanitized_parameters == {
        "provider_parameters": [["api_key", "[REDACTED]"]],
    }
    assert "operator-runtime-key" not in json.dumps(
        sanitized_parameters, sort_keys=True
    )


def test_sanitize_audit_parameters_redacts_structured_provider_encoded_raw_payloads() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "provider_parameters": [
                ["params", "api_key%3Doperator-runtime-api-key"],
                {"params": "api_key%3Doperator-runtime-map-key"},
            ],
        }
    )

    assert sanitized_parameters == {
        "provider_parameters": [
            ["params", "api_key=[REDACTED]"],
            {"params": "api_key=[REDACTED]"},
        ],
    }
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "operator-runtime-map-key" not in rendered


def test_sanitize_audit_parameters_rejects_nested_raw_query_entry_content() -> None:
    with pytest.raises(ValueError, match=r"parameters\.queryParameters\.next"):
        sanitize_audit_parameters(
            {
                "queryParameters": "next=prompt=Lot: ABC-123",
            }
        )


def test_sanitize_audit_parameters_rejects_structured_nested_raw_query_entry_content() -> (
    None
):
    with pytest.raises(ValueError, match=r"parameters\.queryParameters\[0\]\.next"):
        sanitize_audit_parameters(
            {
                "queryParameters": [["next", "prompt=Lot: ABC-123"]],
            }
        )


def test_sanitize_audit_parameters_rejects_structured_encoded_content_entry_names() -> (
    None
):
    with pytest.raises(
        ValueError, match=r"parameters\.queryParameters\[0\]\.raw_source"
    ):
        sanitize_audit_parameters(
            {
                "queryParameters": [["raw%255Fsource", "Lot: ABC-123"]],
            }
        )


def test_sanitize_audit_parameters_redacts_encoded_raw_query_container_text() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": "api_key%3Doperator-runtime-api-key",
            "provider_parameters": "token%3Doperator-runtime-token",
        }
    )

    assert sanitized_parameters == {
        "queryParameters": "api_key=[REDACTED]",
        "provider_parameters": "token=[REDACTED]",
    }
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "operator-runtime-token" not in rendered


def test_sanitize_audit_parameters_redacts_real_parameters_containers() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "parameters": {
                "key": "operator-runtime-key",
            },
        }
    )

    assert sanitized_parameters == {
        "parameters": {
            "key": "[REDACTED]",
        },
    }
    assert "operator-runtime-key" not in json.dumps(
        sanitized_parameters, sort_keys=True
    )


def test_sanitize_audit_parameters_redacts_real_parameters_raw_container() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "parameters": "key=operator-runtime-key",
        }
    )

    assert sanitized_parameters == {"parameters": "key=[REDACTED]"}
    assert "operator-runtime-key" not in json.dumps(
        sanitized_parameters, sort_keys=True
    )


def test_sanitize_audit_parameters_redacts_encoded_real_parameters_raw_container() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "parameters": "api_key%3Doperator-runtime-key",
        }
    )

    assert sanitized_parameters == {"parameters": "api_key=[REDACTED]"}
    assert "operator-runtime-key" not in json.dumps(
        sanitized_parameters, sort_keys=True
    )


def test_sanitize_audit_parameters_allows_encoded_real_parameters_benign_raw_tokens() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "parameters": "message%3Dcomplete",
        }
    )

    assert sanitized_parameters == {"parameters": "message=complete"}


@pytest.mark.parametrize(
    "raw_parameters",
    [
        "message=complete",
        "output=summary",
        "message=complete&output=summary",
        "message=complete;output=summary",
    ],
)
def test_sanitize_audit_parameters_allows_real_parameters_benign_raw_tokens(
    raw_parameters: str,
) -> None:
    sanitized_parameters = sanitize_audit_parameters({"parameters": raw_parameters})

    assert sanitized_parameters == {"parameters": raw_parameters}


@pytest.mark.parametrize(
    "raw_parameters",
    [
        "next=message=complete",
        "next=output=summary",
    ],
)
def test_sanitize_audit_parameters_allows_real_parameters_nested_benign_raw_tokens(
    raw_parameters: str,
) -> None:
    sanitized_parameters = sanitize_audit_parameters({"parameters": raw_parameters})

    assert sanitized_parameters == {"parameters": raw_parameters}


@pytest.mark.parametrize(
    "nested_parameters",
    [
        {"message": "complete"},
        {"output": "summary"},
        {"status": "message=complete"},
        {"mode": "output=summary"},
    ],
)
def test_sanitize_audit_parameters_allows_real_parameters_benign_key_value_tokens(
    nested_parameters: dict[str, str],
) -> None:
    sanitized_parameters = sanitize_audit_parameters({"parameters": nested_parameters})

    assert sanitized_parameters == {"parameters": nested_parameters}


def test_sanitize_audit_parameters_redacts_real_parameters_nested_raw_credentials() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "parameters": {
                "next": "api_key=operator-runtime-key",
            },
        }
    )

    assert sanitized_parameters == {
        "parameters": {
            "next": "[REDACTED]",
        },
    }
    assert "operator-runtime-key" not in json.dumps(
        sanitized_parameters, sort_keys=True
    )


def test_sanitize_audit_parameters_redacts_real_parameters_multi_raw_credentials() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {"parameters": "message=complete&api_key=operator-runtime-key"}
    )

    assert sanitized_parameters == {
        "parameters": "message=complete&api_key=[REDACTED]",
    }
    assert "operator-runtime-key" not in json.dumps(
        sanitized_parameters, sort_keys=True
    )


def test_sanitize_audit_parameters_rejects_real_parameters_nested_raw_content() -> None:
    with pytest.raises(ValueError, match=r"parameters\.parameters\.next"):
        sanitize_audit_parameters({"parameters": {"next": "prompt=Lot: ABC-123"}})


def test_sanitize_audit_parameters_rejects_real_parameters_raw_content() -> None:
    with pytest.raises(ValueError, match=r"parameters\.parameters\.prompt"):
        sanitize_audit_parameters({"parameters": "prompt=Lot: ABC-123"})


def test_sanitize_audit_parameters_rejects_encoded_real_parameters_raw_content() -> (
    None
):
    with pytest.raises(ValueError, match=r"parameters\.parameters\.prompt"):
        sanitize_audit_parameters({"parameters": "prompt%3DLot%3A+ABC-123"})


def test_sanitize_audit_parameters_rejects_real_parameters_nested_raw_string_content() -> (
    None
):
    with pytest.raises(ValueError, match=r"parameters\.parameters\.next"):
        sanitize_audit_parameters({"parameters": "next=prompt=Lot: ABC-123"})


def test_sanitize_audit_parameters_redacts_raw_mapping_parameter_values() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": {
                "next": "api_key=operator-runtime-key",
            },
        }
    )

    assert sanitized_parameters == {
        "queryParameters": {
            "next": "[REDACTED]",
        },
    }
    assert "operator-runtime-key" not in json.dumps(
        sanitized_parameters, sort_keys=True
    )


def test_sanitize_audit_parameters_rejects_raw_mapping_parameter_content_values() -> (
    None
):
    with pytest.raises(ValueError, match=r"parameters\.provider_parameters\.next"):
        sanitize_audit_parameters(
            {
                "provider_parameters": {
                    "next": "prompt=Lot: ABC-123",
                },
            }
        )


def test_sanitize_audit_parameters_preserves_encoded_plus_in_raw_query_values() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": "language=C%2B%2B&expr=a%2Bb&api_key=operator-runtime-key",
        }
    )

    assert sanitized_parameters == {
        "queryParameters": "language=C%2B%2B&expr=a%2Bb&api_key=[REDACTED]",
    }
    assert "operator-runtime-key" not in json.dumps(
        sanitized_parameters, sort_keys=True
    )


def test_sanitize_audit_parameters_preserves_encoded_raw_value_separators() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": "expr=a%26b&api_key=operator-runtime-key",
        }
    )

    assert sanitized_parameters == {
        "queryParameters": "expr=a%26b&api_key=[REDACTED]",
    }
    assert "operator-runtime-key" not in json.dumps(
        sanitized_parameters, sort_keys=True
    )


def test_sanitize_audit_parameters_rejects_encoded_raw_query_container_content() -> (
    None
):
    with pytest.raises(ValueError, match=r"parameters\.queryParameters\.prompt"):
        sanitize_audit_parameters(
            {
                "queryParameters": "prompt%3DLot%3A+ABC-123",
            }
        )


def test_sanitize_audit_parameters_redacts_colon_nested_raw_query_credentials() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": "next=api_key:operator-runtime-key",
        }
    )

    assert sanitized_parameters == {"queryParameters": "next=[REDACTED]"}
    assert "operator-runtime-key" not in json.dumps(
        sanitized_parameters, sort_keys=True
    )


def test_sanitize_audit_parameters_rejects_colon_nested_raw_query_content() -> None:
    with pytest.raises(ValueError, match=r"parameters\.callback_url"):
        sanitize_audit_parameters(
            {
                "callback_url": (
                    "https://example.invalid/cb?params=prompt:Lot%3A+ABC-123"
                ),
            }
        )


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        (
            {
                "query_params": "callback=https://example.invalid/cb?prompt=Lot%3A+ABC-123"
            },
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
def test_sanitize_audit_parameters_rejects_raw_content_url_parameter_values(
    parameters: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        sanitize_audit_parameters(parameters)


@pytest.mark.parametrize(
    "callback_url",
    [
        "https://example.invalid/cb?jsonOutputStatusCode=200",
        "https://example.invalid/cb?messageName=assistant-1",
        "https://example.invalid/cb?messageIndex=0",
    ],
)
def test_sanitize_audit_parameters_allows_url_descriptor_query_values(
    callback_url: str,
) -> None:
    sanitized_parameters = sanitize_audit_parameters({"callback_url": callback_url})

    assert sanitized_parameters == {"callback_url": callback_url}


@pytest.mark.parametrize(
    "callback_url",
    [
        "https://example.invalid/cb?jsonOutputStatusCode=Lot%3A%20ABC-123",
        "https://example.invalid/cb?messageName=Lot%3A%20ABC-123",
        "https://example.invalid/cb?messageIndex=Lot%3A%20ABC-123",
    ],
)
def test_sanitize_audit_parameters_rejects_url_descriptor_query_content(
    callback_url: str,
) -> None:
    with pytest.raises(ValueError, match=r"parameters\.callback_url"):
        sanitize_audit_parameters({"callback_url": callback_url})


def test_sanitize_audit_parameters_rejects_multi_entry_raw_content_parameters() -> None:
    with pytest.raises(ValueError, match=r"parameters\.query_params\.prompt"):
        sanitize_audit_parameters({"query_params": "version=1&prompt=Lot: ABC-123"})


@pytest.mark.parametrize(
    "query_parameters",
    [
        "next=messageName=assistant-1",
        "next=jsonOutputStatusCode=200",
        "next=messageIndex=0",
    ],
)
def test_sanitize_audit_parameters_allows_nested_raw_descriptor_query_values(
    query_parameters: str,
) -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {"queryParameters": query_parameters}
    )

    assert sanitized_parameters == {"queryParameters": query_parameters}


@pytest.mark.parametrize(
    "query_parameters",
    [
        "next=messageName=Lot%3AABC",
        "next=jsonOutputStatusCode=Lot%3AABC",
        "next=messageIndex=Lot%3AABC",
    ],
)
def test_sanitize_audit_parameters_rejects_nested_raw_descriptor_query_content(
    query_parameters: str,
) -> None:
    with pytest.raises(ValueError, match=r"parameters\.queryParameters\.next"):
        sanitize_audit_parameters({"queryParameters": query_parameters})


def test_sanitize_audit_parameters_rejects_semicolon_raw_query_content_parameters() -> (
    None
):
    with pytest.raises(ValueError, match=r"parameters\.query_params\.prompt"):
        sanitize_audit_parameters({"query_params": "version=1;prompt=Lot: ABC-123"})


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        (
            {"default_params": "version=1&prompt=Lot: ABC-123"},
            r"parameters\.default_params\.prompt",
        ),
        (
            {"queryParameters": "version=1&api_key=operator-runtime-api-key"},
            r"parameters\.queryParameters\.api_key",
        ),
        (
            {
                "queryParameters": "redirect=https://example.invalid/cb?prompt=Lot%3A+ABC-123"
            },
            r"parameters\.queryParameters\.redirect",
        ),
        (
            {
                "default_params": (
                    "redirect=https%3A%2F%2Fexample.invalid%2Fcb"
                    "%3Fprompt%3DLot%253A%2BABC-123"
                )
            },
            r"parameters\.default_params\.redirect",
        ),
    ],
)
def test_sanitize_audit_parameters_splits_raw_query_container_aliases(
    parameters: dict[str, object],
    message: str,
) -> None:
    if "api_key" in message:
        sanitized_parameters = sanitize_audit_parameters(parameters)

        rendered = json.dumps(sanitized_parameters, sort_keys=True)
        assert "operator-runtime-api-key" not in rendered
        assert sanitized_parameters == {
            "queryParameters": "version=1&api_key=[REDACTED]"
        }
        return

    with pytest.raises(ValueError, match=message):
        sanitize_audit_parameters(parameters)


def test_sanitize_audit_parameters_redacts_secret_suffixed_params_alias_values() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "client_secret_params": "operator-runtime-client-secret",
            "auth_params": "Bearer operator-runtime-token",
        }
    )

    assert sanitized_parameters == {
        "client_secret_params": "[REDACTED]",
        "auth_params": "[REDACTED]",
    }
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-client-secret" not in rendered
    assert "operator-runtime-token" not in rendered


def test_sanitize_audit_parameters_redacts_secret_parameters_alias_before_raw_parsing() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "auth_parameters": "prompt=consent&token=operator-runtime-token",
        }
    )

    assert sanitized_parameters == {"auth_parameters": "[REDACTED]"}
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "prompt=consent" not in rendered


def test_sanitize_audit_parameters_redacts_url_values_before_query_alias_parsing() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "default_params": (
                "https://example.invalid/callback?api_key=operator-runtime-api-key"
            ),
        }
    )

    assert sanitized_parameters == {"default_params": "[REDACTED]"}
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "https:" not in rendered


def test_sanitize_audit_parameters_redacts_url_parameters_with_decoded_raw_query_credentials() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "callback_url": (
                "https://example.invalid/cb?"
                "redirect=version%3D1%26api_key%3Doperator-runtime-api-key"
            ),
        }
    )

    assert sanitized_parameters == {"callback_url": "[REDACTED]"}
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "redirect=version" not in rendered


def test_sanitize_audit_parameters_redacts_url_descriptor_secret_pairs() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "callback_url": (
                "https://example.invalid/cb?"
                "jsonApiKeyStatusCode=operator-runtime-token"
            ),
        }
    )

    assert sanitized_parameters == {"callback_url": "[REDACTED]"}
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "jsonApiKeyStatusCode" not in rendered


def test_sanitize_audit_parameters_redacts_nested_raw_descriptor_secret_pairs() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": "next=jsonApiKeyStatusCode%3Doperator-runtime-token",
        }
    )

    assert sanitized_parameters == {"queryParameters": "next=[REDACTED]"}
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "jsonApiKeyStatusCode" not in rendered


def test_sanitize_audit_parameters_redacts_encoded_url_parameter_credentials() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "callback_url": (
                "https%3A%2F%2Fexample.invalid%2Fcb"
                "%3Fapi_key%3Doperator-runtime-api-key"
            ),
        }
    )

    assert sanitized_parameters == {"callback_url": "[REDACTED]"}
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "https%3A" not in rendered


@pytest.mark.parametrize("parameter_key", ["callback_url", "image_url"])
def test_sanitize_audit_parameters_rejects_encoded_data_url_parameters(
    parameter_key: str,
) -> None:
    with pytest.raises(ValueError, match=rf"parameters\.{parameter_key}"):
        sanitize_audit_parameters(
            {
                parameter_key: ("data%3Aapplication%2Fpdf%3Bbase64%2CTG90OiBBQkMtMTIz"),
            }
        )


def test_sanitize_audit_parameters_redacts_url_credentials_with_encoded_keys() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "callback_url": (
                "https://example.invalid/cb?" "api%25255Fkey=operator-runtime-api-key"
            ),
        }
    )

    assert sanitized_parameters == {"callback_url": "[REDACTED]"}
    assert "operator-runtime-api-key" not in json.dumps(
        sanitized_parameters, sort_keys=True
    )


def test_sanitize_audit_parameters_redacts_encoded_provider_parameter_url_credentials() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "provider_parameters": {
                "redirect": (
                    "https%3A%2F%2Fexample.invalid%2Fcb"
                    "%3Fapi_key%3Doperator-runtime-provider-key"
                ),
            },
        }
    )

    assert sanitized_parameters == {
        "provider_parameters": {"redirect": "[REDACTED]"},
    }
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-provider-key" not in rendered
    assert "https%3A" not in rendered


def test_sanitize_audit_parameters_redacts_encoded_query_nested_url_credentials() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": (
                "redirect=https%3A%2F%2Fexample.invalid%2Fcb"
                "%3Fapi_key%3Doperator-runtime-api-key"
            ),
        }
    )

    assert sanitized_parameters == {"queryParameters": "redirect=[REDACTED]"}
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "https%3A" not in rendered


def test_sanitize_audit_parameters_redacts_structured_query_encoded_url_credentials() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": [
                [
                    "redirect",
                    (
                        "https%3A%2F%2Fexample.invalid%2Fcb"
                        "%3Fapi_key%3Doperator-runtime-api-key"
                    ),
                ]
            ],
        }
    )

    assert sanitized_parameters == {"queryParameters": [["redirect", "[REDACTED]"]]}
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "https%3A" not in rendered


def test_sanitize_audit_parameters_redacts_encoded_query_nested_url_credentials_with_sibling_params() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": (
                "redirect=https%3A%2F%2Fexample.invalid%2Fcb"
                "%3Fapi_key%3Doperator-runtime-api-key&version=1"
            ),
        }
    )

    assert sanitized_parameters == {
        "queryParameters": "redirect=[REDACTED]&version=1",
    }
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "https%3A" not in rendered


def test_sanitize_audit_parameters_redacts_double_encoded_query_nested_url_credentials() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": (
                "redirect=https%253A%252F%252Fexample.invalid%252Fcb"
                "%253Fapi_key%253Doperator-runtime-api-key"
            ),
        }
    )

    assert sanitized_parameters == {"queryParameters": "redirect=[REDACTED]"}
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "https%253A" not in rendered


def test_sanitize_audit_parameters_redacts_url_embedded_raw_query_credentials() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "callback_url": (
                "https://example.invalid/cb?"
                "next=params%3Dapi_key%253Doperator-runtime-api-key"
            ),
        }
    )

    assert sanitized_parameters == {"callback_url": "[REDACTED]"}
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "params%3D" not in rendered


def test_sanitize_audit_parameters_rejects_url_embedded_raw_query_content() -> None:
    with pytest.raises(ValueError, match=r"parameters\.callback_url"):
        sanitize_audit_parameters(
            {
                "callback_url": (
                    "https://example.invalid/cb?"
                    "next=params%3Dprompt%253DLot%25253A%252BABC-123"
                ),
            }
        )


def test_sanitize_audit_parameters_redacts_deep_url_embedded_raw_query() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "callback_url": "https://example.invalid/cb?next=" + "a=" * 500 + "z",
        }
    )

    assert sanitized_parameters == {"callback_url": "[REDACTED]"}


@pytest.mark.parametrize(
    "callback_url",
    [
        "https://example.invalid/cb?view=message",
        "https://example.invalid/cb?state=token",
        "https://example.invalid/cb?next=message",
        "https://example.invalid/cb?next=output",
    ],
)
def test_sanitize_audit_parameters_allows_nested_url_bare_word_query_values(
    callback_url: str,
) -> None:
    sanitized_parameters = sanitize_audit_parameters({"callback_url": callback_url})

    assert sanitized_parameters == {"callback_url": callback_url}


def test_sanitize_audit_parameters_preserves_recursive_raw_query_redactions() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
            "queryParameters": "params=api_key%3Doperator-runtime-api-key",
        }
    )

    assert sanitized_parameters == {"queryParameters": "params=api_key=[REDACTED]"}
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-api-key" not in rendered
    assert "api_key%3D" not in rendered


def test_sanitize_audit_parameters_rejects_recursive_raw_query_content_entries() -> (
    None
):
    with pytest.raises(
        ValueError, match=r"parameters\.queryParameters\.params\.prompt"
    ):
        sanitize_audit_parameters(
            {
                "queryParameters": "params=prompt%3DLot%253A%2BABC-123",
            }
        )


def test_sanitize_audit_parameters_allows_content_type_header_metadata() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
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
        }
    )

    assert sanitized_parameters == {
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


def test_sanitize_audit_parameters_redacts_credential_bearing_url_values() -> None:
    sanitized_parameters = sanitize_audit_parameters(
        {
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
        }
    )

    assert sanitized_parameters == {
        "base_url": "[REDACTED]",
        "callback_url": "[REDACTED]",
        "redirect_url": "[REDACTED]",
        "function_url": "[REDACTED]",
        "webhook_url": "[REDACTED]",
        "metadata_url": "https://example.invalid/metadata",
    }
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-password" not in rendered
    assert "operator-runtime-api-key" not in rendered
    assert "operator-runtime-api-key-2" not in rendered
    assert "operator-runtime-query-key" not in rendered
    assert "operator-runtime-function-code" not in rendered


def test_sanitize_audit_parameters_redacts_header_suffixed_parameter_containers() -> (
    None
):
    sanitized_parameters = sanitize_audit_parameters(
        {
            "default_headers": [("Authorization", "Bearer operator-runtime-token")],
            "customHeaders": ["X-Api-Key=operator-runtime-api-key"],
        }
    )

    assert sanitized_parameters == {
        "default_headers": [["Authorization", "[REDACTED]"]],
        "customHeaders": ["X-Api-Key=[REDACTED]"],
    }
    rendered = json.dumps(sanitized_parameters, sort_keys=True)
    assert "operator-runtime-token" not in rendered
    assert "operator-runtime-api-key" not in rendered
    assert "Bearer" not in rendered


def test_sanitize_audit_parameters_rejects_raw_key_value_content_parameter_lines() -> (
    None
):
    with pytest.raises(ValueError, match=r"parameters\.headers\[0\]\.Prompt"):
        sanitize_audit_parameters({"headers": ["Prompt: Lot: ABC-123"]})


def test_sanitize_audit_parameters_rejects_root_metadata_key_content_url() -> None:
    with pytest.raises(ValueError, match=r"parameters\.metadata\.key"):
        sanitize_audit_parameters(
            {
                "metadata": {
                    "key": "https://example.invalid/cb?prompt=Lot%3A+ABC-123",
                },
            }
        )
