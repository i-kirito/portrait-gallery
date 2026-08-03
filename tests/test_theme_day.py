import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from data import DailyEntry  # noqa: E402
from main import PortraitGalleryApp  # noqa: E402
from scheduler import DailyScheduler, THEME_DAY_POOL  # noqa: E402


class ThemeDayTests(unittest.IsolatedAsyncioTestCase):
    def test_theme_day_prompt_turns_a_theme_into_a_full_day_constraint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            prompt = scheduler._theme_day_prompt_block("霍格沃兹主题日")

        self.assertIn("霍格沃兹", prompt)
        self.assertIn("穿搭、发型、活动、场景", prompt)
        self.assertIn("只能清楚出现角色本人", prompt)
        self.assertNotIn("霍格沃兹主题日主题日", prompt)

    def test_theme_day_prompt_prefers_free_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            prompt = scheduler._theme_day_prompt_block(
                "霍格沃兹体验日",
                "穿越到霍格沃兹体验当学生的一天",
            )

        self.assertIn("穿越到霍格沃兹体验当学生的一天", prompt)
        self.assertIn("学院学生的一天", prompt)
        self.assertIn("真实发生的世界", prompt)
        self.assertIn("不能因为休息日把它们迁回家中", prompt)

    def test_theme_day_uses_theme_schedule_type_and_calendar_role_exception(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            prompt = scheduler._build_schedule_prompt(
                date(2026, 8, 1),
                "（无）",
                "（无）",
                theme_day="霍格沃兹体验日",
                theme_description="穿越到霍格沃兹体验学生的一天",
            )

        self.assertIn("日程类型：主题体验日", prompt)
        self.assertIn("主题体验例外", prompt)
        self.assertIn("主题课程", prompt)

    def test_theme_scene_drift_rejects_generic_home_majority(self):
        details = [
            {"scene_en": "modern apartment living room with a sofa"},
            {"scene_en": "ordinary home kitchen with contemporary cabinets"},
            {"scene_en": "small residential bedroom"},
            {"scene_en": "apartment balcony overlooking the city"},
            {"scene_en": "ancient stone castle library"},
            {"scene_en": "Gothic dining hall with long wooden tables"},
        ]

        error = DailyScheduler._theme_scene_drift_error(
            "霍格沃兹体验日",
            "穿越到霍格沃兹体验学生的一天",
            details,
        )

        self.assertIn("主题场景出戏", error)
        self.assertIn("普通住宅", error)

    def test_theme_scene_drift_accepts_world_specific_locations(self):
        details = [
            {"scene_en": "ancient castle great hall with floating candles"},
            {"scene_en": "stone-walled charms classroom with wooden desks"},
            {"scene_en": "Gothic magical library with towering shelves"},
            {"scene_en": "covered castle courtyard beside a greenhouse"},
            {"scene_en": "moving-staircase corridor with portraits"},
            {"scene_en": "green-accented house common room inside the castle"},
        ]

        error = DailyScheduler._theme_scene_drift_error(
            "霍格沃兹体验日",
            "穿越到霍格沃兹体验学生的一天",
            details,
        )

        self.assertEqual("", error)

    def test_random_theme_comes_from_curated_pool(self):
        self.assertIn(DailyScheduler.random_theme_day(), THEME_DAY_POOL)

    def test_daily_entry_persists_theme_day_fields(self):
        entry = DailyEntry(
            date=date(2026, 7, 31).isoformat(),
            theme_day="霍格沃兹",
            theme_day_mode="custom",
        )

        restored = DailyEntry.from_dict(entry.to_dict())

        self.assertEqual("霍格沃兹", restored.theme_day)
        self.assertEqual("custom", restored.theme_day_mode)

    @staticmethod
    def _valid_theme_day_data() -> dict:
        times = ["07:23", "09:48", "12:31", "15:27", "18:42", "21:15"]
        activities_zh = [
            "在城堡庭院舒展身体并观察魔法植物",
            "在魔咒教室上课并练习基础手势",
            "在大礼堂享用学院午餐",
            "在魔法图书馆整理植物图鉴笔记",
            "沿移动楼梯回廊寻找学院画像",
            "在学院公共休息室阅读睡前故事",
        ]
        activities_en = [
            "stretching in the castle courtyard while observing magical plants",
            "attending class and practicing basic gestures in the charms classroom",
            "having an academy lunch in the great hall",
            "organizing magical botany notes in the castle library",
            "following the moving-staircase corridor to find house portraits",
            "reading a bedtime story in the house common room",
        ]
        scenes_en = [
            "ancient stone castle courtyard beside an enchanted greenhouse",
            "stone-walled charms classroom with rows of wooden desks",
            "Gothic great hall with long wooden tables and floating candles",
            "towering castle library with carved shelves and old manuscripts",
            "moving-staircase corridor lined with animated portraits",
            "green-accented house common room inside the ancient castle",
        ]
        schedule_lines = [
            f"{time} {activity}" for time, activity in zip(times, activities_zh)
        ]
        schedule_details = [
            {
                "time": time,
                "activity_zh": activity_zh,
                "activity_en": activity_en,
                "action_en": "moving naturally while facing the camera",
                "scene_en": scene_en,
                "outfit_en": "white blouse with a brown skirt and a knitted cardigan",
                "hair_en": "loose natural hair",
            }
            for time, activity_zh, activity_en, scene_en in zip(
                times, activities_zh, activities_en, scenes_en
            )
        ]
        return {
            "outfit_style": "魔法学院风",
            "outfit": {
                "风格": "魔法学院风",
                "发型": "自然披散的长发",
                "穿搭": "白色衬衫搭配棕色半身裙和针织开衫",
                "动作": "日常自然活动",
                "场景": "古老魔法城堡的学院空间",
            },
            "schedule": "\n".join(schedule_lines),
            "schedule_details": schedule_details,
            "prompt": "A woman in a magical academy outfit spending a day inside an ancient castle school",
            "outfit_keywords": "white blouse, brown skirt, knitted cardigan",
            "scene_keywords": "ancient castle, Gothic classroom, great hall, magical library",
            "photo_style_en": "candid handheld photos with natural castle window light",
        }

    def test_salvage_schedule_prompt_rebuilds_missing_english_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            payload = self._valid_theme_day_data()
            display_items = scheduler._schedule_plan_items(payload["schedule"])

            salvaged = scheduler._salvage_schedule_prompt(payload, display_items)

            self.assertEqual(6, len(scheduler._schedule_plan_items(salvaged)))
            self.assertTrue(salvaged.startswith("07:23 "))

    def test_salvage_schedule_prompt_requires_matching_details(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            payload = self._valid_theme_day_data()
            display_items = scheduler._schedule_plan_items(payload["schedule"])

            self.assertEqual("", scheduler._salvage_schedule_prompt({}, display_items))
            payload["schedule_details"] = []
            self.assertEqual("", scheduler._salvage_schedule_prompt(payload, display_items))

    async def test_theme_day_generation_salvages_missing_schedule_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            payload = self._valid_theme_day_data()
            payload.pop("schedule_prompt", None)
            scheduler._call_llm = AsyncMock(
                return_value=json.dumps(payload, ensure_ascii=False)
            )

            entry = await scheduler.generate_today(
                target_date=date(2026, 8, 1),
                theme_day="霍格沃兹体验日",
            )

            self.assertEqual("ok", entry.status)
            self.assertEqual(6, len(scheduler._schedule_plan_items(entry.schedule_prompt)))
            self.assertIn("07:23", entry.schedule_prompt)

    async def test_theme_day_ignores_disliked_outfit_similarity_hard_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            scheduler._call_llm = AsyncMock(
                return_value=json.dumps(self._valid_theme_day_data(), ensure_ascii=False)
            )
            scheduler._disliked_outfit_similarity_error = Mock(
                return_value="与用户标记不喜欢的穿搭高度相似"
            )

            entry = await scheduler.generate_today(
                target_date=date(2026, 8, 1),
                theme_day="霍格沃兹体验日",
            )

            self.assertEqual("ok", entry.status)
            self.assertEqual(1, scheduler._call_llm.await_count)
            scheduler._disliked_outfit_similarity_error.assert_called_once()

    async def test_regular_schedule_keeps_disliked_outfit_similarity_hard_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            scheduler._call_llm = AsyncMock(
                return_value=json.dumps(self._valid_theme_day_data(), ensure_ascii=False)
            )
            scheduler._disliked_outfit_similarity_error = Mock(
                return_value="与用户标记不喜欢的穿搭高度相似"
            )

            entry = await scheduler.generate_today(target_date=date(2026, 8, 3))

            self.assertEqual("failed", entry.status)
            self.assertEqual(3, scheduler._call_llm.await_count)
            self.assertEqual(3, scheduler._disliked_outfit_similarity_error.call_count)
            self.assertIn(
                "上一候选已被系统拒绝",
                scheduler._call_llm.await_args_list[1].args[0],
            )

    async def test_theme_search_query_uses_theme_keyword_when_llm_drifts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            scheduler._call_llm = AsyncMock(
                return_value=json.dumps({"keyword": "夏日温柔居家穿搭"}, ensure_ascii=False)
            )

            keyword = await scheduler.generate_xiaohongshu_search_query(
                theme_day="霍格沃兹体验日",
                target_date=date(2026, 8, 1),
            )

            self.assertEqual("霍格沃兹穿搭", keyword)

    async def test_theme_search_query_accepts_thematic_descriptor(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            scheduler._call_llm = AsyncMock(
                return_value=json.dumps({"keyword": "魔法学院风穿搭"}, ensure_ascii=False)
            )

            keyword = await scheduler.generate_xiaohongshu_search_query(
                theme_day="霍格沃兹体验日",
                theme_description="穿越到霍格沃兹体验当学生的一天",
                target_date=date(2026, 8, 1),
            )

            self.assertEqual("魔法学院风穿搭", keyword)

    async def test_theme_day_reference_prep_forwards_extended_xiaohongshu_timeout(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app.scheduler_gen = SimpleNamespace(
            generate_xiaohongshu_search_query=AsyncMock(
                return_value="霍格沃兹穿搭"
            ),
        )
        app.web_server = SimpleNamespace(
            xiaohongshu_schedule_enabled=lambda: True,
            ensure_xiaohongshu_schedule_reference=AsyncMock(return_value={}),
        )

        selected, keyword = await app._prepare_xiaohongshu_schedule_reference(
            "2026-08-01",
            theme_day="霍格沃兹体验日",
            selection_timeout_seconds=480,
        )

        self.assertEqual({}, selected)
        self.assertEqual("霍格沃兹穿搭", keyword)
        app.web_server.ensure_xiaohongshu_schedule_reference.assert_awaited_once_with(
            "2026-08-01",
            {
                "date": "2026-08-01",
                "xiaohongshu_search_query": "霍格沃兹穿搭",
                "theme_day": "霍格沃兹体验日",
            },
            force=True,
            timeout_seconds=480,
        )

    async def test_manual_reference_failure_does_not_silently_fall_back(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app._today = lambda: date(2026, 8, 1)
        app.scheduler_gen = SimpleNamespace(
            _normalize_theme_day=lambda value: value,
            random_theme_day=lambda: "随机主题",
            generate_today=AsyncMock(),
        )
        app.web_server = SimpleNamespace(
            xiaohongshu_schedule_enabled=lambda: True,
        )
        app._prepare_xiaohongshu_schedule_reference = AsyncMock(
            return_value=({}, "霍格沃兹穿搭")
        )

        with self.assertRaisesRegex(ValueError, "手动指定的小红书穿搭读取失败"):
            await app.generate_theme_day(
                "霍格沃兹体验日",
                manual_reference_url="/local-refs/xiaohongshu/missing.webp",
            )

        app.scheduler_gen.generate_today.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
