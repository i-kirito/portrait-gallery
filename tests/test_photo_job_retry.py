import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
_TEST_LOG_DIR = tempfile.TemporaryDirectory(prefix="portrait-gallery-photo-retry-")
os.environ["HERMES_GALLERY_LOG"] = str(Path(_TEST_LOG_DIR.name) / "gallery.log")

import main as main_module  # noqa: E402
from main import PortraitGalleryApp  # noqa: E402


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        current = cls(2026, 7, 14, 16, 20)
        return current if tz is None else current.replace(tzinfo=tz)


class RequiredPeriods:
    @staticmethod
    def _required_periods():
        return [
            {"label": "早", "start": 6 * 60, "end": 11 * 60 + 59},
            {"label": "中", "start": 12 * 60, "end": 17 * 60 + 59},
            {"label": "晚", "start": 18 * 60, "end": 23 * 60 + 59},
        ]


class FakeJob:
    def __init__(self, scheduler, job_id, run_at, args, kwargs=None):
        self.scheduler = scheduler
        self.id = job_id
        self.next_run_time = run_at
        self.args = args
        self.kwargs = kwargs or {}

    def remove(self):
        self.scheduler.jobs = [job for job in self.scheduler.jobs if job is not self]


class FakeApsScheduler:
    timezone = None

    def __init__(self):
        self.jobs = []

    def get_jobs(self):
        return list(self.jobs)

    def get_job(self, job_id):
        return next((job for job in self.jobs if job.id == job_id), None)

    def add_job(self, _func, _trigger, *, run_date, args, id, kwargs=None, **_extra):
        self.jobs.append(FakeJob(self, id, run_date, args, kwargs))


class PhotoJobRetryTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_app(data_dir: str) -> PortraitGalleryApp:
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app.data_dir = data_dir
        app.config = {}
        app.web_server = SimpleNamespace()
        app.aps = FakeApsScheduler()
        app.scheduler_gen = RequiredPeriods()
        app._photo_job_schedule_meta = {}
        app._failed_photo_jobs = {}
        app._photo_jobs_inflight = set()
        app._photo_jobs_inflight_started = {}
        app._inflight_lock = asyncio.Lock()
        app._backfill_semaphore = asyncio.Semaphore(1)
        app._get_photo_job_limit = lambda: 4
        app._today_photo_plan_times = lambda *_args, **_kwargs: set()
        app._today_photo_plan_periods = lambda *_args, **_kwargs: set()
        app._check_photo_exists_for_slot = lambda *_args, **_kwargs: False
        app._today_schedule_activity_map = lambda: {
            "11:42": "在运动场慢跑训练",
            "15:12": "在树荫下整理运动数据记录",
            "20:22": "听音乐整理房间",
            "22:36": "泡热水澡放松",
        }

        async def photo_job(*_args, **_kwargs):
            return True

        app.photo_job = photo_job
        return app

    async def test_expired_slots_are_listed_and_can_be_retried_manually(self):
        schedule = (
            "11:42 在运动场慢跑训练\n"
            "15:12 在树荫下整理运动数据记录\n"
            "20:22 听音乐整理房间\n"
            "22:36 泡热水澡放松"
        )
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            main_module,
            "datetime",
            FixedDateTime,
        ):
            app = self.make_app(tmpdir)

            await app._schedule_dynamic_photos(schedule)

            self.assertEqual(
                {"2026-07-14 11:42", "2026-07-14 15:12"},
                set(app._failed_photo_jobs),
            )
            self.assertEqual(
                {
                    "photo_dynamic_20260714_20_22",
                    "photo_dynamic_20260714_22_36",
                },
                {job.id for job in app.aps.jobs},
            )
            listed = {job["time"]: job for job in app.list_photo_jobs()}
            self.assertEqual("missed", listed["15:12"]["status"])
            self.assertIn("可手动重试", listed["15:12"]["error_summary"])

            def close_task(coro):
                coro.close()
                return None

            with patch.object(main_module.asyncio, "create_task", side_effect=close_task):
                result = await app.retry_photo_job("15:12")

            self.assertEqual("queued", result["status"])
            self.assertNotIn("2026-07-14 15:12", app._failed_photo_jobs)
            self.assertIn("2026-07-14 15:12", app._photo_jobs_inflight)

    async def test_overnight_tail_slot_job_is_pinned_to_its_schedule_date(self):
        """A 00:xx tail slot must carry the schedule day it belongs to, not the
        calendar day it physically runs on, so `photo_job` can resolve daily
        entry/quota/failure state against the correct day."""
        schedule = "20:22 听音乐整理房间\n00:20 熄灯前听会儿歌"
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            main_module,
            "datetime",
            FixedDateTime,
        ):
            app = self.make_app(tmpdir)

            await app._schedule_dynamic_photos(schedule, "2026-07-14")

            jobs_by_id = {job.id: job for job in app.aps.jobs}
            self.assertEqual(
                {
                    "photo_dynamic_20260714_20_22",
                    "photo_dynamic_20260714_0_20",
                },
                set(jobs_by_id),
            )

            tail_job_id = "photo_dynamic_20260714_0_20"
            tail_job = jobs_by_id[tail_job_id]
            self.assertEqual(
                {
                    "schedule_date": "2026-07-14",
                    "scheduled_job_id": tail_job_id,
                },
                tail_job.kwargs,
            )
            # Physically scheduled for the next calendar day...
            self.assertEqual(datetime(2026, 7, 15, 0, 20), tail_job.next_run_time)
            # ...but still tagged with the schedule day it belongs to.
            self.assertEqual(
                "2026-07-14",
                app._photo_job_schedule_meta[tail_job_id]["schedule_date"],
            )

    async def test_same_time_jobs_from_adjacent_schedule_dates_can_coexist(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            main_module,
            "datetime",
            FixedDateTime,
        ):
            app = self.make_app(tmpdir)

            await app._schedule_dynamic_photos("00:20 今日尾部活动", "2026-07-14")
            await app._schedule_dynamic_photos("00:20 次日尾部活动", "2026-07-15")

            self.assertEqual(
                {
                    "photo_dynamic_20260714_0_20",
                    "photo_dynamic_20260715_0_20",
                },
                {job.id for job in app.aps.jobs},
            )
            self.assertEqual(
                {"2026-07-14", "2026-07-15"},
                {
                    meta["schedule_date"]
                    for meta in app._photo_job_schedule_meta.values()
                },
            )

    def test_scheduled_quota_is_owned_by_schedule_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = self.make_app(tmpdir)
            yesterday_id = "photo_dynamic_20260714_0_30"
            today_id = "photo_dynamic_20260715_0_30"
            app.aps.jobs = [
                FakeJob(app.aps, yesterday_id, datetime(2026, 7, 15, 0, 45, 5), []),
                FakeJob(app.aps, today_id, datetime(2026, 7, 16, 0, 30), []),
            ]
            app._photo_job_schedule_meta = {
                yesterday_id: {
                    "schedule_date": "2026-07-14",
                    "time": "00:30",
                },
                today_id: {
                    "schedule_date": "2026-07-15",
                    "time": "00:30",
                },
            }

            self.assertEqual({"00:30"}, app._today_scheduled_photo_times("2026-07-14"))
            self.assertEqual({"00:30"}, app._today_scheduled_photo_times("2026-07-15"))
            self.assertEqual(1, app._today_scheduled_photo_count("2026-07-14"))
            self.assertEqual(1, app._today_scheduled_photo_count("2026-07-15"))

    def test_slot_key_for_schedule_time_uses_explicit_schedule_date(self):
        with patch.object(main_module, "datetime", FixedDateTime):
            app = PortraitGalleryApp.__new__(PortraitGalleryApp)
            app.config = {}

            pinned = app._slot_key_for_schedule_time("00:20 熄灯前听会儿歌", "2026-07-14")
            self.assertEqual(("2026-07-14 00:20", "00:20", "熄灯前听会儿歌"), pinned)

            defaulted = app._slot_key_for_schedule_time("00:20 熄灯前听会儿歌")
            self.assertEqual(("2026-07-14 00:20", "00:20", "熄灯前听会儿歌"), defaulted)


if __name__ == "__main__":
    unittest.main()
