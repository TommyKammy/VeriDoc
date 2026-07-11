"""Stable warning codes exposed by the PoC API and review UI."""

from __future__ import annotations

import re
from typing import Any


WARNING_DEFINITIONS: dict[str, dict[str, str]] = {
    "DOCUMENT_BBOX_MISSING": {"severity": "warning", "remediation": "Confirm the source location and enter the missing bounding box."},
    "DOCUMENT_LOW_CONFIDENCE": {"severity": "warning", "remediation": "Compare the extracted value with the source document before approval."},
    "DOCUMENT_CONFIDENCE_MISSING": {"severity": "warning", "remediation": "Review the extracted value because no confidence score is available."},
    "DOCUMENT_CONFIDENCE_INVALID": {"severity": "error", "remediation": "Correct the invalid confidence value and review the extraction."},
    "DOCUMENT_TEXT_EMPTY": {"severity": "error", "remediation": "Compare with the source and supply the missing text before approval."},
    "DOCUMENT_PARSER_REVIEW_REQUIRED": {"severity": "warning", "remediation": "Review the parser-marked block against the source document."},
    "LLM_FALLBACK_SCHEMA_INVALID": {"severity": "error", "remediation": "Review the rejected LLM plan and correct its schema before retrying."},
    "LLM_FALLBACK_UNAVAILABLE": {"severity": "warning", "remediation": "Configure a trusted local LLM prerequisite or review the deterministic result."},
    "CONVERSION_SETTING_UNSUPPORTED": {"severity": "warning", "remediation": "Disable the unsupported setting or use a supported conversion path."},
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
    if "llm_fallback_schema_invalid" in message:
        return "LLM_FALLBACK_SCHEMA_INVALID"
    if "LLM conversion plan fallback" in message:
        return "LLM_FALLBACK_UNAVAILABLE"
    if message == "OCR conversion setting is not implemented in the local PoC API":
        return "CONVERSION_SETTING_UNSUPPORTED"
    return "UNCLASSIFIED_WARNING"
