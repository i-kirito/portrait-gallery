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
    def test_daily_prompt_is_normalized_to_clearly_adult_everyday_photography(self):
        with patch.object(
            zhuzhu_core,
            "_read_custom_appearance",
            return_value=(
                "Chinese girl with delicate features and a youthful doll-like face, "
                "hourglass figure, slim waist, and large natural breasts"
            ),
        ):
            prompt = zhuzhu_core.build_prompt(
                "morning",
                schedule_activity="spray water on balcony plants",
                outfit_keywords=(
                    "black lace-trimmed silk camisole, black velvet shorts, "
                    "charcoal grey cardigan"
                ),
                scene_keywords="sunny balcony with potted plants",
                hair_keywords="low side ponytail with a black ribbon",
            )

        lowered = prompt.lower()
        for forbidden in (
            "18-year-old",
            "youthful",
            "doll-like",
            "delicate features",
            "hourglass figure",
            "breasts",
            "lace-trimmed silk camisole",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertIn("chinese girl", lowered)
        self.assertIn("adult woman in her late twenties", lowered)
        self.assertIn("non-sexual everyday lifestyle photograph", lowered)
        self.assertEqual(1, lowered.count("non-sexual everyday lifestyle photograph"))
        self.assertIn("opaque satin square-neck sleeveless top", lowered)
        self.assertIn("tailored high-waisted black velvet shorts", lowered)
        self.assertIn("naturally observed facial width", lowered)
        self.assertIn("cheek volume", lowered)
        self.assertIn("pointed v-shaped chin", lowered)
        self.assertEqual(
            prompt,
            zhuzhu_core.sanitize_daily_image_prompt(prompt, limit=0),
        )

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

    def test_schedule_context_cannot_reintroduce_unsafe_visual_terms(self):
        unsafe_context = (
            "Today's plan: Activity: water balcony plants. "
            "Outfit: black lace-trimmed silk camisole and black velvet shorts. "
            "Mood: seductive young-looking girl"
        )
        with patch.object(
            unified_generate,
            "resolve_prompt",
            return_value="initial prompt",
        ), patch.object(
            unified_generate,
            "_get_schedule_context",
            return_value=(
                unsafe_context,
                "08:24 在阳台给绿植喷水",
                "black lace-trimmed silk camisole, black velvet shorts",
                "sunny balcony",
                "low side ponytail",
            ),
        ), patch.object(
            zhuzhu_core,
            "_read_custom_appearance",
            return_value="Chinese girl with a youthful doll-like face",
        ), patch.object(
            unified_generate,
            "generate_with_gptimage",
            return_value="/tmp/generated.png",
        ) as gpt_image, patch.object(
            zhuzhu_core,
            "sync_to_gallery",
        ):
            result = unified_generate.generate(
                "morning",
                "gptimage",
                source="cron",
                schedule_time="08:24 在阳台给绿植喷水",
                no_auto_style=True,
            )

        self.assertEqual("/tmp/generated.png", result)
        final_prompt = gpt_image.call_args.kwargs["prompt_override"].lower()
        for forbidden in (
            "18-year-old",
            "young-looking",
            " youthful",
            " doll-like",
            "seductive",
            "lace-trimmed silk camisole",
        ):
            self.assertNotIn(forbidden, final_prompt)
        self.assertIn("adult woman in her late twenties", final_prompt)
        self.assertIn("opaque satin square-neck sleeveless top", final_prompt)
        self.assertIn("cheek volume", final_prompt)

    def test_final_daily_reroll_prompt_is_still_safety_normalized(self):
        unsafe_prompt = (
            "Chinese girl with a very youthful and innocent face, "
            "wearing sheer lingerie in an intimate bedroom pose"
        )
        with patch.object(
            unified_generate,
            "_get_schedule_context",
            return_value=("", "", "", "", ""),
        ), patch.object(
            unified_generate,
            "generate_with_gptimage",
            return_value="/tmp/generated.png",
        ) as gpt_image, patch.object(
            zhuzhu_core,
            "sync_to_gallery",
        ) as sync_gallery:
            result = unified_generate.generate(
                "morning",
                "gptimage",
                prompt_override=unsafe_prompt,
                prompt_final=True,
                no_auto_style=True,
                source="cron",
                schedule_time="08:24 历史日程重抽",
            )

        self.assertEqual("/tmp/generated.png", result)
        final_prompt = gpt_image.call_args.kwargs["prompt_override"].lower()
        for forbidden in (
            "18-year-old",
            "youthful",
            "innocent",
            "sheer",
            "lingerie",
            "intimate",
        ):
            self.assertNotIn(forbidden, final_prompt)
        self.assertIn("chinese girl", final_prompt)
        self.assertIn("adult woman in her late twenties", final_prompt)
        self.assertIn("non-sexual everyday lifestyle photograph", final_prompt)
        self.assertIn("cheek volume", final_prompt)
        self.assertEqual(
            1,
            final_prompt.count("non-sexual everyday lifestyle photograph"),
        )
        self.assertEqual(final_prompt, sync_gallery.call_args.kwargs["prompt"].lower())

    def test_injected_custom_prompt_is_normalized_without_face_slimming(self):
        with patch.object(
            unified_generate,
            "_get_schedule_context",
            return_value=("", "", "", "", ""),
        ), patch.object(
            unified_generate,
            "_decide_hairstyle",
            return_value=None,
        ), patch.object(
            unified_generate,
            "generate_with_gptimage",
            return_value="/tmp/generated.png",
        ) as gpt_image, patch.object(
            zhuzhu_core,
            "sync_to_gallery",
        ):
            result = unified_generate.generate(
                "custom",
                "gptimage",
                prompt_override="adult Chinese woman with delicate features in a cafe",
                no_auto_style=True,
                source="custom",
            )

        self.assertEqual("/tmp/generated.png", result)
        final_prompt = gpt_image.call_args.kwargs["prompt_override"].lower()
        self.assertNotIn("delicate features", final_prompt)
        self.assertIn("natural facial features", final_prompt)
        self.assertIn("naturally observed facial width", final_prompt)
        self.assertIn("cheek volume", final_prompt)

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


    def test_english_instruction_caption_is_rejected(self):
        bad = (
            'The user wants me to write a short "小心思" '
            '(little thought) in the tone of "猪猪" for a photo。'
        )
        schedule = '19:33 到社区共享厨房和朋友一起摆盘夏日冷盘'

        self.assertTrue(zhuzhu_core._caption_has_instruction_leak(bad))
        self.assertFalse(zhuzhu_core._caption_is_mostly_chinese(bad))
        self.assertIn(
            zhuzhu_core._caption_rejection_reason(bad, schedule),
            {'instruction_leak', 'not_chinese'},
        )

        cleaned = zhuzhu_core._scene_caption_fallback(
            'evening',
            {'name': '猪猪', 'user_name': '主人'},
            bad,
            schedule,
        )
        self.assertFalse(zhuzhu_core._caption_rejection_reason(cleaned, schedule))
        self.assertTrue(zhuzhu_core._caption_is_mostly_chinese(cleaned))
        self.assertNotIn('The user wants', cleaned)

    def test_best_caption_rejects_instruction_leak(self):
        bad = 'The user wants me to write a short little thought in the tone of 猪猪 for a photo.'
        good = '猪猪先把冷盘摆整齐，别在火候上分心。'
        self.assertEqual(zhuzhu_core._best_caption(bad, good), good)

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
            old_name_extracted = zhuzhu_core._extract_time_from_filename(
                f"zhuzhu_schedule_{timestamp}.png"
            )
            new_name_extracted = zhuzhu_core._extract_time_from_filename(
                f"schedule_0824_a1b2c3_{timestamp}.png"
            )

        expected = datetime.fromtimestamp(
            timestamp,
            ZoneInfo("Asia/Shanghai"),
        ).strftime("%H:%M")
        self.assertEqual(expected, old_name_extracted)
        self.assertEqual(expected, new_name_extracted)


if __name__ == "__main__":
    unittest.main()
