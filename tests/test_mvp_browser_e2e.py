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
    _launch_browser,
    _require_audit_payload_matches_result,
    _require_matching_event,
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
                "different actor",
                review_flow["high_risk"]["approval_block_reason"],
            )
            self.assertEqual(
                review_flow["source_jump"]["page"],
                review_flow["source_jump"]["review_item_page"],
            )
            self.assertEqual(
                review_flow["source_jump"]["bbox"],
                review_flow["source_jump"]["review_item_bbox"],
            )
            self.assertTrue(review_flow["unresolved"]["blocked_before_approval"])
            for warning in review_flow["warnings"]:
                self.assertEqual(
                    set(warning),
                    {"code", "severity", "message", "remediation"},
                )

            evidence_path = run_dir / "evidence.json"
            self.assertEqual(json.loads(evidence_path.read_text()), evidence)
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
