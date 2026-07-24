"""Stable warning codes exposed by the PoC API and review UI."""

from __future__ import annotations

import re
from typing import Any


OCR_TEXT_LAYER_UNAVAILABLE_MESSAGE = (
    "PDF page has no extractable text layer; OCR is unavailable in the MVP"
)
OCR_TRUSTED_WORKFLOW_REMEDIATION = (
    "Route the source through a separately approved trusted OCR workflow "
    "before accepting extracted text."
)


WARNING_DEFINITIONS: dict[str, dict[str, str]] = {
    "DOCUMENT_BBOX_MISSING": {"severity": "warning", "remediation": "Confirm the source location and enter the missing bounding box."},
    "DOCUMENT_LOW_CONFIDENCE": {"severity": "warning", "remediation": "Compare the extracted value with the source document before approval."},
    "DOCUMENT_CONFIDENCE_MISSING": {"severity": "warning", "remediation": "Review the extracted value because no confidence score is available."},
    "DOCUMENT_CONFIDENCE_INVALID": {"severity": "error", "remediation": "Correct the invalid confidence value and review the extraction."},
    "DOCUMENT_TEXT_EMPTY": {"severity": "error", "remediation": "Compare with the source and supply the missing text before approval."},
    "DOCUMENT_PARSER_REVIEW_REQUIRED": {"severity": "warning", "remediation": "Review the parser-marked block against the source document."},
    "LLM_FALLBACK_SCHEMA_INVALID": {"severity": "error", "remediation": "Review the rejected LLM plan and correct its schema before retrying."},
    "LLM_FALLBACK_UNTRUSTED_ENDPOINT": {"severity": "error", "remediation": "Configure a local-only LLM endpoint before retrying."},
    "LLM_FALLBACK_UNTRUSTED_CREDENTIAL": {"severity": "error", "remediation": "Configure a trusted LLM credential before retrying."},
    "LLM_FALLBACK_MISSING_MODEL": {"severity": "warning", "remediation": "Configure the required local LLM model or review the deterministic result."},
    "LLM_FALLBACK_UNAVAILABLE": {"severity": "warning", "remediation": "Configure a trusted local LLM prerequisite or review the deterministic result."},
    "LLM_CONFIGURATION_ENDPOINT_REJECTED": {"severity": "error", "remediation": "Configure a local-only LLM endpoint before retrying."},
    "LLM_CONFIGURATION_CREDENTIAL_REJECTED": {"severity": "error", "remediation": "Replace the placeholder API key with a trusted credential before retrying."},
    "LLM_CONFIGURATION_MODEL_REQUIRED": {"severity": "error", "remediation": "Configure the required local LLM model before retrying."},
    "LLM_CONFIGURATION_INVALID": {"severity": "error", "remediation": "Correct the local LLM profile configuration before retrying."},
    "CONVERSION_SETTING_UNSUPPORTED": {"severity": "warning", "remediation": "Disable the unsupported setting or use a supported conversion path."},
    "OCR_TEXT_LAYER_UNAVAILABLE": {
        "severity": "error",
        "remediation": OCR_TRUSTED_WORKFLOW_REMEDIATION,
    },
    "UNCLASSIFIED_WARNING": {"severity": "warning", "remediation": "Review the warning and source evidence before approval."},
}

_DOCUMENT_WARNING_CODES = {
    "bbox missing": "DOCUMENT_BBOX_MISSING",
    "low confidence": "DOCUMENT_LOW_CONFIDENCE",
    "confidence missing": "DOCUMENT_CONFIDENCE_MISSING",
    "confidence invalid": "DOCUMENT_CONFIDENCE_INVALID",
    "text empty": "DOCUMENT_TEXT_EMPTY",
    "parser marked block requires_review": "DOCUMENT_PARSER_REVIEW_REQUIRED",
}

_LLM_FALLBACK_CODES = {
    "llm_fallback_schema_invalid": "LLM_FALLBACK_SCHEMA_INVALID",
    "llm_fallback_untrusted_endpoint": "LLM_FALLBACK_UNTRUSTED_ENDPOINT",
    "llm_fallback_untrusted_credential": "LLM_FALLBACK_UNTRUSTED_CREDENTIAL",
    "llm_fallback_missing_model": "LLM_FALLBACK_MISSING_MODEL",
    "llm_fallback_unavailable": "LLM_FALLBACK_UNAVAILABLE",
}

_LLM_CONFIGURATION_WARNING_CODES = {
    "LLM conversion blocked: configured endpoint must be local-only": "LLM_CONFIGURATION_ENDPOINT_REJECTED",
    "LLM conversion blocked: configured API key is not trusted": "LLM_CONFIGURATION_CREDENTIAL_REJECTED",
    "LLM conversion blocked: configured model is required": "LLM_CONFIGURATION_MODEL_REQUIRED",
    "LLM conversion blocked: configured local LLM profile is invalid": "LLM_CONFIGURATION_INVALID",
}


def warning_detail(warning: Any) -> dict[str, str]:
    """Return the stable public representation for a warning value."""
    if isinstance(warning, dict):
        code = warning.get("code")
        message = warning.get("message")
        if isinstance(code, str) and code in WARNING_DEFINITIONS and isinstance(message, str):
            definition = WARNING_DEFINITIONS[code]
            return {"code": code, "severity": definition["severity"], "message": message, "remediation": definition["remediation"]}
    message = str(warning)
    code = _warning_code(message)
    definition = WARNING_DEFINITIONS[code]
    return {"code": code, "severity": definition["severity"], "message": message, "remediation": definition["remediation"]}


def warning_details(warnings: list[Any]) -> list[dict[str, str]]:
    return [warning_detail(warning) for warning in warnings]


def _warning_code(message: str) -> str:
    document_match = re.search(r"blocks\[\d+\]\.(.+?)(?:; block marked requires_review)?$", message)
    if document_match:
        code = _DOCUMENT_WARNING_CODES.get(document_match.group(1))
        if code:
            return code
    if "LLM conversion plan fallback" in message:
        for warning_token, code in _LLM_FALLBACK_CODES.items():
            if warning_token in message:
                return code
    configuration_code = _LLM_CONFIGURATION_WARNING_CODES.get(message)
    if configuration_code:
        return configuration_code
    if message == "OCR conversion setting is not implemented in the local PoC API":
        return "CONVERSION_SETTING_UNSUPPORTED"
    return "UNCLASSIFIED_WARNING"
