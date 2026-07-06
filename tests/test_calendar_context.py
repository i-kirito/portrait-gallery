import sys
import unittest
from datetime import date
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from calendar_context import build_day_context  # noqa: E402


class CalendarContextTest(unittest.TestCase):
    def test_weekend_rest_day_excludes_work_and_school_types(self):
        context = build_day_context(date(2026, 7, 5))

        self.assertTrue(context.is_rest_day)
        self.assertEqual(context.day_type_label, "周末休息日")
        pool = context.schedule_type_pool(["工作日", "学习日", "宅家日", "运动日"])
        self.assertNotIn("工作日", pool)
        self.assertNotIn("学习日", pool)
        self.assertTrue(context.rest_day_conflicts("08:12 去公司上班", "10:27 attend class"))

    def test_public_holiday_gets_holiday_guidance(self):
        context = build_day_context(date(2026, 2, 17))

        self.assertTrue(context.is_rest_day)
        self.assertTrue(context.is_public_holiday)
        self.assertEqual(context.holiday_name, "春节")
        self.assertIn("春节假期", context.schedule_type_pool(["宅家日", "放松日"])[0])
        self.assertIn("春节", context.prompt_block("春节假期"))

    def test_makeup_workday_allows_workday_schedule_on_weekend(self):
        context = build_day_context(date(2026, 1, 4))

        self.assertTrue(context.is_weekend)
        self.assertTrue(context.is_makeup_workday)
        self.assertFalse(context.is_rest_day)
        self.assertEqual(context.day_type_label, "调休上班日")
        self.assertFalse(context.rest_day_conflicts("08:12 去公司上班"))
        self.assertIn("工作日", context.schedule_type_pool(["工作日", "学习日", "宅家日"]))

    def test_configured_holiday_override_is_respected(self):
        config = {"schedule": {"calendar": {"holidays": {"2027-01-02": "自定义假期"}}}}
        context = build_day_context(date(2027, 1, 2), config)

        self.assertTrue(context.is_rest_day)
        self.assertTrue(context.is_public_holiday)
        self.assertEqual(context.holiday_name, "自定义假期")


if __name__ == "__main__":
    unittest.main()
