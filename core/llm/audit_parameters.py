from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, unquote, unquote_plus, urlparse

JsonObject = dict[str, Any]

_REDACTED_VALUE = "[REDACTED]"
_JSON_METADATA_NOT_DECODED = object()
_MAX_AUDIT_PARAMETER_DECODE_DEPTH = 3
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
        "output_data",
        "payload",
        "raw_data",
        "request_body",
        "request_data",
        "source",
        "source_bytes",
        "source_data",
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
        "assistant_message_id",
        "last_message_at",
        "message_count",
        "message_id",
        "message_role",
        "message_status",
        "message_type",
        "system_message_id",
        "user_message_id",
    }
)
_SAFE_MESSAGE_METADATA_DESCRIPTOR_COMPONENTS = frozenset(
    {
        "at",
        "count",
        "id",
        "ids",
        "index",
        "name",
        "role",
        "status",
        "timestamp",
        "type",
    }
)
_SAFE_DATA_METADATA_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "meta_data",
        "model_data",
    }
)
_SAFE_JSON_METADATA_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "metadata_json",
        "schema_json",
    }
)
_JSON_ENCODED_AUDIT_METADATA_KEYS = frozenset(
    {
        "metadata_json",
        "schema_json",
    }
)
_SAFE_FORM_DATA_METADATA_AUDIT_PARAMETER_KEYS = frozenset(
    {
        "form_data_content_type",
        "form_data_description",
        "form_data_type",
        "multipart_form_data_content_type",
        "multipart_form_data_description",
        "multipart_form_data_type",
        "request_form_data_content_type",
        "request_form_data_description",
        "request_form_data_type",
    }
)
_SAFE_DESCRIPTOR_COMPONENT_SEQUENCES = (
    ("code",),
    ("content", "type"),
    ("description",),
    ("id",),
    ("status",),
    ("status", "code"),
    ("type",),
)
_SAFE_AUDIT_PARAMETER_SEQUENCE_KEYS = frozenset(
    {
        "stop",
    }
)
_SAFE_TWO_STRING_AUDIT_PARAMETER_LIST_KEYS = frozenset(
    {
        "generation_parameters",
        "model_parameters",
    }
)
_CONTENT_BEARING_AUDIT_PARAMETER_KEY_COMPONENTS = frozenset(
    {
        "content",
        "attachment",
        "attachments",
        "body",
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
    ("form", "data"),
    ("json", "data"),
    ("json", "output"),
    ("json", "raw"),
    ("json", "request"),
    ("json", "response"),
    ("json", "result"),
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
_JSON_SCHEMA_SINGLE_SCHEMA_KEYS = frozenset(
    {
        "additional_properties",
        "contains",
        "else",
        "if",
        "items",
        "not",
        "property_names",
        "then",
        "unevaluated_items",
        "unevaluated_properties",
    }
)
_JSON_SCHEMA_SCHEMA_ARRAY_KEYS = frozenset(
    {
        "all_of",
        "any_of",
        "one_of",
        "prefix_items",
    }
)
_JSON_SCHEMA_SCHEMA_MAP_KEYS = frozenset(
    {
        "defs",
        "definitions",
        "dependent_schemas",
        "pattern_properties",
        "properties",
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
_QUERY_AUDIT_PARAMETER_CONTAINER_PREFIX_COMPONENTS = frozenset(
    {
        "callback",
        "custom",
        "default",
        "extra",
        "query",
        "redirect",
        "request",
        "search",
        "uri",
        "url",
    }
)
_RAW_AUDIT_PARAMETER_CONTAINER_PREFIX_COMPONENTS = frozenset(
    {
        "provider",
    }
)
_FILE_AUDIT_PARAMETER_CONTAINER_KEYS = frozenset({"file", "files"})
_CONTENT_BYTE_AUDIT_PARAMETER_ANCESTOR_COMPONENTS = frozenset(
    {
        "output",
        "source",
    }
)


@dataclass(frozen=True)
class AuditParameterContext:
    key_path: str = "parameters"

    @property
    def components(self) -> tuple[str, ...]:
        return tuple(
            _normalize_parameter_key(_PARAMETER_INDEX_SUFFIX_RE.sub("", component))
            for component in self.key_path.split(".")
        )

    @property
    def leaf(self) -> str:
        return _PARAMETER_INDEX_SUFFIX_RE.sub("", self.key_path.rsplit(".", 1)[-1])

    @property
    def normalized_leaf(self) -> str:
        return _normalize_parameter_key(self.leaf)

    @property
    def is_json_encoded_metadata(self) -> bool:
        return self.normalized_leaf in _JSON_ENCODED_AUDIT_METADATA_KEYS

    @property
    def is_schema_json_root(self) -> bool:
        first_real_component = _first_non_parameter_component_index(self.components)
        return self.components[first_real_component:] == ("schema_json",)

    @property
    def is_raw_query_value(self) -> bool:
        return _is_multi_entry_raw_parameter_container_leaf(self.normalized_leaf)

    def child_path(self, key: object, *, decode_raw_key: bool = False) -> str:
        key_string = str(key)
        if decode_raw_key:
            key_string = _raw_key_value_parameter_entry_key(self.key_path, key_string)
        return _join_parameter_key_path(self.key_path, key_string)


def sanitize_audit_parameters(parameters: Mapping[str, object]) -> JsonObject:
    _reject_content_bearing_audit_parameters(parameters)
    sanitized = _redact_audit_parameters(parameters, key_path=AuditParameterContext().key_path)
    if not isinstance(sanitized, dict):
        raise TypeError("parameters must be JSON-object audit metadata")
    return sanitized


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
            str(key): _redact_mapping_audit_parameter_item(
                key,
                item,
                key_path=key_path,
                key_value_entry=key_value_entry,
            )
            for key, item in value.items()
        }
    if _is_key_value_parameter_entry(value, key_path):
        entry_name = _redact_key_value_parameter_entry_name(str(value[0]), key_path)
        entry_path = _join_parameter_key_path(key_path, entry_name)
        return [
            entry_name,
            _redact_key_value_parameter_entry_value(value[1], key_path, entry_path),
        ]
    if isinstance(value, str):
        parameter_value = _security_checked_audit_parameter_string(value, key_path)
        if any(
            _is_credential_bearing_url(url_value)
            for url_value in _audit_parameter_url_value_forms(parameter_value, key_path)
        ):
            return _REDACTED_VALUE
        redacted_json_value = _redact_json_encoded_audit_metadata_value(parameter_value, key_path)
        if redacted_json_value is not None:
            return redacted_json_value
        redacted_raw_value = _redact_raw_key_value_parameter_text(parameter_value, key_path)
        if redacted_raw_value is not None:
            return redacted_raw_value
        return value
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
        return _redact_audit_parameters(os.fsdecode(value), key_path=key_path)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{key_path} must include finite JSON number audit metadata")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    display_path = f"parameters.{key_path}" if key_path else "parameters"
    raise TypeError(f"{display_path} must be JSON-serializable audit metadata")


def _redact_mapping_audit_parameter_item(
    key: object,
    item: object,
    *,
    key_path: str,
    key_value_entry: tuple[str, str, str] | None,
) -> object:
    item_path = _join_parameter_key_path(
        key_path,
        _raw_key_value_parameter_entry_key(key_path, str(key)),
    )
    if key_value_entry is None:
        if (
            isinstance(item, str)
            and _is_raw_query_audit_parameter_value_key(key_path)
            and not _is_json_encoded_audit_metadata_key_path(item_path)
        ):
            return _redact_key_value_parameter_entry_value(item, key_path, item_path)
        return _redact_audit_parameters(
            item,
            key_path=item_path,
        )
    if str(key) == key_value_entry[0]:
        return _redact_key_value_parameter_entry_name(str(item), key_path)
    if str(key) == key_value_entry[2]:
        entry_name = _redact_key_value_parameter_entry_name(key_value_entry[1], key_path)
        entry_path = _join_parameter_key_path(key_path, entry_name)
        return _redact_key_value_parameter_entry_value(item, key_path, entry_path)
    return _redact_audit_parameters(
        item,
        key_path=item_path,
    )


def _redact_key_value_parameter_entry_name(entry_key: str, key_path: str) -> str:
    decoded_key = _raw_key_value_parameter_entry_key(key_path, entry_key)
    if _is_credential_bearing_url(decoded_key) or _is_credential_bearing_raw_query_text(decoded_key):
        return _REDACTED_VALUE
    return decoded_key


def _redact_key_value_parameter_entry_value(
    value: object,
    key_path: str,
    entry_path: str,
) -> object:
    if not isinstance(value, str):
        return _redact_audit_parameters(value, key_path=entry_path)
    entry_parameter_value = _raw_key_value_parameter_entry_value(key_path, value)
    if (
        _is_raw_query_audit_parameter_value_key(key_path)
        and not _is_key_value_audit_parameter_sequence_container_key(entry_path)
        and _is_credential_bearing_raw_query_text(entry_parameter_value)
    ):
        return _REDACTED_VALUE
    return _redact_audit_parameters(entry_parameter_value, key_path=entry_path)


def _reject_key_value_parameter_entry_value(
    value: object,
    key_path: str,
    entry_path: str,
) -> None:
    if not isinstance(value, str):
        _reject_content_bearing_audit_parameters(value, key_path=entry_path)
        return
    entry_parameter_value = _raw_key_value_parameter_entry_value(key_path, value)
    if _is_content_bearing_url(entry_parameter_value) or (
        _is_raw_query_audit_parameter_value_key(key_path)
        and not _is_key_value_audit_parameter_sequence_container_key(entry_path)
        and _is_content_bearing_raw_query_text(entry_parameter_value)
    ):
        raise ValueError(f"{entry_path} must not include document or request content")
    _reject_content_bearing_audit_parameters(entry_parameter_value, key_path=entry_path)


def _reject_key_value_parameter_entry_name(entry_key: str, key_path: str, entry_path: str) -> None:
    decoded_key = _raw_key_value_parameter_entry_key(key_path, entry_key)
    if _is_content_bearing_audit_parameter_key(entry_path):
        raise ValueError(f"{entry_path} must not include document or request content")
    if _is_content_bearing_url(decoded_key) or _is_content_bearing_raw_query_text(decoded_key):
        raise ValueError(f"{entry_path} must not include document or request content")


def _reject_content_bearing_audit_parameters(value: object, *, key_path: str = "parameters") -> None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError(f"{key_path} must not include document or request content")
    if (
        key_path
        and _is_secret_parameter_key(key_path)
        and not _is_safe_json_schema_audit_parameter_key(key_path)
    ):
        return
    if _is_invalid_json_schema_value_path(key_path, value):
        raise ValueError(f"{key_path} must not include document or request content")
    if isinstance(value, Mapping):
        key_value_entry = _mapping_key_value_parameter_entry(value)
        if key_value_entry is None and _is_file_audit_parameter_container_path(key_path):
            raise ValueError(f"{key_path} must not include document or request content")
        if key_value_entry is not None:
            _key_field, entry_key, value_field = key_value_entry
            item_path = _join_parameter_key_path(
                key_path,
                _raw_key_value_parameter_entry_key(key_path, entry_key),
            )
            if _is_file_audit_parameter_container_path(key_path):
                raise ValueError(f"{item_path} must not include document or request content")
            _reject_key_value_parameter_entry_name(entry_key, key_path, item_path)
            _reject_key_value_parameter_entry_value(
                value[value_field],
                key_path,
                item_path,
            )
        for key, item in value.items():
            key_string = str(key)
            item_path = _join_parameter_key_path(
                key_path,
                _raw_key_value_parameter_entry_key(key_path, key_string),
            )
            if _is_json_schema_schema_map_path(key_path) and not isinstance(item, (Mapping, bool)):
                raise ValueError(f"{item_path} must not include document or request content")
            if _is_content_bearing_schema_value_path(item_path, item):
                raise ValueError(f"{item_path} must not include document or request content")
            if _is_content_bearing_audit_parameter_key(item_path):
                raise ValueError(f"{item_path} must not include document or request content")
            if (
                isinstance(item, str)
                and _is_raw_query_audit_parameter_value_key(key_path)
                and not _is_json_encoded_audit_metadata_key_path(item_path)
            ):
                _reject_key_value_parameter_entry_value(item, key_path, item_path)
                continue
            _reject_content_bearing_audit_parameters(item, key_path=item_path)
    elif _is_key_value_parameter_entry(value, key_path):
        entry_key = _raw_key_value_parameter_entry_key(key_path, str(value[0]))
        item_path = f"{key_path}.{entry_key}"
        if _is_file_audit_parameter_container_path(key_path):
            raise ValueError(f"{item_path} must not include document or request content")
        _reject_key_value_parameter_entry_name(str(value[0]), key_path, item_path)
        _reject_key_value_parameter_entry_value(value[1], key_path, item_path)
    elif isinstance(value, str):
        parameter_value = _security_checked_audit_parameter_string(value, key_path)
        if _is_file_audit_parameter_container_path(key_path):
            raise ValueError(f"{key_path} must not include document or request content")
        if _is_content_bearing_schema_value_path(key_path, parameter_value) or any(
            _is_content_bearing_url(url_value)
            for url_value in _audit_parameter_url_value_forms(parameter_value, key_path)
        ):
            raise ValueError(f"{key_path} must not include document or request content")
        if _is_unsafe_json_encoded_metadata_scalar_path(key_path):
            raise ValueError(f"{key_path} must not include document or request content")
        decoded_json_value = _json_encoded_audit_metadata_value(parameter_value, key_path)
        if decoded_json_value is not _JSON_METADATA_NOT_DECODED:
            if isinstance(decoded_json_value, bool) and _is_schema_json_root_key_path(key_path):
                pass
            elif not isinstance(decoded_json_value, (Mapping, list)):
                raise ValueError(f"{key_path} must not include document or request content")
            else:
                _reject_content_bearing_audit_parameters(decoded_json_value, key_path=key_path)
        for raw_entry in _raw_key_value_parameter_entries(parameter_value, key_path):
            entry_key, _separator, _entry_value = raw_entry
            item_path = _raw_key_value_parameter_entry_path(key_path, entry_key)
            if _is_content_bearing_audit_parameter_key(item_path):
                raise ValueError(f"{item_path} must not include document or request content")
            entry_value = _raw_key_value_parameter_entry_value(key_path, _entry_value)
            if _is_content_bearing_url(entry_value) or (
                _is_raw_query_audit_parameter_value_key(key_path)
                and not _is_key_value_audit_parameter_sequence_container_key(item_path)
                and _is_content_bearing_raw_query_text(entry_value)
            ):
                raise ValueError(f"{item_path} must not include document or request content")
            _reject_content_bearing_audit_parameters(entry_value, key_path=item_path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_content_bearing_audit_parameters(item, key_path=f"{key_path}[{index}]")
    elif isinstance(value, (set, frozenset)):
        for index, item in enumerate(value):
            _reject_content_bearing_audit_parameters(item, key_path=f"{key_path}[{index}]")
    elif isinstance(value, os.PathLike):
        if _is_file_audit_parameter_container_path(key_path):
            raise ValueError(f"{key_path} must not include document or request content")
        _reject_content_bearing_audit_parameters(os.fsdecode(value), key_path=key_path)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{key_path} must include finite JSON number audit metadata")
    elif value is None or isinstance(value, (bool, int, float, Decimal)):
        if _is_unsafe_json_encoded_metadata_scalar_path(key_path):
            raise ValueError(f"{key_path} must not include document or request content")


def _is_content_bearing_audit_parameter_key(key: str) -> bool:
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key))
    if _is_safe_json_schema_audit_parameter_key(key):
        return False
    if normalized_leaf in _SAFE_DATA_METADATA_AUDIT_PARAMETER_KEYS:
        return False
    if _is_safe_json_metadata_audit_parameter_key(normalized_leaf):
        return False
    if _is_safe_form_data_metadata_audit_parameter_key(normalized_leaf):
        return False
    if _is_safe_message_metadata_audit_parameter_key(normalized_leaf):
        return False
    if normalized_leaf in _SAFE_CONTENT_WORD_AUDIT_PARAMETER_KEYS:
        return False
    if _is_secret_parameter_key(key):
        return False
    leaf_components = tuple(normalized_leaf.split("_"))
    singular_leaf_components = tuple(
        _singular_parameter_component(component) for component in leaf_components
    )
    path_components = tuple(_normalize_parameter_key(key).split("_"))
    return (
        normalized_leaf in _CONTENT_BEARING_AUDIT_PARAMETER_KEYS
        or normalized_leaf.endswith("_json")
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


def _is_safe_message_metadata_audit_parameter_key(normalized_leaf: str) -> bool:
    if normalized_leaf in _SAFE_MESSAGE_METADATA_AUDIT_PARAMETER_KEYS:
        return True
    components = tuple(normalized_leaf.split("_"))
    if "message" not in components and "messages" not in components:
        return False
    if _has_disallowed_content_component(
        components,
        allowed_components=frozenset({"content", "message", "messages"}),
    ):
        return False
    return any(component in _SAFE_MESSAGE_METADATA_DESCRIPTOR_COMPONENTS for component in components)


def _is_safe_json_metadata_audit_parameter_key(normalized_leaf: str) -> bool:
    if normalized_leaf in _SAFE_JSON_METADATA_AUDIT_PARAMETER_KEYS:
        return True
    components = tuple(normalized_leaf.split("_"))
    if _has_disallowed_content_component(
        components,
        allowed_components=frozenset({"content"}),
    ):
        return False
    return (
        any(
            _contains_component_sequence(components, sequence)
            for sequence in (
                ("json", "data"),
                ("json", "output"),
                ("json", "request"),
                ("json", "response"),
                ("json", "result"),
            )
        )
    ) and _ends_with_component_sequence(components, _SAFE_DESCRIPTOR_COMPONENT_SEQUENCES)


def _is_safe_form_data_metadata_audit_parameter_key(normalized_leaf: str) -> bool:
    if normalized_leaf in _SAFE_FORM_DATA_METADATA_AUDIT_PARAMETER_KEYS:
        return True
    components = tuple(normalized_leaf.split("_"))
    if _has_disallowed_content_component(
        components,
        allowed_components=frozenset({"content"}),
    ):
        return False
    return _contains_component_sequence(
        components,
        ("form", "data"),
    ) and _ends_with_component_sequence(components, _SAFE_DESCRIPTOR_COMPONENT_SEQUENCES)


def _has_disallowed_content_component(
    components: tuple[str, ...],
    *,
    allowed_components: frozenset[str],
) -> bool:
    disallowed_components = _CONTENT_BEARING_AUDIT_PARAMETER_KEY_COMPONENTS - allowed_components
    singular_components = tuple(_singular_parameter_component(component) for component in components)
    return any(component in disallowed_components for component in components) or any(
        component in disallowed_components for component in singular_components
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
    return AuditParameterContext(key).components


def _is_response_format_json_schema_path(components: tuple[str, ...]) -> bool:
    return "response_format" in components and (
        "json_schema" in components or "schema" in components
    )


def _is_tool_function_json_schema_path(components: tuple[str, ...]) -> bool:
    return "tools" in components and "function" in components and "parameters" in components


def _is_json_schema_audit_parameter_path(components: tuple[str, ...]) -> bool:
    return (
        _is_root_schema_json_audit_parameter_path(components)
        or (
            _JSON_SCHEMA_SCHEMA_MAP_KEYS.intersection(components)
            and (
                _is_response_format_json_schema_path(components)
                or _is_tool_function_json_schema_path(components)
            )
        )
    )


def _is_root_schema_json_audit_parameter_path(components: tuple[str, ...]) -> bool:
    first_real_component = _first_non_parameter_component_index(components)
    return (
        len(components) > first_real_component
        and components[first_real_component] == "schema_json"
    )


def _is_schema_json_root_key_path(key_path: str) -> bool:
    return AuditParameterContext(key_path).is_schema_json_root


def _first_non_parameter_component_index(components: tuple[str, ...]) -> int:
    index = 0
    while index < len(components) and components[index] == "parameters":
        index += 1
    return index


def _is_json_schema_field_name_path(components: tuple[str, ...]) -> bool:
    return (
        len(components) >= 2
        and components[-2] in _JSON_SCHEMA_SCHEMA_MAP_KEYS
        and (len(components) < 3 or components[-3] not in _JSON_SCHEMA_SCHEMA_MAP_KEYS)
    )


def _is_json_schema_schema_map_path(key: str) -> bool:
    components = _audit_parameter_path_components(key)
    return (
        bool(components)
        and _is_json_schema_audit_parameter_path(components)
        and components[-1] in _JSON_SCHEMA_SCHEMA_MAP_KEYS
        and (len(components) < 2 or components[-2] not in _JSON_SCHEMA_SCHEMA_MAP_KEYS)
    )


def _is_content_bearing_schema_value_path(key: str, value: object) -> bool:
    components = _audit_parameter_path_components(key)
    if not _is_json_schema_audit_parameter_path(components):
        return False
    if components[-1] not in _JSON_SCHEMA_VALUE_AUDIT_PARAMETER_KEYS:
        return False
    return not _is_safe_schema_literal_constraint(key, value)


def _is_invalid_json_schema_value_path(key: str, value: object) -> bool:
    components = _audit_parameter_path_components(key)
    if not _is_json_schema_audit_parameter_path(components):
        return False
    if _is_schema_json_root_key_path(key) and not isinstance(value, (str, Mapping, bool)):
        return True
    if _is_json_schema_field_name_path(components):
        return not isinstance(value, (Mapping, bool))

    leaf = key.rsplit(".", 1)[-1]
    has_index_suffix = _PARAMETER_INDEX_SUFFIX_RE.search(leaf) is not None
    normalized_leaf = _normalize_parameter_key(_PARAMETER_INDEX_SUFFIX_RE.sub("", leaf))
    schema_types = (Mapping, bool)
    if normalized_leaf in _JSON_SCHEMA_SCHEMA_MAP_KEYS and not has_index_suffix:
        return not isinstance(value, Mapping)
    if normalized_leaf in _JSON_SCHEMA_SINGLE_SCHEMA_KEYS:
        if has_index_suffix:
            return not isinstance(value, schema_types)
        if normalized_leaf == "items":
            return not isinstance(value, (Mapping, bool, list))
        return not isinstance(value, schema_types)
    if normalized_leaf in _JSON_SCHEMA_SCHEMA_ARRAY_KEYS:
        if has_index_suffix:
            return not isinstance(value, schema_types)
        return not isinstance(value, list)
    return False


def _is_safe_schema_literal_constraint(key: str, value: object) -> bool:
    components = _audit_parameter_path_components(key)
    first_real_component = _first_non_parameter_component_index(components)
    if components[first_real_component:-1] != ("schema_json",):
        return False
    return _is_safe_schema_literal_value(value)


def _is_safe_schema_literal_value(value: object, *, _depth: int = 0) -> bool:
    if _depth > _MAX_AUDIT_PARAMETER_DECODE_DEPTH:
        return False
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return _is_safe_schema_literal_string(value)
    if isinstance(value, (list, tuple)):
        return len(value) <= 20 and all(
            _is_safe_schema_literal_value(item, _depth=_depth + 1) for item in value
        )
    return False


def _is_safe_schema_literal_string(value: str) -> bool:
    if not value or len(value) > 64:
        return False
    if _is_content_bearing_url(value) or _is_credential_bearing_url(value):
        return False
    return re.fullmatch(r"[a-z][a-z0-9_:-]*", value) is not None


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
    real_path_components = path_components[1:] if path_components[:1] == ("parameters",) else path_components
    singular_components = tuple(_singular_parameter_component(component) for component in components)
    return (
        any(candidate in _SECRET_PARAMETER_KEYS for candidate in key_candidates)
        or (
            normalized_leaf in {"code", "key"}
            and (
                "query" in real_path_components
                or "params" in real_path_components
                or "parameters" in real_path_components
                or _is_query_audit_parameter_entry_key(key)
            )
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
    return AuditParameterContext(key).leaf


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
        _is_structured_key_value_audit_parameter_sequence_container_key(key_path)
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
        if (
            entry := _raw_key_value_parameter_chunk_entry(
                chunk,
                key_path,
                normalized_leaf,
            )
        )
        is not None
    ]


def _redact_raw_key_value_parameter_text(value: str, key_path: str) -> str | None:
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key_path))
    if not _is_key_value_audit_parameter_sequence_container_key(key_path):
        return None
    chunks = _raw_key_value_parameter_chunks(value, normalized_leaf)
    if len(chunks) == 1 and chunks[0] == value:
        raw_entry = _raw_key_value_parameter_chunk_entry(value, key_path, normalized_leaf)
        if raw_entry is None:
            return None
        return _redact_raw_key_value_parameter_line(raw_entry, key_path)

    redacted_chunks = []
    changed = False
    for chunk in chunks:
        raw_entry = _raw_key_value_parameter_chunk_entry(chunk, key_path, normalized_leaf)
        if raw_entry is None:
            redacted_chunks.append(chunk)
            continue
        redacted_chunk = _redact_raw_key_value_parameter_line(raw_entry, key_path)
        changed = changed or redacted_chunk != chunk
        redacted_chunks.append(redacted_chunk)
    if not changed:
        return None
    return _raw_key_value_parameter_joiner(value, normalized_leaf).join(redacted_chunks)


def _raw_key_value_parameter_chunk_entry(
    chunk: str,
    key_path: str,
    normalized_leaf: str,
) -> tuple[str, str, str] | None:
    raw_entry = _raw_key_value_parameter_line(chunk, key_path)
    if raw_entry is not None:
        return raw_entry
    if not _is_multi_entry_raw_parameter_container_leaf(normalized_leaf):
        return None
    decoded_chunk = _fully_decode_query_parameter_value(chunk)
    if decoded_chunk == chunk:
        return None
    return _raw_key_value_parameter_line(decoded_chunk, key_path)


def _raw_key_value_parameter_chunks(value: str, normalized_leaf: str) -> list[str]:
    if "\n" in value or "\r" in value:
        return value.splitlines()
    if _is_multi_entry_raw_parameter_container_leaf(normalized_leaf) and (
        "&" in value or ";" in value
    ):
        return re.split(r"[&;]", value)
    if _is_cookie_audit_parameter_container_leaf(normalized_leaf) and ";" in value:
        return [chunk.strip() for chunk in value.split(";")]
    return [value]


def _raw_key_value_parameter_joiner(value: str, normalized_leaf: str) -> str:
    if "\n" in value or "\r" in value:
        return "\n"
    if _is_multi_entry_raw_parameter_container_leaf(normalized_leaf) and "&" in value:
        return "&"
    if _is_multi_entry_raw_parameter_container_leaf(normalized_leaf) and ";" in value:
        return ";"
    if _is_cookie_audit_parameter_container_leaf(normalized_leaf) and ";" in value:
        return "; "
    return ""


def _redact_raw_key_value_parameter_line(
    raw_entry: tuple[str, str, str], key_path: str
) -> str:
    entry_key, separator, entry_value = raw_entry
    entry_path = _raw_key_value_parameter_entry_path(key_path, entry_key)
    entry_parameter_value = _raw_key_value_parameter_entry_value(key_path, entry_value)
    separator_text = ": " if separator == ":" else separator
    if (
        _is_raw_query_audit_parameter_value_key(key_path)
        and not _is_key_value_audit_parameter_sequence_container_key(entry_path)
        and _is_credential_bearing_raw_query_text(entry_parameter_value)
    ):
        return f"{entry_key}{separator_text}{_REDACTED_VALUE}"
    redacted_value = _redact_audit_parameters(
        entry_parameter_value,
        key_path=entry_path,
    )
    if redacted_value == _REDACTED_VALUE:
        return f"{entry_key}{separator_text}{_REDACTED_VALUE}"
    if isinstance(redacted_value, str) and redacted_value != entry_parameter_value:
        return f"{entry_key}{separator_text}{redacted_value}"
    return f"{entry_key}{separator_text}{entry_value}"


def _raw_key_value_parameter_entry_path(key_path: str, entry_key: str) -> str:
    return _join_parameter_key_path(
        key_path,
        _raw_key_value_parameter_entry_key(key_path, entry_key),
    )


def _raw_key_value_parameter_entry_key(key_path: str, entry_key: str) -> str:
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key_path))
    if _is_query_audit_parameter_container_leaf(normalized_leaf):
        return _fully_decode_query_parameter_value(entry_key)
    if _is_raw_audit_parameter_container_leaf(normalized_leaf):
        return _fully_decode_query_parameter_value(entry_key)
    return entry_key


def _raw_key_value_parameter_entry_value(key_path: str, entry_value: str) -> str:
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key_path))
    if _is_query_audit_parameter_container_leaf(
        normalized_leaf
    ) or _is_raw_audit_parameter_container_leaf(normalized_leaf):
        return _fully_decode_query_parameter_value(entry_value)
    return entry_value


def _fully_decode_query_parameter_value(value: str) -> str:
    decoded_value = value
    for _index in range(_MAX_AUDIT_PARAMETER_DECODE_DEPTH):
        next_value = unquote(decoded_value)
        if next_value == decoded_value:
            break
        decoded_value = next_value
    return decoded_value


def _security_checked_audit_parameter_string(value: str, key_path: str) -> str:
    if not _should_decode_structured_raw_audit_parameter_value(key_path):
        return value
    return _fully_decode_query_parameter_value(value)


def _audit_parameter_url_value_forms(value: str, key_path: str) -> tuple[str, ...]:
    decoded_value = _fully_decode_query_parameter_value(value)
    if decoded_value == value:
        return (value,)
    if _should_decode_url_audit_parameter_value(key_path) and (
        _is_absolute_url(decoded_value) or _is_data_url(decoded_value)
    ):
        return (value, decoded_value)
    return (value,)


def _should_decode_url_audit_parameter_value(key_path: str) -> bool:
    leaf = _normalize_parameter_key(_parameter_key_leaf(key_path))
    parent_key = key_path.rsplit(".", 1)[0] if "." in key_path else ""
    parent_leaf = _normalize_parameter_key(_parameter_key_leaf(parent_key))
    return (
        _is_query_audit_parameter_container_leaf(parent_leaf)
        or _is_raw_audit_parameter_container_leaf(parent_leaf)
        or _is_query_audit_parameter_container_leaf(leaf)
        or _is_raw_audit_parameter_container_leaf(leaf)
        or _is_url_value_audit_parameter_leaf(leaf)
    )


def _should_decode_structured_raw_audit_parameter_value(key_path: str) -> bool:
    parent_key = key_path.rsplit(".", 1)[0]
    parent_leaf = _normalize_parameter_key(_parameter_key_leaf(parent_key))
    return (
        _is_query_audit_parameter_container_leaf(parent_leaf)
        or _is_raw_audit_parameter_container_leaf(parent_leaf)
    )


def _is_url_value_audit_parameter_leaf(normalized_leaf: str) -> bool:
    components = tuple(normalized_leaf.split("_"))
    return bool(components) and components[-1] in {"url", "uri"}


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
        or _is_query_audit_parameter_container_leaf(normalized_leaf)
        or _is_raw_audit_parameter_container_leaf(normalized_leaf)
    )


def _is_structured_key_value_audit_parameter_sequence_container_key(key_path: str) -> bool:
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key_path))
    return (
        normalized_leaf in _KEY_VALUE_AUDIT_PARAMETER_SEQUENCE_CONTAINER_KEYS
        or normalized_leaf == "query_parameters"
        or normalized_leaf == "default_parameters"
        or normalized_leaf.endswith("_headers")
        or normalized_leaf.endswith("_cookies")
        or _is_query_audit_parameter_container_leaf(normalized_leaf)
        or _is_raw_audit_parameter_container_leaf(normalized_leaf)
    )


def _is_secret_exempt_key_value_audit_parameter_container_key(key_path: str) -> bool:
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key_path))
    return (
        normalized_leaf in _KEY_VALUE_AUDIT_PARAMETER_SEQUENCE_CONTAINER_KEYS
        or normalized_leaf.endswith("_headers")
        or normalized_leaf.endswith("_cookies")
    )


def _is_query_audit_parameter_container_leaf(normalized_leaf: str) -> bool:
    components = tuple(normalized_leaf.split("_"))
    return (
        normalized_leaf in {"params", "query_params", "query_parameters"}
        or (
            len(components) >= 2
            and components[-1] in {"params", "parameters"}
            and any(
                component in _QUERY_AUDIT_PARAMETER_CONTAINER_PREFIX_COMPONENTS
                for component in components[:-1]
            )
        )
    )


def _is_raw_audit_parameter_container_leaf(normalized_leaf: str) -> bool:
    components = tuple(normalized_leaf.split("_"))
    return (
        len(components) >= 2
        and components[-1] in {"params", "parameters"}
        and any(
            component in _RAW_AUDIT_PARAMETER_CONTAINER_PREFIX_COMPONENTS
            for component in components[:-1]
        )
    )


def _is_multi_entry_raw_parameter_container_leaf(normalized_leaf: str) -> bool:
    return _is_query_audit_parameter_container_leaf(
        normalized_leaf
    ) or _is_raw_audit_parameter_container_leaf(normalized_leaf)


def _is_raw_query_audit_parameter_value_key(key_path: str) -> bool:
    return AuditParameterContext(key_path).is_raw_query_value


def _is_query_audit_parameter_entry_key(key: str) -> bool:
    parent_key = key.rsplit(".", 1)[0]
    parent_leaf = _normalize_parameter_key(_parameter_key_leaf(parent_key))
    return _is_query_audit_parameter_container_leaf(parent_leaf)


def _is_cookie_audit_parameter_container_leaf(normalized_leaf: str) -> bool:
    return normalized_leaf in {"cookies", "extra_cookies"} or normalized_leaf.endswith("_cookies")


def _json_sort_key(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return repr(value)


def _json_encoded_audit_metadata_value(value: str, key_path: str) -> object:
    normalized_leaf = _normalize_parameter_key(_parameter_key_leaf(key_path))
    if normalized_leaf not in _JSON_ENCODED_AUDIT_METADATA_KEYS:
        return _JSON_METADATA_NOT_DECODED
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{key_path} must include valid JSON audit metadata") from exc


def _is_json_encoded_audit_metadata_key_path(key_path: str) -> bool:
    return AuditParameterContext(key_path).is_json_encoded_metadata


def _is_unsafe_json_encoded_metadata_scalar_path(key_path: str) -> bool:
    components = _audit_parameter_path_components(key_path)
    if not any(component in _JSON_ENCODED_AUDIT_METADATA_KEYS for component in components):
        return False
    if _is_json_schema_audit_parameter_path(components):
        return False
    leaf = key_path.rsplit(".", 1)[-1]
    has_index_suffix = _PARAMETER_INDEX_SUFFIX_RE.search(leaf) is not None
    normalized_leaf = _normalize_parameter_key(_PARAMETER_INDEX_SUFFIX_RE.sub("", leaf))
    if normalized_leaf in _JSON_ENCODED_AUDIT_METADATA_KEYS and not has_index_suffix:
        return False
    return True


def _redact_json_encoded_audit_metadata_value(value: str, key_path: str) -> str | None:
    decoded_value = _json_encoded_audit_metadata_value(value, key_path)
    if decoded_value is _JSON_METADATA_NOT_DECODED or not isinstance(decoded_value, (Mapping, list)):
        return None
    redacted_value = _redact_audit_parameters(decoded_value, key_path=key_path)
    if redacted_value == decoded_value:
        return value
    return json.dumps(redacted_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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
        or _is_content_bearing_raw_query_text(parameter_value, _depth=_depth + 1)
        for _key, parameter_value in url_pairs
        if _depth < _MAX_AUDIT_PARAMETER_DECODE_DEPTH
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
        or _is_credential_bearing_raw_query_text(parameter_value, _depth=_depth + 1)
        for _key, parameter_value in url_pairs
        if _depth < _MAX_AUDIT_PARAMETER_DECODE_DEPTH
    )


def _is_absolute_url(value: str) -> bool:
    parsed_url = urlparse(value)
    return bool(parsed_url.scheme and parsed_url.netloc)


def _is_data_url(value: str) -> bool:
    return urlparse(value).scheme.lower() == "data"


def _is_secret_url_parameter_key(key: str) -> bool:
    return _is_secret_parameter_key(key) or _normalize_parameter_key(key) in {"code", "key"}


def _is_content_bearing_raw_query_text(value: str, *, _depth: int = 0) -> bool:
    if not _has_raw_query_key_value_separator(value):
        return False
    if _depth >= _MAX_AUDIT_PARAMETER_DECODE_DEPTH:
        # Let the redaction pass handle overly deep raw-query chains.
        return False
    raw_pairs = _raw_query_parameter_pairs(value)
    return any(
        _is_content_bearing_audit_parameter_key(key)
        for key, _parameter_value in raw_pairs
    ) or any(
        _is_content_bearing_url(parameter_value, _depth=_depth + 1)
        or _is_content_bearing_raw_query_text(parameter_value, _depth=_depth + 1)
        for _key, parameter_value in raw_pairs
    )


def _is_credential_bearing_raw_query_text(value: str, *, _depth: int = 0) -> bool:
    if not _has_raw_query_key_value_separator(value):
        return False
    if _depth >= _MAX_AUDIT_PARAMETER_DECODE_DEPTH:
        # Redact ambiguous raw-query chains instead of recursing without a bound.
        return True
    raw_pairs = _raw_query_parameter_pairs(value)
    return any(
        _is_secret_url_parameter_key(key)
        for key, _parameter_value in raw_pairs
    ) or any(
        _is_credential_bearing_url(parameter_value, _depth=_depth + 1)
        or _is_credential_bearing_raw_query_text(parameter_value, _depth=_depth + 1)
        for _key, parameter_value in raw_pairs
    )


def _has_raw_query_key_value_separator(value: str) -> bool:
    return any(
        _raw_key_value_parameter_separator(value_form) is not None
        for value_form in _decoded_query_parameter_value_forms(value)
    )


def _raw_query_parameter_pairs(value: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for chunk in re.split(r"[&;]", value):
        for value_form in _decoded_query_parameter_value_forms(chunk):
            separator = _raw_key_value_parameter_separator(value_form)
            if separator is None:
                continue
            key, found, parameter_value = value_form.partition(separator)
            if found and key.strip() and parameter_value.strip():
                pairs.append((key.strip(), parameter_value.strip()))
    return pairs


def _url_parameter_pairs(value: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for chunk in re.split(r"[&;]", value):
        for key, parameter_value in parse_qsl(chunk, keep_blank_values=True):
            pairs.extend(
                (decoded_key, decoded_value)
                for decoded_key in _decoded_query_parameter_value_forms(key)
                for decoded_value in _decoded_query_parameter_value_forms(parameter_value)
            )
    return pairs


def _decoded_query_parameter_value_forms(value: str) -> tuple[str, ...]:
    decoded_values = [value]
    for _index in range(_MAX_AUDIT_PARAMETER_DECODE_DEPTH):
        decoded_value = unquote_plus(decoded_values[-1])
        if decoded_value == decoded_values[-1]:
            break
        decoded_values.append(decoded_value)
    return tuple(decoded_values)


def _contains_component_sequence(components: tuple[str, ...], sequence: tuple[str, ...]) -> bool:
    if len(sequence) > len(components):
        return False
    return any(
        components[index : index + len(sequence)] == sequence
        for index in range(len(components) - len(sequence) + 1)
    )


def _ends_with_component_sequence(
    components: tuple[str, ...],
    sequences: tuple[tuple[str, ...], ...],
) -> bool:
    return any(
        len(sequence) <= len(components) and components[-len(sequence) :] == sequence
        for sequence in sequences
    )
