#!/usr/bin/env python3
"""Run the repo-owned MVP upload-to-download browser scenario."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import ipaddress
import json
import math
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit
from zipfile import ZipFile

from packaging.requirements import InvalidRequirement, Requirement

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.ir.document_ir_v1 import (
    SCHEMA_VERSION as DOCUMENT_IR_SCHEMA_VERSION,
    UNITS,
)
from services.api.job_queue import JobQueue
from services.api.poc_web import (
    CONVERSION_AUDIT_SCHEMA_VERSION,
    CONVERSION_PLAN_PROMPT_ID,
    CONVERSION_PLAN_PROMPT_VERSION,
    CONVERSION_PLAN_SCHEMA_VERSION,
    JobAuditEventStore,
    PocWebRequestHandler,
    ReviewAuditEventStore,
    TemplateStore,
)
from core.llm.conversion_plan import is_local_llm_base_url

FIXTURE_PATH = (
    REPO_ROOT / "datasets" / "fixtures" / "pdf" / "pdf-to-word-representative.pdf"
)
HIGH_RISK_FIXTURE_PATH = (
    REPO_ROOT
    / "datasets"
    / "fixtures"
    / "templates"
    / "synthetic-batch-template-regression.json"
)
MVP_MANIFEST_PATH = REPO_ROOT / "datasets" / "mvp_evaluation_manifest_v1.json"
FIXTURE_MANIFEST_PATH = REPO_ROOT / "datasets" / "fixtures" / "manifest.json"
INFERENCE_PROFILES_PATH = REPO_ROOT / "services" / "api" / "inference_profiles.json"
DEPENDENCY_ROOT_PATH = REPO_ROOT / "requirements-browser-e2e.txt"
ENDPOINT_ENVIRONMENT_KEYS = (
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
    "VERIDOC_LLM_BASE_URL",
    "VERIDOC_LLM_ENDPOINT",
)
NETWORK_OBSERVATION_SCHEMA_VERSION = "veridoc-network-observation/v1"
BROWSER_EVIDENCE_SCHEMA_VERSION = "veridoc-mvp-browser-e2e/v1"
RERUN_PACKAGE_SCHEMA_VERSION = "veridoc-mvp-rerun-package/v1"
RERUN_PACKAGE_ENVELOPE_SCHEMA_VERSION = "veridoc-mvp-rerun-package-envelope/v1"
RERUN_EQUIVALENCE_RULE = (
    "decision-relevant fields must match; run identity, generated "
    "identifiers, artifact bytes, timestamps, and processing time are excluded"
)
# Keep this tuple aligned with every file role read by evaluate_acceptance_evidence.
ACCEPTANCE_EVIDENCE_FILE_ROLES = (
    "api_result",
    "audit_artifact",
    "download",
    "job_events",
    "job_response",
    "review_events",
)
RETAINED_EVIDENCE_FILENAMES = frozenset(
    {
        "01-recovery.png",
        "02-completed-review.png",
        "03-audit.png",
        "04-keyboard-high-risk-review.png",
        "api-result.json",
        "audit-artifact.json",
        "evidence.json",
        "high-risk-api-result.json",
        "job-events.json",
        "job-response.json",
        "rerun-package.json",
        "review-events.json",
        "trace.zip",
    }
)


class NetworkBoundaryViolation(AssertionError):
    """Raised when the acceptance harness observes an untrusted network boundary."""


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _url_origin(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not host:
        return None
    default_port = 443 if parsed.scheme in {"https", "wss"} else 80
    port = port or default_port
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{port}"


def _is_loopback_host(host: str) -> bool:
    normalized = _normalize_network_host(host)
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _normalize_network_host(host: str) -> str:
    normalized = host.rstrip(".").lower()
    try:
        return ipaddress.ip_address(normalized).compressed
    except ValueError:
        return normalized


def _safe_network_target(url: str) -> str:
    parsed = urlsplit(url)
    origin = _url_origin(url)
    if origin is None:
        return parsed.scheme or "<malformed>"
    return f"{origin}{parsed.path or '/'}"


class LocalNetworkBoundaryObserver:
    """Record and fail closed on network attempts outside explicit local origins."""

    def __init__(self, *, allowed_origins: tuple[str, ...]) -> None:
        normalized = tuple(_url_origin(origin) for origin in allowed_origins)
        if not normalized or any(origin is None for origin in normalized):
            raise ValueError("allowed_origins must contain valid HTTP(S) origins")
        self.allowed_origins = tuple(str(origin) for origin in normalized)
        self._allowed_hosts = {
            _normalize_network_host(str(urlsplit(origin).hostname))
            for origin in self.allowed_origins
        }
        self._allowed_socket_targets = {
            (
                _normalize_network_host(str(parsed.hostname)),
                parsed.port or (443 if parsed.scheme in {"https", "wss"} else 80),
            )
            for origin in self.allowed_origins
            for parsed in (urlsplit(origin),)
        }
        self._attempts: list[dict[str, object]] = []

    def _record(
        self,
        *,
        kind: str,
        target: str,
        source: str,
        allowed: bool,
        method: str | None = None,
    ) -> None:
        attempt: dict[str, object] = {
            "kind": kind,
            "target": target,
            "source": source,
            "allowed": allowed,
        }
        if method is not None:
            attempt["method"] = method.upper()
        self._attempts.append(attempt)

    def observe_http_attempt(self, url: str, *, method: str, source: str) -> None:
        origin = _url_origin(url)
        if origin is None:
            return
        allowed = origin in self.allowed_origins
        target = _safe_network_target(url)
        self._record(
            kind="http",
            target=target,
            source=source,
            allowed=allowed,
            method=method,
        )
        if not allowed:
            raise NetworkBoundaryViolation(
                f"external HTTP attempt blocked: {method.upper()} {target}"
            )

    def observe_dns_attempt(self, host: str, *, source: str) -> None:
        normalized = _normalize_network_host(str(host))
        allowed = normalized in self._allowed_hosts or _is_loopback_host(normalized)
        self._record(
            kind="dns",
            target=normalized,
            source=source,
            allowed=allowed,
        )
        if not allowed:
            raise NetworkBoundaryViolation(
                f"external DNS attempt blocked: {normalized}"
            )

    def observe_socket_attempt(self, address: object, *, source: str) -> None:
        if not isinstance(address, tuple) or not address:
            return
        host = _normalize_network_host(str(address[0]))
        port = address[1] if len(address) > 1 else None
        allowed = (host, port) in self._allowed_socket_targets
        target = f"{host}:{port}" if port is not None else host
        self._record(
            kind="socket",
            target=target,
            source=source,
            allowed=allowed,
        )
        if not allowed:
            raise NetworkBoundaryViolation(
                f"external socket attempt blocked: {target}"
            )

    @contextmanager
    def observe_python_network(self) -> Iterator[None]:
        original_getaddrinfo = socket.getaddrinfo
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        observer = self

        def guarded_getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
            observer.observe_dns_attempt(str(host), source="python_socket")
            return original_getaddrinfo(host, *args, **kwargs)

        def guarded_connect(sock: socket.socket, address: object) -> object:
            observer.observe_socket_attempt(address, source="python_socket")
            return original_connect(sock, address)

        def guarded_connect_ex(sock: socket.socket, address: object) -> int:
            observer.observe_socket_attempt(address, source="python_socket")
            return original_connect_ex(sock, address)

        socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
        socket.socket.connect = guarded_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
        try:
            yield
        finally:
            socket.getaddrinfo = original_getaddrinfo
            socket.socket.connect = original_connect  # type: ignore[method-assign]
            socket.socket.connect_ex = (  # type: ignore[method-assign]
                original_connect_ex
            )

    def install_playwright_guard(self, context: Any) -> None:
        def guard(route: Any, request: Any) -> None:
            try:
                self.observe_http_attempt(
                    request.url,
                    method=request.method,
                    source="playwright",
                )
            except NetworkBoundaryViolation:
                route.abort("blockedbyclient")
                return
            route.continue_()

        context.route("**/*", guard)

    def assert_clean(self) -> None:
        external = [attempt for attempt in self._attempts if not attempt["allowed"]]
        if external:
            raise NetworkBoundaryViolation(
                f"acceptance network boundary recorded {len(external)} external attempt(s)"
            )

    def result(self) -> dict[str, object]:
        http_attempts = [
            attempt for attempt in self._attempts if attempt["kind"] == "http"
        ]
        dns_attempts = [
            attempt for attempt in self._attempts if attempt["kind"] == "dns"
        ]
        external = [attempt for attempt in self._attempts if not attempt["allowed"]]
        external_http = [
            attempt for attempt in external if attempt["kind"] == "http"
        ]
        return {
            "schema_version": NETWORK_OBSERVATION_SCHEMA_VERSION,
            "status": "pass" if not external else "fail",
            "allowed_origins": list(self.allowed_origins),
            "observation_sources": ["playwright", "python_socket"],
            "http_attempt_count": len(http_attempts),
            "dns_attempt_count": len(dns_attempts),
            "external_attempt_count": len(external),
            "external_ai_api_send_count": len(external_http),
            "attempts": list(self._attempts),
        }


def validate_endpoint_configuration(
    environ: dict[str, str] | os._Environ[str],
    *,
    allowed_origins: tuple[str, ...],
    profiles_path: Path = INFERENCE_PROFILES_PATH,
) -> list[dict[str, str]]:
    """Reject configured external or malformed AI/API endpoints."""
    try:
        profiles = json.loads(profiles_path.read_text(encoding="utf-8")).get("profiles")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise NetworkBoundaryViolation(
            "inference profile endpoint configuration is unreadable"
        ) from exc
    if not isinstance(profiles, list):
        raise NetworkBoundaryViolation(
            "inference profile endpoint configuration is malformed"
        )
    profile_endpoint_keys: list[str] = []
    for profile in profiles:
        key = profile.get("base_url_env") if isinstance(profile, dict) else None
        if not isinstance(key, str) or not key:
            raise NetworkBoundaryViolation(
                "inference profile endpoint configuration is malformed"
            )
        profile_endpoint_keys.append(key)

    configured: list[dict[str, str]] = []
    endpoint_keys = dict.fromkeys((*ENDPOINT_ENVIRONMENT_KEYS, *profile_endpoint_keys))
    for key in endpoint_keys:
        raw_value = environ.get(key)
        if not raw_value:
            continue
        origin = _url_origin(raw_value)
        try:
            host = urlsplit(raw_value).hostname
        except ValueError:
            host = None
        if origin is None or host is None:
            raise NetworkBoundaryViolation(
                f"{key} contains a malformed endpoint configuration"
            )
        if not is_local_llm_base_url(raw_value):
            raise NetworkBoundaryViolation(
                f"{key} configures an external endpoint outside the local-only boundary"
            )
        configured.append({"name": key, "origin": origin})
    return configured


def _equivalence_projection(evidence: dict[str, Any]) -> dict[str, object]:
    correlation = evidence.get("correlation", {})
    review_flow = evidence.get("review_flow", {})
    high_risk = review_flow.get("high_risk", {})
    source_jump = review_flow.get("source_jump", {})
    unresolved = review_flow.get("unresolved", {})
    return {
        "schema_version": evidence.get("schema_version"),
        "job": {
            "status": correlation.get("job", {}).get("status"),
            "conversion_status": correlation.get("job", {}).get(
                "conversion_status"
            ),
        },
        "audit_counts": {
            "job_event_count": correlation.get("audit", {}).get("job_event_count"),
            "review_event_count": correlation.get("audit", {}).get(
                "review_event_count"
            ),
        },
        "recovery": {
            "retry_mode": evidence.get("recovery", {}).get("retry_mode"),
            "result": evidence.get("recovery", {}).get("result"),
        },
        "review": {
            "keyboard_only": review_flow.get("keyboard_only"),
            "actions": review_flow.get("actions"),
            "warnings": review_flow.get("warnings"),
            "high_risk": {
                "review_target_count": high_risk.get("review_target_count"),
                "auto_confirmed_count": high_risk.get("auto_confirmed_count"),
                "approval_blocked_while_unresolved": high_risk.get(
                    "approval_blocked_while_unresolved"
                ),
                "approval_block_reason": high_risk.get("approval_block_reason"),
            },
            "source_jump": {
                "source_filename": source_jump.get("source_filename"),
                "source_type": source_jump.get("source_type"),
                "source_sha256": source_jump.get("source_sha256"),
                "page": source_jump.get("page"),
                "review_item_page": source_jump.get("review_item_page"),
                "bbox": source_jump.get("bbox"),
                "review_item_bbox": source_jump.get("review_item_bbox"),
            },
            "unresolved": {
                "blocked_before_approval": unresolved.get(
                    "blocked_before_approval"
                ),
                "state": unresolved.get("state"),
            },
        },
        "network": {
            "status": evidence.get("network_observation", {}).get("status"),
            "external_attempt_count": evidence.get("network_observation", {}).get(
                "external_attempt_count"
            ),
            "external_ai_api_send_count": evidence.get(
                "network_observation", {}
            ).get("external_ai_api_send_count"),
        },
    }


def assert_rerun_equivalent(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> dict[str, object]:
    expected_projection = _equivalence_projection(expected)
    actual_projection = _equivalence_projection(actual)
    expected_sha256 = _canonical_json_sha256(expected_projection)
    actual_sha256 = _canonical_json_sha256(actual_projection)
    equivalent = expected_sha256 == actual_sha256
    comparison = {
        "schema_version": "veridoc-mvp-rerun-equivalence/v1",
        "equivalent": equivalent,
        "rule": "decision-relevant fields match; run identity and timing are excluded",
        "excluded_fields": [
            "run_id",
            "correlation.run_id",
            "processing_time_ms",
            "generated_at",
            "dynamic identifiers and artifact bytes",
        ],
        "expected_sha256": expected_sha256,
        "actual_sha256": actual_sha256,
    }
    if not equivalent:
        raise AssertionError(
            "rerun result is not equivalent under the decision-relevant field rule"
        )
    return comparison


def seal_rerun_package(package: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": RERUN_PACKAGE_ENVELOPE_SCHEMA_VERSION,
        "package_sha256": _canonical_json_sha256(package),
        "package": deepcopy(package),
    }


def validate_rerun_package_envelope(
    envelope: dict[str, Any],
) -> dict[str, Any]:
    package = envelope.get("package")
    recorded_sha256 = envelope.get("package_sha256")
    if (
        envelope.get("schema_version") != RERUN_PACKAGE_ENVELOPE_SCHEMA_VERSION
        or not isinstance(package, dict)
        or not isinstance(recorded_sha256, str)
        or _canonical_json_sha256(package) != recorded_sha256
    ):
        raise ValueError("rerun package integrity check failed")
    if package.get("schema_version") != RERUN_PACKAGE_SCHEMA_VERSION:
        raise ValueError("rerun package schema version is unsupported")
    required_sections = {
        "commit",
        "inputs",
        "configuration",
        "dependencies",
        "versions",
        "commands",
        "equivalence",
    }
    missing = sorted(required_sections - package.keys())
    if missing:
        raise ValueError(
            "rerun package is incomplete; missing: " + ", ".join(missing)
        )
    return package


def _repo_relative(path: Path, repo_root: Path = REPO_ROOT) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def _current_git_commit(repo_root: Path = REPO_ROOT) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    commit = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("git rev-parse did not return a full commit SHA")
    return commit


def _git_status_porcelain(
    repo_root: Path = REPO_ROOT,
    *,
    excluded_paths: tuple[Path, ...] = (),
) -> str:
    command = ["git", "status", "--porcelain=v1", "--untracked-files=all"]
    exclusion_pathspecs: list[str] = []
    resolved_root = repo_root.resolve()
    for excluded_path in excluded_paths:
        try:
            relative_path = excluded_path.resolve().relative_to(resolved_root)
        except ValueError:
            continue
        if not relative_path.parts:
            raise ValueError("cannot exclude the repository root from the clean check")
        exclusion_pathspecs.append(
            f":(exclude,top,literal){relative_path.as_posix()}"
        )
    if exclusion_pathspecs:
        command.extend(("--", ".", *exclusion_pathspecs))
    completed = subprocess.run(
        command,
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _assert_clean_git_checkout(
    repo_root: Path = REPO_ROOT,
    *,
    excluded_paths: tuple[Path, ...] = (),
) -> None:
    if _git_status_porcelain(repo_root, excluded_paths=excluded_paths).strip():
        raise ValueError("rerun package rejected: dirty checkout")


def _retained_evidence_paths(
    rerun_package_path: Path | None,
    *,
    repo_root: Path,
) -> tuple[Path, ...]:
    if rerun_package_path is None:
        return ()
    resolved_package = rerun_package_path.resolve()
    try:
        resolved_package.relative_to(repo_root.resolve())
    except ValueError:
        return (resolved_package,)
    run_dir = resolved_package.parent
    if (
        resolved_package.name != "rerun-package.json"
        or re.fullmatch(r"p12g03-[0-9a-f]{32}", run_dir.name) is None
        or not run_dir.is_dir()
    ):
        return (resolved_package,)
    retained_paths = tuple(sorted(run_dir.iterdir(), key=lambda path: path.name))
    if any(
        not path.is_file()
        or (
            path.name not in RETAINED_EVIDENCE_FILENAMES
            and not path.name.startswith("download-")
        )
        for path in retained_paths
    ):
        return (resolved_package,)
    return retained_paths


def _requirement_files(
    root_path: Path = DEPENDENCY_ROOT_PATH,
    *,
    repo_root: Path = REPO_ROOT,
) -> tuple[Path, ...]:
    discovered: list[Path] = []

    def visit(path: Path) -> None:
        resolved = path.resolve()
        if resolved in discovered:
            return
        resolved.relative_to(repo_root.resolve())
        if not resolved.is_file():
            raise ValueError(f"dependency file is missing: {_repo_relative(resolved)}")
        discovered.append(resolved)
        for raw_line in resolved.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("-r ") or line.startswith("--requirement "):
                child = line.split(maxsplit=1)[1]
                visit(resolved.parent / child)

    visit(root_path)
    return tuple(discovered)


def _dependency_snapshot(
    *,
    browser_version: str,
    repo_root: Path = REPO_ROOT,
) -> dict[str, object]:
    requirement_files = _requirement_files(
        repo_root / "requirements-browser-e2e.txt",
        repo_root=repo_root,
    )
    specifications: list[str] = []
    distribution_names: set[str] = {"playwright"}
    records: list[dict[str, str]] = []
    for path in requirement_files:
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        specifications.extend(lines)
        for line in lines:
            if line.startswith("-"):
                continue
            match = re.match(r"([A-Za-z0-9_.-]+)", line)
            if match:
                distribution_names.add(match.group(1))
        records.append(
            {
                "path": _repo_relative(path, repo_root),
                "sha256": _sha256(path),
            }
        )
    installed_versions: dict[str, str] = {}
    pending = sorted(distribution_names)
    while pending:
        name = pending.pop(0)
        normalized_name = re.sub(r"[-_.]+", "-", name).lower()
        if normalized_name in installed_versions:
            continue
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            installed_versions[normalized_name] = "not-installed"
            continue
        installed_versions[normalized_name] = distribution.version
        for specification in distribution.requires or ():
            try:
                requirement = Requirement(specification)
            except InvalidRequirement as exc:
                raise ValueError(
                    f"installed dependency metadata is invalid: {normalized_name}"
                ) from exc
            if requirement.marker is None or requirement.marker.evaluate():
                dependency_name = re.sub(r"[-_.]+", "-", requirement.name).lower()
                if dependency_name not in installed_versions:
                    pending.append(requirement.name)
    return {
        "requirement_files": records,
        "specifications": specifications,
        "runtime": {
            "python": platform.python_version(),
            "browser": browser_version,
            "distributions": dict(sorted(installed_versions.items())),
        },
    }


def _rerun_input_paths(repo_root: Path = REPO_ROOT) -> tuple[Path, ...]:
    return (
        repo_root / "datasets" / "mvp_evaluation_manifest_v1.json",
        repo_root / "datasets" / "fixtures" / "manifest.json",
        repo_root
        / "datasets"
        / "fixtures"
        / "pdf"
        / "pdf-to-word-representative.pdf",
        repo_root
        / "datasets"
        / "fixtures"
        / "templates"
        / "synthetic-batch-template-regression.json",
        repo_root / "services" / "api" / "inference_profiles.json",
    )


def _browser_channel() -> str:
    return (
        os.environ.get("VERIDOC_E2E_BROWSER_CHANNEL")
        or "playwright-managed-chromium"
    )


def _inference_environment_snapshot(
    profiles: dict[str, Any],
    *,
    environment: Mapping[str, str] = os.environ,
) -> dict[str, Any]:
    profile_definitions = profiles.get("profiles")
    if not isinstance(profile_definitions, list) or not profile_definitions:
        raise ValueError("inference profile configuration is incomplete")

    records: list[dict[str, Any]] = []
    selected_profile: str | None = None
    for profile in profile_definitions:
        if not isinstance(profile, dict):
            raise ValueError("inference profile configuration is malformed")
        profile_id = profile.get("id")
        base_url_env = profile.get("base_url_env")
        model_env = profile.get("model_env")
        api_key_env = profile.get("api_key_env")
        optional_env = profile.get("optional_env")
        if (
            not isinstance(profile_id, str)
            or not profile_id
            or not isinstance(base_url_env, str)
            or not base_url_env
            or not isinstance(model_env, str)
            or not model_env
            or (
                api_key_env is not None
                and (not isinstance(api_key_env, str) or not api_key_env)
            )
            or not isinstance(optional_env, list)
            or not all(isinstance(name, str) and name for name in optional_env)
        ):
            raise ValueError("inference profile environment binding is incomplete")

        environment_names = sorted(
            {
                base_url_env,
                model_env,
                *optional_env,
                *([api_key_env] if isinstance(api_key_env, str) else []),
            }
        )
        values: dict[str, str | None] = {}
        credential_fingerprints: dict[str, dict[str, object]] = {}
        for name in environment_names:
            raw_value = environment.get(name)
            if name == api_key_env:
                credential_fingerprints[name] = {
                    "configured": raw_value is not None,
                    "sha256": (
                        hashlib.sha256(raw_value.encode("utf-8")).hexdigest()
                        if raw_value is not None
                        else None
                    ),
                }
                continue
            normalized_value = raw_value
            if name == base_url_env and normalized_value is not None:
                parsed = urlsplit(normalized_value)
                userinfo, separator, endpoint_netloc = parsed.netloc.rpartition("@")
                credential_fingerprints[name] = {
                    "configured": bool(separator),
                    "sha256": (
                        hashlib.sha256(userinfo.encode("utf-8")).hexdigest()
                        if separator
                        else None
                    ),
                }
                if separator:
                    leading_whitespace = normalized_value[
                        : len(normalized_value) - len(normalized_value.lstrip())
                    ]
                    normalized_value = parsed._replace(
                        netloc=endpoint_netloc
                    ).geturl()
                    normalized_value = f"{leading_whitespace}{normalized_value}"
            values[name] = normalized_value

        base_url_value = environment.get(base_url_env)
        model_value = environment.get(model_env)
        if (
            selected_profile is None
            and base_url_value is not None
            and base_url_value.strip()
            and model_value is not None
            and model_value.strip()
        ):
            selected_profile = profile_id
        records.append(
            {
                "id": profile_id,
                "environment": values,
                "credential_fingerprints": credential_fingerprints,
            }
        )

    return {
        "mode": "local-llm" if selected_profile is not None else "deterministic-fallback",
        "selected_profile": selected_profile,
        "profiles": records,
    }


def validate_rerun_runtime_dependencies(
    package: dict[str, Any],
    *,
    browser_version: str | None = None,
    repo_root: Path = REPO_ROOT,
) -> None:
    dependencies = package.get("dependencies")
    runtime = dependencies.get("runtime") if isinstance(dependencies, dict) else None
    recorded_browser = runtime.get("browser") if isinstance(runtime, dict) else None
    if not isinstance(recorded_browser, str) or not recorded_browser:
        raise ValueError("rerun package runtime dependency set is incomplete")
    current = _dependency_snapshot(
        browser_version=browser_version or recorded_browser,
        repo_root=repo_root,
    )
    if dependencies != current:
        raise ValueError(
            "rerun package runtime dependencies do not match the current environment"
        )


def _rerun_package_configuration(
    profiles: dict[str, Any],
    *,
    profiles_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    return {
        "path": _repo_relative(profiles_path, repo_root),
        "sha256": _sha256(profiles_path),
        "network_boundary": profiles.get("network_boundary"),
        "profiles": profiles.get("profiles"),
        "browser_channel": _browser_channel(),
        "inference_environment": _inference_environment_snapshot(profiles),
    }


def _rerun_package_versions(profiles: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_profiles_schema": profiles.get("schema_version"),
        "prompt": CONVERSION_PLAN_PROMPT_VERSION,
        "schemas": {
            "conversion_audit": CONVERSION_AUDIT_SCHEMA_VERSION,
            "conversion_plan": CONVERSION_PLAN_SCHEMA_VERSION,
            "document_ir": DOCUMENT_IR_SCHEMA_VERSION,
            "browser_evidence": BROWSER_EVIDENCE_SCHEMA_VERSION,
            "network_observation": NETWORK_OBSERVATION_SCHEMA_VERSION,
        },
    }


def _rerun_package_commands() -> dict[str, str]:
    return {
        "initial": "python3 scripts/ci/mvp_browser_e2e.py",
        "rerun": (
            "python3 scripts/ci/mvp_browser_e2e.py "
            "--rerun-package <rerun-package-path>"
        ),
    }


def build_rerun_package(
    evidence: dict[str, Any],
    *,
    browser_version: str,
    repo_root: Path = REPO_ROOT,
    generated_evidence_dir: Path | None = None,
    retained_rerun_package_path: Path | None = None,
    retained_evidence_dir: Path | None = None,
) -> dict[str, Any]:
    excluded_paths = (
        ((generated_evidence_dir,) if generated_evidence_dir is not None else ())
        + _retained_evidence_paths(
            retained_rerun_package_path,
            repo_root=repo_root,
        )
    )
    _assert_clean_git_checkout(repo_root, excluded_paths=excluded_paths)
    input_paths = _rerun_input_paths(repo_root)
    profiles_path = repo_root / "services" / "api" / "inference_profiles.json"
    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
    if evidence.get("schema_version") != BROWSER_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("browser evidence schema version is unsupported")
    projection = _equivalence_projection(evidence)
    package = {
        "schema_version": RERUN_PACKAGE_SCHEMA_VERSION,
        "commit": _current_git_commit(repo_root),
        "inputs": [
            {
                "path": _repo_relative(path, repo_root),
                "sha256": _sha256(path),
            }
            for path in input_paths
        ],
        "configuration": _rerun_package_configuration(
            profiles,
            profiles_path=profiles_path,
            repo_root=repo_root,
        ),
        "dependencies": _dependency_snapshot(
            browser_version=browser_version,
            repo_root=repo_root,
        ),
        "versions": _rerun_package_versions(profiles),
        "commands": _rerun_package_commands(),
        "equivalence": {
            "rule": RERUN_EQUIVALENCE_RULE,
            "baseline": projection,
            "baseline_sha256": _canonical_json_sha256(projection),
            "baseline_evidence": {
                "path": "evidence.json",
                "sha256": _canonical_json_sha256(evidence),
            },
        },
    }
    if retained_evidence_dir is not None:
        package["equivalence"]["retained_files"] = (
            _retained_evidence_file_records(
                evidence,
                run_dir=retained_evidence_dir,
            )
        )
    return seal_rerun_package(package)


def validate_rerun_package_for_workspace(
    envelope: dict[str, Any],
    *,
    repo_root: Path = REPO_ROOT,
    rerun_package_path: Path | None = None,
) -> dict[str, Any]:
    excluded_paths = _retained_evidence_paths(
        rerun_package_path,
        repo_root=repo_root,
    )
    _assert_clean_git_checkout(repo_root, excluded_paths=excluded_paths)
    package = validate_rerun_package_envelope(envelope)
    if package.get("commit") != _current_git_commit(repo_root):
        raise ValueError("rerun package commit does not match the current checkout")
    configuration = package.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("rerun package configuration is incomplete")
    if configuration.get("browser_channel") != _browser_channel():
        raise ValueError(
            "rerun package browser channel does not match the current environment"
        )
    profiles_path = repo_root / "services" / "api" / "inference_profiles.json"
    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
    if configuration.get(
        "inference_environment"
    ) != _inference_environment_snapshot(profiles):
        raise ValueError(
            "rerun package inference environment does not match the current environment"
        )
    expected_configuration = _rerun_package_configuration(
        profiles,
        profiles_path=profiles_path,
        repo_root=repo_root,
    )
    if configuration != expected_configuration:
        raise ValueError(
            "rerun package configuration metadata does not match the current checkout"
        )
    if package.get("versions") != _rerun_package_versions(profiles):
        raise ValueError(
            "rerun package version metadata does not match the current checkout"
        )
    if package.get("commands") != _rerun_package_commands():
        raise ValueError(
            "rerun package command metadata does not match the supported commands"
        )
    equivalence = package.get("equivalence")
    if (
        not isinstance(equivalence, dict)
        or equivalence.get("rule") != RERUN_EQUIVALENCE_RULE
    ):
        raise ValueError("rerun package equivalence metadata is unsupported")
    baseline = equivalence.get("baseline")
    baseline_evidence = equivalence.get("baseline_evidence")
    if (
        not isinstance(baseline, dict)
        or equivalence.get("baseline_sha256") != _canonical_json_sha256(baseline)
        or not isinstance(baseline_evidence, dict)
        or baseline_evidence.get("path") != "evidence.json"
        or not isinstance(baseline_evidence.get("sha256"), str)
    ):
        raise ValueError("rerun package equivalence metadata is incomplete")
    if rerun_package_path is not None:
        evidence_path = rerun_package_path.resolve().parent / "evidence.json"
        try:
            retained_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                "rerun package retained baseline evidence is unavailable"
            ) from exc
        if (
            not isinstance(retained_evidence, dict)
            or _canonical_json_sha256(retained_evidence)
            != baseline_evidence["sha256"]
            or _equivalence_projection(retained_evidence) != baseline
        ):
            raise ValueError(
                "rerun package equivalence baseline does not match retained evidence"
            )
        retained_files = equivalence.get("retained_files")
        try:
            actual_retained_files = _retained_evidence_file_records(
                retained_evidence,
                run_dir=evidence_path.parent,
            )
        except ValueError as exc:
            raise ValueError(
                "rerun package retained evidence files are unavailable"
            ) from exc
        if (
            not isinstance(retained_files, list)
            or retained_files != actual_retained_files
        ):
            raise ValueError(
                "retained evidence files do not match rerun package"
            )
    records = package.get("inputs")
    if not isinstance(records, list) or not records:
        raise ValueError("rerun package inputs list is missing")
    recorded_input_paths: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("rerun package inputs entry is malformed")
        relative_path = record.get("path")
        expected_sha256 = record.get("sha256")
        if not isinstance(relative_path, str) or not isinstance(
            expected_sha256, str
        ):
            raise ValueError("rerun package inputs entry is incomplete")
        path = (repo_root / relative_path).resolve()
        try:
            path.relative_to(repo_root.resolve())
        except ValueError as exc:
            raise ValueError(
                "rerun package inputs path escapes the repository"
            ) from exc
        if not path.is_file() or _sha256(path) != expected_sha256:
            raise ValueError(f"rerun package input hash mismatch: {relative_path}")
        recorded_input_paths.append(relative_path)
    expected_input_paths = {
        _repo_relative(path, repo_root) for path in _rerun_input_paths(repo_root)
    }
    if (
        len(recorded_input_paths) != len(set(recorded_input_paths))
        or set(recorded_input_paths) != expected_input_paths
    ):
        raise ValueError("rerun package inputs do not match the required input set")
    dependencies = package.get("dependencies")
    requirement_files = (
        dependencies.get("requirement_files")
        if isinstance(dependencies, dict)
        else None
    )
    if not isinstance(requirement_files, list) or not requirement_files:
        raise ValueError("rerun package dependency set is incomplete")
    for record in requirement_files:
        if not isinstance(record, dict):
            raise ValueError("rerun package dependency record is malformed")
        path_value = record.get("path")
        expected_sha256 = record.get("sha256")
        if not isinstance(path_value, str) or not isinstance(expected_sha256, str):
            raise ValueError("rerun package dependency record is incomplete")
        path = (repo_root / path_value).resolve()
        try:
            path.relative_to(repo_root.resolve())
        except ValueError as exc:
            raise ValueError("rerun package dependency path escapes repository") from exc
        if not path.is_file() or _sha256(path) != expected_sha256:
            raise ValueError(f"rerun package dependency hash mismatch: {path_value}")
    validate_rerun_runtime_dependencies(package, repo_root=repo_root)
    return package


def _compare_projection_to_package(
    package: dict[str, Any],
    actual: dict[str, Any],
) -> dict[str, object]:
    equivalence = package.get("equivalence")
    baseline = equivalence.get("baseline") if isinstance(equivalence, dict) else None
    if not isinstance(baseline, dict):
        raise ValueError("rerun package equivalence baseline is missing")
    expected_sha256 = equivalence.get("baseline_sha256")
    if expected_sha256 != _canonical_json_sha256(baseline):
        raise ValueError("rerun package equivalence baseline integrity check failed")
    actual_projection = _equivalence_projection(actual)
    actual_sha256 = _canonical_json_sha256(actual_projection)
    equivalent = actual_sha256 == expected_sha256
    comparison = {
        "schema_version": "veridoc-mvp-rerun-equivalence/v1",
        "equivalent": equivalent,
        "rule": equivalence.get("rule"),
        "expected_sha256": expected_sha256,
        "actual_sha256": actual_sha256,
    }
    if not equivalent:
        raise AssertionError(
            "rerun result is not equivalent under the packaged equivalence rule"
        )
    return comparison


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _retain_redacted_trace(
    raw_trace_path: Path, retained_trace_path: Path, *, secret: str
) -> None:
    """Copy a Playwright trace while removing the ephemeral bearer credential."""
    secret_bytes = secret.encode("utf-8")
    with ZipFile(raw_trace_path) as raw_trace, ZipFile(retained_trace_path, "w") as retained:
        for entry in raw_trace.infolist():
            retained.writestr(
                entry,
                raw_trace.read(entry).replace(secret_bytes, b"<redacted-e2e-token>"),
            )
    with ZipFile(retained_trace_path) as retained:
        if any(
            secret_bytes in retained.read(entry) for entry in retained.infolist()
        ):
            raise AssertionError("retained browser trace contains the bearer credential")


def _json_response(response: Any) -> dict[str, Any]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise AssertionError("browser E2E API response must be a JSON object")
    return payload


def _events(payload: dict[str, Any]) -> list[dict[str, Any]]:
    events = payload.get(
        "events",
        payload.get(
            "audit_events",
            payload.get("job_events", payload.get("review_events", [])),
        ),
    )
    if not isinstance(events, list):
        raise AssertionError("audit response did not contain an event list")
    return [event for event in events if isinstance(event, dict)]


def _require_matching_event(
    events: list[dict[str, Any]],
    *,
    expected_fields: dict[str, Any],
    description: str,
) -> tuple[dict[str, Any], int]:
    matching_events = [
        event
        for event in events
        if all(event.get(field) == expected for field, expected in expected_fields.items())
    ]
    if not matching_events:
        raise AssertionError(
            f"{description} was not bound to the browser run: "
            f"expected fields {expected_fields!r}"
        )
    return matching_events[-1], len(matching_events)


def _require_audit_payload_matches_result(
    audit_payload: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    result_audit = result.get("audit")
    if not isinstance(result_audit, dict):
        raise AssertionError("completed browser result did not contain audit metadata")
    if audit_payload != result_audit:
        raise AssertionError(
            "downloaded audit artifact did not match the current browser result audit"
        )
    return result_audit


def _evidence_failure(code: str, boundary: str, message: str) -> dict[str, str]:
    return {"code": code, "boundary": boundary, "message": message}


def _resolve_evidence_path(run_dir: Path, filename: object) -> Path | None:
    if not isinstance(filename, str) or not filename:
        return None
    try:
        resolved_run_dir = run_dir.resolve()
        resolved_path = (resolved_run_dir / filename).resolve()
        resolved_path.relative_to(resolved_run_dir)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return resolved_path if resolved_path != resolved_run_dir else None


def _load_evidence_json(run_dir: Path, filename: object) -> object | None:
    evidence_path = _resolve_evidence_path(run_dir, filename)
    if evidence_path is None:
        return None
    try:
        return json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None


def _retained_evidence_file_records(
    evidence: dict[str, Any],
    *,
    run_dir: Path,
) -> list[dict[str, str]]:
    files = evidence.get("files")
    if not isinstance(files, dict):
        raise ValueError("browser evidence files manifest is missing")
    records: list[dict[str, str]] = []
    for role in ACCEPTANCE_EVIDENCE_FILE_ROLES:
        path = _resolve_evidence_path(run_dir, files.get(role))
        if path is None or not path.is_file():
            raise ValueError(f"browser evidence file is unavailable: {role}")
        records.append(
            {
                "role": role,
                "path": path.relative_to(run_dir.resolve()).as_posix(),
                "sha256": _sha256(path),
            }
        )
    return records


def _valid_source_bbox(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    coordinates = {
        field: value.get(field)
        for field in ("x", "y", "width", "height")
    }
    for coordinate in coordinates.values():
        if not isinstance(coordinate, (int, float)) or isinstance(coordinate, bool):
            return False
        try:
            if not math.isfinite(coordinate):
                return False
        except OverflowError:
            return False
    return (
        coordinates["x"] >= 0
        and coordinates["y"] >= 0
        and coordinates["width"] > 0
        and coordinates["height"] > 0
        and str(value.get("unit") or "").strip() in UNITS
        and value.get("origin") == "top-left"
    )


def _audit_chain_is_valid(events: object) -> bool:
    if not isinstance(events, list) or not events:
        return False
    previous_hash: str | None = None
    for sequence, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            return False
        if event.get("integrity_algorithm") != "sha256-canonical-json-chain-v1":
            return False
        if event.get("sequence") != sequence:
            return False
        if event.get("prev_event_hash") != previous_hash:
            return False
        event_hash = event.get("event_hash")
        canonical = json.dumps(
            {key: value for key, value in event.items() if key != "event_hash"},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        expected_hash = hashlib.sha256(canonical).hexdigest()
        if event_hash != expected_hash:
            return False
        previous_hash = event_hash
    return True


def _matching_events(
    events: object,
    *,
    expected_fields: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        return []
    return [
        event
        for event in events
        if isinstance(event, dict)
        and all(event.get(field) == value for field, value in expected_fields.items())
    ]


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_approver_actor(value: object) -> bool:
    return (
        isinstance(value, dict)
        and _nonempty_string(value.get("id"))
        and value.get("role") == "approver"
    )


def evaluate_acceptance_evidence(
    evidence: dict[str, Any],
    *,
    run_dir: Path,
) -> dict[str, Any]:
    """Evaluate one browser evidence package without inferring missing linkage."""

    failures: list[dict[str, str]] = []

    def fail(code: str, boundary: str, message: str) -> None:
        failure = _evidence_failure(code, boundary, message)
        if failure not in failures:
            failures.append(failure)

    if evidence.get("schema_version") != BROWSER_EVIDENCE_SCHEMA_VERSION:
        fail(
            "EVIDENCE_SCHEMA_UNSUPPORTED",
            "evidence_schema",
            "Browser evidence must use the supported fail-closed schema version.",
        )

    correlation = evidence.get("correlation")
    correlation = correlation if isinstance(correlation, dict) else {}
    provenance = correlation.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    if (
        not isinstance(provenance.get("source_page"), int)
        or isinstance(provenance.get("source_page"), bool)
        or provenance["source_page"] < 1
        or not _valid_source_bbox(provenance.get("source_bbox"))
    ):
        fail(
            "EVIDENCE_PROVENANCE_MISSING",
            "provenance",
            "Source page and bounding-box provenance are required before acceptance can pass.",
        )

    files = evidence.get("files")
    files = files if isinstance(files, dict) else {}
    job_events = _load_evidence_json(run_dir, files.get("job_events"))
    review_events = _load_evidence_json(run_dir, files.get("review_events"))
    if not isinstance(job_events, list) or not isinstance(review_events, list):
        fail(
            "EVIDENCE_AUDIT_MISSING",
            "audit",
            "Job and review audit events are required before acceptance can pass.",
        )

    run_id = evidence.get("run_id")
    surfaces = evidence.get("evidence_surfaces")
    surfaces = surfaces if isinstance(surfaces, dict) else {}
    required_surfaces = {
        "browser_run",
        "harness_result",
        "download_artifact",
        "audit_events",
    }
    surface_ids = {
        surface.get("correlation_id")
        for surface in surfaces.values()
        if isinstance(surface, dict)
    }
    if (
        not isinstance(run_id, str)
        or not run_id
        or correlation.get("run_id") != run_id
        or set(surfaces) != required_surfaces
        or surface_ids != {run_id}
    ):
        fail(
            "EVIDENCE_CORRELATION_MISMATCH",
            "correlation",
            "Browser, harness, artifact, and audit evidence must share one correlation ID.",
        )

    if failures and {
        failure["code"] for failure in failures
    } >= {"EVIDENCE_PROVENANCE_MISSING", "EVIDENCE_AUDIT_MISSING"}:
        return {
            "status": "fail",
            "correlation_id": run_id,
            "criteria": ["AC-PROVENANCE", "AC-AUDIT", "FC-EVIDENCE"],
            "failure_reasons": failures[:2],
        }

    api_result = _load_evidence_json(run_dir, files.get("api_result"))
    audit_artifact = _load_evidence_json(run_dir, files.get("audit_artifact"))
    if not isinstance(api_result, dict) or not isinstance(audit_artifact, dict):
        fail(
            "EVIDENCE_AUDIT_MISSING",
            "audit",
            "Conversion result and downloadable audit evidence are both required.",
        )
        result_audit: dict[str, Any] = {}
    else:
        audit_value = api_result.get("audit")
        result_audit = audit_value if isinstance(audit_value, dict) else {}
        if not result_audit or audit_artifact != result_audit:
            fail(
                "EVIDENCE_AUDIT_TAMPERED",
                "audit",
                "The downloadable audit record must exactly match the browser result audit.",
            )

    upload = correlation.get("upload")
    upload = upload if isinstance(upload, dict) else {}
    artifact = correlation.get("artifact")
    artifact = artifact if isinstance(artifact, dict) else {}
    review = correlation.get("review")
    review = review if isinstance(review, dict) else {}
    job = correlation.get("job")
    job = job if isinstance(job, dict) else {}
    required_identifiers = {
        "job_id": job.get("job_id"),
        "conversion_id": review.get("conversion_id"),
        "document_id": review.get("document_id"),
        "block_id": review.get("block_id"),
        "artifact_id": artifact.get("artifact_id"),
        "source_filename": upload.get("source_filename"),
    }
    if any(
        not _nonempty_string(value)
        for value in required_identifiers.values()
    ):
        fail(
            "EVIDENCE_IDENTIFIER_MISSING",
            "correlation",
            (
                "Job, conversion, review-item, artifact, and source filename "
                "values must be explicit."
            ),
        )
    if (
        job.get("status") != "succeeded"
        or job.get("conversion_status") not in {"converted", "requires_review"}
    ):
        fail(
            "EVIDENCE_JOB_INCOMPLETE",
            "job_state",
            "The authoritative job and browser conversion must both be complete.",
        )
    if not _valid_approver_actor(review.get("actor")):
        fail(
            "EVIDENCE_ACTOR_MISSING",
            "review_actor",
            "A non-empty approver identity is required for the accepted decision.",
        )

    job_response = _load_evidence_json(run_dir, files.get("job_response"))
    job_response_hashes = (
        job_response.get("hashes")
        if isinstance(job_response, dict)
        and isinstance(job_response.get("hashes"), dict)
        else {}
    )
    hash_verification = (
        job_response.get("hash_verification", {}).get("source")
        if isinstance(job_response, dict)
        and isinstance(job_response.get("hash_verification"), dict)
        and isinstance(job_response["hash_verification"].get("source"), dict)
        else {}
    )
    expected_display_status = {
        "converted": "completed",
        "requires_review": "review_required",
    }.get(job.get("conversion_status"))
    if (
        not isinstance(job_response, dict)
        or job_response.get("job_id") != job.get("job_id")
        or job_response.get("status") != job.get("status")
        or job_response.get("created_at") != job.get("created_at")
        or job_response.get("display_status") != expected_display_status
        or job_response.get("has_result") is not True
        or job_response.get("filename") != upload.get("source_filename")
        or job_response_hashes.get("source_sha256")
        != upload.get("source_sha256")
        or hash_verification.get("status") != "recorded"
        or hash_verification.get("sha256") != upload.get("source_sha256")
    ):
        fail(
            "EVIDENCE_JOB_STATE_MISMATCH",
            "job_state",
            (
                "The retained authoritative job response must match job state "
                "and uploaded source provenance."
            ),
        )
    if (
        isinstance(api_result, dict)
        and "status" in api_result
        and api_result.get("status") != job.get("conversion_status")
    ):
        fail(
            "EVIDENCE_JOB_STATE_MISMATCH",
            "conversion_status",
            "The browser result status must match the correlated successful conversion.",
        )

    audit_input = result_audit.get("input")
    audit_input = audit_input if isinstance(audit_input, dict) else {}
    result_artifacts = (
        api_result.get("artifacts")
        if isinstance(api_result, dict)
        else None
    )
    if not isinstance(result_artifacts, list):
        fail(
            "EVIDENCE_ARTIFACT_MISSING",
            "artifact",
            "The browser result must contain a well-formed artifact list.",
        )
        result_artifacts = []
    result_artifact = next(
        (
            item
            for item in result_artifacts
            if isinstance(item, dict)
            and item.get("artifact_id") == artifact.get("artifact_id")
        ),
        None,
    )
    result_artifact_metadata = (
        result_artifact.get("metadata")
        if isinstance(result_artifact, dict)
        and isinstance(result_artifact.get("metadata"), dict)
        else {}
    )
    if (
        not isinstance(result_artifact, dict)
        or not _nonempty_string(result_artifact.get("id"))
        or not str(result_artifact["id"]).startswith("primary-")
        or result_artifact.get("kind") != "primary"
        or result_artifact_metadata.get("role") != "primary"
    ):
        fail(
            "EVIDENCE_ARTIFACT_MISMATCH",
            "artifact",
            "The correlated download must be the conversion's primary artifact.",
        )
    job_response_artifacts = (
        job_response.get("artifacts")
        if isinstance(job_response, dict)
        and isinstance(job_response.get("artifacts"), list)
        else []
    )
    if (
        not isinstance(result_artifact, dict)
        or len(
            [
                item
                for item in job_response_artifacts
                if isinstance(item, dict)
                and item.get("artifact_id") == artifact.get("artifact_id")
                and item.get("id") == result_artifact.get("id")
            ]
        )
        != 1
    ):
        fail(
            "EVIDENCE_ARTIFACT_MISMATCH",
            "artifact",
            "The retained job response must reference the correlated primary artifact.",
        )

    result_review_items = (
        api_result.get("review_items")
        if isinstance(api_result, dict)
        else None
    )
    review_item_matches = (
        [
            item
            for item in result_review_items
            if isinstance(item, dict)
            and item.get("document_id") == review.get("document_id")
            and item.get("block_id") == review.get("block_id")
            and item.get("source_page") == provenance.get("source_page")
            and item.get("source_bbox") == provenance.get("source_bbox")
        ]
        if isinstance(result_review_items, list)
        else []
    )
    if len(review_item_matches) != 1:
        fail(
            "EVIDENCE_PROVENANCE_MISMATCH",
            "review_item",
            "Reviewed source coordinates must match one authoritative result item.",
        )

    source_hashes = {
        upload.get("source_sha256"),
        provenance.get("source_sha256"),
        result_audit.get("source_sha256"),
        audit_input.get("sha256"),
    }
    hashes = api_result.get("hashes") if isinstance(api_result, dict) else None
    if isinstance(hashes, dict):
        source_hashes.add(hashes.get("source_sha256"))
    source_hashes.add(result_artifact_metadata.get("source_sha256"))
    if (
        len(source_hashes) != 1
        or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in source_hashes
        )
    ):
        fail(
            "EVIDENCE_HASH_MISMATCH",
            "input_hash",
            (
                "The uploaded source hash must match provenance and all "
                "conversion audit input records."
            ),
        )

    download_name = files.get("download")
    download_path = _resolve_evidence_path(run_dir, download_name)
    downloaded_hash = (
        _sha256(download_path)
        if download_path is not None and download_path.is_file()
        else None
    )
    output_hashes = {
        downloaded_hash,
        artifact.get("sha256"),
        (
            result_artifact.get("sha256")
            if isinstance(result_artifact, dict)
            else None
        ),
        result_artifact_metadata.get("output_sha256"),
    }
    if (
        len(output_hashes) != 1
        or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in output_hashes
        )
    ):
        fail(
            "EVIDENCE_HASH_MISMATCH",
            "output_hash",
            "The downloaded artifact hash must match the result and artifact audit metadata.",
        )

    audit_download_name = files.get("audit_artifact")
    audit_download_path = _resolve_evidence_path(run_dir, audit_download_name)
    audit_downloaded_hash = (
        _sha256(audit_download_path)
        if audit_download_path is not None and audit_download_path.is_file()
        else None
    )
    result_audit_artifact = None
    result_audit_artifact = next(
        (
            item
            for item in result_artifacts
            if isinstance(item, dict) and item.get("id") == "audit-json"
        ),
        None,
    )
    audit_hashes = {
        audit_downloaded_hash,
        correlation.get("audit", {}).get("audit_artifact_sha256")
        if isinstance(correlation.get("audit"), dict)
        else None,
        (
            result_audit_artifact.get("sha256")
            if isinstance(result_audit_artifact, dict)
            else None
        ),
    }
    if (
        len(audit_hashes) != 1
        or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in audit_hashes
        )
    ):
        fail(
            "EVIDENCE_HASH_MISMATCH",
            "audit_hash",
            "The downloadable audit hash must match the result and evidence manifest.",
        )

    versions = result_audit.get("versions")
    versions = versions if isinstance(versions, dict) else {}
    prompt = versions.get("prompt")
    schemas = versions.get("schemas")
    expected_schemas = {
        "conversion_audit": CONVERSION_AUDIT_SCHEMA_VERSION,
        "conversion_plan": CONVERSION_PLAN_SCHEMA_VERSION,
        "document_ir": DOCUMENT_IR_SCHEMA_VERSION,
    }
    expected_prompt = {
        "id": CONVERSION_PLAN_PROMPT_ID,
        "version": CONVERSION_PLAN_PROMPT_VERSION,
    }
    llm_audit = result_audit.get("llm")
    explicit_no_model = (
        isinstance(llm_audit, dict)
        and llm_audit.get("requested") is False
        and llm_audit.get("model") is None
        and versions.get("model") is None
    )
    explicit_model = (
        isinstance(llm_audit, dict)
        and llm_audit.get("requested") is True
        and isinstance(llm_audit.get("model"), str)
        and bool(llm_audit["model"])
        and llm_audit.get("model") == versions.get("model")
    )
    if (
        "model" not in versions
        or prompt != expected_prompt
        or result_audit.get("schema_version") != CONVERSION_AUDIT_SCHEMA_VERSION
        or not isinstance(schemas, dict)
        or any(
            schemas.get(field) != expected
            for field, expected in expected_schemas.items()
        )
        or not isinstance(llm_audit, dict)
        or "model" not in llm_audit
        or not (explicit_no_model or explicit_model)
        or llm_audit.get("prompt") != prompt
        or llm_audit.get("schema_version") != schemas.get("conversion_plan")
    ):
        fail(
            "EVIDENCE_VERSION_MISMATCH",
            "version_lineage",
            "Model, prompt, and schema lineage must be complete and mutually consistent.",
        )

    created_at = job.get("created_at")
    try:
        parsed_created_at = (
            datetime.fromisoformat(created_at)
            if isinstance(created_at, str)
            else None
        )
    except ValueError:
        parsed_created_at = None
    if parsed_created_at is None or parsed_created_at.tzinfo is None:
        fail(
            "EVIDENCE_TIMESTAMP_MISSING",
            "timestamp",
            "The authoritative job timestamp is required in the correlated evidence.",
        )

    if isinstance(job_events, list) and isinstance(review_events, list):
        browser_surface = surfaces.get("browser_run")
        browser_surface = browser_surface if isinstance(browser_surface, dict) else {}
        harness_surface = surfaces.get("harness_result")
        harness_surface = harness_surface if isinstance(harness_surface, dict) else {}
        artifact_surface = surfaces.get("download_artifact")
        artifact_surface = artifact_surface if isinstance(artifact_surface, dict) else {}
        audit_surface = surfaces.get("audit_events")
        audit_surface = audit_surface if isinstance(audit_surface, dict) else {}
        if not _audit_chain_is_valid(job_events) or not _audit_chain_is_valid(
            review_events
        ):
            fail(
                "EVIDENCE_AUDIT_CHAIN_INVALID",
                "audit_hash_chain",
                "Every audit event must belong to a valid canonical hash chain.",
            )
        audit_correlation = correlation.get("audit")
        audit_correlation = (
            audit_correlation if isinstance(audit_correlation, dict) else {}
        )
        chain_commitments = {
            "job_event_count": len(job_events),
            "review_event_count": len(review_events),
            "job_terminal_event_hash": (
                job_events[-1].get("event_hash")
                if job_events and isinstance(job_events[-1], dict)
                else None
            ),
            "review_terminal_event_hash": (
                review_events[-1].get("event_hash")
                if review_events and isinstance(review_events[-1], dict)
                else None
            ),
        }
        if any(
            audit_correlation.get(field) != expected
            or audit_surface.get(field) != expected
            for field, expected in chain_commitments.items()
        ):
            fail(
                "EVIDENCE_AUDIT_CHAIN_TRUNCATED",
                "audit_hash_chain",
                "Retained event counts and terminal hashes must cover each complete chain.",
            )
        upload_matches = _matching_events(
            job_events,
            expected_fields={
                "event_type": "web.job_operation",
                "action": "browser_upload",
                "job_id": job.get("job_id"),
                "filename": upload.get("source_filename"),
                "source_sha256": upload.get("source_sha256"),
            },
        )
        review_matches = _matching_events(
            review_events,
            expected_fields={
                "event_type": "conversion_review.action_requested",
                "action": "approve",
                "conversion_id": review.get("conversion_id"),
                "document_id": review.get("document_id"),
                "block_id": review.get("block_id"),
                "actor": review.get("actor"),
                "source_page": provenance.get("source_page"),
                "source_bbox": provenance.get("source_bbox"),
            },
        )
        if (
            review.get("action") != "approve"
            or len(upload_matches) != 1
            or len(review_matches) != 1
        ):
            fail(
                "EVIDENCE_AUDIT_EVENT_MISSING",
                "audit",
                (
                    "Exactly one upload event and one approval decision must "
                    "match the correlated run."
                ),
            )
        elif (
            browser_surface.get("job_id") != job.get("job_id")
            or harness_surface.get("conversion_id")
            != review.get("conversion_id")
            or not isinstance(api_result, dict)
            or api_result.get("conversion_id") != review.get("conversion_id")
            or result_audit.get("conversion_id") != review.get("conversion_id")
            or artifact_surface.get("artifact_id")
            != artifact.get("artifact_id")
            or audit_surface.get("job_event_hash")
            != upload_matches[0].get("event_hash")
            or audit_surface.get("review_event_hash")
            != review_matches[0].get("event_hash")
        ):
            fail(
                "EVIDENCE_CORRELATION_MISMATCH",
                "correlation",
                "Surface identifiers and audit hashes must remain bound to this run.",
            )

    if (
        provenance.get("source_filename") != upload.get("source_filename")
        or result_audit.get("source_filename") != upload.get("source_filename")
        or audit_input.get("filename") != upload.get("source_filename")
        or result_artifact_metadata.get("source_filename")
        != upload.get("source_filename")
        or provenance.get("document_id") != review.get("document_id")
        or provenance.get("block_id") != review.get("block_id")
    ):
        fail(
            "EVIDENCE_PROVENANCE_MISMATCH",
            "provenance",
            "Source coordinates must remain bound to the reviewed record in this run.",
        )

    return {
        "status": "fail" if failures else "pass",
        "correlation_id": run_id,
        "criteria": ["AC-PROVENANCE", "AC-AUDIT", "FC-EVIDENCE"],
        "failure_reasons": failures,
    }


def _build_accepted_rerun_package(
    evidence: dict[str, Any],
    *,
    run_dir: Path,
    browser_version: str,
    repo_root: Path = REPO_ROOT,
    generated_evidence_dir: Path | None = None,
    retained_rerun_package_path: Path | None = None,
) -> dict[str, Any]:
    evidence["acceptance_snapshot"] = evaluate_acceptance_evidence(
        evidence,
        run_dir=run_dir,
    )
    return build_rerun_package(
        evidence,
        browser_version=browser_version,
        repo_root=repo_root,
        generated_evidence_dir=generated_evidence_dir,
        retained_rerun_package_path=retained_rerun_package_path,
        retained_evidence_dir=run_dir,
    )


def _high_risk_fixture() -> dict[str, Any]:
    fixture = json.loads(HIGH_RISK_FIXTURE_PATH.read_text(encoding="utf-8"))
    if not isinstance(fixture, dict):
        raise AssertionError("high-risk browser fixture must be a JSON object")
    return fixture


def _high_risk_template_store() -> TemplateStore:
    fixture = _high_risk_fixture()
    definition = fixture.get("template_definition")
    if not isinstance(definition, dict):
        raise AssertionError("high-risk browser fixture is missing template_definition")
    definition = deepcopy(definition)
    anchors = definition.get("anchors")
    fields = definition.get("fields")
    validation_rules = definition.get("validation_rules")
    output_mapping = definition.get("output_mapping")
    field_map = (
        output_mapping.get("field_map")
        if isinstance(output_mapping, dict)
        else None
    )
    if (
        not isinstance(anchors, list)
        or not anchors
        or not isinstance(anchors[0], dict)
        or not isinstance(fields, list)
        or not fields
        or not isinstance(fields[0], dict)
        or not isinstance(validation_rules, list)
        or not validation_rules
        or not isinstance(output_mapping, dict)
        or not isinstance(field_map, list)
        or not field_map
    ):
        raise AssertionError("high-risk browser template fixture is malformed")
    definition["anchors"] = [{**anchors[0], "text": "Manufacturing Summary"}]
    definition["fields"] = [{**fields[0], "label": "Batch"}]
    definition["tables"] = []
    definition["validation_rules"] = validation_rules[:1]
    output_mapping["field_map"] = field_map[:1]
    output_mapping["table_map"] = []
    registration = {
        key: value
        for key, value in definition.items()
        if key not in {"version", "template_version", "status", "effective"}
    }
    registration.update(
        {
            "name": "Real PDF high-risk browser review",
            "category": "manufacturing",
            "change_reason": "Register committed high-risk browser review fixture",
            "actor": {"principal_id": "e2e-template-admin", "role": "admin"},
        }
    )
    store = TemplateStore()
    store.register_template(registration)
    return store


@contextmanager
def _poc_server(
    state_root: Path, *, auth_token: str, approver_actor: str
) -> Iterator[str]:
    previous_auth = os.environ.get("VERIDOC_LOCAL_AUTH_TOKENS")
    os.environ["VERIDOC_LOCAL_AUTH_TOKENS"] = (
        f"approver:{approver_actor}={auth_token}"
    )
    database_path = state_root / "veridoc.sqlite3"
    server = ThreadingHTTPServer(("127.0.0.1", 0), PocWebRequestHandler)
    server.job_queue = JobQueue(
        database_path=database_path,
        artifact_store_root=state_root / "artifacts",
    )
    server.job_event_store = JobAuditEventStore(database_path=database_path)
    server.review_event_store = ReviewAuditEventStore()
    server.template_store = _high_risk_template_store()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if previous_auth is None:
            os.environ.pop("VERIDOC_LOCAL_AUTH_TOKENS", None)
        else:
            os.environ["VERIDOC_LOCAL_AUTH_TOKENS"] = previous_auth


@contextmanager
def _acceptance_network_boundary(
    base_url: str,
) -> Iterator[LocalNetworkBoundaryObserver]:
    configured_endpoints = validate_endpoint_configuration(
        os.environ,
        allowed_origins=(base_url,),
    )
    observer = LocalNetworkBoundaryObserver(
        allowed_origins=(
            base_url,
            *(endpoint["origin"] for endpoint in configured_endpoints),
        )
    )
    observer.configured_endpoints = configured_endpoints
    with observer.observe_python_network():
        yield observer
    observer.assert_clean()


def _launch_browser(playwright: Any) -> Any:
    requested_channel = _browser_channel()
    if requested_channel != "playwright-managed-chromium":
        return playwright.chromium.launch(channel=requested_channel, headless=True)
    return playwright.chromium.launch(headless=True)


def _request_local_api_get(
    request: Any,
    url: str,
    *,
    headers: dict[str, str],
) -> Any:
    return request.get(
        url,
        headers=headers,
        max_redirects=0,
    )


def _record_active_focus(page: Any, focus_trace: list[dict[str, Any]]) -> dict[str, Any]:
    focused = page.evaluate(
        """() => {
          const element = document.activeElement;
          const style = element ? getComputedStyle(element) : null;
          return {
            tag: element?.tagName?.toLowerCase() || "",
            id: element?.id || "",
            aria_label: element?.getAttribute?.("aria-label") || "",
            review_action: element?.dataset?.reviewActionName || "",
            visible_focus: Boolean(
              element &&
              element.matches(":focus-visible") &&
              style &&
              style.outlineStyle !== "none" &&
              parseFloat(style.outlineWidth) > 0
            ),
          };
        }"""
    )
    if not isinstance(focused, dict):
        raise AssertionError("keyboard focus inspection did not return an object")
    if focused["tag"] in {"a", "button", "input", "select", "textarea"}:
        focus_trace.append(focused)
    return focused


def _tab_to(
    page: Any,
    selector: str,
    focus_trace: list[dict[str, Any]],
    *,
    limit: int = 120,
) -> Any:
    target = page.locator(selector).first
    for _ in range(limit):
        page.keyboard.press("Tab")
        focused = _record_active_focus(page, focus_trace)
        if target.evaluate("(target) => target === document.activeElement"):
            if not focused["visible_focus"]:
                raise AssertionError(f"keyboard target did not expose visible focus: {selector}")
            return target
    raise AssertionError(f"keyboard target was not reachable in tab order: {selector}")


def _keyboard_activate(
    page: Any,
    selector: str,
    focus_trace: list[dict[str, Any]],
) -> Any:
    target = _tab_to(page, selector, focus_trace)
    page.keyboard.press("Enter")
    return target


def run_browser_e2e(
    *,
    evidence_root: Path,
    expected_rerun_package: dict[str, Any] | None = None,
    retained_rerun_package_path: Path | None = None,
) -> dict[str, Any]:
    """Exercise recovery and upload-to-download paths and return evidence metadata."""
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised by setup failures
        raise RuntimeError(
            "Playwright is required; install requirements-browser-e2e.txt and run "
            "`python3 -m playwright install chromium`."
        ) from exc

    run_id = f"p12g03-{uuid.uuid4().hex}"
    run_dir = evidence_root / run_id
    run_dir.mkdir(parents=True)
    trace_path = run_dir / "trace.zip"
    recovery_screenshot = run_dir / "01-recovery.png"
    completed_screenshot = run_dir / "02-completed-review.png"
    audit_screenshot = run_dir / "03-audit.png"
    keyboard_screenshot = run_dir / "04-keyboard-high-risk-review.png"
    api_result_path = run_dir / "api-result.json"
    high_risk_api_result_path = run_dir / "high-risk-api-result.json"
    job_events_path = run_dir / "job-events.json"
    job_response_path = run_dir / "job-response.json"
    review_events_path = run_dir / "review-events.json"
    rerun_package_path = run_dir / "rerun-package.json"
    auth_token = uuid.uuid4().hex
    approver_actor = f"e2e-{uuid.uuid4().hex}"
    source_sha256 = _sha256(FIXTURE_PATH)

    with tempfile.TemporaryDirectory(prefix="veridoc-browser-e2e-") as state_dir:
        high_risk_fixture = _high_risk_fixture()
        with _poc_server(
            Path(state_dir),
            auth_token=auth_token,
            approver_actor=approver_actor,
        ) as base_url, _acceptance_network_boundary(
            base_url
        ) as network_boundary, sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            browser_version = str(browser.version)
            if expected_rerun_package is not None:
                validate_rerun_runtime_dependencies(
                    expected_rerun_package,
                    browser_version=browser_version,
                )
            context = browser.new_context(accept_downloads=True)
            network_boundary.install_playwright_guard(context)
            page = context.new_page()
            focus_trace: list[dict[str, Any]] = []
            tracing_started = False
            raw_trace_path = Path(state_dir) / "trace.zip"
            try:
                page.goto(base_url, wait_until="domcontentloaded")
                page.locator("#auth-token").fill(auth_token)
                page.locator("#save-auth-token").click()
                expect(page.locator('#auth-status[data-auth-state="configured"]')).to_be_visible()
                page.locator("#auth-token").fill("")
                expect(page.locator("#auth-token")).to_have_value("")
                context.tracing.start(screenshots=True, snapshots=True, sources=True)
                tracing_started = True
                page.locator('[data-nav-target="upload"]').click()
                page.locator("#document-file").set_input_files(str(FIXTURE_PATH))

                # Deliberately choose an incompatible mode to prove the visible
                # failure/recovery path before retrying with the correct setting.
                page.locator("#direct-conversion-mode").select_option("word_to_excel")
                page.locator("#convert-button").click()
                expect(page.locator("#direct-convert-error")).to_be_visible(timeout=30_000)
                recovery_message = page.locator("#direct-convert-error").inner_text().strip()
                if not recovery_message:
                    raise AssertionError("recovery path did not expose a user-visible error")
                page.screenshot(path=str(recovery_screenshot), full_page=True)

                page.locator('[data-nav-target="upload"]').click()
                page.locator("#direct-conversion-mode").select_option("pdf_to_word")
                page.locator("#convert-button").click()
                expect(page.locator("#status")).to_contain_text(
                    re.compile(r"converted|requires_review"), timeout=30_000
                )
                conversion_status = page.locator("#status").inner_text().strip()
                if conversion_status not in {"converted", "requires_review"}:
                    raise AssertionError(
                        f"completed conversion has unexpected status: {conversion_status!r}"
                    )
                expect(page.locator("#artifact-downloads-panel")).to_be_visible(timeout=10_000)
                expect(page.locator("#pdf-preview-panel")).to_be_visible(timeout=10_000)
                preview_canvas = page.locator("#pdf-page-canvas")
                expect(preview_canvas).to_be_visible(timeout=30_000)
                preview_size = preview_canvas.evaluate(
                    "(canvas) => ({width: canvas.width, height: canvas.height})"
                )
                if preview_size["width"] <= 0 or preview_size["height"] <= 0:
                    raise AssertionError("PDF preview canvas did not render any pixels")
                expect(page.locator("#review-list .review-item").first).to_be_visible(
                    timeout=10_000
                )

                result = json.loads(page.locator("#raw-result").inner_text())
                if not isinstance(result, dict):
                    raise AssertionError("completed browser result must be a JSON object")
                api_result_path.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                result_audit = result.get("audit")
                if not isinstance(result_audit, dict):
                    raise AssertionError(
                        "completed browser result did not contain audit metadata"
                    )
                conversion_id = result_audit.get("conversion_id")
                if not isinstance(conversion_id, str) or not conversion_id:
                    raise AssertionError(
                        "completed browser result did not bind an audit conversion ID"
                    )
                if result_audit.get("source_sha256") != source_sha256:
                    raise AssertionError(
                        "completed browser result audit did not match the uploaded fixture"
                    )
                job_id = result.get("job_id")
                if not isinstance(job_id, str) or not job_id:
                    page_status = page.locator("#page-status")
                    expect(page_status).to_contain_text(
                        re.compile(r"Conversion job job-[a-zA-Z0-9_-]+ finished\."),
                        timeout=30_000,
                    )
                    status_text = page_status.inner_text()
                    match = re.search(r"(job-[a-zA-Z0-9_-]+)", status_text)
                    if not match:
                        raise AssertionError("completed browser result did not expose a job ID")
                    job_id = match.group(1)
                auth_headers = {"Authorization": f"Bearer {auth_token}"}
                network_boundary.observe_http_attempt(
                    base_url + f"/api/jobs/{job_id}",
                    method="GET",
                    source="playwright_api_request",
                )
                job_response = _request_local_api_get(
                    context.request,
                    base_url + f"/api/jobs/{job_id}", headers=auth_headers
                )
                if not job_response.ok:
                    raise AssertionError("completed browser job could not be reloaded")
                authoritative_job = _json_response(job_response).get("job", {})
                job_status = authoritative_job.get("status")
                if job_status != "succeeded":
                    raise AssertionError(
                        f"completed browser job has unexpected status: {job_status!r}"
                    )
                job_response_path.write_text(
                    json.dumps(authoritative_job, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

                approve = page.locator(
                    '#review-list button[data-review-action-name="approve"]:not([disabled])'
                ).first
                expect(approve).to_be_visible(timeout=10_000)
                review_action_key = approve.get_attribute("data-review-action-key")
                review_items = result.get("review_items")
                if not isinstance(review_items, list):
                    raise AssertionError(
                        "completed browser result did not contain review items"
                    )
                review_item = next(
                    (
                        item
                        for item in review_items
                        if isinstance(item, dict)
                        and review_action_key
                        == f"{item.get('document_id')}:{item.get('block_id')}"
                    ),
                    None,
                )
                if review_item is None:
                    raise AssertionError(
                        "browser approval target was not bound to a current result review item"
                    )
                review_document_id = review_item.get("document_id")
                review_block_id = review_item.get("block_id")
                review_source_page = review_item.get("source_page")
                review_source_bbox = review_item.get("source_bbox")
                if (
                    not isinstance(review_document_id, str)
                    or not review_document_id
                    or not isinstance(review_block_id, str)
                    or not review_block_id
                    or not isinstance(review_source_page, int)
                    or review_source_page < 1
                    or not _valid_source_bbox(review_source_bbox)
                ):
                    raise AssertionError(
                        "browser approval target did not expose authoritative review "
                        "provenance"
                    )
                _keyboard_activate(
                    page,
                    (
                        '#review-list button[data-review-action-name="approve"]'
                        ':not([disabled])'
                    ),
                    focus_trace,
                )
                expect(page.locator("#review-action-status")).to_contain_text(
                    "queued for audit", timeout=10_000
                )
                page.screenshot(path=str(completed_screenshot), full_page=True)

                artifact = next(
                    (
                        item
                        for item in result.get("artifacts", [])
                        if isinstance(item, dict) and item.get("id", "").startswith("primary-")
                    ),
                    None,
                )
                if artifact is None:
                    raise AssertionError("completed browser result did not contain a primary artifact")
                artifact_id = artifact.get("artifact_id")
                if not isinstance(artifact_id, str) or not artifact_id:
                    raise AssertionError(
                        "completed browser result did not bind the primary artifact ID"
                    )
                artifact_href = artifact.get("href")
                if artifact_href != f"/api/artifacts/{artifact_id}":
                    raise AssertionError(
                        "completed browser result did not expose a persisted primary artifact"
                    )
                network_boundary.observe_http_attempt(
                    base_url + artifact_href,
                    method="GET",
                    source="playwright_api_request",
                )
                persisted_artifact_response = _request_local_api_get(
                    context.request,
                    base_url + artifact_href, headers=auth_headers
                )
                if not persisted_artifact_response.ok:
                    raise AssertionError("persisted primary artifact download failed")
                persisted_artifact_content = persisted_artifact_response.body()
                persisted_artifact_sha256 = hashlib.sha256(
                    persisted_artifact_content
                ).hexdigest()
                if persisted_artifact_sha256 != artifact.get("sha256"):
                    raise AssertionError(
                        "persisted primary artifact hash did not match the API result"
                    )
                with page.expect_download(timeout=10_000) as download_info:
                    page.locator("#download-link").click()
                download = download_info.value
                download_name = f"download-{download.suggested_filename}"
                download_path = run_dir / download_name
                download.save_as(download_path)
                downloaded_sha256 = _sha256(download_path)
                if downloaded_sha256 != persisted_artifact_sha256:
                    raise AssertionError(
                        "browser download did not match the persisted primary artifact"
                    )
                artifact_audit_sha256 = artifact.get("metadata", {}).get("output_sha256")
                if downloaded_sha256 != artifact_audit_sha256:
                    raise AssertionError(
                        "downloaded artifact hash did not match its audit metadata"
                    )

                audit_artifact = next(
                    (
                        item
                        for item in result.get("artifacts", [])
                        if isinstance(item, dict) and item.get("id") == "audit-json"
                    ),
                    None,
                )
                if audit_artifact is None:
                    raise AssertionError(
                        "completed browser result did not contain the audit-json artifact"
                    )
                audit_artifact_id = audit_artifact.get("artifact_id")
                if not isinstance(audit_artifact_id, str) or not audit_artifact_id:
                    raise AssertionError(
                        "completed browser result did not bind the audit artifact ID"
                    )
                audit_artifact_href = audit_artifact.get("href")
                if audit_artifact_href != f"/api/artifacts/{audit_artifact_id}":
                    raise AssertionError(
                        "completed browser result did not expose a persisted audit artifact"
                    )
                network_boundary.observe_http_attempt(
                    base_url + audit_artifact_href,
                    method="GET",
                    source="playwright_api_request",
                )
                audit_response = _request_local_api_get(
                    context.request,
                    base_url + audit_artifact_href, headers=auth_headers
                )
                if not audit_response.ok:
                    raise AssertionError("audit JSON artifact download failed")
                audit_content = audit_response.body()
                audit_downloaded_sha256 = hashlib.sha256(audit_content).hexdigest()
                if audit_downloaded_sha256 != audit_artifact.get("sha256"):
                    raise AssertionError(
                        "downloaded audit artifact hash did not match the API result"
                    )
                try:
                    audit_payload = json.loads(audit_content)
                except (TypeError, ValueError) as exc:
                    raise AssertionError(
                        "downloaded audit artifact was not valid JSON"
                    ) from exc
                if not isinstance(audit_payload, dict):
                    raise AssertionError(
                        "downloaded audit artifact must be a JSON object"
                    )
                _require_audit_payload_matches_result(audit_payload, result)
                audit_artifact_path = run_dir / "audit-artifact.json"
                audit_artifact_path.write_bytes(audit_content)

                page.locator('[data-nav-target="audit"]').click()
                page.locator("#refresh-audit").click()
                expect(page.locator("#audit-body tr").first).to_be_visible(timeout=10_000)
                page.screenshot(path=str(audit_screenshot), full_page=True)

                page.locator('[data-nav-target="upload"]').click()
                page.locator("#document-file").set_input_files(
                    str(FIXTURE_PATH)
                )
                page.locator("#direct-conversion-mode").select_option("auto")
                high_risk_template_id = high_risk_fixture["template_definition"][
                    "template_id"
                ]
                page.locator("#direct-template").select_option(high_risk_template_id)
                page.locator("#convert-button").click()
                expect(page.locator("#status")).to_have_text(
                    "requires_review",
                    timeout=30_000,
                )
                high_risk_items = page.locator(
                    '#review-list .review-item[data-review-risk="high"]'
                )
                expect(high_risk_items.first).to_be_visible(timeout=10_000)
                if high_risk_items.count() < 1:
                    raise AssertionError(
                        "high-risk browser fixture did not expose a review target"
                    )
                high_risk_result = json.loads(page.locator("#raw-result").inner_text())
                if not isinstance(high_risk_result, dict):
                    raise AssertionError(
                        "high-risk browser result must be a JSON object"
                    )
                high_risk_audit = high_risk_result.get("audit")
                if not isinstance(high_risk_audit, dict) or (
                    high_risk_audit.get("source_filename") != FIXTURE_PATH.name
                    or high_risk_audit.get("source_type") != "pdf"
                    or high_risk_audit.get("source_sha256") != source_sha256
                ):
                    raise AssertionError(
                        "high-risk review evidence was not bound to the real PDF source"
                    )
                high_risk_api_result_path.write_text(
                    json.dumps(high_risk_result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                high_risk_api_items = [
                    item
                    for item in high_risk_result.get("review_items", [])
                    if isinstance(item, dict) and item.get("high_risk") is True
                ]
                if len(high_risk_api_items) != high_risk_items.count():
                    raise AssertionError(
                        "high-risk API targets did not match the review UI"
                    )
                auto_confirmed_count = sum(
                    item.get("auto_confirmed") is True for item in high_risk_api_items
                )
                if auto_confirmed_count:
                    raise AssertionError("high-risk review target was auto-confirmed")

                first_high_risk_item = high_risk_api_items[0]
                first_high_risk_block = first_high_risk_item["block_id"]
                first_item_selector = (
                    '#review-list .review-item[data-block-id="'
                    + first_high_risk_block
                    + '"]'
                )
                warning_button = _keyboard_activate(
                    page,
                    first_item_selector + " .warning-badge",
                    focus_trace,
                )
                focused_overlay = page.locator(
                    '#bbox-layer .bbox-overlay[data-block-id="'
                    + first_high_risk_block
                    + '"]'
                )
                expect(focused_overlay).to_be_focused(timeout=10_000)
                _record_active_focus(page, focus_trace)
                source_jump_page = int(
                    page.locator("#preview-page-select").input_value()
                )
                overlay_percent = focused_overlay.evaluate(
                    """(element) => ({
                      x: parseFloat(element.style.left),
                      y: parseFloat(element.style.top),
                      width: parseFloat(element.style.width),
                      height: parseFloat(element.style.height),
                    })"""
                )
                source_geometry = first_high_risk_item["source_page_geometry"]
                source_jump_bbox = {
                    key: round(
                        overlay_percent[key]
                        * source_geometry[
                            "width" if key in {"x", "width"} else "height"
                        ]
                        / 100,
                        3,
                    )
                    for key in ("x", "y", "width", "height")
                }
                source_jump_bbox.update(
                    {
                        "unit": first_high_risk_item["source_bbox"]["unit"],
                        "origin": first_high_risk_item["source_bbox"]["origin"],
                    }
                )
                warning_details_payload = first_high_risk_item.get("warning_details")
                if not isinstance(warning_details_payload, list) or not warning_details_payload:
                    raise AssertionError(
                        "high-risk review target did not expose warning details"
                    )
                warning_evidence = warning_details_payload[0]
                warning_text = warning_button.inner_text()
                for warning_field in ("code", "message", "remediation"):
                    warning_value = warning_evidence.get(warning_field)
                    if not isinstance(warning_value, str) or warning_value not in warning_text:
                        raise AssertionError(
                            f"warning UI did not match API {warning_field}"
                        )

                edit = _tab_to(
                    page,
                    first_item_selector + " .review-edit",
                    focus_trace,
                )
                page.keyboard.press("ControlOrMeta+A")
                revised_text = first_high_risk_item["text"] + " verified"
                page.keyboard.type(revised_text)
                expect(edit).to_have_value(revised_text)
                _keyboard_activate(
                    page,
                    first_item_selector
                    + ' button[data-review-action-name="edit"]:not([disabled])',
                    focus_trace,
                )
                expect(page.locator("#review-action-status")).to_contain_text(
                    "queued for audit",
                    timeout=10_000,
                )
                _keyboard_activate(
                    page,
                    first_item_selector
                    + ' button[data-review-action-name="needs_fix"]:not([disabled])',
                    focus_trace,
                )
                expect(
                    page.locator(
                        first_item_selector + ' [data-review-state-for="'
                        + first_high_risk_block
                        + '"]'
                    )
                ).to_have_text("needs fix")
                blocked_before_approval = (
                    high_risk_items.first.get_attribute("data-review-state")
                    == "needs_fix"
                )
                approval_selector = (
                    first_item_selector
                    + ' button[data-review-action-name="approve"]:not([disabled])'
                )
                approval_state = page.locator(
                    first_item_selector + ' [data-review-state-for="'
                    + first_high_risk_block
                    + '"]'
                )
                _keyboard_activate(
                    page,
                    approval_selector,
                    focus_trace,
                )
                approval_status = page.locator("#review-action-status")
                expect(approval_status).to_contain_text(
                    "review approval is blocked while needs-fix is unresolved",
                    timeout=10_000,
                )
                expect(approval_state).to_have_text("needs fix")
                approval_block_message = approval_status.inner_text().strip()
                with page.expect_response(
                    lambda response: (
                        response.request.method == "POST"
                        and response.url == base_url + "/api/review-events"
                    )
                ) as reject_response_info:
                    _keyboard_activate(
                        page,
                        first_item_selector
                        + ' button[data-review-action-name="reject"]:not([disabled])',
                        focus_trace,
                    )
                reject_response = reject_response_info.value
                reject_payload = _json_response(reject_response)
                reject_event = reject_payload.get("audit_event")
                if (
                    not reject_response.ok
                    or not isinstance(reject_event, dict)
                    or reject_event.get("action") != "reject"
                    or reject_event.get("block_id") != first_high_risk_block
                ):
                    raise AssertionError(
                        "keyboard reject audit request was not accepted for the target"
                    )

                page.screenshot(path=str(keyboard_screenshot), full_page=True)

                network_boundary.observe_http_attempt(
                    base_url + "/api/job-events",
                    method="GET",
                    source="playwright_api_request",
                )
                job_events_response = _request_local_api_get(
                    context.request,
                    base_url + "/api/job-events",
                    headers=auth_headers,
                )
                if not job_events_response.ok:
                    raise AssertionError("job audit event lookup failed")
                job_events = _events(_json_response(job_events_response))
                upload_event, upload_event_count = _require_matching_event(
                    job_events,
                    expected_fields={
                        "action": "browser_upload",
                        "job_id": job_id,
                        "filename": FIXTURE_PATH.name,
                        "source_sha256": source_sha256,
                    },
                    description="browser upload audit event",
                )
                job_events_path.write_text(
                    json.dumps(job_events, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                network_boundary.observe_http_attempt(
                    base_url + "/api/review-events",
                    method="GET",
                    source="playwright_api_request",
                )
                review_events_response = _request_local_api_get(
                    context.request,
                    base_url + "/api/review-events",
                    headers=auth_headers,
                )
                if not review_events_response.ok:
                    raise AssertionError("review audit event lookup failed")
                review_events = _events(_json_response(review_events_response))
                expected_review_actor = {
                    "id": f"local-principal:{approver_actor}",
                    "role": "approver",
                }
                review_event, approval_event_count = _require_matching_event(
                    review_events,
                    expected_fields={
                        "action": "approve",
                        "conversion_id": conversion_id,
                        "document_id": review_document_id,
                        "block_id": review_block_id,
                        "actor": expected_review_actor,
                        "source_page": review_source_page,
                        "source_bbox": review_source_bbox,
                    },
                    description="browser approval audit event",
                )
                high_risk_conversion_id = high_risk_result["audit"]["conversion_id"]
                for action, target in (
                    ("edit", first_high_risk_item),
                    ("needs_fix", first_high_risk_item),
                    ("reject", first_high_risk_item),
                ):
                    _require_matching_event(
                        review_events,
                        expected_fields={
                            "action": action,
                            "conversion_id": high_risk_conversion_id,
                            "document_id": target["document_id"],
                            "block_id": target["block_id"],
                            "actor": expected_review_actor,
                        },
                        description=f"keyboard {action} audit event",
                    )
                review_events_path.write_text(
                    json.dumps(review_events, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                review_actor = review_event["actor"]
                network_boundary.assert_clean()
                evidence = {
                    "schema_version": BROWSER_EVIDENCE_SCHEMA_VERSION,
                    "run_id": run_id,
                    "network_observation": {
                        **network_boundary.result(),
                        "configured_endpoints": network_boundary.configured_endpoints,
                    },
                    "correlation": {
                        "run_id": run_id,
                        "upload": {
                            "source_filename": FIXTURE_PATH.name,
                            "source_sha256": source_sha256,
                        },
                        "job": {
                            "job_id": job_id,
                            "status": job_status,
                            "conversion_status": conversion_status,
                            "created_at": authoritative_job.get("created_at"),
                        },
                        "review": {
                            "conversion_id": review_event.get("conversion_id"),
                            "document_id": review_event.get("document_id"),
                            "block_id": review_event.get("block_id"),
                            "action": review_event.get("action"),
                            "actor": review_actor,
                        },
                        "provenance": {
                            "source_filename": result_audit.get("source_filename"),
                            "source_sha256": result_audit.get("source_sha256"),
                            "document_id": review_document_id,
                            "block_id": review_block_id,
                            "source_page": review_source_page,
                            "source_bbox": review_source_bbox,
                        },
                        "artifact": {
                            "artifact_id": artifact_id,
                            "filename": artifact.get("filename"),
                            "sha256": downloaded_sha256,
                        },
                        "audit": {
                            "artifact_sha256": artifact_audit_sha256,
                            "audit_artifact_sha256": audit_downloaded_sha256,
                            "job_event_count": len(job_events),
                            "review_event_count": len(review_events),
                            "job_terminal_event_hash": job_events[-1].get(
                                "event_hash"
                            ),
                            "review_terminal_event_hash": review_events[-1].get(
                                "event_hash"
                            ),
                        },
                    },
                    "evidence_surfaces": {
                        "browser_run": {
                            "correlation_id": run_id,
                            "job_id": job_id,
                        },
                        "harness_result": {
                            "correlation_id": run_id,
                            "conversion_id": conversion_id,
                        },
                        "download_artifact": {
                            "correlation_id": run_id,
                            "artifact_id": artifact_id,
                        },
                        "audit_events": {
                            "correlation_id": run_id,
                            "job_event_hash": upload_event.get("event_hash"),
                            "review_event_hash": review_event.get("event_hash"),
                            "job_event_count": len(job_events),
                            "review_event_count": len(review_events),
                            "job_terminal_event_hash": job_events[-1].get(
                                "event_hash"
                            ),
                            "review_terminal_event_hash": review_events[-1].get(
                                "event_hash"
                            ),
                        },
                    },
                    "recovery": {
                        "user_visible_error": recovery_message,
                        "retry_mode": "pdf_to_word",
                        "result": "completed",
                    },
                    "review_flow": {
                        "keyboard_only": True,
                        "focus_trace": focus_trace,
                        "actions": ["edit", "needs_fix", "approve", "reject"],
                        "warnings": [warning_evidence],
                        "high_risk": {
                            "conversion_id": high_risk_conversion_id,
                            "review_target_count": len(high_risk_api_items),
                            "auto_confirmed_count": auto_confirmed_count,
                            "approval_blocked_while_unresolved": True,
                            "approval_block_reason": approval_block_message,
                        },
                        "source_jump": {
                            "block_id": first_high_risk_block,
                            "source_filename": high_risk_audit["source_filename"],
                            "source_type": high_risk_audit["source_type"],
                            "source_sha256": high_risk_audit["source_sha256"],
                            "page": source_jump_page,
                            "review_item_page": first_high_risk_item["source_page"],
                            "bbox": source_jump_bbox,
                            "review_item_bbox": first_high_risk_item["source_bbox"],
                        },
                        "unresolved": {
                            "blocked_before_approval": blocked_before_approval,
                            "block_id": first_high_risk_block,
                            "state": "needs_fix",
                        },
                    },
                    "files": {
                        "trace": trace_path.name,
                        "screenshots": [
                            recovery_screenshot.name,
                            completed_screenshot.name,
                            audit_screenshot.name,
                            keyboard_screenshot.name,
                        ],
                        "api_result": api_result_path.name,
                        "high_risk_api_result": high_risk_api_result_path.name,
                        "job_events": job_events_path.name,
                        "job_response": job_response_path.name,
                        "review_events": review_events_path.name,
                        "audit_artifact": audit_artifact_path.name,
                        "download": download_name,
                        "rerun_package": rerun_package_path.name,
                    },
                }
                if expected_rerun_package is not None:
                    evidence["rerun_equivalence"] = _compare_projection_to_package(
                        expected_rerun_package,
                        evidence,
                    )
                rerun_package = _build_accepted_rerun_package(
                    evidence,
                    run_dir=run_dir,
                    browser_version=browser_version,
                    generated_evidence_dir=run_dir,
                    retained_rerun_package_path=retained_rerun_package_path,
                )
                rerun_package_path.write_text(
                    json.dumps(rerun_package, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                (run_dir / "evidence.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                if evidence["acceptance_snapshot"]["status"] != "pass":
                    raise AssertionError(
                        "browser acceptance evidence failed closed: "
                        + json.dumps(
                            evidence["acceptance_snapshot"]["failure_reasons"],
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
                return evidence
            finally:
                if tracing_started:
                    context.tracing.stop(path=str(raw_trace_path))
                    _retain_redacted_trace(
                        raw_trace_path, trace_path, secret=auth_token
                    )
                context.close()
                browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the local-only MVP browser acceptance scenario."
    )
    parser.add_argument(
        "--rerun-package",
        type=Path,
        help="validate and rerun a previously retained rerun-package.json",
    )
    args = parser.parse_args()
    evidence_root = Path(
        os.environ.get("VERIDOC_E2E_EVIDENCE_DIR", "artifacts/mvp-browser-e2e")
    )
    expected_package = None
    if args.rerun_package is not None:
        envelope = json.loads(args.rerun_package.read_text(encoding="utf-8"))
        expected_package = validate_rerun_package_for_workspace(
            envelope,
            rerun_package_path=args.rerun_package,
        )
    evidence = run_browser_e2e(
        evidence_root=evidence_root,
        expected_rerun_package=expected_package,
        retained_rerun_package_path=args.rerun_package,
    )
    print(evidence_root / evidence["run_id"] / "evidence.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
