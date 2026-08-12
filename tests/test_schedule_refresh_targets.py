import asyncio
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from data import DailyEntry  # noqa: E402
import main as main_module  # noqa: E402
from main import PortraitGalleryApp  # noqa: E402
from store import ScheduleStore  # noqa: E402
from web_server import GalleryServer  # noqa: E402


class JsonRequest:
    can_read_body = True

    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class InvalidJsonRequest:
    can_read_body = True

    async def json(self):
        raise ValueError("invalid json")


class ScheduleRefreshTargetTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _app(data_dir: str = "unused") -> PortraitGalleryApp:
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app.data_dir = data_dir
        app._today = lambda: date(2026, 8, 11)
        app._schedule_refresh_task = None
        app._schedule_refresh_task_date = ""
        app._schedule_refresh_state = None
        app._tomorrow_schedule_refresh_task = None
        app._tomorrow_schedule_refresh_task_date = ""
        app._schedule_refresh_preserve_theme_day = False
        app._schedule_dynamic_photos = AsyncMock()
        return app

    @staticmethod
    def _entry(
        entry_date: str,
        activity: str,
        *,
        theme: str,
        mode: str = "random",
        source: str = "theme_day",
        status: str = "ok",
    ) -> DailyEntry:
        return DailyEntry(
            date=entry_date,
            schedule=f"08:00 {activity}",
            status=status,
            source=source,
            theme_day=theme,
            theme_day_mode=mode,
        )

    async def test_refresh_endpoint_routes_tomorrow_to_tomorrow_date(self):
        server = GalleryServer.__new__(GalleryServer)
        server.on_refresh_schedule = AsyncMock()
        server._today = lambda: date(2026, 8, 11)
        refreshed = self._entry(
            "2026-08-12",
            "在海边灯塔记录潮汐",
            theme="海岸观察日",
        )
        server._refresh_schedule_singleflight = AsyncMock(return_value=refreshed)

        response = await server.handle_refresh_schedule(
            JsonRequest({
                "target": "tomorrow",
                "target_date": "2026-08-12",
            })
        )
        payload = json.loads(response.text)

        self.assertEqual(200, response.status)
        self.assertEqual("tomorrow", payload["target"])
        self.assertEqual("2026-08-12", payload["target_date"])
        self.assertEqual("2026-08-12", payload["entry"]["date"])
        server._refresh_schedule_singleflight.assert_awaited_once_with(
            target="tomorrow",
            target_date="2026-08-12",
        )

    async def test_refresh_endpoint_rejects_invalid_target_or_date(self):
        server = GalleryServer.__new__(GalleryServer)
        server.on_refresh_schedule = AsyncMock()
        server._today = lambda: date(2026, 8, 11)
        server._refresh_schedule_singleflight = AsyncMock()

        cases = (
            (
                {"target": "day_after_tomorrow"},
                "invalid_target",
            ),
            (
                {"target": "tomorrow", "target_date": "2026-08-11"},
                "invalid_target_date",
            ),
            (
                {"target": "tomorrow", "target_date": "2026/08/12"},
                "invalid_target_date",
            ),
        )
        for body, expected_error in cases:
            with self.subTest(body=body):
                response = await server.handle_refresh_schedule(JsonRequest(body))
                payload = json.loads(response.text)
                self.assertEqual(400, response.status)
                self.assertEqual(expected_error, payload["error"])

        server._refresh_schedule_singleflight.assert_not_awaited()

    async def test_refresh_endpoint_rejects_malformed_json_without_refreshing_today(self):
        server = GalleryServer.__new__(GalleryServer)
        server.on_refresh_schedule = AsyncMock()
        server._today = lambda: date(2026, 8, 11)
        server._refresh_schedule_singleflight = AsyncMock()

        response = await server.handle_refresh_schedule(InvalidJsonRequest())
        payload = json.loads(response.text)

        self.assertEqual(400, response.status)
        self.assertEqual("invalid_json", payload["error"])
        server._refresh_schedule_singleflight.assert_not_awaited()

    async def test_refresh_endpoint_rejects_success_entry_for_wrong_date(self):
        server = GalleryServer.__new__(GalleryServer)
        server.on_refresh_schedule = AsyncMock()
        server._today = lambda: date(2026, 8, 11)
        wrong_date = self._entry(
            "2026-08-11",
            "错误地刷新了当天",
            theme="错误目标",
        )
        server._refresh_schedule_singleflight = AsyncMock(return_value=wrong_date)

        response = await server.handle_refresh_schedule(
            JsonRequest({
                "target": "tomorrow",
                "target_date": "2026-08-12",
            })
        )
        payload = json.loads(response.text)

        self.assertEqual(500, response.status)
        self.assertEqual("schedule_generate_failed", payload["error"])
        self.assertIn("2026-08-12", payload["message"])

    async def test_tomorrow_refresh_replaces_only_tomorrow_and_never_rebuilds_jobs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            today = self._entry(
                "2026-08-11",
                "在老城散步",
                theme="旧城漫游日",
            )
            old_tomorrow = self._entry(
                "2026-08-12",
                "整理旅行照片",
                theme="居家整理日",
            )
            replacement = self._entry(
                "2026-08-12",
                "在海边灯塔记录潮汐",
                theme="海岸观察日",
            )
            ScheduleStore(tmpdir).save({
                today.date: today.to_dict(),
                old_tomorrow.date: old_tomorrow.to_dict(),
            })
            app = self._app(tmpdir)
            app.generate_theme_day = AsyncMock(return_value=replacement)

            result = await app._refresh_tomorrow_schedule_impl("2026-08-12")
            stored = ScheduleStore(tmpdir).load()

        self.assertIs(replacement, result)
        self.assertEqual(today.schedule, stored["2026-08-11"]["schedule"])
        self.assertEqual(replacement.schedule, stored["2026-08-12"]["schedule"])
        app.generate_theme_day.assert_awaited_once_with(
            target="tomorrow",
            target_date="2026-08-12",
            mode="random",
            persist=False,
            schedule_photos=False,
        )
        app._schedule_dynamic_photos.assert_not_awaited()

    async def test_failed_tomorrow_refresh_preserves_existing_tomorrow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            existing = self._entry(
                "2026-08-12",
                "参加夜间天文观测",
                theme="星空观测日",
            )
            failed = self._entry(
                "2026-08-12",
                "生成失败",
                theme="",
                source="fallback",
                status="failed",
            )
            ScheduleStore(tmpdir).save({existing.date: existing.to_dict()})
            app = self._app(tmpdir)
            app.generate_theme_day = AsyncMock(return_value=failed)

            result = await app._refresh_tomorrow_schedule_impl("2026-08-12")
            stored = ScheduleStore(tmpdir).load()

        self.assertEqual("preserved", result.source)
        self.assertEqual(existing.schedule, result.schedule)
        self.assertEqual(existing.schedule, stored["2026-08-12"]["schedule"])
        app._schedule_dynamic_photos.assert_not_awaited()

    async def test_wrong_date_candidate_without_existing_tomorrow_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wrong_date = self._entry(
                "2026-08-11",
                "错误地刷新了当天",
                theme="错误目标",
            )
            app = self._app(tmpdir)
            app.generate_theme_day = AsyncMock(return_value=wrong_date)

            result = await app._refresh_tomorrow_schedule_impl("2026-08-12")
            stored = ScheduleStore(tmpdir).load()

        self.assertEqual("failed", result.status)
        self.assertEqual("refresh_target_mismatch", result.source)
        self.assertEqual("2026-08-12", result.date)
        self.assertEqual({}, stored)
        app._schedule_dynamic_photos.assert_not_awaited()

    async def test_atomic_guard_keeps_manual_tomorrow_saved_during_refresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            existing = self._entry(
                "2026-08-12",
                "在旧书店淘书",
                theme="旧城寻宝日",
            )
            random_candidate = self._entry(
                "2026-08-12",
                "体验水上运动",
                theme="海岛冒险日",
            )
            manual = self._entry(
                "2026-08-12",
                "进入魔法学院参加变形课",
                theme="霍格沃兹体验日",
                mode="custom",
            )
            ScheduleStore(tmpdir).save({existing.date: existing.to_dict()})
            app = self._app(tmpdir)
            app.generate_theme_day = AsyncMock(return_value=random_candidate)
            real_save = main_module.save_schedule_entry

            def save_after_manual_write(data_dir, entry, *, replace_guard=None):
                ScheduleStore(data_dir).update(
                    lambda all_data: {
                        **all_data,
                        manual.date: manual.to_dict(),
                    }
                )
                return real_save(
                    data_dir,
                    entry,
                    replace_guard=replace_guard,
                )

            with patch.object(
                main_module,
                "save_schedule_entry",
                side_effect=save_after_manual_write,
            ):
                result = await app._refresh_tomorrow_schedule_impl("2026-08-12")
            stored = ScheduleStore(tmpdir).load()

        self.assertEqual("preserved", result.source)
        self.assertEqual("custom", result.theme_day_mode)
        self.assertEqual(manual.schedule, stored["2026-08-12"]["schedule"])
        self.assertNotEqual(
            random_candidate.schedule,
            stored["2026-08-12"]["schedule"],
        )
        app._schedule_dynamic_photos.assert_not_awaited()

    async def test_atomic_guard_keeps_manual_today_saved_during_refresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            existing = self._entry(
                "2026-08-11",
                "在旧书店淘书",
                theme="旧城寻宝日",
            )
            random_candidate = self._entry(
                "2026-08-11",
                "体验水上运动",
                theme="海岛冒险日",
            )
            manual = self._entry(
                "2026-08-11",
                "进入魔法学院参加变形课",
                theme="霍格沃兹体验日",
                mode="custom",
            )
            ScheduleStore(tmpdir).save({existing.date: existing.to_dict()})
            app = self._app(tmpdir)
            app.generate_theme_day = AsyncMock(return_value=random_candidate)
            app._schedule_missing_required_periods = Mock(return_value=[])
            real_save = main_module.save_schedule_entry

            def save_after_manual_write(data_dir, entry, *, replace_guard=None):
                ScheduleStore(data_dir).update(
                    lambda all_data: {
                        **all_data,
                        manual.date: manual.to_dict(),
                    }
                )
                return real_save(
                    data_dir,
                    entry,
                    replace_guard=replace_guard,
                )

            with patch.object(
                main_module,
                "save_schedule_entry",
                side_effect=save_after_manual_write,
            ):
                result = await app._refresh_schedule_impl()
            stored = ScheduleStore(tmpdir).load()

        self.assertEqual("custom", result.theme_day_mode)
        self.assertEqual(manual.schedule, stored["2026-08-11"]["schedule"])
        self.assertNotEqual(
            random_candidate.schedule,
            stored["2026-08-11"]["schedule"],
        )
        app._schedule_dynamic_photos.assert_awaited_once_with(
            manual.schedule,
            manual.date,
        )

    async def test_late_today_rollback_does_not_overwrite_newer_manual_theme(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            existing = self._entry(
                "2026-08-11",
                "在旧书店淘书",
                theme="旧城寻宝日",
                mode="custom",
            )
            random_candidate = self._entry(
                "2026-08-11",
                "体验水上运动",
                theme="海岛冒险日",
            )
            newer_manual = self._entry(
                "2026-08-11",
                "进入魔法学院参加变形课",
                theme="霍格沃兹体验日",
                mode="custom",
            )
            ScheduleStore(tmpdir).save({existing.date: existing.to_dict()})
            app = self._app(tmpdir)
            app.generate_theme_day = AsyncMock(return_value=random_candidate)
            app._schedule_missing_required_periods = Mock(return_value=[])
            real_save = main_module.save_schedule_entry
            save_calls = {"count": 0}

            def save_with_newer_manual_before_rollback(
                data_dir,
                entry,
                *,
                replace_guard=None,
            ):
                save_calls["count"] += 1
                if save_calls["count"] == 2:
                    real_save(data_dir, newer_manual)
                return real_save(
                    data_dir,
                    entry,
                    replace_guard=replace_guard,
                )

            async def rebuild_jobs(schedule, _schedule_date):
                if schedule == random_candidate.schedule:
                    app._schedule_refresh_preserve_theme_day = True

            app._schedule_dynamic_photos = AsyncMock(side_effect=rebuild_jobs)
            with patch.object(
                main_module,
                "save_schedule_entry",
                side_effect=save_with_newer_manual_before_rollback,
            ):
                result = await app._refresh_schedule_impl()
            stored = ScheduleStore(tmpdir).load()

        self.assertEqual("霍格沃兹体验日", result.theme_day)
        self.assertEqual(
            newer_manual.schedule,
            stored["2026-08-11"]["schedule"],
        )
        self.assertEqual(
            [
                (random_candidate.schedule, random_candidate.date),
                (newer_manual.schedule, newer_manual.date),
            ],
            [call.args for call in app._schedule_dynamic_photos.await_args_list],
        )

    def test_replace_guard_sees_legacy_same_date_schedule_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manual = self._entry(
                "2026-08-12",
                "进入魔法学院参加变形课",
                theme="霍格沃兹体验日",
                mode="custom",
            )
            candidate = self._entry(
                "2026-08-12",
                "体验水上运动",
                theme="海岛冒险日",
            )
            ScheduleStore(tmpdir).save({"legacy-plan": manual.to_dict()})

            saved = main_module.save_schedule_entry(
                tmpdir,
                candidate,
                replace_guard=lambda current: (
                    str((current or {}).get("theme_day_mode") or "") != "custom"
                ),
            )
            stored = ScheduleStore(tmpdir).load()

        self.assertFalse(saved)
        self.assertNotIn("2026-08-12", stored)
        self.assertEqual(
            "霍格沃兹体验日",
            stored["legacy-plan"]["theme_day"],
        )

    def test_main_target_date_requires_canonical_iso_format(self):
        app = self._app()

        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            app._resolve_schedule_refresh_target(
                "tomorrow",
                "2026-8-12",
            )

    async def test_today_and_tomorrow_refreshes_do_not_share_one_task(self):
        app = self._app()
        today_started = asyncio.Event()
        tomorrow_started = asyncio.Event()
        release = asyncio.Event()
        today_entry = self._entry(
            "2026-08-11",
            "参观陶艺工作室",
            theme="手作体验日",
        )
        tomorrow_entry = self._entry(
            "2026-08-12",
            "沿海岸骑行",
            theme="海岸骑行日",
        )
        calls = {"today": 0, "tomorrow": 0}

        async def refresh_today(*, preserve_theme_day=False):
            self.assertFalse(preserve_theme_day)
            calls["today"] += 1
            today_started.set()
            await release.wait()
            return today_entry

        async def refresh_tomorrow(schedule_date):
            self.assertEqual("2026-08-12", schedule_date)
            calls["tomorrow"] += 1
            tomorrow_started.set()
            await release.wait()
            return tomorrow_entry

        app._refresh_schedule_impl = refresh_today
        app._refresh_tomorrow_schedule_impl = refresh_tomorrow
        today_task = asyncio.create_task(app.refresh_schedule(target="today"))
        tomorrow_task = asyncio.create_task(
            app.refresh_schedule(
                target="tomorrow",
                target_date="2026-08-12",
            )
        )
        await asyncio.gather(today_started.wait(), tomorrow_started.wait())
        release.set()

        today_result, tomorrow_result = await asyncio.gather(
            today_task,
            tomorrow_task,
        )
        await asyncio.sleep(0)

        self.assertIs(today_entry, today_result)
        self.assertIs(tomorrow_entry, tomorrow_result)
        self.assertEqual({"today": 1, "tomorrow": 1}, calls)
        self.assertIsNone(app._schedule_refresh_task)
        self.assertIsNone(app._tomorrow_schedule_refresh_task)

    async def test_tomorrow_singleflight_is_scoped_to_absolute_date(self):
        app = self._app()
        current_day = {"value": date(2026, 8, 11)}
        app._today = lambda: current_day["value"]
        started = {
            "2026-08-12": asyncio.Event(),
            "2026-08-13": asyncio.Event(),
        }
        release = asyncio.Event()
        calls = []

        async def refresh_tomorrow(schedule_date):
            calls.append(schedule_date)
            started[schedule_date].set()
            await release.wait()
            return self._entry(
                schedule_date,
                "沿海岸骑行",
                theme="海岸骑行日",
            )

        app._refresh_tomorrow_schedule_impl = refresh_tomorrow
        first = asyncio.create_task(
            app.refresh_schedule(
                target="tomorrow",
                target_date="2026-08-12",
            )
        )
        await started["2026-08-12"].wait()
        current_day["value"] = date(2026, 8, 12)
        second = asyncio.create_task(
            app.refresh_schedule(
                target="tomorrow",
                target_date="2026-08-13",
            )
        )
        await started["2026-08-13"].wait()
        release.set()

        first_result, second_result = await asyncio.gather(first, second)
        await asyncio.sleep(0)

        self.assertEqual(["2026-08-12", "2026-08-13"], calls)
        self.assertEqual("2026-08-12", first_result.date)
        self.assertEqual("2026-08-13", second_result.date)
        self.assertIsNone(app._tomorrow_schedule_refresh_task)
        self.assertEqual("", app._tomorrow_schedule_refresh_task_date)

    async def test_today_singleflight_is_scoped_to_absolute_date_and_preserve_state(self):
        app = self._app()
        current_day = {"value": date(2026, 8, 11)}
        app._today = lambda: current_day["value"]
        started = {
            "2026-08-11": asyncio.Event(),
            "2026-08-12": asyncio.Event(),
        }
        release = asyncio.Event()
        calls = []
        refresh_states = {}

        async def refresh_today(
            schedule_date,
            *,
            preserve_theme_day=False,
            refresh_state=None,
        ):
            calls.append(schedule_date)
            refresh_states[schedule_date] = refresh_state
            started[schedule_date].set()
            await release.wait()
            return self._entry(
                schedule_date,
                "沿海岸骑行",
                theme="海岸骑行日",
            )

        app._refresh_schedule_impl = refresh_today
        first = asyncio.create_task(
            app.refresh_schedule(
                target="today",
                target_date="2026-08-11",
            )
        )
        await started["2026-08-11"].wait()
        current_day["value"] = date(2026, 8, 12)
        second = asyncio.create_task(
            app.refresh_schedule(
                preserve_theme_day=True,
                target="today",
                target_date="2026-08-12",
            )
        )
        await started["2026-08-12"].wait()

        self.assertIsNot(
            refresh_states["2026-08-11"],
            refresh_states["2026-08-12"],
        )
        self.assertFalse(
            refresh_states["2026-08-11"]["preserve_theme_day"]
        )
        self.assertTrue(
            refresh_states["2026-08-12"]["preserve_theme_day"]
        )
        release.set()

        first_result, second_result = await asyncio.gather(first, second)
        await asyncio.sleep(0)

        self.assertEqual(["2026-08-11", "2026-08-12"], calls)
        self.assertEqual("2026-08-11", first_result.date)
        self.assertEqual("2026-08-12", second_result.date)
        self.assertIsNone(app._schedule_refresh_task)
        self.assertEqual("", app._schedule_refresh_task_date)
        self.assertIsNone(app._schedule_refresh_state)

    async def test_web_tomorrow_singleflight_is_scoped_to_absolute_date(self):
        server = GalleryServer.__new__(GalleryServer)
        server._schedule_refresh_task = None
        server._schedule_refresh_task_date = ""
        server._tomorrow_schedule_refresh_task = None
        server._tomorrow_schedule_refresh_task_date = ""
        started = {
            "2026-08-12": asyncio.Event(),
            "2026-08-13": asyncio.Event(),
        }
        release = asyncio.Event()
        calls = []

        async def refresh_schedule(*, target, target_date):
            self.assertEqual("tomorrow", target)
            calls.append(target_date)
            started[target_date].set()
            await release.wait()
            return self._entry(
                target_date,
                "沿海岸骑行",
                theme="海岸骑行日",
            )

        server.on_refresh_schedule = refresh_schedule
        first = asyncio.create_task(
            server._refresh_schedule_singleflight(
                target="tomorrow",
                target_date="2026-08-12",
            )
        )
        await started["2026-08-12"].wait()
        second = asyncio.create_task(
            server._refresh_schedule_singleflight(
                target="tomorrow",
                target_date="2026-08-13",
            )
        )
        await started["2026-08-13"].wait()
        release.set()

        first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(["2026-08-12", "2026-08-13"], calls)
        self.assertEqual("2026-08-12", first_result.date)
        self.assertEqual("2026-08-13", second_result.date)
        self.assertIsNone(server._tomorrow_schedule_refresh_task)
        self.assertEqual("", server._tomorrow_schedule_refresh_task_date)

    async def test_web_today_singleflight_is_scoped_to_absolute_date(self):
        server = GalleryServer.__new__(GalleryServer)
        server._schedule_refresh_task = None
        server._schedule_refresh_task_date = ""
        server._tomorrow_schedule_refresh_task = None
        server._tomorrow_schedule_refresh_task_date = ""
        started = {
            "2026-08-11": asyncio.Event(),
            "2026-08-12": asyncio.Event(),
        }
        release = asyncio.Event()
        calls = []

        async def refresh_schedule(*, target, target_date):
            self.assertEqual("today", target)
            calls.append(target_date)
            started[target_date].set()
            await release.wait()
            return self._entry(
                target_date,
                "沿海岸骑行",
                theme="海岸骑行日",
            )

        server.on_refresh_schedule = refresh_schedule
        first = asyncio.create_task(
            server._refresh_schedule_singleflight(
                target="today",
                target_date="2026-08-11",
            )
        )
        await started["2026-08-11"].wait()
        second = asyncio.create_task(
            server._refresh_schedule_singleflight(
                target="today",
                target_date="2026-08-12",
            )
        )
        await started["2026-08-12"].wait()
        release.set()

        first_result, second_result = await asyncio.gather(first, second)

        self.assertEqual(["2026-08-11", "2026-08-12"], calls)
        self.assertEqual("2026-08-11", first_result.date)
        self.assertEqual("2026-08-12", second_result.date)
        self.assertIsNone(server._schedule_refresh_task)
        self.assertEqual("", server._schedule_refresh_task_date)

    async def test_today_impl_keeps_locked_date_when_clock_rolls_over(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_today = self._entry(
                "2026-08-11",
                "在旧书店淘书",
                theme="旧城寻宝日",
            )
            new_today = self._entry(
                "2026-08-12",
                "进入魔法学院参加变形课",
                theme="霍格沃兹体验日",
                mode="custom",
            )
            replacement = self._entry(
                "2026-08-11",
                "体验水上运动",
                theme="海岛冒险日",
            )
            ScheduleStore(tmpdir).save({
                old_today.date: old_today.to_dict(),
                new_today.date: new_today.to_dict(),
            })
            app = self._app(tmpdir)
            current_day = {"value": date(2026, 8, 11)}
            app._today = lambda: current_day["value"]
            app._schedule_missing_required_periods = Mock(return_value=[])

            async def generate_theme_day(**_kwargs):
                current_day["value"] = date(2026, 8, 12)
                return replacement

            app.generate_theme_day = AsyncMock(side_effect=generate_theme_day)
            result = await app._refresh_schedule_impl("2026-08-11")
            stored = ScheduleStore(tmpdir).load()

        self.assertIs(replacement, result)
        self.assertEqual(
            replacement.schedule,
            stored["2026-08-11"]["schedule"],
        )
        self.assertEqual(
            new_today.schedule,
            stored["2026-08-12"]["schedule"],
        )
        app.generate_theme_day.assert_awaited_once_with(
            target="today",
            target_date="2026-08-11",
            mode="random",
            persist=False,
            schedule_photos=False,
        )
        app._schedule_dynamic_photos.assert_awaited_once_with(
            replacement.schedule,
            replacement.date,
        )

    async def test_web_singleflight_cleans_detached_failed_task(self):
        server = GalleryServer.__new__(GalleryServer)
        server._schedule_refresh_task = None
        server._schedule_refresh_task_date = ""
        server._tomorrow_schedule_refresh_task = None
        server._tomorrow_schedule_refresh_task_date = ""
        loop = asyncio.get_running_loop()
        previous_exception_handler = loop.get_exception_handler()
        loop_errors = []
        started = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def refresh_schedule(*, target, target_date):
            self.assertEqual("tomorrow", target)
            self.assertEqual("2026-08-12", target_date)
            started.set()
            try:
                await release.wait()
                raise RuntimeError("detached refresh failed")
            finally:
                finished.set()

        server.on_refresh_schedule = refresh_schedule
        loop.set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )
        try:
            waiter = asyncio.create_task(
                server._refresh_schedule_singleflight(
                    target="tomorrow",
                    target_date="2026-08-12",
                )
            )
            await started.wait()
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            release.set()
            await finished.wait()
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_exception_handler)

        self.assertIsNone(server._tomorrow_schedule_refresh_task)
        self.assertEqual("", server._tomorrow_schedule_refresh_task_date)
        self.assertEqual([], loop_errors)

    async def test_app_singleflight_cleans_detached_failed_task_without_shield_log(self):
        app = self._app()
        loop = asyncio.get_running_loop()
        previous_exception_handler = loop.get_exception_handler()
        loop_errors = []
        started = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def refresh_today(*, preserve_theme_day=False):
            self.assertFalse(preserve_theme_day)
            started.set()
            try:
                await release.wait()
                raise RuntimeError("detached app refresh failed")
            finally:
                finished.set()

        app._refresh_schedule_impl = refresh_today
        loop.set_exception_handler(
            lambda _loop, context: loop_errors.append(context)
        )
        try:
            waiter = asyncio.create_task(
                app.refresh_schedule(
                    target="today",
                    target_date="2026-08-11",
                )
            )
            await started.wait()
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            release.set()
            await finished.wait()
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_exception_handler)

        self.assertIsNone(app._schedule_refresh_task)
        self.assertFalse(app._schedule_refresh_preserve_theme_day)
        self.assertEqual([], loop_errors)


if __name__ == "__main__":
    unittest.main()
