import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from data import DailyEntry  # noqa: E402
from main import save_schedule_entry  # noqa: E402
from store import ScheduleStore  # noqa: E402


class ScheduleHistoryArchiveTest(unittest.TestCase):
    def test_save_schedule_entry_propagates_persistence_error(self):
        entry = DailyEntry(date="2026-07-30", schedule="08:00 出门买早餐")

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            ScheduleStore,
            "update",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                save_schedule_entry(tmpdir, entry)

    def test_replacing_daily_plan_archives_previous_unique_schedule(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            previous = DailyEntry(
                date="2026-07-30",
                outfit_style="甜美风",
                outfit="风格：甜美风\n穿搭：粉色衬衫和百褶裙",
                schedule="12:50 在公园草坪上吃一份蔬菜沙拉午餐",
                schedule_prompt="12:50 eat a vegetable salad on the park lawn",
                photo_style_en="casual handheld botanical garden snapshot",
                schedule_llm_model="grok-4.5",
                source="cron",
            )
            replacement = DailyEntry(
                date="2026-07-30",
                outfit_style="酷飒风",
                outfit="风格：酷飒风\n穿搭：灰色机能马甲和黑色工装长裤",
                schedule="12:41 在陶艺工作室完成一只手捏餐盘",
                schedule_prompt="12:41 finish a hand-built plate in a pottery studio",
                photo_style_en="high-angle documentary workshop photography",
                schedule_llm_model="mimo-v2.5-pro",
                source="web",
            )

            save_schedule_entry(tmpdir, previous)
            save_schedule_entry(tmpdir, replacement)
            save_schedule_entry(tmpdir, replacement)

            stored = ScheduleStore(tmpdir).load()["2026-07-30"]
            history = stored.get("schedule_history")

            self.assertEqual(replacement.schedule, stored["schedule"])
            self.assertEqual(1, len(history))
            self.assertEqual(previous.schedule, history[0]["schedule"])
            self.assertEqual(previous.outfit, history[0]["outfit"])
            self.assertEqual(previous.photo_style_en, history[0]["photo_style_en"])
            self.assertEqual("schedule_replaced", history[0]["archive_reason"])
            self.assertTrue(history[0]["archived_at"])
            self.assertNotIn("schedule_prompt", history[0])

    def test_same_schedule_with_new_outfit_still_archives_visible_plan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schedule = "12:41 在陶艺工作室完成一只手捏餐盘"
            previous = DailyEntry(
                date="2026-07-30",
                outfit_style="清新风",
                outfit="风格：清新风\n穿搭：绿色亚麻衬衫和白色长裤",
                schedule=schedule,
                photo_style_en="soft eye-level workshop snapshot",
            )
            replacement = DailyEntry(
                date="2026-07-30",
                outfit_style="酷飒风",
                outfit="风格：酷飒风\n穿搭：灰色机能马甲和黑色工装长裤",
                schedule=schedule,
                photo_style_en="high-angle documentary workshop photography",
            )

            save_schedule_entry(tmpdir, previous)
            save_schedule_entry(tmpdir, replacement)

            history = ScheduleStore(tmpdir).load()["2026-07-30"]["schedule_history"]

            self.assertEqual(1, len(history))
            self.assertEqual(previous.outfit, history[0]["outfit"])
            self.assertEqual(previous.photo_style_en, history[0]["photo_style_en"])


if __name__ == "__main__":
    unittest.main()
