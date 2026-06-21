from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


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
_LOCAL_RUNTIME_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    )
)
_SUPPORTED_ACTIONS = {"extract_field", "extract_table", "normalize_value", "flag_review"}


class ConversionPlanValidationError(ValueError):
    """Raised when local LLM output is not an acceptable conversion plan."""


class LocalLLMConfigurationError(ValueError):
    """Raised when the local LLM adapter would violate the local-only boundary."""


@dataclass(frozen=True)
class LocalLLMConversionPlanAdapter:
    """Minimal OpenAI-compatible local LLM adapter for JSON Schema plans."""

    base_url: str
    model: str
    api_key: str | None = None
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
        if not _is_local_base_url(self.base_url):
            raise LocalLLMConfigurationError("base_url must target a local-only OpenAI-compatible endpoint")
        if self.api_key is not None and _is_placeholder_secret(self.api_key):
            raise LocalLLMConfigurationError("placeholder API keys are not valid local LLM credentials")

    def create_conversion_plan(self, synthetic_text: str) -> JsonObject:
        if not synthetic_text.strip():
            raise ValueError("synthetic_text is required")
        if not _is_local_base_url(self.base_url):
            raise LocalLLMConfigurationError("base_url must target a local-only OpenAI-compatible endpoint")

        payload: JsonObject = {
            "model": self.model,
            "temperature": 0,
            "stream": False,
            "max_tokens": self.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "veridoc_conversion_plan",
                    "strict": True,
                    "schema": CONVERSION_PLAN_SCHEMA,
                },
            },
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only JSON that matches the supplied schema. "
                        "Use synthetic input only and keep external_transmission false."
                    ),
                },
                {
                    "role": "user",
                    "content": synthetic_text,
                },
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        transport = self.transport or _urllib_transport
        response = transport(_chat_completions_url(self.base_url), payload, headers, self.timeout_seconds)
        plan = _extract_json_content(response)
        validate_conversion_plan(plan)
        return plan


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
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ConversionPlanValidationError("LLM response did not contain choices[0].message.content") from exc

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


def _urllib_transport(url: str, payload: JsonObject, headers: dict[str, str], timeout_seconds: float) -> JsonObject:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with _local_only_url_opener().open(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"local LLM request failed: {exc}") from exc


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


def _is_local_base_url(base_url: str) -> bool:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    if port == 0:
        return False

    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return _hostname_resolves_to_local_runtime(hostname, port)
    return _is_local_runtime_address(address)


def _hostname_resolves_to_local_runtime(hostname: str, port: int | None) -> bool:
    try:
        address_info = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False

    resolved_addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in address_info:
        sockaddr = info[4]
        if not sockaddr:
            return False
        raw_address = str(sockaddr[0]).split("%", maxsplit=1)[0]
        try:
            resolved_addresses.append(ipaddress.ip_address(raw_address))
        except ValueError:
            return False

    return bool(resolved_addresses) and all(_is_local_runtime_address(address) for address in resolved_addresses)


def _is_local_runtime_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if address.is_link_local:
        return False
    if address.is_loopback:
        return True
    return any(address in network for network in _LOCAL_RUNTIME_NETWORKS)


def _is_placeholder_secret(secret: str) -> bool:
    normalized = secret.strip().lower()
    return not normalized or normalized in _PLACEHOLDER_API_KEYS or "todo" in normalized


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
