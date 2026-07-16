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
    def __init__(self, scheduler, job_id, run_at, args):
        self.scheduler = scheduler
        self.id = job_id
        self.next_run_time = run_at
        self.args = args

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

    def add_job(self, _func, _trigger, *, run_date, args, id, **_kwargs):
        self.jobs.append(FakeJob(self, id, run_date, args))


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
                {"photo_dynamic_20_22", "photo_dynamic_22_36"},
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


if __name__ == "__main__":
    unittest.main()
