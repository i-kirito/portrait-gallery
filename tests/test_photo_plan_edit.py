import sys
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from photo_plan_edit import replace_schedule_activity, update_schedule_details_activity  # noqa: E402


class PhotoPlanEditTest(unittest.TestCase):
    def test_replace_schedule_activity_keeps_other_lines(self):
        schedule = "8:03 起床洗漱\n14:52 做瑜伽\n19:24 调试直播设备"

        updated, found = replace_schedule_activity(
            schedule,
            "14:52",
            "在阳台整理新买的花",
        )

        self.assertTrue(found)
        self.assertEqual(
            "8:03 起床洗漱\n14:52 在阳台整理新买的花\n19:24 调试直播设备",
            updated,
        )

    def test_update_schedule_details_activity_removes_stale_scene(self):
        details = [
            {
                "time": "14:52",
                "activity_zh": "做瑜伽",
                "activity_en": "doing yoga",
                "action_en": "stretching on a yoga mat",
                "scene_en": "living room",
                "props_en": "yoga mat",
                "lighting_en": "soft afternoon light",
                "outfit_en": "daily outfit",
            }
        ]

        updated, changed = update_schedule_details_activity(
            details,
            "14:52",
            "在阳台整理新买的花",
        )

        self.assertTrue(changed)
        self.assertEqual("在阳台整理新买的花", updated[0]["activity_zh"])
        self.assertEqual("在阳台整理新买的花", updated[0]["activity_en"])
        self.assertEqual("在阳台整理新买的花", updated[0]["action_en"])
        self.assertEqual("", updated[0]["scene_en"])
        self.assertEqual("", updated[0]["props_en"])
        self.assertEqual("", updated[0]["lighting_en"])
        self.assertEqual("daily outfit", updated[0]["outfit_en"])
        self.assertTrue(updated[0]["manual_activity_edit"])


if __name__ == "__main__":
    unittest.main()
