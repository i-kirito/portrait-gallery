import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


APP_DIR = Path(__file__).resolve().parents[1] / "app"
ZHUZHU_DIR = APP_DIR / "zhuzhu"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(ZHUZHU_DIR))

import core as zhuzhu_core  # noqa: E402
import generate as unified_generate  # noqa: E402
from generate import _apply_schedule_clock_render_guard, _schedule_time_constraint  # noqa: E402


class ScheduleClockPromptTest(unittest.TestCase):
    def test_time_constraint_uses_period_without_exposing_clock_digits(self):
        constraint = _schedule_time_constraint("09:24 去便利店买气泡水")

        self.assertIn("morning", constraint)
        self.assertIn("natural daylight", constraint)
        self.assertNotIn("09:24", constraint)
        self.assertIsNone(re.search(r"\b\d{1,2}:\d{2}\b", constraint))

    def test_clock_guard_removes_only_the_schedule_time_and_forbids_rendering(self):
        guarded = _apply_schedule_clock_render_guard(
            "A convenience store visit at 09:24 (9:24), composed for a 16:10 frame.",
            "09:24 去便利店买气泡水",
        )

        self.assertNotIn("09:24", guarded)
        self.assertNotIn("9:24", guarded)
        self.assertIn("16:10 frame", guarded)
        self.assertIn("schedule clock is metadata only", guarded)
        self.assertIn("storefront sign time", guarded)
        self.assertIn("must be unreadable", guarded)

    def test_gpt_and_gitee_receive_the_same_guarded_final_prompt(self):
        for engine in ("gptimage", "gitee"):
            with self.subTest(engine=engine), patch.object(
                unified_generate,
                "generate_with_gptimage",
                return_value="/tmp/generated.png",
            ) as gpt_image, patch.object(
                unified_generate,
                "generate_with_gitee",
                return_value="/tmp/generated.png",
            ) as gitee, patch.object(
                zhuzhu_core,
                "sync_to_gallery",
            ):
                result = unified_generate.generate(
                    "custom",
                    engine,
                    prompt_override="A candid convenience-store photo at 09:24.",
                    prompt_final=True,
                    no_auto_style=True,
                    source="cron",
                    schedule_time="09:24 去便利店买气泡水",
                )

            self.assertEqual("/tmp/generated.png", result)
            call = gpt_image.call_args if engine == "gptimage" else gitee.call_args
            final_prompt = call.kwargs["prompt_override"]
            self.assertNotIn("09:24", final_prompt)
            self.assertIn("schedule clock is metadata only", final_prompt)

    def test_caption_is_persisted_before_gallery_sync(self):
        with patch.object(
            unified_generate,
            "generate_with_gptimage",
            return_value="/tmp/generated.png",
        ), patch.object(
            unified_generate,
            "build_caption_for_image",
            return_value="运动装备再检查一遍。",
        ), patch.object(
            unified_generate,
            "update_metadata_caption",
        ) as persist_caption, patch.object(
            zhuzhu_core,
            "sync_to_gallery",
            side_effect=RuntimeError("gallery sync interrupted"),
        ):
            with self.assertRaisesRegex(RuntimeError, "gallery sync interrupted"):
                unified_generate.generate(
                    "noon",
                    "gptimage",
                    caption=True,
                    prompt_override="A gym locker room photo.",
                    prompt_final=True,
                    no_auto_style=True,
                    source="cron",
                    schedule_time="12:36 在健身房更衣室整理运动装备",
                )

        persist_caption.assert_called_once_with(
            "generated.png",
            "运动装备再检查一遍。",
        )

    def test_next_caption_activity_comes_only_from_persisted_schedule(self):
        with patch.object(
            zhuzhu_core,
            "_load_daily_schedule_context",
            return_value={
                "schedule": (
                    "09:24 去便利店买气泡水\n"
                    "12:36 在书店阅读\n"
                    "19:18 沿河散步"
                ),
            },
        ):
            next_activity = zhuzhu_core._next_schedule_activity(
                "09:24 去便利店买气泡水"
            )

        self.assertEqual("12:36 在书店阅读", next_activity)

    def test_caption_fallback_does_not_invent_a_future_task(self):
        with patch.object(zhuzhu_core, "_next_schedule_activity", return_value=""):
            caption = zhuzhu_core._personalized_caption_fallback(
                "noon",
                {"name": "测试角色", "user_name": "用户"},
                "12:36 在厨房做午餐",
            )

        self.assertNotIn("等会儿", caption)
        self.assertNotIn("开播", caption)
        self.assertNotIn("下午", caption)
        self.assertNotIn("明天", caption)

    def test_filename_timestamp_uses_configured_timezone(self):
        timestamp = 1784176761
        with patch.object(
            zhuzhu_core,
            "_GALLERY_CONFIG",
            {"config": {"timezone": "Asia/Shanghai"}},
        ):
            extracted = zhuzhu_core._extract_time_from_filename(
                f"zhuzhu_schedule_{timestamp}.png"
            )

        expected = datetime.fromtimestamp(
            timestamp,
            ZoneInfo("Asia/Shanghai"),
        ).strftime("%H:%M")
        self.assertEqual(expected, extracted)


if __name__ == "__main__":
    unittest.main()
