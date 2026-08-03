import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
_TEST_LOG_DIR = tempfile.TemporaryDirectory(prefix="portrait-gallery-quota-tests-")
os.environ["HERMES_GALLERY_LOG"] = str(Path(_TEST_LOG_DIR.name) / "gallery.log")

from main import PortraitGalleryApp  # noqa: E402
from store import ScheduleStore  # noqa: E402
from web_server import GalleryServer  # noqa: E402


class ScheduleQuotaSourcesTest(unittest.TestCase):
    def test_web_images_do_not_consume_scheduled_photo_quota(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            image_dir = data_dir / "images"
            image_dir.mkdir(parents=True)
            today = datetime(2026, 8, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            (image_dir / "scheduled.png").write_bytes(b"scheduled")
            (image_dir / "on-demand.png").write_bytes(b"on-demand")
            ScheduleStore(str(data_dir)).save({
                "scheduled.png": {
                    "date": today.date().isoformat(),
                    "status": "ok",
                    "source": "cron",
                    "schedule_time": "08:12 早餐",
                    "image_filename": "scheduled.png",
                },
                "on-demand.png": {
                    "date": today.date().isoformat(),
                    "status": "ok",
                    "source": "web",
                    "schedule_time": "12:00 午间散步",
                    "image_filename": "on-demand.png",
                },
            })

            app = PortraitGalleryApp.__new__(PortraitGalleryApp)
            app.data_dir = str(data_dir)
            app.web_server = SimpleNamespace(image_dir=str(image_dir))
            app._now = lambda: today
            app._get_photo_job_limit = lambda: 6
            app._today_failed_photo_times = lambda *_args: set()
            app._today_inflight_photo_times = lambda *_args: set()
            app._today_scheduled_photo_times = lambda *_args: set()

            self.assertEqual({"08:12"}, app._today_completed_photo_times())
            self.assertTrue(app._check_photo_exists_for_slot(today.date().isoformat(), "08:12"))
            self.assertFalse(app._check_photo_exists_for_slot(today.date().isoformat(), "12:00"))
            self.assertEqual((6, 1, 0, 0, 0, 1, 5), app._photo_quota_snapshot())

            server = GalleryServer.__new__(GalleryServer)
            server.data_dir = str(data_dir)
            server._today = lambda: today.date()
            server._image_exists = lambda filename: (image_dir / filename).is_file()
            self.assertEqual(1, server._today_completed_photo_count())


if __name__ == "__main__":
    unittest.main()
