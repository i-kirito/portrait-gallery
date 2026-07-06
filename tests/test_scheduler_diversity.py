import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from scheduler import DailyScheduler  # noqa: E402


class ScheduleDiversityTest(unittest.TestCase):
    def make_scheduler(self, data_dir: str) -> DailyScheduler:
        return DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, data_dir)

    def test_rejects_bed_idle_opening(self):
        scheduler = self.make_scheduler("data")
        items = scheduler._schedule_plan_items(
            "08:23 赖床窝在被子里翻手机看消息\n"
            "10:17 起床梳洗换上居家穿搭\n"
            "12:38 在咖啡馆吃轻食午餐"
        )

        error = scheduler._schedule_diversity_error(items)

        self.assertIn("第一条", error)

    def test_rejects_multiple_cooking_items_in_one_day(self):
        scheduler = self.make_scheduler("data")
        items = scheduler._schedule_plan_items(
            "08:23 去楼下取一杯热拿铁\n"
            "12:38 在厨房为自己做午餐\n"
            "15:20 去书店挑选新的小说\n"
            "19:24 准备晚餐并收拾餐桌"
        )

        error = scheduler._schedule_diversity_error(items)

        self.assertIn("最多 1 条", error)

    def test_accepts_varied_schedule(self):
        scheduler = self.make_scheduler("data")
        items = scheduler._schedule_plan_items(
            "08:23 去楼下取一杯热拿铁\n"
            "10:17 整理书桌和今日灵感板\n"
            "12:38 在咖啡馆吃轻食午餐\n"
            "15:20 去书店挑选新的小说\n"
            "19:24 沿着河边散步听播客\n"
            "22:18 做睡前护肤准备休息"
        )

        self.assertEqual("", scheduler._schedule_diversity_error(items))

    def test_recent_cooking_history_blocks_more_cooking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schedule_data = {
                "2026-07-03": {
                    "status": "ok",
                    "schedule": "08:12 整理书桌\n12:38 在厨房为自己做午餐\n19:24 看展后回家休息",
                },
                "2026-07-04": {
                    "status": "ok",
                    "schedule": "08:12 楼下买咖啡\n12:38 逛书店\n19:24 准备晚餐并布置餐桌",
                },
            }
            Path(tmpdir, "schedule_data.json").write_text(
                json.dumps(schedule_data, ensure_ascii=False),
                encoding="utf-8",
            )
            scheduler = self.make_scheduler(tmpdir)
            recent_counts = scheduler._recent_schedule_category_counts(date(2026, 7, 6), days=3)
            items = scheduler._schedule_plan_items(
                "08:23 去楼下取一杯热拿铁\n"
                "12:38 在厨房为自己做午餐\n"
                "15:20 去书店挑选新的小说"
            )

            error = scheduler._schedule_diversity_error(items, recent_counts)

            self.assertIn("最近 3 天", error)


if __name__ == "__main__":
    unittest.main()
