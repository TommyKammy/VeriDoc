from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from scripts.ci.mvp_browser_e2e import run_browser_e2e


class MvpBrowserE2ETest(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("playwright"),
        "install requirements-browser-e2e.txt to run the browser acceptance test",
    )
    def test_upload_to_download_evidence_is_bound_to_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            evidence = run_browser_e2e(evidence_root=Path(temp_dir))
            run_dir = Path(temp_dir) / evidence["run_id"]

            self.assertEqual(evidence["schema_version"], "veridoc-mvp-browser-e2e/v1")
            self.assertEqual(evidence["run_id"], evidence["correlation"]["run_id"])
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
            self.assertGreaterEqual(
                evidence["correlation"]["audit"]["job_event_count"]
                + evidence["correlation"]["audit"]["review_event_count"],
                1,
            )

            evidence_path = run_dir / "evidence.json"
            self.assertEqual(json.loads(evidence_path.read_text()), evidence)
            self.assertTrue((run_dir / evidence["files"]["trace"]).is_file())
            self.assertTrue((run_dir / evidence["files"]["api_result"]).is_file())
            self.assertTrue((run_dir / evidence["files"]["audit_artifact"]).is_file())
            self.assertGreaterEqual(len(evidence["files"]["screenshots"]), 2)
            for screenshot in evidence["files"]["screenshots"]:
                self.assertTrue((run_dir / screenshot).is_file())


if __name__ == "__main__":
    unittest.main()
