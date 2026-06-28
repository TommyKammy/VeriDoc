from __future__ import annotations

import copy
import re
from typing import Any, Iterable


_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_ACTIVE_STATUS = "active"
_INACTIVE_STATUS = "inactive"
_SUPPORTED_STATUSES = {_ACTIVE_STATUS, _INACTIVE_STATUS}


class TemplateVersionError(ValueError):
    """Raised when template version metadata cannot be trusted."""


class TemplateVersionRegistry:
    """Fail-closed selector for versioned template definitions."""

    def __init__(self, templates: Iterable[dict[str, Any]]) -> None:
        self._templates_by_id: dict[str, dict[str, dict[str, Any]]] = {}
        for index, template in enumerate(templates):
            template_id = _required_string(template, "template_id", index)
            version = _required_string(template, "version", index)
            _version_key(version, index)
            _validate_status(template, index)
            _validate_effective_metadata(template, index)

            versions = self._templates_by_id.setdefault(template_id, {})
            if version in versions:
                raise TemplateVersionError(
                    f"duplicate template version for template_id {template_id!r}: {version!r}"
                )
            versions[version] = copy.deepcopy(template)

    def select_active(self, template_id: str, *, as_of: str | None = None) -> dict[str, Any]:
        if template_id not in self._templates_by_id:
            raise TemplateVersionError(f"template_id {template_id!r} is not registered")

        active_versions = [
            template
            for template in self._templates_by_id[template_id].values()
            if template.get("status") == _ACTIVE_STATUS and _is_effective_at(template, as_of)
        ]
        if not active_versions:
            raise TemplateVersionError(f"template_id {template_id!r} has no active template version")

        selected = max(active_versions, key=lambda template: _version_key(str(template["version"])))
        return copy.deepcopy(selected)

    def get_version(
        self,
        template_id: str,
        version: str,
        *,
        include_inactive: bool = False,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        try:
            template = self._templates_by_id[template_id][version]
        except KeyError as exc:
            raise TemplateVersionError(
                f"template_id {template_id!r} version {version!r} is not registered"
            ) from exc

        if template.get("status") != _ACTIVE_STATUS and not include_inactive:
            raise TemplateVersionError(
                f"template_id {template_id!r} version {version!r} is inactive"
            )
        if not _is_effective_at(template, as_of):
            raise TemplateVersionError(
                f"template_id {template_id!r} version {version!r} is not effective"
            )
        return copy.deepcopy(template)

    def selection_metadata(self, template_id: str, *, as_of: str | None = None) -> dict[str, str]:
        selected = self.select_active(template_id, as_of=as_of)
        return {
            "template_id": str(selected["template_id"]),
            "version": str(selected["version"]),
            "status": str(selected["status"]),
            "selected_by": "highest_active_version",
        }


def _required_string(template: dict[str, Any], key: str, index: int) -> str:
    value = template.get(key)
    if not isinstance(value, str) or not value:
        raise TemplateVersionError(f"template[{index}].{key} must be a non-empty string")
    return value


def _validate_status(template: dict[str, Any], index: int) -> None:
    status = template.get("status")
    if status not in _SUPPORTED_STATUSES:
        raise TemplateVersionError(
            f"template[{index}].status must be one of {sorted(_SUPPORTED_STATUSES)!r}"
        )


def _validate_effective_metadata(template: dict[str, Any], index: int) -> None:
    effective = template.get("effective")
    if not isinstance(effective, dict):
        raise TemplateVersionError(f"template[{index}].effective must be an object")
    effective_from = effective.get("from")
    if not isinstance(effective_from, str) or not effective_from:
        raise TemplateVersionError(f"template[{index}].effective.from must be a non-empty string")
    effective_until = effective.get("until")
    if effective_until is not None and (not isinstance(effective_until, str) or not effective_until):
        raise TemplateVersionError(f"template[{index}].effective.until must be a non-empty string")
    if isinstance(effective_until, str) and effective_until <= effective_from:
        raise TemplateVersionError(f"template[{index}].effective.until must be after effective.from")


def _is_effective_at(template: dict[str, Any], as_of: str | None) -> bool:
    if as_of is None:
        return True
    effective = template["effective"]
    effective_from = effective["from"]
    effective_until = effective.get("until")
    return effective_from <= as_of and (effective_until is None or as_of < effective_until)


def _version_key(version: str, index: int | None = None) -> tuple[int, int, int]:
    match = _SEMVER_RE.fullmatch(version)
    if match is None:
        prefix = f"template[{index}].version" if index is not None else "version"
        raise TemplateVersionError(f"{prefix} must use major.minor.patch numeric versioning")
    return tuple(int(part) for part in match.groups())
