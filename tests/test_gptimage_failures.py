import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
ZHUZHU_DIR = APP_DIR / "zhuzhu"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(ZHUZHU_DIR))

import generate_gptimage  # noqa: E402


class GptImageFailureTest(unittest.TestCase):
    def tearDown(self):
        generate_gptimage._LAST_TERMINAL_IMAGE_FAILURE = ""
        generate_gptimage._IMAGES_API_UNSUPPORTED_BASES.clear()

    def test_face_reference_keeps_identity_without_face_slimming(self):
        instruction = generate_gptimage._reference_edit_instruction(
            "/tmp/reference_face.jpg"
        ).lower()

        self.assertIn("stable identity cues", instruction)
        self.assertIn("observed facial width", instruction)
        self.assertIn("cheek volume", instruction)
        self.assertIn("pointed v-shaped chin", instruction)
        self.assertNotIn("matching the face shape", instruction)
        self.assertNotIn("matching the facial structure", instruction)

    def test_precision_edit_does_not_receive_face_shape_guard(self):
        instruction = generate_gptimage._reference_edit_instruction(
            "/tmp/source.png",
            precise_edit=True,
        ).lower()

        self.assertIn("immutable source image", instruction)
        self.assertNotIn("pointed v-shaped chin", instruction)

    def test_reference_guard_can_be_omitted_when_prompt_already_contains_it(self):
        instruction = generate_gptimage._reference_edit_instruction(
            "/tmp/reference_face.jpg",
            include_face_shape_guard=False,
        ).lower()

        self.assertIn("observed facial width", instruction)
        self.assertNotIn("do not beautify by narrowing", instruction)

    def test_quota_failure_is_terminal_and_is_not_retried(self):
        response = SimpleNamespace(
            status_code=429,
            text='{"error":{"code":"insufficient_quota","message":"图片账号额度已用完"}}',
        )

        with patch.object(
            generate_gptimage.REQUEST_SESSION,
            "post",
            return_value=response,
        ) as post, patch.object(generate_gptimage.time, "sleep") as sleep:
            result = generate_gptimage._generate_via_images_api(
                "a red apple",
                None,
                "1024x1024",
                "http://example.test/v1",
            )

        self.assertIsNone(result)
        self.assertEqual(1, post.call_count)
        sleep.assert_not_called()
        self.assertEqual(
            "GPT Image 图片账号额度已用完",
            generate_gptimage._LAST_TERMINAL_IMAGE_FAILURE,
        )

    def test_terminal_img2img_failure_skips_text2img_retry(self):
        def fail_once(*_args, **_kwargs):
            generate_gptimage._LAST_TERMINAL_IMAGE_FAILURE = "GPT Image 没有可用渠道"
            return None

        with patch.object(
            generate_gptimage,
            "_generate_via_direct_gpt",
            side_effect=fail_once,
        ) as direct:
            result = generate_gptimage.generate(
                "custom",
                prompt_override="adult portrait",
                ref_image="/tmp/reference.png",
                prompt_is_final=True,
                sync_gallery=False,
            )

        self.assertIsNone(result)
        self.assertEqual(1, direct.call_count)

    def test_precision_edit_never_retries_without_reference_image(self):
        with patch.object(
            generate_gptimage,
            "_generate_via_direct_gpt",
            return_value=None,
        ) as direct:
            result = generate_gptimage.generate(
                "custom",
                prompt_override="change only the background",
                ref_image="/tmp/reference.png",
                prompt_is_final=True,
                sync_gallery=False,
                precise_edit=True,
            )

        self.assertIsNone(result)
        self.assertEqual(1, direct.call_count)
        self.assertEqual("/tmp/reference.png", direct.call_args.args[1])
        self.assertTrue(direct.call_args.kwargs["precise_edit"])

    def test_generation_records_requested_size_without_rewriting_output(self):
        with patch.object(
            generate_gptimage,
            "_generate_via_direct_gpt",
            return_value=(b"upstream-image", 1.25),
        ), patch.object(
            generate_gptimage,
            "save_image",
            return_value=("/tmp/native-output.png", "native-output.png", 1784615821),
        ), patch.object(
            generate_gptimage,
            "update_metadata",
        ) as update_metadata:
            result = generate_gptimage.generate(
                "custom",
                prompt_override="adult portrait",
                prompt_is_final=True,
                size="1536x2048",
                sync_gallery=False,
            )

        self.assertEqual("/tmp/native-output.png", result)
        self.assertEqual(
            "1536x2048",
            update_metadata.call_args.args[6]["requested_size"],
        )

    def test_terminal_failure_reason_covers_current_axonhub_errors(self):
        cases = {
            '{"code":"insufficient_quota"}': "GPT Image 图片账号额度已用完",
            '{"detail":{"code":"deactivated_workspace"}}': "GPT Image 工作区已停用",
            '{"message":"No available compatible accounts"}': "GPT Image 没有可用的兼容账号",
            '{"code":"model_not_found","message":"model not found"}': "GPT Image 模型不可用",
        }

        for body, expected in cases.items():
            with self.subTest(body=body):
                self.assertEqual(
                    expected,
                    generate_gptimage._terminal_image_failure_reason(429, body),
                )

    def test_temporary_channel_routing_errors_are_retried(self):
        responses = [
            SimpleNamespace(
                status_code=503,
                text=(
                    '{"error":{"code":"model_not_found",'
                    '"message":"No available channel for model gpt-image-2 under group auto (distributor)"}}'
                ),
            ),
            SimpleNamespace(
                status_code=503,
                text='{"error":{"code":"model_not_found","message":"分组 生图 下模型 gpt-image-2 无可用渠道（distributor）"}}',
            ),
            SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {"data": [{"b64_json": "aW1hZ2U="}]},
            ),
        ]

        with patch.object(
            generate_gptimage.REQUEST_SESSION,
            "post",
            side_effect=responses,
        ) as post, patch.object(generate_gptimage.time, "sleep") as sleep:
            result = generate_gptimage._generate_via_images_api(
                "a red apple",
                None,
                "1024x1024",
                "http://example.test/v1",
            )

        self.assertIsNotNone(result)
        img_data, elapsed = result
        self.assertEqual(b"image", img_data)
        self.assertEqual(3, post.call_count)
        self.assertEqual(2, sleep.call_count)
        self.assertEqual("", generate_gptimage._LAST_TERMINAL_IMAGE_FAILURE)

    def test_chat_endpoint_fallback_is_disabled_by_default(self):
        with patch.object(
            generate_gptimage,
            "_get_gpt_raw_base_url",
            return_value="http://example.test/v1",
        ), patch.object(
            generate_gptimage,
            "GPTIMAGE_DIRECT_MODEL",
            "gpt-image-2",
        ), patch.object(
            generate_gptimage,
            "_generate_via_images_api",
            return_value=None,
        ) as images_api, patch.object(
            generate_gptimage,
            "_gpt_chat_fallback_enabled",
            return_value=False,
        ), patch.object(
            generate_gptimage,
            "_generate_via_chat_gpt",
            return_value=(b"image", 1.0),
        ) as chat_api:
            result = generate_gptimage._generate_via_direct_gpt("portrait")

        self.assertIsNone(result)
        images_api.assert_called_once()
        chat_api.assert_not_called()

    def test_chat_endpoint_fallback_runs_only_when_enabled(self):
        expected = (b"image", 1.0)
        with patch.object(
            generate_gptimage,
            "_get_gpt_raw_base_url",
            return_value="http://example.test/v1",
        ), patch.object(
            generate_gptimage,
            "GPTIMAGE_DIRECT_MODEL",
            "gpt-image-2",
        ), patch.object(
            generate_gptimage,
            "_generate_via_images_api",
            return_value=None,
        ), patch.object(
            generate_gptimage,
            "_gpt_chat_fallback_enabled",
            return_value=True,
        ), patch.object(
            generate_gptimage,
            "_generate_via_chat_gpt",
            return_value=expected,
        ) as chat_api:
            result = generate_gptimage._generate_via_direct_gpt("portrait")

        self.assertEqual(expected, result)
        chat_api.assert_called_once_with("portrait", None, None, ref_images=None)

    def test_explicit_chat_endpoint_does_not_require_fallback_switch(self):
        expected = (b"image", 1.0)
        with patch.object(
            generate_gptimage,
            "_get_gpt_raw_base_url",
            return_value="http://example.test/v1/chat/completions",
        ), patch.object(
            generate_gptimage,
            "GPTIMAGE_DIRECT_MODEL",
            "gpt-image-2",
        ), patch.object(
            generate_gptimage,
            "_generate_via_images_api",
        ) as images_api, patch.object(
            generate_gptimage,
            "_gpt_chat_fallback_enabled",
            return_value=False,
        ), patch.object(
            generate_gptimage,
            "_generate_via_chat_gpt",
            return_value=expected,
        ) as chat_api:
            result = generate_gptimage._generate_via_direct_gpt("portrait")

        self.assertEqual(expected, result)
        images_api.assert_not_called()
        chat_api.assert_called_once_with("portrait", None, None, ref_images=None)


if __name__ == "__main__":
    unittest.main()
