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
from web_server import GalleryServer  # noqa: E402


class ThemeDayTests(unittest.IsolatedAsyncioTestCase):
    def test_theme_day_prompt_turns_a_theme_into_a_full_day_constraint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            prompt = scheduler._theme_day_prompt_block("霍格沃兹主题日")

        self.assertIn("霍格沃兹", prompt)
        self.assertIn("穿搭、发型、活动、场景", prompt)
        self.assertIn("不要擅自把宽泛主题收窄成某一种职业", prompt)
        self.assertIn("不要求所有时段都困在同一场馆、岗位或专业流程", prompt)
        self.assertIn("先设计一天的体验弧线，再填写时间", prompt)
        self.assertIn("不要把同一任务流程的不同阶段拆成多条日程", prompt)
        self.assertIn("不是要求每条活动都执行同一主题任务", prompt)
        self.assertIn("自然生活过渡和少量支线", prompt)
        self.assertIn("不要自行设定成该职业的一整天", prompt)
        self.assertIn("只能清楚出现角色本人", prompt)
        self.assertNotIn("每个 schedule_details.scene_en 都必须明确一个属于该主题的具体地点", prompt)
        self.assertNotIn("霍格沃兹主题日主题日", prompt)

    def test_theme_revision_rule_breaks_workflow_without_dropping_theme(self):
        rule = DailyScheduler._theme_similarity_revision_rule("博物馆灵感日")

        self.assertIn("重构后必须仍然是「博物馆灵感日」", rule)
        self.assertIn("不得把它替换成另一个无关主题", rule)
        self.assertIn("主题应保留为审美和故事背景", rule)
        self.assertIn("解除单一场馆、职业或工作流绑定", rule)
        self.assertIn("大多数非过渡时段重新设计", rule)
        self.assertIn("不要只把同一任务流程改写成另一种同结构流程", rule)
        self.assertIn("不能只是用餐、移动、观看、整理或复盘", rule)
        self.assertIn("与指定主题的联系都应能由当天故事和实际行动自然解释", rule)
        self.assertEqual("", DailyScheduler._theme_similarity_revision_rule())

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

    async def test_generate_today_passes_random_theme_source_to_semantic_reviewer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            scheduler._call_llm = AsyncMock(
                return_value=json.dumps(self._valid_theme_day_data(), ensure_ascii=False)
            )
            review = AsyncMock(return_value={
                "available": True,
                "needs_revision": False,
                "similar": False,
                "cross_day_repeat": False,
                "within_day_homogeneous": False,
                "dominant_themes": ["多种主题参与方式"],
                "candidate_clusters": [],
                "novel_anchor": "完成主题体验成果",
                "matches": [],
                "revision_guidance": "",
                "reason": "候选内部有充分行动变化",
            })
            scheduler._review_schedule_similarity_with_llm = review

            entry = await scheduler.generate_today(
                target_date=date(2026, 8, 3),
                theme_day="博物馆灵感日",
                theme_day_mode="random",
            )

        self.assertEqual("ok", entry.status)
        self.assertEqual("random", entry.theme_day_mode)
        self.assertEqual(
            "random",
            review.await_args.kwargs["theme_day_mode"],
        )

    def test_legacy_theme_day_state_maps_false_to_auto_and_true_to_manual(self):
        server = GalleryServer.__new__(GalleryServer)
        server.theme_day_state_store = SimpleNamespace(
            load=lambda: {"enabled": False, "updated_at": "2026-08-09T12:00:00+08:00"}
        )

        automatic = server.theme_day_state()

        self.assertFalse(automatic["enabled"])
        self.assertEqual("auto", automatic["mode"])

        server.theme_day_state_store = SimpleNamespace(
            load=lambda: {"enabled": True, "updated_at": "2026-08-09T12:01:00+08:00"}
        )

        manual = server.theme_day_state()

        self.assertTrue(manual["enabled"])
        self.assertEqual("manual", manual["mode"])

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

    async def test_theme_day_generation_salvages_nonempty_malformed_schedule_prompt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            payload = self._valid_theme_day_data()
            # Providers sometimes emit a non-empty prose/partial field. It
            # must take the same structured-details recovery path as omission.
            payload["schedule_prompt"] = "07:23 explore the castle"
            scheduler._call_llm = AsyncMock(
                return_value=json.dumps(payload, ensure_ascii=False)
            )
            scheduler._review_schedule_similarity_with_llm = AsyncMock(
                return_value={
                    "available": True,
                    "needs_revision": False,
                    "cross_day_repeat": False,
                    "within_day_homogeneous": False,
                    "theme_drift": False,
                }
            )
            salvage = Mock(wraps=scheduler._salvage_schedule_prompt)
            scheduler._salvage_schedule_prompt = salvage

            entry = await scheduler.generate_today(
                target_date=date(2026, 8, 1),
                theme_day="霍格沃兹体验日",
            )

            self.assertEqual("ok", entry.status)
            self.assertEqual(6, len(scheduler._schedule_plan_items(entry.schedule_prompt)))
            self.assertIn("07:23", entry.schedule_prompt)
            self.assertGreaterEqual(salvage.call_count, 1)

    async def test_theme_day_rejects_candidate_still_homogeneous_after_revision_budget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            payload = self._valid_theme_day_data()
            scheduler._call_llm = AsyncMock(
                return_value=json.dumps(payload, ensure_ascii=False)
            )
            review = AsyncMock(return_value={
                "available": True,
                "needs_revision": True,
                "cross_day_repeat": True,
                "within_day_homogeneous": True,
                "theme_drift": False,
                "theme_connection": "活动仍与主题有关，但重复同一工作流",
                "dominant_themes": ["单一主题工作流"],
                "candidate_clusters": [{
                    "theme": "单一主题工作流",
                    "times": ["07:23", "10:17", "14:18", "17:11", "20:18", "22:27"],
                    "role": "core_active",
                    "why": "所有核心时段仍是同一流程的连续步骤",
                }],
                "novel_anchor": "",
                "matches": [],
                "revision_guidance": "丢弃原核心流程并从空白重建不同参与方式",
                "reason": "多次改稿后仍内部同质化",
            })
            scheduler._review_schedule_similarity_with_llm = review

            entry = await scheduler.generate_today(
                target_date=date(2026, 8, 3),
                theme_day="博物馆灵感日",
                theme_day_mode="random",
            )

            self.assertEqual("failed", entry.status)
            self.assertEqual("fallback", entry.source)
            # One initial candidate plus three bounded semantic rewrites.
            self.assertEqual(4, scheduler._call_llm.await_count)
            self.assertEqual(4, review.await_count)
            final_prompt = scheduler._call_llm.await_args_list[-1].args[0]
            self.assertIn("整稿策略", final_prompt)
            self.assertIn("核心体验从空白重新设计", final_prompt)

    async def test_theme_day_gets_one_extra_targeted_revision_for_isolated_repeat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            payload = self._valid_theme_day_data()
            scheduler._call_llm = AsyncMock(
                return_value=json.dumps(payload, ensure_ascii=False)
            )
            homogeneous_review = {
                "available": True,
                "needs_revision": True,
                "cross_day_repeat": True,
                "within_day_homogeneous": True,
                "theme_drift": False,
                "theme_connection": "主题成立但核心流程仍单一",
                "dominant_themes": ["单一主题工作流"],
                "candidate_clusters": [{
                    "theme": "单一主题工作流",
                    "times": ["07:23", "10:17", "14:18", "17:11"],
                    "role": "core_active",
                    "why": "核心时段仍属于同一流程",
                }],
                "novel_anchor": "",
                "matches": [],
                "revision_guidance": "从空白重建核心体验",
                "reason": "候选内部同质化",
            }
            isolated_repeat = {
                "available": True,
                "needs_revision": True,
                "cross_day_repeat": True,
                "within_day_homogeneous": False,
                "theme_drift": False,
                "theme_connection": "全天计划结构已经丰富且主题成立",
                "dominant_themes": ["多种主题参与方式"],
                "candidate_clusters": [],
                "novel_anchor": "完成一次与艺术家的共同视觉决策",
                "matches": [{
                    "candidate_time": "15:42",
                    "candidate_activity": "参与一项文化资料修复协作",
                    "history_date": "2026-08-10",
                    "history_activity": "参与古籍修复",
                    "reason": "仅该时段与近期核心任务实质重复",
                }],
                "revision_guidance": "只替换15:42，保留其他已通过时段",
                "reason": "全天不再同质，但仍有一个孤立跨日重复",
            }
            accepted_review = {
                "available": True,
                "needs_revision": False,
                "cross_day_repeat": False,
                "within_day_homogeneous": False,
                "theme_drift": False,
                "theme_connection": "主题成立且孤立重复已替换",
                "dominant_themes": ["多种主题参与方式"],
                "candidate_clusters": [],
                "novel_anchor": "完成一次与艺术家的共同视觉决策",
                "matches": [],
                "revision_guidance": "",
                "reason": "候选通过",
            }
            review = AsyncMock(side_effect=[
                homogeneous_review,
                homogeneous_review,
                homogeneous_review,
                isolated_repeat,
                accepted_review,
            ])
            scheduler._review_schedule_similarity_with_llm = review

            entry = await scheduler.generate_today(
                target_date=date(2026, 8, 3),
                theme_day="博物馆灵感日",
                theme_day_mode="random",
            )

            self.assertEqual("ok", entry.status)
            self.assertEqual(5, scheduler._call_llm.await_count)
            self.assertEqual(5, review.await_count)
            targeted_prompt = scheduler._call_llm.await_args_list[-1].args[0]
            self.assertIn("需要定点改稿", targeted_prompt)
            self.assertIn("只定点替换审查器明确命中的跨日重复时段", targeted_prompt)
            self.assertIn("保留审查器未命中的核心体验、精彩锚点和整体节奏", targeted_prompt)
            self.assertNotIn("上一候选的非过渡核心活动、地点、道具、步骤和结果关系都视为废弃草稿", targeted_prompt)

    async def test_theme_day_ignores_disliked_outfit_similarity_hard_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, tmpdir)
            scheduler._call_llm = AsyncMock(side_effect=[
                json.dumps(self._valid_theme_day_data(), ensure_ascii=False),
                json.dumps({
                    "needs_revision": False,
                    "cross_day_repeat": False,
                    "within_day_homogeneous": False,
                    "dominant_themes": ["霍格沃兹体验"],
                    "matches": [],
                    "revision_guidance": "",
                    "reason": "主题统一来自用户明确选择，内部活动仍有行动变化",
                }, ensure_ascii=False),
            ])
            scheduler._disliked_outfit_similarity_error = Mock(
                return_value="与用户标记不喜欢的穿搭高度相似"
            )

            entry = await scheduler.generate_today(
                target_date=date(2026, 8, 1),
                theme_day="霍格沃兹体验日",
            )

            self.assertEqual("ok", entry.status)
            self.assertEqual(2, scheduler._call_llm.await_count)
            review_prompt = scheduler._call_llm.await_args_list[1].args[0]
            self.assertIn("霍格沃兹体验日", review_prompt)
            self.assertIn("不要只因多个活动共享该主题名称就要求改稿", review_prompt)
            self.assertIn("主题只是叙事背景，不豁免同一工作流程", review_prompt)
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

    async def test_refresh_schedule_uses_random_theme_day_pipeline(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        entry = DailyEntry(
            date="2026-08-01",
            schedule="08:00 逛旧书市集",
            status="ok",
            source="theme_day",
            theme_day="旧城寻宝日",
            theme_day_mode="random",
        )
        app.generate_theme_day = AsyncMock(return_value=entry)
        app._today_schedule_entry = Mock(return_value={})
        app._schedule_dynamic_photos = AsyncMock()
        app.data_dir = "unused"

        with unittest.mock.patch("main.save_schedule_entry") as save_entry:
            refreshed = await app._refresh_schedule_impl()

        self.assertIs(entry, refreshed)
        app.generate_theme_day.assert_awaited_once_with(
            target="today",
            mode="random",
            persist=False,
            schedule_photos=False,
        )
        self.assertEqual(("unused", entry), save_entry.call_args.args)
        self.assertTrue(callable(save_entry.call_args.kwargs.get("replace_guard")))
        app._schedule_dynamic_photos.assert_awaited_once_with(
            entry.schedule,
            entry.date,
        )

    async def test_daily_refresh_preserves_an_explicitly_planned_theme_day(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        planned = DailyEntry(
            date="2026-08-01",
            schedule="08:00 进入魔法学院大礼堂",
            status="ok",
            source="theme_day",
            theme_day="霍格沃兹体验日",
            theme_day_mode="custom",
        )
        app.generate_theme_day = AsyncMock()
        app._today_schedule_entry = Mock(return_value=planned.to_dict())
        app._schedule_missing_required_periods = Mock(return_value=[])
        app._schedule_dynamic_photos = AsyncMock()

        refreshed = await app._refresh_schedule_impl(preserve_theme_day=True)

        self.assertEqual("霍格沃兹体验日", refreshed.theme_day)
        self.assertEqual("custom", refreshed.theme_day_mode)
        app.generate_theme_day.assert_not_awaited()
        app._schedule_dynamic_photos.assert_awaited_once_with(
            planned.schedule,
            planned.date,
        )

    async def test_custom_theme_is_not_replaced_by_random_theme(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app._today = lambda: date(2026, 8, 1)
        entry = DailyEntry(
            date="2026-08-01",
            schedule="08:00 在未来城市搭乘悬浮列车",
            status="ok",
        )
        app.scheduler_gen = SimpleNamespace(
            _normalize_theme_day=lambda value: value,
            random_theme_day=Mock(return_value="不应使用的随机主题"),
            generate_today=AsyncMock(return_value=entry),
        )
        app.web_server = SimpleNamespace(
            xiaohongshu_schedule_enabled=lambda: False,
        )
        app._prepare_xiaohongshu_schedule_reference = AsyncMock(
            return_value=({}, "未来都市穿搭")
        )
        app._schedule_dynamic_photos = AsyncMock()
        app.data_dir = "unused"

        generated = await app.generate_theme_day(
            "未来都市通勤日",
            mode="custom",
            persist=False,
            schedule_photos=False,
        )

        self.assertEqual("未来都市通勤日", generated.theme_day)
        self.assertEqual("custom", generated.theme_day_mode)
        app.scheduler_gen.random_theme_day.assert_not_called()
        kwargs = app.scheduler_gen.generate_today.await_args.kwargs
        self.assertEqual("未来都市通勤日", kwargs["theme_day"])
        self.assertEqual("custom", kwargs["theme_day_mode"])
        app._schedule_dynamic_photos.assert_not_awaited()

    async def test_random_theme_mode_selects_from_existing_theme_day_pipeline(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app._today = lambda: date(2026, 8, 1)
        entry = DailyEntry(
            date="2026-08-01",
            schedule="08:00 在老城寻找隐藏壁画",
            status="ok",
        )
        app.scheduler_gen = SimpleNamespace(
            _normalize_theme_day=lambda value: value,
            random_theme_day=Mock(return_value="旧城寻宝日"),
            generate_today=AsyncMock(return_value=entry),
        )
        app.web_server = SimpleNamespace(
            xiaohongshu_schedule_enabled=lambda: False,
        )
        app._prepare_xiaohongshu_schedule_reference = AsyncMock(
            return_value=({}, "")
        )
        app._schedule_dynamic_photos = AsyncMock()
        app.data_dir = "unused"

        generated = await app.generate_theme_day(
            mode="random",
            persist=False,
            schedule_photos=False,
        )

        app.scheduler_gen.random_theme_day.assert_called_once_with()
        self.assertEqual("旧城寻宝日", generated.theme_day)
        self.assertEqual("random", generated.theme_day_mode)
        kwargs = app.scheduler_gen.generate_today.await_args.kwargs
        self.assertEqual("旧城寻宝日", kwargs["theme_day"])
        self.assertEqual("random", kwargs["theme_day_mode"])

    async def test_automatic_theme_reuses_existing_manual_xiaohongshu_reference(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app.scheduler_gen = SimpleNamespace(
            generate_xiaohongshu_search_query=AsyncMock(
                return_value="不应重新生成的搜索词"
            ),
        )
        existing = {
            "path": "/tmp/manual-theme.webp",
            "url": "/local-refs/xiaohongshu/manual-theme.webp",
            "query": "手动选择的魔法学院穿搭",
            "selection_source": "manual",
        }
        app.web_server = SimpleNamespace(
            xiaohongshu_schedule_enabled=lambda: True,
            ensure_xiaohongshu_schedule_reference=AsyncMock(return_value={}),
            _xiaohongshu_schedule_reference=Mock(return_value=existing),
        )

        selected, keyword = await app._prepare_xiaohongshu_schedule_reference(
            "2026-08-01",
            force=True,
            theme_day="旧城寻宝日",
            manual_reference_url=existing["url"],
        )

        self.assertEqual(existing, selected)
        self.assertEqual("手动选择的魔法学院穿搭", keyword)
        app.scheduler_gen.generate_xiaohongshu_search_query.assert_not_awaited()
        app.web_server.ensure_xiaohongshu_schedule_reference.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
