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
