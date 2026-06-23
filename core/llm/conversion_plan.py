from __future__ import annotations

import hashlib
import ipaddress
import json
import http.client
import re
import socket
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from core.llm.audit_parameters import sanitize_audit_parameters


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
    sanitized_parameters = sanitize_audit_parameters(parameters)

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
        "parameters": sanitized_parameters,
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
