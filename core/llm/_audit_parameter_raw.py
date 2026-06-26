from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, unquote_plus, urlparse


RawEntry = tuple[str, str, str]


@dataclass(frozen=True)
class RawAuditParameterPolicy:
    normalize_parameter_key: Callable[[str], str]
    parameter_key_leaf: Callable[[str], str]
    join_parameter_key_path: Callable[[str, str], str]
    is_query_container_leaf: Callable[[str], bool]
    is_raw_container_leaf: Callable[[str], bool]
    is_multi_entry_raw_container_leaf: Callable[[str], bool]
    is_key_value_sequence_container_key: Callable[[str], bool]
    is_cookie_container_leaf: Callable[[str], bool]
    is_raw_query_value_key: Callable[[str], bool]
    is_secret_parameter_key: Callable[[str], bool]
    is_content_bearing_parameter_key: Callable[[str], bool]
    is_invalid_safe_metadata_value: Callable[[str, object], bool]
    is_safe_schema_literal_string: Callable[[str], bool]
    redact_audit_parameters: Callable[[object, str], object]


@dataclass(frozen=True)
class RawKeyValueParameterContext:
    key_path: str
    normalized_leaf: str
    split_query_pairs: bool = False

    @classmethod
    def from_key_path(
        cls,
        key_path: str,
        *,
        normalized_leaf: str,
        split_query_pairs: bool,
    ) -> RawKeyValueParameterContext:
        return cls(
            key_path=key_path,
            normalized_leaf=normalized_leaf,
            split_query_pairs=split_query_pairs,
        )

    def can_split_query_pairs(self, policy: RawAuditParameterPolicy) -> bool:
        return self.split_query_pairs or policy.is_multi_entry_raw_container_leaf(
            self.normalized_leaf
        )

    def can_decode_encoded_pairs(self, policy: RawAuditParameterPolicy) -> bool:
        return self.can_split_query_pairs(policy)

    def is_cookie_container(self, policy: RawAuditParameterPolicy) -> bool:
        return policy.is_cookie_container_leaf(self.normalized_leaf)


def raw_key_value_parameter_line(
    value: str,
    key_path: str,
    policy: RawAuditParameterPolicy,
) -> RawEntry | None:
    if not policy.is_key_value_sequence_container_key(key_path):
        return None
    if is_absolute_url(value):
        return None
    separator = raw_key_value_parameter_separator(value)
    if separator is not None:
        entry_key, found, entry_value = value.partition(separator)
        if found and entry_key.strip() and entry_value.strip():
            return (entry_key.strip(), found, entry_value.strip())
    return None


def raw_key_value_parameter_separator(value: str) -> str | None:
    positions = [
        (position, separator)
        for separator in ("=", ":")
        if (position := value.find(separator)) >= 0
    ]
    if not positions:
        return None
    return min(positions)[1]


def raw_key_value_parameter_entries(
    value: str,
    key_path: str,
    policy: RawAuditParameterPolicy,
    *,
    normalized_leaf: str,
    split_query_pairs: bool,
) -> list[RawEntry]:
    if not policy.is_key_value_sequence_container_key(key_path):
        return []
    raw_context = RawKeyValueParameterContext.from_key_path(
        key_path,
        normalized_leaf=normalized_leaf,
        split_query_pairs=split_query_pairs,
    )
    return [
        entry
        for chunk in raw_key_value_parameter_chunks(value, raw_context, policy)
        if (entry := raw_key_value_parameter_chunk_entry(chunk, raw_context, policy))
        is not None
    ]


def redact_raw_key_value_parameter_text(
    value: str,
    key_path: str,
    policy: RawAuditParameterPolicy,
    *,
    normalized_leaf: str,
    split_query_pairs: bool,
    redacted_value: str,
) -> str | None:
    if not policy.is_key_value_sequence_container_key(key_path):
        return None
    raw_context = RawKeyValueParameterContext.from_key_path(
        key_path,
        normalized_leaf=normalized_leaf,
        split_query_pairs=split_query_pairs,
    )
    chunks = raw_key_value_parameter_chunks(value, raw_context, policy)
    if len(chunks) == 1 and chunks[0] == value:
        return _redact_single_raw_key_value_parameter_text(
            value,
            raw_context,
            policy,
            redacted_value=redacted_value,
        )
    return _redact_chunked_raw_key_value_parameter_text(
        value,
        chunks,
        raw_context,
        policy,
        redacted_value=redacted_value,
    )


def _redact_single_raw_key_value_parameter_text(
    value: str,
    raw_context: RawKeyValueParameterContext,
    policy: RawAuditParameterPolicy,
    *,
    redacted_value: str,
) -> str | None:
    raw_entry = raw_key_value_parameter_chunk_entry(value, raw_context, policy)
    if raw_entry is None:
        return None
    return redact_raw_key_value_parameter_line(
        raw_entry,
        raw_context.key_path,
        policy,
        redacted_value=redacted_value,
    )


def _redact_chunked_raw_key_value_parameter_text(
    value: str,
    chunks: list[str],
    raw_context: RawKeyValueParameterContext,
    policy: RawAuditParameterPolicy,
    *,
    redacted_value: str,
) -> str | None:
    redacted_chunks = []
    changed = False
    for chunk in chunks:
        raw_entry = raw_key_value_parameter_chunk_entry(chunk, raw_context, policy)
        if raw_entry is None:
            redacted_chunks.append(chunk)
            continue
        redacted_chunk = redact_raw_key_value_parameter_line(
            raw_entry,
            raw_context.key_path,
            policy,
            redacted_value=redacted_value,
        )
        changed = changed or redacted_chunk != chunk
        redacted_chunks.append(redacted_chunk)
    if not changed:
        return None
    return raw_key_value_parameter_joiner(value, raw_context, policy).join(redacted_chunks)


def raw_key_value_parameter_chunk_entry(
    chunk: str,
    raw_context: RawKeyValueParameterContext,
    policy: RawAuditParameterPolicy,
) -> RawEntry | None:
    raw_entry = raw_key_value_parameter_line(chunk, raw_context.key_path, policy)
    if raw_entry is not None:
        return raw_entry
    if not raw_context.can_decode_encoded_pairs(policy):
        return None
    decoded_chunk = fully_decode_query_parameter_value(chunk)
    if decoded_chunk == chunk:
        return None
    return raw_key_value_parameter_line(decoded_chunk, raw_context.key_path, policy)


def raw_key_value_parameter_chunks(
    value: str,
    raw_context: RawKeyValueParameterContext,
    policy: RawAuditParameterPolicy,
) -> list[str]:
    if "\n" in value or "\r" in value:
        return value.splitlines()
    if raw_context.can_split_query_pairs(policy) and ("&" in value or ";" in value):
        return re.split(r"[&;]", value)
    if raw_context.is_cookie_container(policy) and ";" in value:
        return [chunk.strip() for chunk in value.split(";")]
    return [value]


def raw_key_value_parameter_joiner(
    value: str,
    raw_context: RawKeyValueParameterContext,
    policy: RawAuditParameterPolicy,
) -> str:
    if "\n" in value or "\r" in value:
        return "\n"
    if raw_context.can_split_query_pairs(policy) and "&" in value:
        return "&"
    if raw_context.can_split_query_pairs(policy) and ";" in value:
        return ";"
    if raw_context.is_cookie_container(policy) and ";" in value:
        return "; "
    return ""


def redact_raw_key_value_parameter_line(
    raw_entry: RawEntry,
    key_path: str,
    policy: RawAuditParameterPolicy,
    *,
    redacted_value: str,
) -> str:
    entry_key, separator, entry_value = raw_entry
    entry_path = raw_key_value_parameter_entry_path(key_path, entry_key, policy)
    entry_parameter_value = raw_key_value_parameter_entry_value(
        key_path,
        entry_value,
        policy,
    )
    separator_text = ": " if separator == ":" else separator
    if (
        policy.is_raw_query_value_key(key_path)
        and not policy.is_key_value_sequence_container_key(entry_path)
        and is_credential_bearing_raw_query_text(entry_parameter_value, policy)
    ):
        return f"{entry_key}{separator_text}{redacted_value}"
    redacted_entry_value = policy.redact_audit_parameters(
        entry_parameter_value,
        entry_path,
    )
    if redacted_entry_value == redacted_value:
        return f"{entry_key}{separator_text}{redacted_value}"
    if (
        isinstance(redacted_entry_value, str)
        and redacted_entry_value != entry_parameter_value
    ):
        return f"{entry_key}{separator_text}{redacted_entry_value}"
    return f"{entry_key}{separator_text}{entry_value}"


def raw_key_value_parameter_entry_path(
    key_path: str,
    entry_key: str,
    policy: RawAuditParameterPolicy,
) -> str:
    return policy.join_parameter_key_path(
        key_path,
        raw_key_value_parameter_entry_key(key_path, entry_key, policy),
    )


def raw_key_value_parameter_entry_key(
    key_path: str,
    entry_key: str,
    policy: RawAuditParameterPolicy,
) -> str:
    normalized_leaf = policy.normalize_parameter_key(policy.parameter_key_leaf(key_path))
    if policy.is_query_container_leaf(normalized_leaf):
        return fully_decode_query_parameter_value(entry_key)
    if policy.is_raw_container_leaf(normalized_leaf):
        return fully_decode_query_parameter_value(entry_key)
    return entry_key


def raw_key_value_parameter_entry_value(
    key_path: str,
    entry_value: str,
    policy: RawAuditParameterPolicy,
) -> str:
    normalized_leaf = policy.normalize_parameter_key(policy.parameter_key_leaf(key_path))
    if policy.is_query_container_leaf(
        normalized_leaf
    ) or policy.is_raw_container_leaf(normalized_leaf):
        return fully_decode_query_parameter_value(entry_value)
    return entry_value


def fully_decode_query_parameter_value(value: str) -> str:
    decoded_value = value
    for _index in range(_MAX_AUDIT_PARAMETER_DECODE_DEPTH):
        next_value = unquote(decoded_value)
        if next_value == decoded_value:
            break
        decoded_value = next_value
    return decoded_value


def audit_parameter_url_value_forms(
    value: str,
    key_path: str,
    policy: RawAuditParameterPolicy,
) -> tuple[str, ...]:
    decoded_value = fully_decode_query_parameter_value(value)
    if decoded_value == value:
        return (value,)
    if should_decode_url_audit_parameter_value(key_path, policy) and (
        is_absolute_url(decoded_value) or is_data_url(decoded_value)
    ):
        return (value, decoded_value)
    return (value,)


def should_decode_url_audit_parameter_value(
    key_path: str,
    policy: RawAuditParameterPolicy,
) -> bool:
    leaf = policy.normalize_parameter_key(policy.parameter_key_leaf(key_path))
    parent_key = key_path.rsplit(".", 1)[0] if "." in key_path else ""
    parent_leaf = policy.normalize_parameter_key(policy.parameter_key_leaf(parent_key))
    return (
        policy.is_query_container_leaf(parent_leaf)
        or policy.is_raw_container_leaf(parent_leaf)
        or policy.is_query_container_leaf(leaf)
        or policy.is_raw_container_leaf(leaf)
        or is_url_value_audit_parameter_leaf(leaf)
    )


def should_decode_structured_raw_audit_parameter_value(
    key_path: str,
    policy: RawAuditParameterPolicy,
) -> bool:
    parent_key = key_path.rsplit(".", 1)[0]
    parent_leaf = policy.normalize_parameter_key(policy.parameter_key_leaf(parent_key))
    return policy.is_query_container_leaf(parent_leaf) or policy.is_raw_container_leaf(
        parent_leaf
    )


def is_url_value_audit_parameter_leaf(normalized_leaf: str) -> bool:
    components = tuple(normalized_leaf.split("_"))
    return bool(components) and components[-1] in {"url", "uri"}


def is_content_bearing_url(
    value: str,
    policy: RawAuditParameterPolicy,
    *,
    _depth: int = 0,
) -> bool:
    parsed_url = urlparse(value)
    if parsed_url.scheme.lower() == "data":
        return True
    if not parsed_url.scheme or not parsed_url.netloc:
        return False
    url_pairs = url_parameter_pairs(parsed_url.query) + url_parameter_pairs(
        parsed_url.fragment
    )
    return any(
        is_content_bearing_query_parameter_pair(key, parameter_value, policy)
        for key, parameter_value in url_pairs
    ) or any(
        is_content_bearing_url(parameter_value, policy, _depth=_depth + 1)
        or is_content_bearing_raw_query_text(parameter_value, policy, _depth=_depth + 1)
        for _key, parameter_value in url_pairs
        if _depth < _MAX_AUDIT_PARAMETER_DECODE_DEPTH
    )


def is_credential_bearing_url(
    value: str,
    policy: RawAuditParameterPolicy,
    *,
    _depth: int = 0,
) -> bool:
    parsed_url = urlparse(value)
    if not parsed_url.scheme or not parsed_url.netloc:
        return False
    if parsed_url.username is not None or parsed_url.password is not None:
        return True
    url_pairs = url_parameter_pairs(parsed_url.query) + url_parameter_pairs(
        parsed_url.fragment
    )
    if any(is_secret_url_parameter_key(key, policy) for key, _value in url_pairs):
        return True
    return any(
        is_credential_bearing_url(parameter_value, policy, _depth=_depth + 1)
        or is_credential_bearing_raw_query_text(
            parameter_value,
            policy,
            _depth=_depth + 1,
        )
        for _key, parameter_value in url_pairs
        if _depth < _MAX_AUDIT_PARAMETER_DECODE_DEPTH
    )


def is_absolute_url(value: str) -> bool:
    parsed_url = urlparse(value)
    return bool(parsed_url.scheme and parsed_url.netloc)


def is_data_url(value: str) -> bool:
    return urlparse(value).scheme.lower() == "data"


def is_secret_url_parameter_key(key: str, policy: RawAuditParameterPolicy) -> bool:
    return policy.is_secret_parameter_key(key) or policy.normalize_parameter_key(key) in {
        "code",
        "key",
    }


def is_content_bearing_raw_query_text(
    value: str,
    policy: RawAuditParameterPolicy,
    *,
    _depth: int = 0,
) -> bool:
    if not has_raw_query_key_value_separator(value):
        return False
    if _depth >= _MAX_AUDIT_PARAMETER_DECODE_DEPTH:
        # Let the redaction pass handle overly deep raw-query chains.
        return False
    raw_pairs = raw_query_parameter_pairs(value)
    return any(
        is_content_bearing_query_parameter_pair(key, parameter_value, policy)
        for key, parameter_value in raw_pairs
    ) or any(
        is_content_bearing_url(parameter_value, policy, _depth=_depth + 1)
        or is_content_bearing_raw_query_text(
            parameter_value,
            policy,
            _depth=_depth + 1,
        )
        for _key, parameter_value in raw_pairs
    )


def is_content_bearing_real_raw_query_text(
    value: str,
    policy: RawAuditParameterPolicy,
    *,
    _depth: int = 0,
) -> bool:
    if not has_raw_query_key_value_separator(value):
        return False
    if _depth >= _MAX_AUDIT_PARAMETER_DECODE_DEPTH:
        return False
    raw_pairs = raw_query_parameter_pairs(value)
    return any(
        is_content_bearing_real_raw_query_parameter_pair(key, parameter_value, policy)
        or is_content_bearing_url(parameter_value, policy, _depth=_depth + 1)
        or is_content_bearing_real_raw_query_text(
            parameter_value,
            policy,
            _depth=_depth + 1,
        )
        for key, parameter_value in raw_pairs
    )


def is_content_bearing_query_parameter_pair(
    key: str,
    value: str,
    policy: RawAuditParameterPolicy,
) -> bool:
    if is_secret_url_parameter_key(key, policy):
        return False
    return policy.is_content_bearing_parameter_key(
        key
    ) or policy.is_invalid_safe_metadata_value(key, value)


def is_content_bearing_real_raw_query_parameter_pair(
    key: str,
    value: str,
    policy: RawAuditParameterPolicy,
) -> bool:
    if is_safe_real_raw_query_metadata_pair(key, value, policy):
        return False
    return is_content_bearing_query_parameter_pair(key, value, policy)


def is_safe_real_raw_query_metadata_pair(
    key: str,
    value: str,
    policy: RawAuditParameterPolicy,
) -> bool:
    normalized_key = policy.normalize_parameter_key(key)
    return normalized_key in {"message", "mode", "output", "status"} and (
        policy.is_safe_schema_literal_string(value)
    )


def is_credential_bearing_raw_query_text(
    value: str,
    policy: RawAuditParameterPolicy,
    *,
    _depth: int = 0,
) -> bool:
    if not has_raw_query_key_value_separator(value):
        return False
    if _depth >= _MAX_AUDIT_PARAMETER_DECODE_DEPTH:
        # Redact ambiguous raw-query chains instead of recursing without a bound.
        return True
    raw_pairs = raw_query_parameter_pairs(value)
    return any(
        is_secret_url_parameter_key(key, policy) for key, _parameter_value in raw_pairs
    ) or any(
        is_credential_bearing_url(parameter_value, policy, _depth=_depth + 1)
        or is_credential_bearing_raw_query_text(
            parameter_value,
            policy,
            _depth=_depth + 1,
        )
        for _key, parameter_value in raw_pairs
    )


def has_raw_query_key_value_separator(value: str) -> bool:
    return any(
        raw_key_value_parameter_separator(value_form) is not None
        for value_form in decoded_query_parameter_value_forms(value)
    )


def raw_query_parameter_pairs(value: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for chunk in re.split(r"[&;]", value):
        for value_form in decoded_query_parameter_value_forms(chunk):
            separator = raw_key_value_parameter_separator(value_form)
            if separator is None:
                continue
            key, found, parameter_value = value_form.partition(separator)
            if found and key.strip() and parameter_value.strip():
                pairs.append((key.strip(), parameter_value.strip()))
    return pairs


def url_parameter_pairs(value: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for chunk in re.split(r"[&;]", value):
        for key, parameter_value in parse_qsl(chunk, keep_blank_values=True):
            pairs.extend(
                (decoded_key, decoded_value)
                for decoded_key in decoded_query_parameter_value_forms(key)
                for decoded_value in decoded_query_parameter_value_forms(parameter_value)
            )
    return pairs


def decoded_query_parameter_value_forms(value: str) -> tuple[str, ...]:
    decoded_values = [value]
    for _index in range(_MAX_AUDIT_PARAMETER_DECODE_DEPTH):
        decoded_value = unquote_plus(decoded_values[-1])
        if decoded_value == decoded_values[-1]:
            break
        decoded_values.append(decoded_value)
    return tuple(decoded_values)


_MAX_AUDIT_PARAMETER_DECODE_DEPTH = 3
