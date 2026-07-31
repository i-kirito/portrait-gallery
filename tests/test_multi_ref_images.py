"""Multi-reference image helpers and dual-ref fallback."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "zhuzhu"))

import generate_gptimage  # noqa: E402
import generate as generate_main  # noqa: E402
from generate_gptimage import (  # noqa: E402
    _face_only_fallback_instruction,
    _images_api_failure_kind,
    _multi_reference_edit_instruction,
    _normalize_ref_images,
    _terminal_image_failure_reason,
)


class MultiRefImageTests(unittest.TestCase):
    def test_normalize_prefers_primary_then_extra(self):
        refs = _normalize_ref_images("base.png", ["face.png", "base.png", ""])
        self.assertEqual(refs[0], "base.png")
        self.assertIn("face.png", refs)
        self.assertEqual(len(refs), 2)

    def test_multi_instruction_locks_base_and_face(self):
        text = _multi_reference_edit_instruction(["base.png", "face.png"])
        self.assertIn("Image 1", text)
        self.assertIn("Image 2", text)
        self.assertIn("pose", text.lower())
        self.assertIn("face", text.lower())

    def test_xiaohongshu_instruction_does_not_copy_reference_body(self):
        text = _multi_reference_edit_instruction(
            ["xiaohongshu.webp", "face.png"],
            outfit_reference_mode=True,
        )
        low = text.lower()

        self.assertIn("outfit-reference mode", low)
        self.assertIn("never copy image 1", low)
        self.assertIn("configured body profile", low)
        self.assertIn("re-tailor", low)
        self.assertIn("sole and authoritative facial identity source", low)
        self.assertIn("generic influencer face", low)
        self.assertIn("outside its facial region as transparent", low)
        self.assertIn("never image 2", low)
        self.assertNotIn("immutable base photo", low)
        self.assertNotIn("everything else must match image 1", low)

    def test_single_uses_legacy_path(self):
        text = _multi_reference_edit_instruction(["face.png"])
        self.assertTrue(text)
        self.assertNotIn("Multi-reference edit mode", text)

    def test_daily_xiaohongshu_flag_marks_prompt_and_removes_generic_eye_cue(self):
        captured = {}

        def fake_generate(_theme, **kwargs):
            captured["prompt"] = kwargs.get("prompt_override", "")
            captured["refs"] = kwargs.get("ref_images")
            return None

        with patch.object(
            generate_main,
            "_get_schedule_context",
            return_value=("Today's plan: cafe work", "10:30 cafe work", "white shirt", "cafe", "ponytail"),
        ), patch.object(
            generate_main,
            "build_prompt",
            return_value="adult woman, expressive eyes, configured slender body",
        ), patch.object(
            generate_main,
            "generate_with_gptimage",
            side_effect=fake_generate,
        ), patch.object(generate_main, "_gitee_fallback_enabled", return_value=False):
            result = generate_main.generate(
                "morning",
                source="cron",
                ref_image="/tmp/outfit.png",
                ref_images=["/tmp/outfit.png", "/tmp/face.jpg"],
                schedule_time="10:30 cafe work",
                xiaohongshu_outfit_reference=True,
            )

        self.assertIsNone(result)
        self.assertIn("[XHS_OUTFIT_REFERENCE_MODE]", captured["prompt"])
        self.assertNotIn("expressive eyes", captured["prompt"].lower())
        self.assertIn("Image 2 is the sole authoritative facial identity", captured["prompt"])
        self.assertEqual(["/tmp/outfit.png", "/tmp/face.jpg"], captured["refs"])


class DualRefFallbackTests(unittest.TestCase):
    def tearDown(self):
        generate_gptimage._LAST_TERMINAL_IMAGE_FAILURE = ""
        generate_gptimage._LAST_IMAGE_FAILURE_KIND = ""

    def test_failure_kind_timeout_and_eof(self):
        self.assertEqual("timeout", _images_api_failure_kind(exc=TimeoutError("read timed out")))
        self.assertEqual(
            "codex_edits_eof",
            _images_api_failure_kind(
                200,
                'Post "https://chatgpt.com/backend-api/codex/images/edits": unexpected EOF',
            ),
        )
        self.assertEqual(
            "moderation",
            _images_api_failure_kind(400, "moderation_blocked safety_violations=[sexual]"),
        )

    def test_moderation_is_terminal_reason(self):
        self.assertIn(
            "内容安全拦截",
            _terminal_image_failure_reason(400, "moderation_blocked safety_violations=[sexual]"),
        )

    def test_face_only_fallback_instruction_present(self):
        text = _face_only_fallback_instruction().lower()
        self.assertIn("face-only fallback", text)
        self.assertIn("one subject", text)

    def test_dual_ref_failure_falls_back_to_face_only(self):
        calls = []

        def fake_direct(
            prompt,
            ref_image=None,
            size=None,
            precise_edit=False,
            ref_images=None,
            request_info=None,
        ):
            calls.append(
                {
                    "ref_image": ref_image,
                    "ref_images": list(ref_images or []),
                    "precise_edit": precise_edit,
                    "prompt_tail": (prompt or "")[-80:],
                }
            )
            if ref_images and len(ref_images) > 1:
                generate_gptimage._LAST_IMAGE_FAILURE_KIND = "codex_edits_eof"
                return None
            if ref_images and len(ref_images) == 1:
                if isinstance(request_info, dict):
                    request_info["submitted_prompt"] = prompt
                return (b"face-only-image", 1.5)
            return None

        with patch.object(
            generate_gptimage, "_generate_via_direct_gpt", side_effect=fake_direct
        ), patch.object(
            generate_gptimage,
            "save_image",
            return_value=("/tmp/dual-fallback.png", "dual-fallback.png", 1784825000),
        ), patch.object(generate_gptimage, "update_metadata") as update_metadata:
            result = generate_gptimage.generate(
                "custom",
                prompt_override="lock pose candle dinner, character only",
                ref_image="/tmp/base.png",
                ref_images=["/tmp/base.png", "/tmp/face.png"],
                prompt_is_final=True,
                sync_gallery=False,
            )

        self.assertEqual("/tmp/dual-fallback.png", result)
        self.assertEqual(2, len(calls))
        self.assertEqual(["/tmp/base.png", "/tmp/face.png"], calls[0]["ref_images"])
        self.assertEqual(["/tmp/face.png"], calls[1]["ref_images"])
        meta = update_metadata.call_args.args[6]
        self.assertTrue(meta["dual_ref_fallback_used"])
        self.assertEqual("img2img_face_only_fallback", meta["generation_mode"])
        self.assertEqual("img2img_face_only", meta["fallback_to"])
        self.assertEqual("codex_edits_eof", meta["dual_ref_fallback_reason"])

    def test_precise_edit_does_not_face_only_fallback(self):
        with patch.object(
            generate_gptimage,
            "_generate_via_direct_gpt",
            return_value=None,
        ) as direct:
            result = generate_gptimage.generate(
                "custom",
                prompt_override="precise dual",
                ref_image="/tmp/base.png",
                ref_images=["/tmp/base.png", "/tmp/face.png"],
                prompt_is_final=True,
                sync_gallery=False,
                precise_edit=True,
            )
        self.assertIsNone(result)
        self.assertEqual(1, direct.call_count)

    def test_quota_terminal_skips_face_only_fallback(self):
        def fail_quota(*_a, **_k):
            generate_gptimage._LAST_TERMINAL_IMAGE_FAILURE = "GPT Image 图片账号额度已用完"
            generate_gptimage._LAST_IMAGE_FAILURE_KIND = "http_429"
            return None

        with patch.object(
            generate_gptimage, "_generate_via_direct_gpt", side_effect=fail_quota
        ) as direct:
            result = generate_gptimage.generate(
                "custom",
                prompt_override="dual with quota fail",
                ref_image="/tmp/base.png",
                ref_images=["/tmp/base.png", "/tmp/face.png"],
                prompt_is_final=True,
                sync_gallery=False,
            )
        self.assertIsNone(result)
        self.assertEqual(1, direct.call_count)

    def test_multi_ref_images_api_fast_fails_on_codex_eof_without_retry(self):
        response = SimpleNamespace(
            status_code=200,
            text=(
                '{"error":{"message":"Post "https://chatgpt.com/backend-api/codex/images/edits": '
                'unexpected EOF","type":"server_error","code":"internal_server_error"}}'
            ),
            json=lambda: {
                "error": {
                    "message": 'Post "https://chatgpt.com/backend-api/codex/images/edits": unexpected EOF',
                    "type": "server_error",
                    "code": "internal_server_error",
                }
            },
        )

        with patch.object(
            generate_gptimage.REQUEST_SESSION, "post", return_value=response
        ) as post, patch.object(
            generate_gptimage, "_image_bytes_for_edit", return_value=b"png"
        ), patch.object(generate_gptimage.time, "sleep") as sleep, patch.object(
            generate_gptimage, "_get_gpt_key", return_value="test-key"
        ):
            result = generate_gptimage._generate_via_images_api(
                "dual prompt",
                "/tmp/base.png",
                "1024x1024",
                "http://example.test/v1",
                ref_images=["/tmp/base.png", "/tmp/face.png"],
            )

        self.assertIsNone(result)
        self.assertEqual(1, post.call_count)
        sleep.assert_not_called()
        self.assertEqual("codex_edits_eof", generate_gptimage._LAST_IMAGE_FAILURE_KIND)


if __name__ == "__main__":
    unittest.main()
