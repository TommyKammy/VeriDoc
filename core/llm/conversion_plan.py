from __future__ import annotations

import hashlib
import ipaddress
import json
import http.client
import os
import re
import socket
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlparse


CONVERSION_PLAN_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "source_kind", "operations", "constraints"],
    "properties": {
        "schema_version": {"const": 1},
        "source_kind": {"type": "string", "minLength": 1},
        "operations": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "action", "inputs", "output", "rationale"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "action": {
                        "type": "string",
                        "enum": ["extract_field", "extract_table", "normalize_value", "flag_review"],
                    },
                    "inputs": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "output": {"type": "string", "minLength": 1},
                    "rationale": {"type": "string", "minLength": 1},
                },
            },
        },
        "constraints": {
            "type": "object",
            "additionalProperties": False,
            "required": ["external_transmission"],
            "properties": {
                "external_transmission": {"const": False},
            },
        },
    },
}

JsonObject = dict[str, Any]
Transport = Callable[[str, JsonObject, dict[str, str], float], JsonObject]

_PLACEHOLDER_API_KEYS = {
    "placeholder",
    "todo",
    "changeme",
    "change-me",
    "dummy",
    "fake",
    "sample",
    "example",
    "local-placeholder-only",
}
_PLACEHOLDER_API_KEY_MARKERS = tuple(_PLACEHOLDER_API_KEYS)
_LOCAL_RUNTIME_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    )
)
_BLOCKED_LOCAL_RUNTIME_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("fd00:ec2::254"),
    }
)
_SUPPORTED_ACTIONS = {"extract_field", "extract_table", "normalize_value", "flag_review"}
_AUDIT_LOG_SCHEMA_VERSION = "veridoc-conversion-audit-log/v0"
_REDACTED_VALUE = "[REDACTED]"
_SECRET_PARAMETER_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "auth",
        "authentication",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "csrftoken",
        "connection_string",
        "jwt",
        "password",
        "private_key",
        "secret",
        "session",
        "sessionid",
        "sig",
        "signature",
        "set_cookie",
        "token",
        "xsrf",
        "xsrf_token",
    }
)
_SECRET_PARAMETER_KEY_SUFFIXES = (
    "_api_key",
    "_apikey",
    "_authorization",
    "_credential",
    "_credentials",
    "_password",
    "_private_key",
    "_secret",
    "_sig",
    "_signature",
    "_token",
)
_SECRET_PARAMETER_KEY_PREFIXES = (
    "api_key_",
    "apikey_",
    "auth_",
    "authorization_",
    "credential_",
    "credentials_",
    "password_",
    "private_key_",
    "secret_",
    "sig_",
    "signature_",
    "token_",
)
_SECRET_PARAMETER_KEY_PHRASES = (
    "api_key",
    "apikey",
    "private_key",
    "secret",
    "signature",
    "connection_string",
)
_SECRET_PARAMETER_KEY_COMPONENTS = frozenset(
    {
        "authorization",
        "auth",
        "authentication",
        "cookie",
        "credential",
        "credentials",
        "jwt",
        "password",
        "secret",
        "session",
        "signature",
        "sig",
        "token",
    }
)
_SECRET_PARAMETER_KEY_COMPONENT_SEQUENCES = (
    ("account", "key"),
    ("api", "key"),
    ("access", "key"),
    ("functions", "key"),
    ("private", "key"),
    ("subscription", "key"),
)
_CAMEL_ACRONYM_BOUNDARY_RE = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_CASE_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_PARAMETER_KEY_SEPARATOR_RE = re.compile(r"[^A-Za-z0-9]+")
_PARAMETER_INDEX_SUFFIX_RE = re.compile(r"\[\d+\]$")
_CONTENT_BEARING_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "content",
        "attachment",
        "attachments",
        "body",
        "data",
        "document",
        "form_data",
        "input",
        "instructions",
        "json",
        "message",
        "messages",
        "output_bytes",
        "previous_response",
        "prompt",
        "output",
        "payload",
        "request_body",
        "source",
        "source_bytes",
        "synthetic_text",
        "text",
        "upload",
        "uploads",
    }
)
_SAFE_CONTENT_WORD_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "content_type",
        "content_encoding",
        "content_length",
        "content_md5",
        "max_tokens",
        "max_prompt_tokens",
        "x_amz_content_sha256",
    }
)
_SAFE_MESSAGE_METADATA_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "message_count",
        "message_id",
        "user_message_id",
    }
)
_SAFE_DATA_METADATA_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "meta_data",
        "model_data",
    }
)
_SAFE_AUDIT_PARAMETER_SEQUENCE_KEYS = frozenset(
    {
        "stop",
    }
)
_CONTENT_BEARING_AUDIT_PARAMETER_KEY_COMPONENTS = frozenset(
    {
        "content",
        "attachment",
        "attachments",
        "body",
        "data",
        "document",
        "form_data",
        "input",
        "instructions",
        "message",
        "messages",
        "prompt",
        "payload",
        "text",
        "upload",
        "uploads",
    }
)
_CONTENT_BEARING_AUDIT_PARAMETER_KEY_COMPONENT_SEQUENCES = (
    ("raw", "source"),
    ("raw", "output"),
)
_JSON_SCHEMA_VALUE_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "const",
        "default",
        "enum",
        "examples",
    }
)
_SAFE_JSON_SCHEMA_AUDIT_PARAMETER_METADATA_KEYS = frozenset(
    {
        "content_encoding",
        "content_media_type",
        "data_type",
        "description",
        "format",
        "title",
        "type",
    }
)
_KEY_VALUE_AUDIT_PARAMETER_SEQUENCE_CONTAINER_KEYS = frozenset(
    {
        "cookies",
        "extra_cookies",
        "extra_headers",
        "files",
        "headers",
        "http_headers",
        "options",
        "params",
        "parameters",
        "query_params",
        "request_headers",
    }
)
_FILE_AUDIT_PARAMETER_CONTAINER_KEYS = frozenset({"file", "files"})
_CONTENT_BYTE_AUDIT_PARAMETER_ANCESTOR_COMPONENTS = frozenset(
    {
        "output",
        "source",
    }
)
CONVERSION_TASK_PROMPTS = {
    "text_pdf": (
        "For text PDF conversion, use embedded text and page/table cues from the synthetic input; "
        "flag uncertain layout reconstruction for review."
    ),
    "scanned_pdf_ocr": (
        "For scanned PDF OCR conversion, treat OCR text, confidence, and bounding boxes as "
        "provisional signals; flag low-confidence or missing regions for review."
    ),
    "word_document": (
        "For Word conversion, preserve paragraph, heading, table, and list intent from the "
        "synthetic structure without inferring hidden styles."
    ),
    "excel_workbook": (
        "For Excel conversion, preserve sheet, cell, merged-range, and table cues; never infer "
        "formulas or values that are not present in the synthetic input."
    ),
}


class ConversionPlanValidationError(ValueError):
    """Raised when local LLM output is not an acceptable conversion plan."""


class LocalLLMConfigurationError(ValueError):
    """Raised when the local LLM adapter would violate the local-only boundary."""


@dataclass(frozen=True)
class _LocalBaseUrl:
    request_base_urls: tuple[str, ...]
    host_header: str | None = None
    tls_server_name: str | None = None


@dataclass(frozen=True)
class LocalLLMConversionPlanAdapter:
    """Minimal OpenAI-compatible local LLM adapter for JSON Schema plans."""

    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 30
    max_tokens: int = 1024
    transport: Transport | None = None

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise LocalLLMConfigurationError("model is required")
        if self.timeout_seconds <= 0:
            raise LocalLLMConfigurationError("timeout_seconds must be positive")
        if self.max_tokens <= 0:
            raise LocalLLMConfigurationError("max_tokens must be positive")
        if _local_base_url(self.base_url) is None:
            raise LocalLLMConfigurationError("base_url must target a local-only OpenAI-compatible endpoint")
        if self.api_key is not None and _is_placeholder_secret(self.api_key):
            raise LocalLLMConfigurationError("placeholder API keys are not valid local LLM credentials")

    def create_conversion_plan(self, synthetic_text: str) -> JsonObject:
        if not synthetic_text.strip():
            raise ValueError("synthetic_text is required")
        local_base_url = _local_base_url(self.base_url)
        if local_base_url is None:
            raise LocalLLMConfigurationError("base_url must target a local-only OpenAI-compatible endpoint")

        payload = _build_conversion_plan_payload(
            model=self.model,
            max_tokens=self.max_tokens,
            synthetic_text=synthetic_text,
        )
        headers = {"Content-Type": "application/json"}
        if local_base_url.host_header:
            headers["Host"] = local_base_url.host_header
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        first_response = self._request(local_base_url, payload, headers)
        first_plan = _extract_json_content(first_response)
        try:
            validate_conversion_plan(first_plan)
        except ConversionPlanValidationError as exc:
            repair_payload = _build_conversion_plan_payload(
                model=self.model,
                max_tokens=self.max_tokens,
                synthetic_text=synthetic_text,
                repair_error=str(exc),
                previous_response=first_response,
            )
            repaired_response = self._request(local_base_url, repair_payload, headers)
            repaired_plan = _extract_json_content(repaired_response)
            validate_conversion_plan(repaired_plan)
            return repaired_plan
        return first_plan

    def _request(
        self,
        local_base_url: _LocalBaseUrl,
        payload: JsonObject,
        headers: dict[str, str],
    ) -> JsonObject:
        if self.transport is None:
            return _send_local_llm_request(local_base_url, payload, headers, self.timeout_seconds)
        request_url = _chat_completions_url(local_base_url.request_base_urls[0])
        return self.transport(request_url, payload, headers, self.timeout_seconds)


def build_conversion_audit_log(
    *,
    source_bytes: bytes,
    output_bytes: bytes,
    model: str,
    prompt_id: str,
    prompt_version: str,
    ir_version: str,
    parameters: Mapping[str, object],
) -> JsonObject:
    """Build a minimal conversion audit record without retaining document content."""

    if not isinstance(source_bytes, bytes) or not isinstance(output_bytes, bytes):
        raise TypeError("source_bytes and output_bytes must be bytes")
    if not _non_empty_string(model):
        raise ValueError("model is required")
    if not _non_empty_string(prompt_id):
        raise ValueError("prompt_id is required")
    if not _non_empty_string(prompt_version):
        raise ValueError("prompt_version is required")
    if not _non_empty_string(ir_version):
        raise ValueError("ir_version is required")
    if not isinstance(parameters, Mapping):
        raise TypeError("parameters must be a mapping")
    _reject_content_bearing_audit_parameters(parameters)

    return {
        "schema_version": _AUDIT_LOG_SCHEMA_VERSION,
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
        "model": model,
        "prompt": {
            "id": prompt_id,
            "version": prompt_version,
        },
        "ir_version": ir_version,
        "parameters": _redact_audit_parameters(parameters),
    }


def _build_conversion_plan_payload(
    *,
    model: str,
    max_tokens: int,
    synthetic_text: str,
    repair_error: str | None = None,
    previous_response: JsonObject | None = None,
) -> JsonObject:
    task_prompts = "\n".join(
        f"- {action}: {prompt}" for action, prompt in sorted(CONVERSION_TASK_PROMPTS.items())
    )
    messages: list[JsonObject] = [
        {
            "role": "system",
            "content": (
                "Return only JSON that matches the supplied schema. "
                "Use synthetic input only and keep external_transmission false.\n"
                "Task prompts:\n"
                f"{task_prompts}"
            ),
        },
        {
            "role": "user",
            "content": synthetic_text,
        },
    ]
    if repair_error is not None:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Repair the previous JSON so it matches the schema exactly. "
                    "Do not add unsupported keys, do not set external_transmission true, "
                    "and return only the repaired JSON.\n"
                    f"Validation error: {repair_error}\n"
                    f"Previous response: {json.dumps(previous_response, ensure_ascii=False, sort_keys=True)}"
                ),
            }
        )

    return {
        "model": model,
        "temperature": 0,
        "stream": False,
        "max_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "veridoc_conversion_plan",
                "strict": True,
                "schema": CONVERSION_PLAN_SCHEMA,
            },
        },
        "messages": messages,
    }


def _redact_audit_parameters(value: object, *, key_path: str = "") -> object:
    if (
        key_path
        and _is_secret_parameter_key(key_path)
        and not _is_safe_json_schema_audit_parameter_key(key_path)
    ):
        return _REDACTED_VALUE
    if isinstance(value, Mapping):
        key_value_entry = _mapping_key_value_parameter_entry(value)
        return {
            str(key): _redact_audit_parameters(
                item,
                key_path=(
                    _join_parameter_key_path(key_path, key_value_entry[1])
                    if key_value_entry is not None and str(key) == key_value_entry[2]
                    else _join_parameter_key_path(key_path, str(key))
                ),
            )
            for key, item in value.items()
        }
    if _is_key_value_parameter_entry(value, key_path):
        entry_key = str(value[0])
        entry_path = _join_parameter_key_path(key_path, entry_key)
        return [entry_key, _redact_audit_parameters(value[1], key_path=entry_path)]
    if isinstance(value, str):
        if _is_credential_bearing_url(value):
            return _REDACTED_VALUE
        redacted_raw_value = _redact_raw_key_value_parameter_text(value, key_path)
        if redacted_raw_value is not None:
            return redacted_raw_value
    if isinstance(value, list):
        return [
            _redact_audit_parameters(item, key_path=f"{key_path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        return [
            _redact_audit_parameters(item, key_path=f"{key_path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, (set, frozenset)):
        redacted_items = [
            _redact_audit_parameters(item, key_path=f"{key_path}[{index}]")
            for index, item in enumerate(value)
        ]
        return sorted(redacted_items, key=_json_sort_key)
    if isinstance(value, os.PathLike):
        return os.fsdecode(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def _reject_content_bearing_audit_parameters(value: object, *, key_path: str = "parameters") -> None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"{key_path} must not include document or request content")
    if isinstance(value, Mapping):
        key_value_entry = _mapping_key_value_parameter_entry(value)
        if key_value_entry is None and _is_file_audit_parameter_container_path(key_path):
            raise ValueError(f"{key_path} must not include document or request content")
        if key_value_entry is not None:
            _key_field, entry_key, value_field = key_value_entry
            item_path = _join_parameter_key_path(key_path, entry_key)
            if _is_file_audit_parameter_container_path(key_path):
                raise ValueError(f"{item_path} must not include document or request content")
            if _is_content_bearing_audit_parameter_key(item_path):
                raise ValueError(f"{item_path} must not include document or request content")
            _reject_content_bearing_audit_parameters(value[value_field], key_path=item_path)
        for key, item in value.items():
            key_string = str(key)
            item_path = f"{key_path}.{key_string}"
            if _is_content_bearing_schema_value_path(item_path):
                raise ValueError(f"{item_path} must not include document or request content")
            if _is_invalid_json_schema_field_value_path(item_path, item):
                raise ValueError(f"{item_path} must not include document or request content")
            if _is_content_bearing_audit_parameter_key(item_path):
                raise ValueError(f"{item_path} must not include document or request content")
            _reject_content_bearing_audit_parameters(item, key_path=item_path)
    elif _is_key_value_parameter_entry(value, key_path):
        entry_key = str(value[0])
        item_path = f"{key_path}.{entry_key}"
        if _is_file_audit_parameter_container_path(key_path):
            raise ValueError(f"{item_path} must not include document or request content")
        if _is_content_bearing_audit_parameter_key(item_path):
            raise ValueError(f"{item_path} must not include document or request content")
        _reject_content_bearing_audit_parameters(value[1], key_path=item_path)
    elif isinstance(value, str):
        if _is_file_audit_parameter_container_path(key_path):
            raise ValueError(f"{key_path} must not include document or request content")
        if _is_content_bearing_schema_value_path(key_path) or _is_content_bearing_url(value):
            raise ValueError(f"{key_path} must not include document or request content")
        for raw_entry in _raw_key_value_parameter_entries(value, key_path):
            entry_key, _separator, _entry_value = raw_entry
            item_path = _raw_key_value_parameter_entry_path(key_path, entry_key)
            if _is_content_bearing_audit_parameter_key(item_path):
                raise ValueError(f"{item_path} must not include document or request content")
            if _is_content_bearing_url(_entry_value):
                raise ValueError(f"{item_path} must not include document or request content")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_content_bearing_audit_parameters(item, key_path=f"{key_path}[{index}]")
    elif isinstance(value, (set, frozenset)):
        for index, item in enumerate(value):
            _reject_content_bearing_audit_parameters(item, key_path=f"{key_path}[{index}]")
    elif isinstance(value, os.PathLike) and _is_file_audit_parameter_container_path(key_path):
        raise ValueError(f"{key_path} must not include document or request content")


def _is_content_bearing_audit_parameter_key(key: str) -> bool:
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key))
    if _is_safe_json_schema_audit_parameter_key(key):
        return False
    if normalized_leaf in _SAFE_DATA_METADATA_AUDIT_PARAMETER_KEYS:
        return False
    if normalized_leaf in _SAFE_MESSAGE_METADATA_AUDIT_PARAMETER_KEYS:
        return False
    if normalized_leaf in _SAFE_CONTENT_WORD_AUDIT_PARAMETER_KEYS:
        return False
    leaf_components = tuple(normalized_leaf.split("_"))
    singular_leaf_components = tuple(
        _singular_parameter_component(component) for component in leaf_components
    )
    path_components = tuple(_normalize_parameter_key(key).split("_"))
    return (
        normalized_leaf in _CONTENT_BEARING_AUDIT_PARAMETER_KEYS
        or "previous_response" in normalized_leaf
        or any(component in _CONTENT_BEARING_AUDIT_PARAMETER_KEY_COMPONENTS for component in leaf_components)
        or any(
            component in _CONTENT_BEARING_AUDIT_PARAMETER_KEY_COMPONENTS
            for component in singular_leaf_components
        )
        or any(
            _contains_component_sequence(leaf_components, sequence)
            for sequence in _CONTENT_BEARING_AUDIT_PARAMETER_KEY_COMPONENT_SEQUENCES
        )
        or (
            normalized_leaf == "bytes"
            and any(
                component in _CONTENT_BYTE_AUDIT_PARAMETER_ANCESTOR_COMPONENTS
                for component in path_components
            )
        )
    )


def _is_safe_json_schema_audit_parameter_key(key: str) -> bool:
    components = _audit_parameter_path_components(key)
    if not _is_json_schema_audit_parameter_path(components):
        return False
    leaf = components[-1]
    return _is_json_schema_field_name_path(components) or (
        leaf in _SAFE_JSON_SCHEMA_AUDIT_PARAMETER_METADATA_KEYS
    )


def _audit_parameter_path_components(key: str) -> tuple[str, ...]:
    return tuple(
        _normalize_parameter_key(_PARAMETER_INDEX_SUFFIX_RE.sub("", component))
        for component in key.split(".")
    )


def _is_response_format_json_schema_path(components: tuple[str, ...]) -> bool:
    return "response_format" in components and (
        "json_schema" in components or "schema" in components
    )


def _is_tool_function_json_schema_path(components: tuple[str, ...]) -> bool:
    return "tools" in components and "function" in components and "parameters" in components


def _is_json_schema_audit_parameter_path(components: tuple[str, ...]) -> bool:
    return (
        {"properties", "defs", "definitions"}.intersection(components)
        and (
            _is_response_format_json_schema_path(components)
            or _is_tool_function_json_schema_path(components)
        )
    )


def _is_json_schema_field_name_path(components: tuple[str, ...]) -> bool:
    return (
        len(components) >= 2
        and components[-2] in {"properties", "defs", "definitions"}
    )


def _is_content_bearing_schema_value_path(key: str) -> bool:
    components = _audit_parameter_path_components(key)
    if not _is_json_schema_audit_parameter_path(components):
        return False
    return components[-1] in _JSON_SCHEMA_VALUE_AUDIT_PARAMETER_KEYS


def _is_invalid_json_schema_field_value_path(key: str, value: object) -> bool:
    components = _audit_parameter_path_components(key)
    return _is_json_schema_field_name_path(components) and not isinstance(value, (Mapping, bool))


def _is_content_bearing_schema_field_name(name: str) -> bool:
    if name in _SAFE_CONTENT_WORD_AUDIT_PARAMETER_KEYS:
        return False
    components = tuple(name.split("_"))
    singular_components = tuple(_singular_parameter_component(component) for component in components)
    return (
        name in _CONTENT_BEARING_AUDIT_PARAMETER_KEYS
        or any(component in _CONTENT_BEARING_AUDIT_PARAMETER_KEY_COMPONENTS for component in components)
        or any(
            component in _CONTENT_BEARING_AUDIT_PARAMETER_KEY_COMPONENTS
            for component in singular_components
        )
        or any(
            _contains_component_sequence(components, sequence)
            for sequence in _CONTENT_BEARING_AUDIT_PARAMETER_KEY_COMPONENT_SEQUENCES
        )
    )


def _is_secret_parameter_key(key: str) -> bool:
    normalized = _normalize_parameter_key(key)
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key))
    if (
        normalized in _SAFE_CONTENT_WORD_AUDIT_PARAMETER_KEYS
        or normalized_leaf in _SAFE_CONTENT_WORD_AUDIT_PARAMETER_KEYS
        or _is_secret_exempt_key_value_audit_parameter_container_key(key)
    ):
        return False
    key_candidates = (normalized, normalized_leaf)
    components = tuple(normalized_leaf.split("_"))
    path_components = tuple(normalized.split("_"))
    singular_components = tuple(_singular_parameter_component(component) for component in components)
    return (
        any(candidate in _SECRET_PARAMETER_KEYS for candidate in key_candidates)
        or (
            normalized_leaf in {"code", "key"}
            and ("query" in path_components or "params" in path_components)
        )
        or any(
            candidate.endswith(suffix)
            for candidate in key_candidates
            for suffix in _SECRET_PARAMETER_KEY_SUFFIXES
        )
        or any(
            candidate.startswith(prefix)
            for candidate in key_candidates
            for prefix in _SECRET_PARAMETER_KEY_PREFIXES
        )
        or any(
            phrase in candidate
            for candidate in key_candidates
            for phrase in _SECRET_PARAMETER_KEY_PHRASES
        )
        or any(component in _SECRET_PARAMETER_KEY_COMPONENTS for component in components)
        or any(component in _SECRET_PARAMETER_KEY_COMPONENTS for component in singular_components)
        or any(
            _contains_component_sequence(components, sequence)
            for sequence in _SECRET_PARAMETER_KEY_COMPONENT_SEQUENCES
        )
    )


def _normalize_parameter_key(key: str) -> str:
    normalized = _CAMEL_ACRONYM_BOUNDARY_RE.sub(r"\1_\2", unquote_plus(key.strip()))
    normalized = _CAMEL_CASE_BOUNDARY_RE.sub(r"\1_\2", normalized)
    normalized = _PARAMETER_KEY_SEPARATOR_RE.sub("_", normalized)
    return normalized.strip("_").lower()


def _singular_parameter_component(component: str) -> str:
    return component[:-1] if component.endswith("s") else component


def _parameter_key_leaf(key: str) -> str:
    return _PARAMETER_INDEX_SUFFIX_RE.sub("", key.rsplit(".", 1)[-1])


def _is_file_audit_parameter_container_path(key_path: str) -> bool:
    return (
        _normalize_parameter_key(_parameter_key_leaf(key_path))
        in _FILE_AUDIT_PARAMETER_CONTAINER_KEYS
    )


def _join_parameter_key_path(parent: str, key: str) -> str:
    if not parent:
        return key
    return f"{parent}.{key}"


def _is_key_value_parameter_entry(value: object, key_path: str) -> bool:
    if not (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[0], str)
        and _raw_key_value_parameter_line(value[0], key_path) is None
    ):
        return False
    entry_path = _join_parameter_key_path(key_path, value[0])
    return (
        _is_key_value_audit_parameter_sequence_container_key(key_path)
        or (
            _is_parameter_sequence_item_key(key_path)
            and (
                _is_secret_parameter_key(entry_path)
                or _is_content_bearing_audit_parameter_key(entry_path)
            )
        )
    )


def _is_parameter_sequence_item_key(key: str) -> bool:
    return _PARAMETER_INDEX_SUFFIX_RE.search(key.rsplit(".", 1)[-1]) is not None


def _is_safe_audit_parameter_sequence_key(key: str) -> bool:
    return _normalize_parameter_key(_parameter_key_leaf(key)) in _SAFE_AUDIT_PARAMETER_SEQUENCE_KEYS


def _raw_key_value_parameter_line(value: str, key_path: str) -> tuple[str, str, str] | None:
    if not _is_key_value_audit_parameter_sequence_container_key(key_path):
        return None
    if _is_absolute_url(value):
        return None
    separator = _raw_key_value_parameter_separator(value)
    if separator is not None:
        entry_key, found, entry_value = value.partition(separator)
        if found and entry_key.strip() and entry_value.strip():
            return (entry_key.strip(), found, entry_value.strip())
    return None


def _raw_key_value_parameter_separator(value: str) -> str | None:
    positions = [
        (position, separator)
        for separator in ("=", ":")
        if (position := value.find(separator)) >= 0
    ]
    if not positions:
        return None
    return min(positions)[1]


def _raw_key_value_parameter_entries(value: str, key_path: str) -> list[tuple[str, str, str]]:
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key_path))
    if not _is_key_value_audit_parameter_sequence_container_key(key_path):
        return []
    return [
        entry
        for chunk in _raw_key_value_parameter_chunks(value, normalized_leaf)
        if (entry := _raw_key_value_parameter_line(chunk, key_path)) is not None
    ]


def _redact_raw_key_value_parameter_text(value: str, key_path: str) -> str | None:
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key_path))
    if not _is_key_value_audit_parameter_sequence_container_key(key_path):
        return None
    chunks = _raw_key_value_parameter_chunks(value, normalized_leaf)
    if len(chunks) == 1 and chunks[0] == value:
        raw_entry = _raw_key_value_parameter_line(value, key_path)
        if raw_entry is None:
            return None
        return _redact_raw_key_value_parameter_line(raw_entry, key_path)

    redacted_chunks = []
    changed = False
    for chunk in chunks:
        raw_entry = _raw_key_value_parameter_line(chunk, key_path)
        if raw_entry is None:
            redacted_chunks.append(chunk)
            continue
        redacted_chunk = _redact_raw_key_value_parameter_line(raw_entry, key_path)
        changed = changed or redacted_chunk != chunk
        redacted_chunks.append(redacted_chunk)
    if not changed:
        return None
    return _raw_key_value_parameter_joiner(value, normalized_leaf).join(redacted_chunks)


def _raw_key_value_parameter_chunks(value: str, normalized_leaf: str) -> list[str]:
    if "\n" in value or "\r" in value:
        return value.splitlines()
    if _is_query_audit_parameter_container_leaf(normalized_leaf) and ("&" in value or ";" in value):
        return re.split(r"[&;]", value)
    if _is_cookie_audit_parameter_container_leaf(normalized_leaf) and ";" in value:
        return [chunk.strip() for chunk in value.split(";")]
    return [value]


def _raw_key_value_parameter_joiner(value: str, normalized_leaf: str) -> str:
    if "\n" in value or "\r" in value:
        return "\n"
    if _is_query_audit_parameter_container_leaf(normalized_leaf) and "&" in value:
        return "&"
    if _is_query_audit_parameter_container_leaf(normalized_leaf) and ";" in value:
        return ";"
    if _is_cookie_audit_parameter_container_leaf(normalized_leaf) and ";" in value:
        return "; "
    return ""


def _redact_raw_key_value_parameter_line(
    raw_entry: tuple[str, str, str], key_path: str
) -> str:
    entry_key, separator, entry_value = raw_entry
    entry_path = _raw_key_value_parameter_entry_path(key_path, entry_key)
    redacted_value = _redact_audit_parameters(entry_value, key_path=entry_path)
    if redacted_value == _REDACTED_VALUE:
        redacted_separator = ": " if separator == ":" else separator
        return f"{entry_key}{redacted_separator}{_REDACTED_VALUE}"
    separator_text = ": " if separator == ":" else separator
    return f"{entry_key}{separator_text}{entry_value}"


def _raw_key_value_parameter_entry_path(key_path: str, entry_key: str) -> str:
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key_path))
    if _is_query_audit_parameter_container_leaf(normalized_leaf):
        return _join_parameter_key_path(key_path, unquote_plus(entry_key))
    return _join_parameter_key_path(key_path, entry_key)


def _mapping_key_value_parameter_entry(value: Mapping[object, object]) -> tuple[str, str, str] | None:
    normalized_fields: dict[str, object] = {}
    for field in value:
        if isinstance(field, str):
            normalized_fields[_normalize_parameter_key(field)] = field

    value_field = normalized_fields.get("value")
    if value_field is None:
        return None
    for key_field in ("key", "name", "header", "parameter"):
        original_key_field = normalized_fields.get(key_field)
        if original_key_field is None:
            continue
        key = value.get(original_key_field)
        if isinstance(key, str):
            return (str(original_key_field), key, str(value_field))
    return None


def _is_key_value_audit_parameter_sequence_container_key(key_path: str) -> bool:
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key_path))
    return (
        normalized_leaf in _KEY_VALUE_AUDIT_PARAMETER_SEQUENCE_CONTAINER_KEYS
        or normalized_leaf.endswith("_headers")
        or normalized_leaf.endswith("_cookies")
        or normalized_leaf.endswith("_params")
        or normalized_leaf.endswith("_parameters")
    )


def _is_secret_exempt_key_value_audit_parameter_container_key(key_path: str) -> bool:
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key_path))
    return (
        normalized_leaf in _KEY_VALUE_AUDIT_PARAMETER_SEQUENCE_CONTAINER_KEYS
        or normalized_leaf.endswith("_headers")
        or normalized_leaf.endswith("_cookies")
    )


def _is_query_audit_parameter_container_leaf(normalized_leaf: str) -> bool:
    return (
        normalized_leaf in {"params", "query_params", "query_parameters"}
        or normalized_leaf.endswith("_params")
        or normalized_leaf.endswith("_parameters")
    )


def _is_cookie_audit_parameter_container_leaf(normalized_leaf: str) -> bool:
    return normalized_leaf in {"cookies", "extra_cookies"} or normalized_leaf.endswith("_cookies")


def _json_sort_key(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return repr(value)


def _is_content_bearing_url(value: str, *, _depth: int = 0) -> bool:
    parsed_url = urlparse(value)
    if parsed_url.scheme.lower() == "data":
        return True
    if not parsed_url.scheme or not parsed_url.netloc:
        return False
    url_pairs = _url_parameter_pairs(parsed_url.query) + _url_parameter_pairs(parsed_url.fragment)
    return any(
        _is_content_bearing_audit_parameter_key(key)
        for key, _value in url_pairs
    ) or any(
        _is_content_bearing_url(parameter_value, _depth=_depth + 1)
        for _key, parameter_value in url_pairs
        if _depth < 3
    )


def _is_credential_bearing_url(value: str, *, _depth: int = 0) -> bool:
    parsed_url = urlparse(value)
    if not parsed_url.scheme or not parsed_url.netloc:
        return False
    if parsed_url.username is not None or parsed_url.password is not None:
        return True
    url_pairs = _url_parameter_pairs(parsed_url.query) + _url_parameter_pairs(parsed_url.fragment)
    if any(_is_secret_url_parameter_key(key) for key, _value in url_pairs):
        return True
    return any(
        _is_credential_bearing_url(parameter_value, _depth=_depth + 1)
        for _key, parameter_value in url_pairs
        if _depth < 3
    )


def _is_absolute_url(value: str) -> bool:
    parsed_url = urlparse(value)
    return bool(parsed_url.scheme and parsed_url.netloc)


def _is_secret_url_parameter_key(key: str) -> bool:
    return _is_secret_parameter_key(key) or _normalize_parameter_key(key) in {"code", "key"}


def _url_parameter_pairs(value: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for chunk in re.split(r"[&;]", value):
        pairs.extend(parse_qsl(chunk, keep_blank_values=True))
    return pairs


def _contains_component_sequence(components: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    if len(sequence) > len(components):
        return False
    return any(
        components[index : index + len(sequence)] == sequence
        for index in range(len(components) - len(sequence) + 1)
    )


def validate_conversion_plan(plan: object) -> None:
    if not isinstance(plan, dict):
        raise ConversionPlanValidationError("conversion plan must be a JSON object")

    _require_exact_keys(plan, {"schema_version", "source_kind", "operations", "constraints"}, "$")
    schema_version = plan["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 1:
        raise ConversionPlanValidationError("$.schema_version must be 1")
    if not _non_empty_string(plan["source_kind"]):
        raise ConversionPlanValidationError("$.source_kind must be a non-empty string")

    operations = plan["operations"]
    if not isinstance(operations, list) or not operations:
        raise ConversionPlanValidationError("$.operations must be a non-empty array")
    for index, operation in enumerate(operations):
        _validate_operation(operation, f"$.operations[{index}]")

    constraints = plan["constraints"]
    if not isinstance(constraints, dict):
        raise ConversionPlanValidationError("$.constraints must be an object")
    _require_exact_keys(constraints, {"external_transmission"}, "$.constraints")
    if constraints["external_transmission"] is not False:
        raise ConversionPlanValidationError("$.constraints.external_transmission must be false")


def _validate_operation(operation: object, path: str) -> None:
    if not isinstance(operation, dict):
        raise ConversionPlanValidationError(f"{path} must be an object")
    _require_exact_keys(operation, {"id", "action", "inputs", "output", "rationale"}, path)
    if not _non_empty_string(operation["id"]):
        raise ConversionPlanValidationError(f"{path}.id must be a non-empty string")
    if not isinstance(operation["action"], str) or operation["action"] not in _SUPPORTED_ACTIONS:
        raise ConversionPlanValidationError(f"{path}.action is not supported")
    inputs = operation["inputs"]
    if not isinstance(inputs, list) or not inputs or not all(_non_empty_string(item) for item in inputs):
        raise ConversionPlanValidationError(f"{path}.inputs must be a non-empty string array")
    if not _non_empty_string(operation["output"]):
        raise ConversionPlanValidationError(f"{path}.output must be a non-empty string")
    if not _non_empty_string(operation["rationale"]):
        raise ConversionPlanValidationError(f"{path}.rationale must be a non-empty string")


def _extract_json_content(response: JsonObject) -> JsonObject:
    try:
        choices = response["choices"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ConversionPlanValidationError("LLM response did not contain choices[0].message.content") from exc
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ConversionPlanValidationError("LLM response did not contain choices[0].message.content")

    choice = choices[0]
    try:
        message = choice["message"]
    except KeyError as exc:
        raise ConversionPlanValidationError("LLM response did not contain choices[0].message.content") from exc
    if not isinstance(message, dict) or "content" not in message:
        raise ConversionPlanValidationError("LLM response did not contain choices[0].message.content")

    finish_reason = choice.get("finish_reason")
    content = message["content"]

    if finish_reason not in (None, "stop"):
        raise ConversionPlanValidationError(f"LLM response did not finish cleanly: finish_reason={finish_reason}")

    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise ConversionPlanValidationError("LLM message content must be a JSON string or object")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ConversionPlanValidationError(f"LLM message content is not valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ConversionPlanValidationError("LLM message content must decode to a JSON object")
    return parsed


def _send_local_llm_request(
    local_base_url: _LocalBaseUrl,
    payload: JsonObject,
    headers: dict[str, str],
    timeout_seconds: float,
) -> JsonObject:
    last_error: RuntimeError | None = None
    for request_base_url in local_base_url.request_base_urls:
        request_url = _chat_completions_url(request_base_url)
        try:
            return _urllib_transport(
                request_url,
                payload,
                headers,
                timeout_seconds,
                tls_server_name=local_base_url.tls_server_name,
            )
        except RuntimeError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def _urllib_transport(
    url: str,
    payload: JsonObject,
    headers: dict[str, str],
    timeout_seconds: float,
    *,
    tls_server_name: str | None = None,
) -> JsonObject:
    if tls_server_name is not None:
        return _pinned_https_transport(url, payload, headers, timeout_seconds, tls_server_name)

    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with _local_only_url_opener().open(request, timeout=timeout_seconds) as response:
            return _decode_local_llm_response_body(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"local LLM request failed: {exc}") from exc


def _pinned_https_transport(
    url: str,
    payload: JsonObject,
    headers: dict[str, str],
    timeout_seconds: float,
    tls_server_name: str,
) -> JsonObject:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise LocalLLMConfigurationError("TLS server name pinning requires an https URL")

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    connection = _PinnedHTTPSConnection(
        connect_host=parsed.hostname,
        port=parsed.port or 443,
        tls_server_name=tls_server_name,
        timeout=timeout_seconds,
    )
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read().decode("utf-8")
        if 300 <= response.status < 400:
            raise urllib.error.HTTPError(
                url,
                response.status,
                f"local LLM redirects are disabled: {response.reason}",
                response.msg,
                None,
            )
        if response.status >= 400:
            raise urllib.error.HTTPError(url, response.status, response.reason, response.msg, None)
        return _decode_local_llm_response_body(response_body)
    except (OSError, http.client.HTTPException, urllib.error.URLError) as exc:
        raise RuntimeError(f"local LLM request failed: {exc}") from exc
    finally:
        connection.close()


def _decode_local_llm_response_body(response_body: str) -> JsonObject:
    try:
        response = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise ConversionPlanValidationError(f"local LLM response body is not valid JSON: {exc.msg}") from exc
    if not isinstance(response, dict):
        raise ConversionPlanValidationError("local LLM response body must decode to a JSON object")
    return response


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, connect_host: str, port: int, tls_server_name: str, timeout: float) -> None:
        super().__init__(
            tls_server_name,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._connect_host = connect_host
        self._tls_server_name = tls_server_name

    def connect(self) -> None:
        sock = socket.create_connection((self._connect_host, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._tls_server_name)


def _chat_completions_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/chat/completions"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"local LLM redirects are disabled: {msg}",
            headers,
            fp,
        )


def _local_only_url_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())


def _local_base_url(base_url: str) -> _LocalBaseUrl | None:
    try:
        parsed = urlparse(base_url)
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port == 0:
        return None

    hostname = hostname.lower()
    if _is_localhost_name(hostname):
        resolved_addresses = _resolve_localhost_runtime_addresses(hostname, port)
        if resolved_addresses is None:
            return None
        return _local_base_url_for_dns_host(parsed, hostname, port, resolved_addresses)
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return None
    if not _is_local_runtime_address(address):
        return None
    return _LocalBaseUrl((base_url,))


def _local_base_url_for_dns_host(
    parsed: Any,
    hostname: str,
    port: int | None,
    resolved_addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
) -> _LocalBaseUrl:
    tls_server_name = hostname if parsed.scheme == "https" else None
    return _LocalBaseUrl(
        request_base_urls=tuple(_base_url_with_address(parsed, address) for address in resolved_addresses),
        host_header=_host_header(hostname, port),
        tls_server_name=tls_server_name,
    )


def _resolve_localhost_runtime_addresses(
    hostname: str,
    port: int | None,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...] | None:
    resolved_addresses = _resolve_runtime_addresses(hostname, port)
    if resolved_addresses is None or not all(address.is_loopback for address in resolved_addresses):
        return None
    return resolved_addresses


def _resolve_runtime_addresses(
    hostname: str,
    port: int | None,
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...] | None:
    try:
        address_info = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None

    resolved_addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen_addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for info in address_info:
        sockaddr = info[4]
        if not sockaddr:
            return None
        raw_address = str(sockaddr[0]).split("%", maxsplit=1)[0]
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            return None
        if address not in seen_addresses:
            resolved_addresses.append(address)
            seen_addresses.add(address)

    if not resolved_addresses:
        return None
    return tuple(resolved_addresses)


def _base_url_with_address(parsed: Any, address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return parsed._replace(netloc=netloc).geturl()


def _host_header(hostname: str, port: int | None) -> str:
    if port is None:
        return hostname
    return f"{hostname}:{port}"


def _is_local_runtime_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address in _BLOCKED_LOCAL_RUNTIME_ADDRESSES:
        return False
    if address.is_link_local:
        return False
    if address.is_loopback:
        return True
    for network in _LOCAL_RUNTIME_NETWORKS:
        if address in network:
            return True
    return False


def _is_localhost_name(hostname: str) -> bool:
    return hostname == "localhost" or hostname.endswith(".localhost")


def _is_placeholder_secret(secret: str) -> bool:
    normalized = secret.strip().lower()
    return not normalized or any(marker in normalized for marker in _PLACEHOLDER_API_KEY_MARKERS)


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _require_exact_keys(value: dict[str, object], expected: set[str], path: str) -> None:
    keys = set(value)
    missing = expected - keys
    extra = keys - expected
    if missing:
        raise ConversionPlanValidationError(f"{path} missing required key(s): {', '.join(sorted(missing))}")
    if extra:
        raise ConversionPlanValidationError(f"{path} has unsupported key(s): {', '.join(sorted(extra))}")
