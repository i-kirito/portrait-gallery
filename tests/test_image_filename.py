import sys
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
ZHUZHU_DIR = APP_DIR / "zhuzhu"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(ZHUZHU_DIR))

from core import schedule_filename_theme  # noqa: E402


class ImageFilenameTest(unittest.TestCase):
    def test_schedule_time_uses_schedule_prefix(self):
        self.assertEqual(
            "schedule_0830",
            schedule_filename_theme("morning", "8:30 去买咖啡"),
        )
        self.assertEqual("schedule_2015", schedule_filename_theme("evening", "20:15"))

    def test_missing_schedule_time_keeps_theme_label(self):
        self.assertEqual("custom", schedule_filename_theme("custom", ""))
        self.assertEqual("noon", schedule_filename_theme("noon", "午饭后散步"))

    def test_invalid_schedule_time_keeps_theme_label(self):
        self.assertEqual("morning", schedule_filename_theme("morning", "29:80 睡觉"))


if __name__ == "__main__":
    unittest.main()
