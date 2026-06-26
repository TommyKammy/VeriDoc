from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = REPO_ROOT / "services" / "api" / "inference_profiles.json"
DOC_PATH = REPO_ROOT / "docs" / "local-inference-setup.md"
ADR_PATH = REPO_ROOT / "adr" / "ADR-001-local-llm-standard-model.md"


class LocalInferenceSetupTest(unittest.TestCase):
    def test_phase0_inference_profiles_define_local_standard_and_high_quality_modes(self) -> None:
        self.assertTrue(
            PROFILE_PATH.is_file(),
            msg=f"missing local inference profiles: {PROFILE_PATH.relative_to(REPO_ROOT)}",
        )

        profiles = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(profiles["schema_version"], 1)
        self.assertEqual(profiles["network_boundary"], "local-only")
        self.assertEqual(profiles["api_contract"], "openai-compatible-chat-completions")

        by_id = {profile["id"]: profile for profile in profiles["profiles"]}
        self.assertEqual({"standard", "high_quality"}, set(by_id))

        standard = by_id["standard"]
        self.assertEqual(standard["quality_tier"], "standard")
        self.assertEqual(standard["provider"], "Qwen")
        self.assertEqual(standard["model_family"], "Qwen3-8B")
        self.assertEqual(standard["recommended_model"], "Qwen/Qwen3-8B")
        self.assertEqual(
            standard["decision_ref"],
            "adr/ADR-001-local-llm-standard-model.md",
        )
        self.assertIn("VERIDOC_STANDARD_OPENAI_BASE_URL", standard["required_env"])
        self.assertIn("VERIDOC_STANDARD_MODEL", standard["required_env"])

        high_quality = by_id["high_quality"]
        self.assertEqual(high_quality["quality_tier"], "high-quality")
        self.assertEqual(high_quality["provider"], "DwarfStar 4")
        self.assertEqual(high_quality["model_family"], "DeepSeek V4 Flash")
        self.assertIn("VERIDOC_HIGH_QUALITY_OPENAI_BASE_URL", high_quality["required_env"])
        self.assertIn("VERIDOC_HIGH_QUALITY_MODEL", high_quality["required_env"])

        for profile in profiles["profiles"]:
            self.assertEqual(profile["egress"], "disabled")
            self.assertEqual(profile["credential_source"], "local-placeholder-only")
            self.assertTrue(profile["base_url_env"].startswith("VERIDOC_"))
            self.assertTrue(profile["model_env"].startswith("VERIDOC_"))

    def test_local_inference_docs_capture_offline_boundary_and_api_settings(self) -> None:
        self.assertTrue(
            DOC_PATH.is_file(),
            msg=f"missing local inference setup docs: {DOC_PATH.relative_to(REPO_ROOT)}",
        )

        docs = DOC_PATH.read_text(encoding="utf-8")

        for required_text in (
            "標準モード",
            "高品質モード",
            "DeepSeek V4 Flash on DwarfStar 4",
            "OpenAI互換 API",
            "外部送信なし",
            "services/api/inference_profiles.json",
            "VERIDOC_STANDARD_OPENAI_BASE_URL",
            "VERIDOC_HIGH_QUALITY_OPENAI_BASE_URL",
            "placeholder",
            "GMP適合や業務利用可能性は主張しない",
        ):
            self.assertIn(required_text, docs)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, docs)

    def test_standard_model_adr_records_comparison_and_phase1_open_items(self) -> None:
        self.assertTrue(
            ADR_PATH.is_file(),
            msg=f"missing standard model ADR: {ADR_PATH.relative_to(REPO_ROOT)}",
        )

        adr = ADR_PATH.read_text(encoding="utf-8")

        for required_text in (
            "Qwen3-8B",
            "Mistral NeMo Instruct 2407",
            "Llama 3.1 8B Instruct",
            "日本語",
            "JSON安定性",
            "ライセンス",
            "Apache-2.0",
            "暫定標準モデル",
            "Phase1以降の未決事項",
            "VERIDOC_STANDARD_MODEL",
            "requires_review",
            "fail closed",
        ):
            self.assertIn(required_text, adr)

        forbidden_fragments = ("/" + "Users" + "/", "C:" + "\\Users" + "\\")
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, adr)


if __name__ == "__main__":
    unittest.main()
