import base64
import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image


APP_DIR = Path(__file__).resolve().parents[1] / "app"
ZHUZHU_DIR = APP_DIR / "zhuzhu"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(ZHUZHU_DIR))

import core as zhuzhu_core  # noqa: E402
import generate as unified_generate  # noqa: E402
import generate_gitee  # noqa: E402
import generate_gptimage  # noqa: E402


def _image_b64() -> str:
    image = Image.new("RGB", (2, 2), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _caption_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=200,
        json=lambda: {"choices": [{"message": {"content": text}}]},
    )


class CaptionGroundingTest(unittest.TestCase):
    def test_scheduled_caption_sends_the_image_and_retries_vague_copy(self):
        responses = [
            _caption_response("现在只管手上这一项，按自己的节奏慢慢来。"),
            _caption_response("饭团旁边的味噌汤还冒着热气，先趁热喝一口。"),
        ]
        with patch.object(zhuzhu_core, "_runtime_persona", return_value={
            "name": "测试角色",
            "user_name": "用户",
            "caption_voice": "自然口语",
        }), patch.object(zhuzhu_core, "get_cpa_key", return_value="key"), patch.object(
            zhuzhu_core, "get_llm_models", return_value=["vision-model"]
        ), patch.object(
            zhuzhu_core, "get_cpa_chat_url", return_value="https://example.invalid/chat"
        ), patch.object(
            zhuzhu_core,
            "_post_llm_with_retry",
            side_effect=responses,
        ) as post_llm:
            caption = zhuzhu_core.build_caption(
                "noon",
                img_b64=_image_b64(),
                schedule_time="12:36 在露天庭院吃饭团配味噌汤",
                require_image=True,
                allow_fallback=False,
            )

        self.assertEqual("饭团旁边的味噌汤还冒着热气，先趁热喝一口。", caption)
        self.assertEqual(2, post_llm.call_count)
        payload = post_llm.call_args_list[0].args[2]
        user_content = payload["messages"][1]["content"]
        self.assertIsInstance(user_content, list)
        self.assertEqual("image_url", user_content[0]["type"])
        self.assertTrue(user_content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertIn("照片中清楚可见的具体细节", payload["messages"][0]["content"])

    def test_image_grounded_caption_can_override_schedule_time_assumption(self):
        caption = "窗外夜色已经浓起来了，杯里的热茶正好暖手。"

        self.assertEqual(
            "schedule_conflict",
            zhuzhu_core._caption_rejection_reason(caption, "12:36 在露台喝茶"),
        )
        self.assertEqual(
            "",
            zhuzhu_core._caption_rejection_reason(
                caption,
                "12:36 在露台喝茶",
                image_grounded=True,
            ),
        )

    def test_known_vague_templates_are_rejected(self):
        for caption in (
            "测试角色先把眼前这件事认真做好，不急着想别的。",
            "现在只管手上这一项，按自己的节奏慢慢来。",
            "测试角色把注意力放回当前动作，先不让思绪跑远。",
            "眼前这一步做稳就好，不需要把自己催得太紧。",
        ):
            with self.subTest(caption=caption):
                self.assertEqual(
                    "generic_template",
                    zhuzhu_core._caption_rejection_reason(
                        caption,
                        "12:36 在露台吃午餐",
                        image_grounded=True,
                    ),
                )

    def test_gitee_backend_persists_visual_caption_metadata(self):
        with patch.object(
            generate_gitee,
            "generate_image_bytes",
            return_value=(b"image", 1.0),
        ), patch.object(
            generate_gitee,
            "save_image",
            return_value=("/tmp/gitee.png", "gitee.png", 1787040000),
        ), patch.object(
            generate_gitee,
            "update_metadata",
        ), patch.object(
            generate_gitee,
            "build_caption_for_image",
            return_value="饭团旁边的味噌汤还冒着热气。",
        ), patch.object(
            generate_gitee,
            "update_metadata_caption",
        ) as persist_caption:
            result = generate_gitee.generate(
                "custom",
                caption=True,
                prompt_override="portrait",
                prompt_is_final=True,
                sync_gallery=False,
            )

        self.assertEqual("/tmp/gitee.png", result)
        persist_caption.assert_called_once_with(
            "gitee.png",
            "饭团旁边的味噌汤还冒着热气。",
        )

    def test_gptimage_backend_persists_visual_caption_metadata(self):
        with patch.object(
            generate_gptimage,
            "_generate_via_direct_gpt",
            return_value=(b"image", 1.0),
        ), patch.object(
            generate_gptimage,
            "save_image",
            return_value=("/tmp/gptimage.png", "gptimage.png", 1787040000),
        ), patch.object(
            generate_gptimage,
            "update_metadata",
        ), patch.object(
            generate_gptimage,
            "build_caption_for_image",
            return_value="玻璃杯里的柠檬片正贴着杯壁。",
        ), patch.object(
            generate_gptimage,
            "update_metadata_caption",
        ) as persist_caption:
            result = generate_gptimage.generate(
                "custom",
                caption=True,
                prompt_override="portrait",
                prompt_is_final=True,
                sync_gallery=False,
            )

        self.assertEqual("/tmp/gptimage.png", result)
        persist_caption.assert_called_once_with(
            "gptimage.png",
            "玻璃杯里的柠檬片正贴着杯壁。",
        )

    def test_unified_generation_sends_image_when_visual_caption_is_empty(self):
        with patch.object(
            unified_generate,
            "generate_with_gptimage",
            return_value="/tmp/generated.png",
        ), patch.object(
            unified_generate,
            "build_caption_for_image",
            return_value="",
        ), patch.object(
            unified_generate,
            "update_metadata_caption",
        ) as persist_caption, patch.object(
            unified_generate,
            "send_photo",
        ) as send_photo, patch.object(
            zhuzhu_core,
            "sync_to_gallery",
        ):
            result = unified_generate.generate(
                "custom",
                engine="gptimage",
                caption=True,
                prompt_override="portrait",
                prompt_final=True,
                no_auto_style=True,
                send=True,
            )

        self.assertEqual("/tmp/generated.png", result)
        persist_caption.assert_not_called()
        send_photo.assert_called_once_with("/tmp/generated.png", "")


if __name__ == "__main__":
    unittest.main()
