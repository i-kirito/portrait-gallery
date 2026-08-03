import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from main import PortraitGalleryApp  # noqa: E402
from store import ScheduleStore  # noqa: E402
from web_server import GalleryServer  # noqa: E402


class PhotoStyleMatchesPlanTest(unittest.TestCase):
    def test_matching_styles_merge(self):
        self.assertTrue(GalleryServer._photo_style_matches_plan("元气风", "元气风"))

    def test_stale_plan_style_is_filtered(self):
        self.assertFalse(GalleryServer._photo_style_matches_plan("元气风", "优雅风"))
        self.assertFalse(GalleryServer._photo_style_matches_plan("元气风", "温柔风"))

    def test_missing_style_is_lenient(self):
        self.assertTrue(GalleryServer._photo_style_matches_plan("元气风", ""))
        self.assertTrue(GalleryServer._photo_style_matches_plan("", "优雅风"))
        self.assertTrue(GalleryServer._photo_style_matches_plan("", ""))


class SavedTodayScheduleReferenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        self.app.data_dir = self.tmp.name
        self.app._today = Mock(return_value=date(2026, 8, 2))
        self.ref_a = Path(self.tmp.name) / "ref_a.jpg"
        self.ref_a.write_bytes(b"jpeg-a")
        self.ref_b = Path(self.tmp.name) / "ref_b.jpg"
        self.ref_b.write_bytes(b"jpeg-b")

    def tearDown(self):
        self.tmp.cleanup()

    def _save(self, plan_style, photos):
        data = {}
        if plan_style:
            data["2026-08-02"] = {
                "date": "2026-08-02",
                "outfit_style": plan_style,
                "status": "ok",
                "source": "theme_day",
                "schedule": "08:27 在露天广场观看晨间喷泉表演",
            }
        for i, photo in enumerate(photos):
            data[f"photo_{i}.png"] = {
                "date": "2026-08-02",
                "image_filename": f"photo_{i}.png",
                "source": "cron",
                "outfit_style": photo.get("outfit_style", ""),
                "schedule_time": photo["schedule_time"],
                "status": "ok",
                "selected_reference": {"path": photo["ref_path"]},
            }
        ScheduleStore(self.tmp.name).save(data)

    def test_old_plan_references_are_ignored(self):
        self._save("元气风", [
            {"outfit_style": "优雅风", "schedule_time": "19:45 壁炉旁", "ref_path": str(self.ref_a)},
            {"outfit_style": "温柔风", "schedule_time": "12:45 厨房", "ref_path": str(self.ref_b)},
        ])
        self.assertEqual({}, self.app._saved_today_schedule_reference())

    def test_current_plan_reference_is_used(self):
        self._save("元气风", [
            {"outfit_style": "优雅风", "schedule_time": "19:45 壁炉旁", "ref_path": str(self.ref_a)},
            {"outfit_style": "元气风", "schedule_time": "08:27 喷泉旁", "ref_path": str(self.ref_b)},
        ])
        result = self.app._saved_today_schedule_reference()
        self.assertEqual(str(self.ref_b), result.get("path"))
        self.assertEqual("saved_schedule", result.get("selection_mode"))

    def test_unknown_photo_style_is_lenient(self):
        self._save("元气风", [
            {"outfit_style": "", "schedule_time": "10:48 涂鸦墙", "ref_path": str(self.ref_a)},
        ])
        result = self.app._saved_today_schedule_reference()
        self.assertEqual(str(self.ref_a), result.get("path"))


class OutfitDirectiveTest(unittest.TestCase):
    def setUp(self):
        self.app = PortraitGalleryApp.__new__(PortraitGalleryApp)

    def test_extract_outfit_clothing_text(self):
        raw = "风格：元气风\n发型：灰粉色长发扎成高位双丸子头\n穿搭：鹅黄色短袖T恤配砖红色阔腿裤\n动作：奔跑"
        text = self.app._extract_outfit_clothing_text(raw)
        self.assertIn("双丸子头", text)
        self.assertIn("鹅黄色短袖T恤", text)
        self.assertNotIn("奔跑", text)

    def test_directive_uses_outfit_keywords(self):
        self.app._today_schedule_entry = Mock(return_value={
            "outfit_style": "元气风",
            "outfit_keywords": "yellow t-shirt, corduroy trousers, platform sneakers",
            "outfit": "风格：元气风\n穿搭：鹅黄色短袖T恤",
        })
        directive = self.app._today_schedule_outfit_directive()
        self.assertIn("yellow t-shirt", directive)
        self.assertIn("Today's style: 元气风", directive)
        self.assertIn("Wear exactly this planned outfit", directive)

    def test_directive_falls_back_to_outfit_text(self):
        self.app._today_schedule_entry = Mock(return_value={
            "outfit_style": "温柔风",
            "outfit_keywords": "",
            "outfit": "风格：温柔风\n发型：微卷长发\n穿搭：米白色针织开衫",
        })
        directive = self.app._today_schedule_outfit_directive()
        self.assertIn("米白色针织开衫", directive)

    def test_directive_empty_without_plan(self):
        self.app._today_schedule_entry = Mock(return_value={})
        self.assertEqual("", self.app._today_schedule_outfit_directive())


class ScheduleTweetUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

    def test_publish_force_refreshes_schedule_panel(self):
        self.assertIn(
            "await loadSocialSchedulePanel({force: true});\n"
            "  if (!socialSchedulePanelData) {",
            self.html,
        )

    def test_photo_candidates_filter_to_current_plan_times(self):
        self.assertIn(
            "itemTimes.has(String(photo.time || photo.schedule_time || \"\").trim().slice(0, 5))",
            self.html,
        )
        self.assertIn("time: String(photo.time || photo.schedule_time || \"\").trim().slice(0, 5)", self.html)

    def test_missing_images_use_uncovered_plan_items(self):
        self.assertIn("const uncoveredItems = items.filter(item => !coveredTimes.has(String(item.time || \"\").slice(0, 5)));", self.html)
        self.assertIn("const item = uncoveredItems[index]", self.html)


if __name__ == "__main__":
    unittest.main()
