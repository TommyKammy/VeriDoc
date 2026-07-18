from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from scripts.ci.mvp_browser_e2e import (
    FIXTURE_PATH,
    LocalNetworkBoundaryObserver,
    NetworkBoundaryViolation,
    _acceptance_network_boundary,
    _assert_clean_git_checkout,
    _launch_browser,
    _request_local_api_get,
    _require_audit_payload_matches_result,
    _require_matching_event,
    assert_rerun_equivalent,
    build_rerun_package,
    main,
    seal_rerun_package,
    validate_endpoint_configuration,
    validate_rerun_package_envelope,
    validate_rerun_package_for_workspace,
    validate_rerun_runtime_dependencies,
    run_browser_e2e,
)


def _browser_e2e_runtime_available() -> bool:
    if importlib.util.find_spec("playwright") is None:
        return False
    if os.environ.get("VERIDOC_E2E_BROWSER_CHANNEL"):
        return True
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).is_file()
    except Exception:
        return False


BROWSER_E2E_RUNTIME_AVAILABLE = _browser_e2e_runtime_available()


class BrowserLaunchSelectionTest(unittest.TestCase):
    def test_default_launch_uses_playwright_managed_chromium(self) -> None:
        class Chromium:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def launch(self, **kwargs: object) -> object:
                self.calls.append(kwargs)
                return object()

        chromium = Chromium()
        playwright = type("Playwright", (), {"chromium": chromium})()

        with patch.dict(os.environ, {}, clear=True):
            _launch_browser(playwright)

        self.assertEqual(chromium.calls, [{"headless": True}])

    def test_empty_channel_uses_playwright_managed_chromium(self) -> None:
        class Chromium:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def launch(self, **kwargs: object) -> object:
                self.calls.append(kwargs)
                return object()

        chromium = Chromium()
        playwright = type("Playwright", (), {"chromium": chromium})()

        with patch.dict(
            os.environ,
            {"VERIDOC_E2E_BROWSER_CHANNEL": ""},
            clear=True,
        ):
            _launch_browser(playwright)

        self.assertEqual(chromium.calls, [{"headless": True}])

    def test_configured_channel_is_an_explicit_override(self) -> None:
        class Chromium:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def launch(self, **kwargs: object) -> object:
                self.calls.append(kwargs)
                return object()

        chromium = Chromium()
        playwright = type("Playwright", (), {"chromium": chromium})()

        with patch.dict(
            os.environ,
            {"VERIDOC_E2E_BROWSER_CHANNEL": "chrome"},
            clear=True,
        ):
            _launch_browser(playwright)

        self.assertEqual(
            chromium.calls,
            [{"channel": "chrome", "headless": True}],
        )


class LocalApiRequestTest(unittest.TestCase):
    def test_get_disables_redirects_before_the_response_is_observed(self) -> None:
        response = object()

        class Request:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, object]]] = []

            def get(self, url: str, **kwargs: object) -> object:
                self.calls.append((url, kwargs))
                return response

        request = Request()

        self.assertIs(
            _request_local_api_get(
                request,
                "http://127.0.0.1:8788/api/jobs/job-test",
                headers={"Authorization": "Bearer local-token"},
            ),
            response,
        )
        self.assertEqual(
            request.calls,
            [
                (
                    "http://127.0.0.1:8788/api/jobs/job-test",
                    {
                        "headers": {"Authorization": "Bearer local-token"},
                        "max_redirects": 0,
                    },
                )
            ],
        )


class EvidenceBoundaryValidationTest(unittest.TestCase):
    def test_matching_event_requires_every_authoritative_field(self) -> None:
        expected_fields = {
            "action": "browser_upload",
            "job_id": "job-current",
            "source_sha256": "a" * 64,
        }
        current_event = {**expected_fields, "event_type": "web.job_operation"}
        stale_event = {**current_event, "source_sha256": "b" * 64}

        event, count = _require_matching_event(
            [stale_event, current_event],
            expected_fields=expected_fields,
            description="browser upload audit event",
        )

        self.assertIs(event, current_event)
        self.assertEqual(count, 1)

    def test_matching_event_rejects_approval_for_another_block(self) -> None:
        expected_fields = {
            "action": "approve",
            "conversion_id": "conversion-current",
            "document_id": "document-current",
            "block_id": "block-current",
        }

        with self.assertRaisesRegex(
            AssertionError,
            "browser approval audit event was not bound to the browser run",
        ):
            _require_matching_event(
                [{**expected_fields, "block_id": "block-stale"}],
                expected_fields=expected_fields,
                description="browser approval audit event",
            )

    def test_audit_artifact_must_equal_current_result_audit(self) -> None:
        current_audit = {
            "conversion_id": "conversion-current",
            "source_sha256": "a" * 64,
            "schema_version": "veridoc-poc-conversion-audit/v1",
        }
        self.assertIs(
            _require_audit_payload_matches_result(
                current_audit,
                {"audit": current_audit},
            ),
            current_audit,
        )

        with self.assertRaisesRegex(
            AssertionError,
            "did not match the current browser result audit",
        ):
            _require_audit_payload_matches_result(
                {**current_audit, "conversion_id": "conversion-stale"},
                {"audit": current_audit},
            )


class LocalNetworkBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.observer = LocalNetworkBoundaryObserver(
            allowed_origins=("http://127.0.0.1:8765",)
        )

    def test_local_http_attempt_is_recorded_without_external_send(self) -> None:
        self.observer.observe_http_attempt(
            "http://127.0.0.1:8765/api/jobs",
            method="POST",
            source="playwright",
        )

        result = self.observer.result()

        self.assertEqual(result["http_attempt_count"], 1)
        self.assertEqual(result["external_attempt_count"], 0)
        self.assertEqual(result["external_ai_api_send_count"], 0)
        self.assertEqual(result["status"], "pass")

    def test_external_http_attempt_is_recorded_and_rejected(self) -> None:
        with self.assertRaisesRegex(
            NetworkBoundaryViolation,
            "external HTTP attempt blocked",
        ):
            self.observer.observe_http_attempt(
                "https://api.example.invalid/v1/chat/completions",
                method="POST",
                source="playwright",
            )

        result = self.observer.result()
        self.assertEqual(result["external_attempt_count"], 1)
        self.assertEqual(result["external_ai_api_send_count"], 1)
        self.assertEqual(result["status"], "fail")

    def test_external_dns_attempt_is_recorded_and_rejected(self) -> None:
        with self.assertRaisesRegex(
            NetworkBoundaryViolation,
            "external DNS attempt blocked",
        ):
            self.observer.observe_dns_attempt(
                "api.example.invalid",
                source="python_socket",
            )

        result = self.observer.result()
        self.assertEqual(result["dns_attempt_count"], 1)
        self.assertEqual(result["external_attempt_count"], 1)

    def test_external_endpoint_configuration_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            NetworkBoundaryViolation,
            "OPENAI_BASE_URL.*external endpoint",
        ):
            validate_endpoint_configuration(
                {"OPENAI_BASE_URL": "https://api.example.invalid/v1"},
                allowed_origins=self.observer.allowed_origins,
            )

    def test_external_profile_endpoint_configuration_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            NetworkBoundaryViolation,
            "VERIDOC_STANDARD_OPENAI_BASE_URL.*external endpoint",
        ):
            validate_endpoint_configuration(
                {
                    "VERIDOC_STANDARD_OPENAI_BASE_URL": (
                        "https://api.example.invalid/v1"
                    )
                },
                allowed_origins=self.observer.allowed_origins,
            )

    def test_private_profile_endpoint_configuration_is_allowed(self) -> None:
        configured = validate_endpoint_configuration(
            {
                "VERIDOC_STANDARD_OPENAI_BASE_URL": (
                    "http://192.168.1.10:8000/v1"
                )
            },
            allowed_origins=self.observer.allowed_origins,
        )

        self.assertEqual(
            configured,
            [
                {
                    "name": "VERIDOC_STANDARD_OPENAI_BASE_URL",
                    "origin": "http://192.168.1.10:8000",
                }
            ],
        )

    def test_configured_private_endpoint_is_allowed_by_runtime_guards(self) -> None:
        endpoint_environment = {
            "VERIDOC_STANDARD_OPENAI_BASE_URL": "http://192.168.1.10:8000/v1",
            "VERIDOC_HIGH_QUALITY_OPENAI_BASE_URL": "http://[fd00::10]:8001/v1",
        }
        with patch.dict(os.environ, endpoint_environment, clear=True):
            with _acceptance_network_boundary(
                "http://127.0.0.1:8765"
            ) as observer:
                observer.observe_dns_attempt(
                    "192.168.1.10",
                    source="python_socket",
                )
                observer.observe_socket_attempt(
                    ("192.168.1.10", 8000),
                    source="python_socket",
                )
                observer.observe_dns_attempt(
                    "fd00::10",
                    source="python_socket",
                )
                observer.observe_socket_attempt(
                    ("fd00::10", 8001, 0, 0),
                    source="python_socket",
                )

        result = observer.result()
        self.assertIn("http://192.168.1.10:8000", result["allowed_origins"])
        self.assertIn("http://[fd00::10]:8001", result["allowed_origins"])
        self.assertEqual(result["external_attempt_count"], 0)

    def test_unconfigured_private_socket_target_is_rejected(self) -> None:
        observer = LocalNetworkBoundaryObserver(
            allowed_origins=(
                "http://127.0.0.1:8765",
                "http://192.168.1.10:8000",
            )
        )

        with self.assertRaisesRegex(
            NetworkBoundaryViolation,
            "external socket attempt blocked",
        ):
            observer.observe_socket_attempt(
                ("192.168.1.10", 8001),
                source="python_socket",
            )

        self.assertEqual(observer.result()["external_attempt_count"], 1)


class RerunPackageValidationTest(unittest.TestCase):
    def test_dirty_checkout_is_rejected_before_package_sealing(self) -> None:
        with patch(
            "scripts.ci.mvp_browser_e2e._git_status_porcelain",
            return_value=" M scripts/ci/mvp_browser_e2e.py\n",
        ):
            with self.assertRaisesRegex(ValueError, "dirty checkout"):
                _assert_clean_git_checkout()

    def test_generated_evidence_dir_is_excluded_from_clean_check(self) -> None:
        repo_root = Path("repo").resolve()
        generated_evidence_dir = repo_root / "evidence" / "run"
        with patch("scripts.ci.mvp_browser_e2e.subprocess.run") as run:
            run.return_value.stdout = ""

            _assert_clean_git_checkout(
                repo_root,
                excluded_paths=(generated_evidence_dir,),
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".",
                ":(exclude,top,literal)evidence/run",
            ],
        )

    def test_dirty_checkout_is_rejected_before_rerun_validation(self) -> None:
        with patch(
            "scripts.ci.mvp_browser_e2e._git_status_porcelain",
            return_value=" M services/api/poc_web.py\n",
        ):
            with self.assertRaisesRegex(ValueError, "dirty checkout"):
                validate_rerun_package_for_workspace({})

    def test_retained_package_is_excluded_from_rerun_clean_check(self) -> None:
        repo_root = Path("repo").resolve()
        rerun_package_path = repo_root / "evidence" / "rerun-package.json"
        with patch("scripts.ci.mvp_browser_e2e.subprocess.run") as run:
            run.return_value.stdout = ""
            with self.assertRaises(ValueError):
                validate_rerun_package_for_workspace(
                    {},
                    repo_root=repo_root,
                    rerun_package_path=rerun_package_path,
                )

        self.assertEqual(
            run.call_args.args[0],
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                ".",
                ":(exclude,top,literal)evidence/rerun-package.json",
            ],
        )

    def test_retained_package_is_excluded_from_final_package_sealing(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        generated_evidence_dir = Path("generated-evidence").resolve()
        retained_package_path = Path("retained-rerun-package.json").resolve()
        with patch(
            "scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"
        ) as assert_clean:
            build_rerun_package(
                evidence,
                browser_version="test-browser",
                generated_evidence_dir=generated_evidence_dir,
                retained_rerun_package_path=retained_package_path,
            )

        self.assertEqual(
            assert_clean.call_args.kwargs["excluded_paths"],
            (generated_evidence_dir, retained_package_path),
        )

    def test_cli_passes_retained_package_path_to_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            rerun_package_path = Path(temp_dir) / "rerun-package.json"
            rerun_package_path.write_text("{}\n", encoding="utf-8")
            with (
                patch(
                    "sys.argv",
                    [
                        "mvp_browser_e2e.py",
                        "--rerun-package",
                        str(rerun_package_path),
                    ],
                ),
                patch(
                    "scripts.ci.mvp_browser_e2e.validate_rerun_package_for_workspace",
                    return_value={"validated": True},
                ) as validate,
                patch(
                    "scripts.ci.mvp_browser_e2e.run_browser_e2e",
                    return_value={"run_id": "rerun"},
                ) as run,
                patch("builtins.print"),
            ):
                self.assertEqual(main(), 0)

        validate.assert_called_once_with(
            {},
            rerun_package_path=rerun_package_path,
        )
        self.assertEqual(
            run.call_args.kwargs["retained_rerun_package_path"],
            rerun_package_path,
        )

    def test_package_payload_tampering_is_rejected(self) -> None:
        envelope = seal_rerun_package(
            {
                "schema_version": "veridoc-mvp-rerun-package/v1",
                "commit": "a" * 40,
                "inputs": [],
            }
        )
        envelope["package"]["commit"] = "b" * 40

        with self.assertRaisesRegex(ValueError, "rerun package integrity"):
            validate_rerun_package_envelope(envelope)

    def test_generated_package_pins_complete_inputs_and_rejects_hash_drift(
        self,
    ) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
            envelope = build_rerun_package(
                evidence,
                browser_version="test-browser",
            )
        with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
            package = validate_rerun_package_for_workspace(envelope)

        self.assertRegex(package["commit"], r"^[0-9a-f]{40}$")
        self.assertGreaterEqual(len(package["inputs"]), 5)
        self.assertTrue(package["configuration"]["profiles"])
        self.assertIn(
            "inference_environment",
            package["configuration"],
        )
        self.assertTrue(package["dependencies"]["requirement_files"])
        self.assertIn("prompt", package["versions"])
        self.assertIn("<rerun-package-path>", package["commands"]["rerun"])

        package["inputs"][0]["sha256"] = "0" * 64
        drifted_envelope = seal_rerun_package(package)
        with self.assertRaisesRegex(ValueError, "input hash mismatch"):
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                validate_rerun_package_for_workspace(drifted_envelope)

    def test_resealed_package_metadata_drift_is_rejected(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
            baseline = build_rerun_package(
                evidence,
                browser_version="test-browser",
            )["package"]

        mutations = (
            (
                "configuration sha256",
                ("configuration", "sha256"),
                "0" * 64,
                "configuration metadata",
            ),
            (
                "configuration profiles",
                ("configuration", "profiles"),
                [],
                "configuration metadata",
            ),
            (
                "configuration network boundary",
                ("configuration", "network_boundary"),
                {"mode": "tampered"},
                "configuration metadata",
            ),
            (
                "prompt version",
                ("versions", "prompt"),
                "tampered-prompt",
                "version metadata",
            ),
            (
                "schema version",
                ("versions", "schemas", "conversion_plan"),
                "tampered-schema",
                "version metadata",
            ),
            (
                "initial command",
                ("commands", "initial"),
                "python3 tampered.py",
                "command metadata",
            ),
            (
                "equivalence rule",
                ("equivalence", "rule"),
                "tampered rule",
                "equivalence metadata",
            ),
        )
        for label, path, replacement, error in mutations:
            with self.subTest(label=label):
                package = json.loads(json.dumps(baseline))
                target = package
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = replacement
                with self.assertRaisesRegex(ValueError, error):
                    with patch(
                        "scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"
                    ):
                        validate_rerun_package_for_workspace(
                            seal_rerun_package(package)
                        )

    def test_package_missing_required_input_is_rejected(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
            envelope = build_rerun_package(
                evidence,
                browser_version="test-browser",
            )
        package = envelope["package"]
        package["inputs"].pop()

        with self.assertRaisesRegex(ValueError, "required input set"):
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                validate_rerun_package_for_workspace(seal_rerun_package(package))

    def test_package_runtime_dependency_drift_is_rejected(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
            envelope = build_rerun_package(
                evidence,
                browser_version="test-browser",
            )
        package = envelope["package"]
        package["dependencies"]["runtime"]["python"] = "0.0.0"

        with self.assertRaisesRegex(ValueError, "runtime dependencies"):
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                validate_rerun_package_for_workspace(seal_rerun_package(package))

    def test_package_browser_runtime_drift_is_rejected(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
            package = build_rerun_package(
                evidence,
                browser_version="expected-browser",
            )["package"]

        with self.assertRaisesRegex(ValueError, "runtime dependencies"):
            validate_rerun_runtime_dependencies(
                package,
                browser_version="different-browser",
            )

    def test_package_inference_environment_drift_is_rejected(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        baseline_environment = {
            "VERIDOC_STANDARD_OPENAI_BASE_URL": "http://127.0.0.1:8000/v1",
            "VERIDOC_STANDARD_MODEL": "baseline-model",
            "VERIDOC_STANDARD_OPENAI_API_KEY": "local-secret",
        }
        with patch.dict(os.environ, baseline_environment):
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                envelope = build_rerun_package(
                    evidence,
                    browser_version="test-browser",
                )

        inference_environment = envelope["package"]["configuration"][
            "inference_environment"
        ]
        self.assertNotIn("local-secret", json.dumps(inference_environment))
        with patch.dict(
            os.environ,
            {
                **baseline_environment,
                "VERIDOC_STANDARD_MODEL": "rerun-model",
            },
        ):
            with self.assertRaisesRegex(ValueError, "inference environment"):
                with patch(
                    "scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"
                ):
                    validate_rerun_package_for_workspace(envelope)

    def test_package_browser_channel_drift_is_rejected(self) -> None:
        evidence = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
        }
        with patch.dict(
            os.environ,
            {"VERIDOC_E2E_BROWSER_CHANNEL": "chrome"},
        ):
            with patch("scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"):
                envelope = build_rerun_package(
                    evidence,
                    browser_version="test-browser",
                )

        with patch.dict(
            os.environ,
            {"VERIDOC_E2E_BROWSER_CHANNEL": "chromium"},
        ):
            with self.assertRaisesRegex(ValueError, "browser channel"):
                with patch(
                    "scripts.ci.mvp_browser_e2e._assert_clean_git_checkout"
                ):
                    validate_rerun_package_for_workspace(envelope)

    def test_equivalence_ignores_run_identity_and_timing_only(self) -> None:
        expected = {
            "schema_version": "veridoc-mvp-browser-e2e/v1",
            "run_id": "first",
            "processing_time_ms": 1.0,
            "correlation": {
                "run_id": "first",
                "job": {
                    "job_id": "job-first",
                    "status": "succeeded",
                    "conversion_status": "converted",
                },
            },
            "network_observation": {
                "status": "pass",
                "external_ai_api_send_count": 0,
                "external_attempt_count": 0,
            },
            "recovery": {"result": "completed"},
        }
        actual = json.loads(json.dumps(expected))
        actual["run_id"] = "second"
        actual["processing_time_ms"] = 9.0
        actual["correlation"]["run_id"] = "second"
        actual["correlation"]["job"]["job_id"] = "job-second"

        comparison = assert_rerun_equivalent(expected, actual)

        self.assertTrue(comparison["equivalent"])
        actual["network_observation"]["external_ai_api_send_count"] = 1
        with self.assertRaisesRegex(AssertionError, "rerun result is not equivalent"):
            assert_rerun_equivalent(expected, actual)


class MvpBrowserE2ETest(unittest.TestCase):
    @unittest.skipUnless(
        BROWSER_E2E_RUNTIME_AVAILABLE,
        "Playwright and its Chromium runtime are optional browser E2E dependencies",
    )
    def test_upload_to_download_evidence_is_bound_to_one_run(self) -> None:
        with ExitStack() as stack:
            configured_root = os.environ.get("VERIDOC_E2E_EVIDENCE_DIR")
            evidence_root = (
                Path(configured_root)
                if configured_root
                else Path(stack.enter_context(tempfile.TemporaryDirectory()))
            )
            evidence = run_browser_e2e(evidence_root=evidence_root)
            run_dir = evidence_root / evidence["run_id"]

            self.assertEqual(evidence["schema_version"], "veridoc-mvp-browser-e2e/v1")
            self.assertEqual(evidence["network_observation"]["status"], "pass")
            self.assertEqual(
                evidence["network_observation"]["external_ai_api_send_count"],
                0,
            )
            self.assertEqual(evidence["network_observation"]["external_attempt_count"], 0)
            self.assertEqual(evidence["run_id"], evidence["correlation"]["run_id"])
            self.assertEqual(evidence["correlation"]["job"]["status"], "succeeded")
            self.assertIn(
                evidence["correlation"]["job"]["conversion_status"],
                {"converted", "requires_review"},
            )
            self.assertEqual(
                evidence["correlation"]["artifact"]["sha256"],
                evidence["correlation"]["audit"]["artifact_sha256"],
            )

            artifact_path = run_dir / evidence["files"]["download"]
            self.assertEqual(
                hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                evidence["correlation"]["artifact"]["sha256"],
            )
            self.assertEqual(evidence["recovery"]["result"], "completed")
            self.assertTrue(evidence["recovery"]["user_visible_error"])
            self.assertGreaterEqual(evidence["correlation"]["audit"]["review_event_count"], 1)
            self.assertGreaterEqual(evidence["correlation"]["audit"]["job_event_count"], 1)
            self.assertEqual(evidence["correlation"]["review"]["action"], "approve")
            self.assertEqual(
                evidence["correlation"]["review"]["conversion_id"],
                json.loads(
                    (run_dir / evidence["files"]["api_result"]).read_text()
                )["audit"]["conversion_id"],
            )
            self.assertEqual(
                evidence["correlation"]["review"]["actor"]["role"],
                "approver",
            )
            self.assertTrue(evidence["correlation"]["review"]["actor"]["id"])
            review_flow = evidence["review_flow"]
            self.assertTrue(review_flow["keyboard_only"])
            self.assertGreaterEqual(len(review_flow["focus_trace"]), 1)
            self.assertTrue(
                all(step["visible_focus"] for step in review_flow["focus_trace"])
            )
            self.assertEqual(
                set(review_flow["actions"]),
                {"edit", "approve", "reject", "needs_fix"},
            )
            self.assertEqual(
                review_flow["actions"],
                ["edit", "needs_fix", "approve", "reject"],
            )
            self.assertEqual(review_flow["high_risk"]["auto_confirmed_count"], 0)
            self.assertGreaterEqual(review_flow["high_risk"]["review_target_count"], 1)
            self.assertTrue(
                review_flow["high_risk"]["approval_blocked_while_unresolved"]
            )
            self.assertIn(
                "needs-fix is unresolved",
                review_flow["high_risk"]["approval_block_reason"],
            )
            self.assertEqual(
                review_flow["source_jump"]["source_filename"],
                FIXTURE_PATH.name,
            )
            self.assertEqual(review_flow["source_jump"]["source_type"], "pdf")
            self.assertEqual(
                review_flow["source_jump"]["source_sha256"],
                hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                review_flow["source_jump"]["page"],
                review_flow["source_jump"]["review_item_page"],
            )
            for coordinate in ("x", "y", "width", "height"):
                self.assertAlmostEqual(
                    review_flow["source_jump"]["bbox"][coordinate],
                    review_flow["source_jump"]["review_item_bbox"][coordinate],
                    places=3,
                )
            self.assertEqual(
                review_flow["source_jump"]["bbox"]["unit"],
                review_flow["source_jump"]["review_item_bbox"]["unit"],
            )
            self.assertEqual(
                review_flow["source_jump"]["bbox"]["origin"],
                review_flow["source_jump"]["review_item_bbox"]["origin"],
            )
            self.assertTrue(review_flow["unresolved"]["blocked_before_approval"])
            for warning in review_flow["warnings"]:
                self.assertEqual(
                    set(warning),
                    {"code", "severity", "message", "remediation"},
                )

            evidence_path = run_dir / "evidence.json"
            self.assertEqual(json.loads(evidence_path.read_text()), evidence)
            rerun_package_path = run_dir / evidence["files"]["rerun_package"]
            validate_rerun_package_envelope(
                json.loads(rerun_package_path.read_text(encoding="utf-8"))
            )
            self.assertTrue((run_dir / evidence["files"]["trace"]).is_file())
            self.assertTrue((run_dir / evidence["files"]["api_result"]).is_file())
            high_risk_api_result = json.loads(
                (run_dir / evidence["files"]["high_risk_api_result"]).read_text()
            )
            self.assertEqual(
                high_risk_api_result["audit"]["conversion_id"],
                review_flow["high_risk"]["conversion_id"],
            )
            high_risk_api_items = [
                item
                for item in high_risk_api_result["review_items"]
                if item.get("high_risk") is True
            ]
            self.assertEqual(
                len(high_risk_api_items),
                review_flow["high_risk"]["review_target_count"],
            )
            self.assertEqual(
                sum(item.get("auto_confirmed") is True for item in high_risk_api_items),
                review_flow["high_risk"]["auto_confirmed_count"],
            )
            self.assertEqual(
                high_risk_api_items[0]["warning_details"][0],
                review_flow["warnings"][0],
            )
            review_events = json.loads(
                (run_dir / evidence["files"]["review_events"]).read_text()
            )
            self.assertTrue(
                {"edit", "approve", "reject", "needs_fix"}.issubset(
                    {event["action"] for event in review_events}
                )
            )
            high_risk_review_actions = {
                event["action"]
                for event in review_events
                if event.get("conversion_id")
                == review_flow["high_risk"]["conversion_id"]
            }
            self.assertNotIn("approve", high_risk_review_actions)
            self.assertTrue(
                {"edit", "needs_fix", "reject"}.issubset(
                    high_risk_review_actions
                )
            )
            audit_artifact_path = run_dir / evidence["files"]["audit_artifact"]
            self.assertEqual(
                hashlib.sha256(audit_artifact_path.read_bytes()).hexdigest(),
                evidence["correlation"]["audit"]["audit_artifact_sha256"],
            )
            self.assertGreaterEqual(len(evidence["files"]["screenshots"]), 2)
            for screenshot in evidence["files"]["screenshots"]:
                self.assertTrue((run_dir / screenshot).is_file())


if __name__ == "__main__":
    unittest.main()
