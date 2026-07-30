import os
import sys
import unittest
from unittest.mock import patch


APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
ZHUZHU_DIR = os.path.join(APP_DIR, "zhuzhu")
for path in (APP_DIR, ZHUZHU_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from zhuzhu import generate_gptimage


class PromptCompactTests(unittest.TestCase):
    def test_disabled_returns_original_prompt(self):
        prompt = "A" * 900
        with patch.object(generate_gptimage, "_prompt_compact_enabled", return_value=False):
            self.assertEqual(generate_gptimage._compact_request_prompt(prompt), prompt)

    def test_llm_result_is_limited_and_keeps_reference_rule(self):
        prompt = "Subject details. " * 80 + "[IMPORTANT] reference rules"
        with (
            patch.object(generate_gptimage, "_prompt_compact_enabled", return_value=True),
            patch.object(
                generate_gptimage,
                "_llm_compact_prompt",
                return_value=("Compact visual prompt. " * 40, "test-model"),
            ),
        ):
            compacted = generate_gptimage._compact_request_prompt(prompt, 500)

        self.assertLessEqual(len(compacted), 500)
        self.assertIn("reference image only to match facial identity", compacted)

    def test_llm_failure_uses_local_limit(self):
        prompt = "Paofu appearance and activity. " * 80
        with (
            patch.object(generate_gptimage, "_prompt_compact_enabled", return_value=True),
            patch.object(generate_gptimage, "_llm_compact_prompt", return_value=("", "")),
        ):
            compacted = generate_gptimage._compact_request_prompt(prompt, 500)

        self.assertLessEqual(len(compacted), 500)
        self.assertTrue(compacted.startswith("Paofu appearance and activity"))

    def test_existing_compact_reference_rule_is_not_duplicated(self):
        prompt = "Paofu writing at a cafe. Reference matches facial identity only."
        limited = generate_gptimage._hard_limit_prompt(prompt, 480, has_reference=True)

        self.assertEqual(limited.count("Reference"), 1)
        self.assertNotIn("Use the reference image", limited)

    def test_reference_instruction_is_compacted_before_request(self):
        captured = {}

        def fake_compact(prompt, target_chars=500):
            captured["before"] = prompt
            return "compact request prompt"

        def fake_images(prompt, ref_image, size, raw_base_url):
            captured["submitted"] = prompt
            return b"image", 1.5

        with (
            patch.object(generate_gptimage, "_get_gpt_raw_base_url", return_value="https://example.test/v1"),
            patch.object(generate_gptimage, "_prompt_compact_enabled", return_value=True),
            patch.object(generate_gptimage, "_compact_request_prompt", side_effect=fake_compact),
            patch.object(generate_gptimage, "_generate_via_images_api", side_effect=fake_images),
        ):
            result = generate_gptimage._generate_via_direct_gpt(
                "base prompt",
                "/tmp/reference.png",
            )

        self.assertIn("[IMPORTANT]", captured["before"])
        self.assertEqual(captured["submitted"], "compact request prompt")
        self.assertEqual(result, (b"image", 1.5, "compact request prompt"))


if __name__ == "__main__":
    unittest.main()
