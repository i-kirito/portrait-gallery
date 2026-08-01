import os
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
_TEST_LOG_DIR = tempfile.TemporaryDirectory(prefix="portrait-gallery-reroll-tests-")
os.environ["HERMES_GALLERY_LOG"] = str(Path(_TEST_LOG_DIR.name) / "gallery.log")

from main import PortraitGalleryApp  # noqa: E402


class RerollScheduleContextTest(unittest.TestCase):
    def setUp(self):
        self.app = PortraitGalleryApp.__new__(PortraitGalleryApp)

    def test_historical_reroll_uses_original_date_and_overwrites_generated_fields(self):
        historical = {
            "outfit_style": "清新风",
            "outfit": "白色衬衫搭配藏青色 JK 百褶裙",
            "base_style": "",
            "reference_query": "fresh JK uniform",
            "outfit_keywords": "white blouse, navy pleated skirt",
            "scene_keywords": "creative district bistro",
        }
        original = {"date": "2026-07-13", "outfit_style": "旧记录"}
        context = self.app._reroll_schedule_context(
            {"2026-07-13": historical},
            original,
            "2026-07-13",
            False,
        )
        generated = {
            "outfit_style": "甜美风",
            "outfit": "草莓红百褶裙",
            "base_style": "sweet",
            "reference_query": "sweet red outfit",
            "outfit_keywords": "red skirt",
            "scene_keywords": "stationery shop",
        }

        self.app._apply_scheduled_reroll_context(generated, context)

        self.assertEqual(historical, generated)

    def test_today_reroll_keeps_using_live_today_schedule(self):
        today = {"outfit_style": "今日风格", "outfit": "今日穿搭"}
        self.app._today_schedule_entry = lambda: today

        context = self.app._reroll_schedule_context(
            {"2026-07-15": {"outfit_style": "磁盘旧值"}},
            {"outfit_style": "原图旧值"},
            "2026-07-15",
            True,
        )

        self.assertIs(today, context)

    def test_historical_reroll_falls_back_to_original_when_daily_entry_is_missing(self):
        original = {"outfit_style": "原图风格", "outfit": "原图穿搭"}

        context = self.app._reroll_schedule_context({}, original, "2026-07-01", False)

        self.assertIs(original, context)


if __name__ == "__main__":
    unittest.main()
