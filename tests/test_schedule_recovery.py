import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from data import DailyEntry  # noqa: E402
import main as main_module  # noqa: E402
from main import PortraitGalleryApp  # noqa: E402
from web_server import GalleryServer  # noqa: E402


class DummyRequest:
    can_read_body = False


class FixedOvernightDateTime(datetime):
    """Wall clock frozen at 00:45, inside the 00:00-01:59 restart window."""

    @classmethod
    def now(cls, tz=None):
        current = cls(2026, 7, 15, 0, 45)
        return current if tz is None else current.replace(tzinfo=tz)


class FixedDaytimeDateTime(datetime):
    """Wall clock frozen at 02:30, outside the 00:00-01:59 restart window."""

    @classmethod
    def now(cls, tz=None):
        current = cls(2026, 7, 15, 2, 30)
        return current if tz is None else current.replace(tzinfo=tz)


class RecoveryFakeJob:
    def __init__(self, scheduler, job_id, run_at, args, kwargs=None):
        self.scheduler = scheduler
        self.id = job_id
        self.next_run_time = run_at
        self.args = args
        self.kwargs = kwargs or {}

    def remove(self):
        self.scheduler.jobs = [job for job in self.scheduler.jobs if job is not self]


class RecoveryFakeScheduler:
    timezone = None

    def __init__(self):
        self.jobs = []

    def get_jobs(self):
        return list(self.jobs)

    def get_job(self, job_id):
        return next((job for job in self.jobs if job.id == job_id), None)

    def add_job(self, _func, _trigger, *, run_date, args, id, kwargs=None, **_extra):
        self.jobs.append(RecoveryFakeJob(self, id, run_date, args, kwargs))


class RecoveryRequiredPeriods:
    @staticmethod
    def _required_periods():
        return [
            {"label": "早", "start": 6 * 60, "end": 11 * 60 + 59},
            {"label": "中午", "start": 12 * 60, "end": 13 * 60 + 59},
            {"label": "午", "start": 14 * 60, "end": 18 * 60 + 59},
            {"label": "晚", "start": 19 * 60, "end": 1 * 60 + 59},
        ]


class ScheduleRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_app_schedule_refresh_is_singleflight_for_concurrent_callers(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app._schedule_refresh_task = None
        app._schedule_refresh_preserve_theme_day = False
        started = asyncio.Event()
        release = asyncio.Event()
        entry = DailyEntry(date="2026-07-15", schedule="08:00 买早餐", status="ok")
        calls = 0

        async def refresh_impl():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return entry

        app._refresh_schedule_impl = refresh_impl
        first = asyncio.create_task(app.refresh_schedule())
        await started.wait()
        second = asyncio.create_task(app.refresh_schedule())
        await asyncio.sleep(0)
        release.set()

        first_result, second_result = await asyncio.gather(first, second)

        self.assertIs(entry, first_result)
        self.assertIs(entry, second_result)
        self.assertEqual(1, calls)
        self.assertIsNone(app._schedule_refresh_task)
        self.assertFalse(app._schedule_refresh_preserve_theme_day)

    async def test_preserving_caller_promotes_inflight_regular_refresh(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app._schedule_refresh_task = None
        app._schedule_refresh_preserve_theme_day = False
        started = asyncio.Event()
        release = asyncio.Event()
        planned = DailyEntry(
            date="2026-07-15",
            schedule="08:00 进入魔法学院大礼堂",
            status="ok",
            source="theme_day",
            theme_day="霍格沃兹体验日",
            theme_day_mode="custom",
        )
        random_candidate = DailyEntry(
            date="2026-07-15",
            schedule="08:00 去旧城寻找壁画",
            status="ok",
            source="theme_day",
            theme_day="旧城寻宝日",
            theme_day_mode="random",
        )

        async def generate_theme_day(**_kwargs):
            started.set()
            await release.wait()
            return random_candidate

        app.generate_theme_day = AsyncMock(side_effect=generate_theme_day)
        app._today_schedule_entry = Mock(return_value=planned.to_dict())
        app._schedule_missing_required_periods = Mock(return_value=[])
        app._schedule_dynamic_photos = AsyncMock()
        app.data_dir = "unused"

        with patch.object(main_module, "save_schedule_entry") as save_entry:
            regular_refresh = asyncio.create_task(app.refresh_schedule())
            await started.wait()
            preserving_refresh = asyncio.create_task(
                app.refresh_schedule(preserve_theme_day=True)
            )
            await asyncio.sleep(0)
            release.set()
            regular_result, preserving_result = await asyncio.gather(
                regular_refresh,
                preserving_refresh,
            )

        self.assertEqual("霍格沃兹体验日", regular_result.theme_day)
        self.assertEqual("霍格沃兹体验日", preserving_result.theme_day)
        app.generate_theme_day.assert_awaited_once_with(
            target="today",
            target_date=app._today().isoformat(),
            mode="random",
            persist=False,
            schedule_photos=False,
        )
        save_entry.assert_not_called()
        app._schedule_dynamic_photos.assert_awaited_once_with(
            planned.schedule,
            planned.date,
        )
        self.assertIsNone(app._schedule_refresh_task)
        self.assertFalse(app._schedule_refresh_preserve_theme_day)

    async def test_inflight_refresh_does_not_overwrite_new_custom_theme(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app._schedule_refresh_preserve_theme_day = False
        started = asyncio.Event()
        release = asyncio.Event()
        stored_entry = {}
        planned = DailyEntry(
            date="2026-07-15",
            schedule="08:00 进入魔法学院大礼堂",
            status="ok",
            source="theme_day",
            theme_day="霍格沃兹体验日",
            theme_day_mode="custom",
        )
        random_candidate = DailyEntry(
            date="2026-07-15",
            schedule="08:00 去旧城寻找壁画",
            status="ok",
            source="theme_day",
            theme_day="旧城寻宝日",
            theme_day_mode="random",
        )

        async def generate_theme_day(**_kwargs):
            started.set()
            await release.wait()
            return random_candidate

        app.generate_theme_day = AsyncMock(side_effect=generate_theme_day)
        app._today_schedule_entry = Mock(
            side_effect=lambda _schedule_date="": dict(stored_entry)
        )
        app._schedule_missing_required_periods = Mock(return_value=[])
        app._schedule_dynamic_photos = AsyncMock()
        app.data_dir = "unused"

        with patch.object(main_module, "save_schedule_entry") as save_entry:
            refresh = asyncio.create_task(app._refresh_schedule_impl())
            await started.wait()
            stored_entry = planned.to_dict()
            release.set()
            result = await refresh

        self.assertEqual("霍格沃兹体验日", result.theme_day)
        save_entry.assert_not_called()
        app._schedule_dynamic_photos.assert_awaited_once_with(
            planned.schedule,
            planned.date,
        )

    async def test_late_preserve_request_rolls_back_committed_candidate(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app._schedule_refresh_task = None
        app._schedule_refresh_preserve_theme_day = False
        jobs_started = asyncio.Event()
        release_jobs = asyncio.Event()
        planned = DailyEntry(
            date="2026-07-15",
            schedule="08:00 进入魔法学院大礼堂",
            status="ok",
            source="theme_day",
            theme_day="霍格沃兹体验日",
            theme_day_mode="custom",
        )
        random_candidate = DailyEntry(
            date="2026-07-15",
            schedule="08:00 去旧城寻找壁画",
            status="ok",
            source="theme_day",
            theme_day="旧城寻宝日",
            theme_day_mode="random",
        )
        stored_entry = planned.to_dict()

        async def rebuild_jobs(schedule, _date):
            if schedule == random_candidate.schedule:
                jobs_started.set()
                await release_jobs.wait()

        def save_entry(_data_dir, entry, **_kwargs):
            nonlocal stored_entry
            stored_entry = entry.to_dict()
            return True

        app.generate_theme_day = AsyncMock(return_value=random_candidate)
        app._today_schedule_entry = Mock(
            side_effect=lambda _schedule_date="": dict(stored_entry)
        )
        app._schedule_missing_required_periods = Mock(return_value=[])
        app._schedule_dynamic_photos = AsyncMock(side_effect=rebuild_jobs)
        app.data_dir = "unused"

        with patch.object(
            main_module,
            "save_schedule_entry",
            side_effect=save_entry,
        ) as save_schedule:
            regular_refresh = asyncio.create_task(app.refresh_schedule())
            await jobs_started.wait()
            preserving_refresh = asyncio.create_task(
                app.refresh_schedule(preserve_theme_day=True)
            )
            await asyncio.sleep(0)
            release_jobs.set()
            regular_result, preserving_result = await asyncio.gather(
                regular_refresh,
                preserving_refresh,
            )

        self.assertEqual("霍格沃兹体验日", regular_result.theme_day)
        self.assertEqual("霍格沃兹体验日", preserving_result.theme_day)
        self.assertEqual(
            [random_candidate, planned],
            [call.args[1] for call in save_schedule.call_args_list],
        )
        self.assertEqual(planned.to_dict(), stored_entry)
        self.assertEqual(
            [
                (random_candidate.schedule, random_candidate.date),
                (planned.schedule, planned.date),
            ],
            [call.args for call in app._schedule_dynamic_photos.await_args_list],
        )

    async def test_cancelled_waiter_does_not_leave_refresh_state_behind(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app._schedule_refresh_task = None
        app._schedule_refresh_preserve_theme_day = False
        started = asyncio.Event()
        release = asyncio.Event()
        completed = asyncio.Event()
        entry = DailyEntry(date="2026-07-15", schedule="08:00 买早餐", status="ok")

        async def refresh_impl(*, preserve_theme_day=False):
            self.assertTrue(preserve_theme_day)
            started.set()
            try:
                await release.wait()
                return entry
            finally:
                completed.set()

        app._refresh_schedule_impl = refresh_impl
        waiter = asyncio.create_task(
            app.refresh_schedule(preserve_theme_day=True)
        )
        await started.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        release.set()
        await completed.wait()
        await asyncio.sleep(0)

        self.assertIsNone(app._schedule_refresh_task)
        self.assertFalse(app._schedule_refresh_preserve_theme_day)

    async def test_refresh_stops_before_photo_job_rebuild_when_save_fails(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        entry = DailyEntry(
            date="2026-07-15",
            schedule="08:00 买早餐",
            status="ok",
            source="theme_day",
            theme_day="城市漫游日",
            theme_day_mode="random",
        )
        app.generate_theme_day = AsyncMock(return_value=entry)
        app._today_schedule_entry = Mock(return_value={})
        app.data_dir = "unused"
        app._schedule_dynamic_photos = AsyncMock()

        with patch.object(
            main_module,
            "save_schedule_entry",
            side_effect=OSError("disk full"),
        ) as save_entry:
            with self.assertRaisesRegex(OSError, "disk full"):
                await app._refresh_schedule_impl()

        app.generate_theme_day.assert_awaited_once_with(
            target="today",
            mode="random",
            persist=False,
            schedule_photos=False,
        )
        self.assertEqual((app.data_dir, entry), save_entry.call_args.args)
        self.assertTrue(callable(save_entry.call_args.kwargs.get("replace_guard")))
        app._schedule_dynamic_photos.assert_not_awaited()

    async def test_startup_missing_schedule_always_starts_background_generation(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app._today_schedule_entry = Mock(return_value=None)
        app._recover_overnight_tail_jobs = Mock(return_value=0)
        app.daily_job = AsyncMock()

        await app._restore_daily_schedule_state()
        await asyncio.sleep(0)

        app.daily_job.assert_awaited_once()
        app._recover_overnight_tail_jobs.assert_called_once()

    async def test_generate_now_refreshes_missing_schedule_before_returning(self):
        server = GalleryServer.__new__(GalleryServer)
        refreshed = DailyEntry(
            date="2026-07-15",
            schedule="08:00 买早餐",
            status="ok",
        )
        server.on_refresh_schedule = AsyncMock(return_value=refreshed)
        server._schedule_refresh_task = None
        server._today_schedule_entry = Mock(side_effect=[{}, refreshed.to_dict()])
        server._schedule_items_for_inference = Mock(return_value=[])

        response = await server.handle_generate_now(DummyRequest())
        payload = json.loads(response.text)

        server.on_refresh_schedule.assert_awaited_once()
        self.assertEqual(400, response.status)
        self.assertEqual("schedule_time_not_found", payload.get("error"))


class OvernightTailRecoveryTest(unittest.IsolatedAsyncioTestCase):
    """`_recover_overnight_tail_jobs` covers a restart landing in 00:00-01:59:

    APScheduler's in-memory jobstore drops any pending job across a restart, so
    yesterday's overnight tail slots (00:00-01:59) would otherwise silently
    vanish instead of running or being recorded as retryable."""

    @staticmethod
    def make_app(data_dir: str) -> PortraitGalleryApp:
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app.data_dir = data_dir
        app.config = {}
        app._get_photo_job_limit = lambda: 4
        app._failed_photo_jobs = {}
        app._photo_jobs_inflight = set()
        app._photo_jobs_inflight_started = {}
        app._photo_job_schedule_meta = {}
        app.aps = Mock()
        app.aps.get_jobs = Mock(return_value=[])
        app.aps.get_job = Mock(return_value=None)
        return app

    @staticmethod
    def _seed_schedule(data_dir: str, entry: dict) -> None:
        from store import ScheduleStore

        ScheduleStore(data_dir).save({entry["date"]: entry})

    def test_recovers_pending_overnight_tail_slot_after_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            main_module, "datetime", FixedOvernightDateTime
        ):
            self._seed_schedule(tmpdir, {
                "date": "2026-07-14",
                "status": "ok",
                "schedule": "20:22 听音乐整理房间\n00:30 熄灯前听会儿歌",
            })
            app = self.make_app(tmpdir)

            recovered = app._recover_overnight_tail_jobs()

            self.assertEqual(1, recovered)
            app.aps.add_job.assert_called_once()
            call = app.aps.add_job.call_args
            job_id = "photo_dynamic_20260714_0_30"
            self.assertEqual(job_id, call.kwargs["id"])
            self.assertEqual(
                {
                    "schedule_date": "2026-07-14",
                    "scheduled_job_id": job_id,
                },
                call.kwargs["kwargs"],
            )
            self.assertEqual(["bedtime", "00:30 熄灯前听会儿歌"], call.kwargs["args"])
            # Original slot time (2026-07-15 00:30) already elapsed while the
            # service was down, so it must be bumped to fire right away rather
            # than rely on APScheduler misfire handling.
            self.assertEqual(datetime(2026, 7, 15, 0, 45, 5), call.kwargs["run_date"])
            self.assertEqual(
                "2026-07-14",
                app._photo_job_schedule_meta[job_id]["schedule_date"],
            )

    def test_recovery_does_not_exceed_daily_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            main_module, "datetime", FixedOvernightDateTime
        ):
            self._seed_schedule(tmpdir, {
                "date": "2026-07-14",
                "status": "ok",
                "schedule": "00:30 第一条尾部活动\n01:30 第二条尾部活动",
            })
            app = self.make_app(tmpdir)
            app._get_photo_job_limit = lambda: 1
            scheduled_jobs = []

            def add_job(_func, _trigger, **kwargs):
                job = Mock()
                job.id = kwargs["id"]
                job.next_run_time = kwargs["run_date"]
                job.args = kwargs["args"]
                scheduled_jobs.append(job)
                app._photo_job_schedule_meta[job.id] = {
                    "schedule_date": "2026-07-14",
                    "time": kwargs["args"][1][:5],
                }

            app.aps.add_job.side_effect = add_job
            app.aps.get_jobs.side_effect = lambda: list(scheduled_jobs)

            recovered = app._recover_overnight_tail_jobs()

            self.assertEqual(1, recovered)
            self.assertEqual(1, len(scheduled_jobs))

    async def test_today_rebuild_preserves_recovered_yesterday_job(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            main_module, "datetime", FixedOvernightDateTime
        ):
            self._seed_schedule(tmpdir, {
                "date": "2026-07-14",
                "status": "ok",
                "schedule": "00:30 昨日尾部活动",
            })
            app = self.make_app(tmpdir)
            app.aps = RecoveryFakeScheduler()
            app.scheduler_gen = RecoveryRequiredPeriods()
            app.web_server = SimpleNamespace()
            app._today_photo_plan_times = lambda *_args, **_kwargs: set()
            app._today_photo_plan_periods = lambda *_args, **_kwargs: set()

            self.assertEqual(1, app._recover_overnight_tail_jobs())
            await app._schedule_dynamic_photos(
                "20:30 今日晚间活动\n00:30 今日尾部活动",
                "2026-07-15",
            )

            self.assertEqual(
                {
                    "photo_dynamic_20260714_0_30",
                    "photo_dynamic_20260715_20_30",
                    "photo_dynamic_20260715_0_30",
                },
                {job.id for job in app.aps.jobs},
            )
            recovered_item = next(
                item
                for item in app.list_photo_jobs()
                if item["id"] == "photo_dynamic_20260714_0_30"
            )
            self.assertEqual("00:30", recovered_item["time"])
            self.assertEqual("昨日尾部活动", recovered_item["activity"])
            self.assertEqual("2026-07-14", recovered_item["schedule_date"])

    def test_no_recovery_outside_the_overnight_restart_window(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            main_module, "datetime", FixedDaytimeDateTime
        ):
            self._seed_schedule(tmpdir, {
                "date": "2026-07-14",
                "status": "ok",
                "schedule": "00:30 熄灯前听会儿歌",
            })
            app = self.make_app(tmpdir)

            recovered = app._recover_overnight_tail_jobs()

            self.assertEqual(0, recovered)
            app.aps.add_job.assert_not_called()

    def test_recovery_skips_a_slot_that_already_has_a_photo(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            main_module, "datetime", FixedOvernightDateTime
        ):
            from store import ScheduleStore

            ScheduleStore(tmpdir).save({
                "2026-07-14": {
                    "date": "2026-07-14",
                    "status": "ok",
                    "schedule": "00:30 熄灯前听会儿歌",
                },
                "already_done.png": {
                    "date": "2026-07-14",
                    "status": "ok",
                    "source": "cron",
                    "schedule_time": "00:30 熄灯前听会儿歌",
                    "image_filename": "already_done.png",
                },
            })
            app = self.make_app(tmpdir)

            recovered = app._recover_overnight_tail_jobs()

            self.assertEqual(0, recovered)
            app.aps.add_job.assert_not_called()

    async def test_restore_daily_schedule_state_recovers_before_handling_today(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app._recover_overnight_tail_jobs = Mock(return_value=1)
        app._today_schedule_entry = Mock(return_value={
            "date": "2026-07-15",
            "schedule": "08:00 买早餐",
        })
        app._schedule_missing_required_periods = Mock(return_value=[])
        app._schedule_next_daily_job = Mock()
        app._schedule_dynamic_photos = AsyncMock()

        await app._restore_daily_schedule_state()

        app._recover_overnight_tail_jobs.assert_called_once()
        app._schedule_dynamic_photos.assert_awaited_once_with(
            "08:00 买早餐", "2026-07-15"
        )


if __name__ == "__main__":
    unittest.main()
