import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock

from aiohttp.test_utils import TestClient, TestServer


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from main import PortraitGalleryApp  # noqa: E402
from outfit_plan_edit import (  # noqa: E402
    replace_outfit_plan_field,
    update_schedule_details_outfit,
)
from store import ScheduleStore  # noqa: E402
from web_server import GalleryServer  # noqa: E402


class OutfitPlanEditHelperTest(unittest.TestCase):
    def test_replace_hair_preserves_other_labeled_sections(self):
        original = (
            "风格：酷飒风\n"
            "发型：高马尾\n"
            "穿搭：黑色机车造型\n"
            "动作：整理运动包\n"
            "场景：健身房更衣室"
        )

        updated = replace_outfit_plan_field(original, "发型", "低丸子头")

        self.assertIn("发型：低丸子头", updated)
        self.assertIn("穿搭：黑色机车造型", updated)
        self.assertIn("动作：整理运动包", updated)
        self.assertIn("场景：健身房更衣室", updated)
        self.assertNotIn("发型：高马尾", updated)

    def test_missing_outfit_is_inserted_before_action(self):
        updated = replace_outfit_plan_field(
            "风格：清新风\n发型：低马尾\n动作：整理书桌",
            "穿搭",
            "白衬衫搭配百褶裙",
        )

        self.assertLess(updated.index("穿搭："), updated.index("动作："))

    def test_schedule_details_all_receive_edited_field(self):
        updated = update_schedule_details_outfit(
            [
                {"time": "09:00", "hair_en": "ponytail", "scene_en": "cafe"},
                {"time": "15:00", "hair_en": "ponytail", "scene_en": "park"},
            ],
            "发型",
            "低双马尾",
        )

        self.assertEqual(["低双马尾", "低双马尾"], [item["hair_en"] for item in updated])
        self.assertEqual(["cafe", "park"], [item["scene_en"] for item in updated])


class PortraitGalleryOutfitPlanEditTest(unittest.TestCase):
    def test_update_outfit_plan_persists_display_and_generation_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            data_dir.mkdir()
            ScheduleStore(str(data_dir)).save({
                "2026-07-16": {
                    "date": "2026-07-16",
                    "status": "ok",
                    "source": "daily",
                    "schedule": "09:00 去咖啡馆\n15:00 逛公园",
                    "outfit": "风格：酷飒风\n发型：高马尾\n穿搭：黑色机车造型",
                    "outfit_keywords": "black biker outfit",
                    "schedule_details": [
                        {"time": "09:00", "hair_en": "ponytail", "outfit_en": "black outfit"},
                        {"time": "15:00", "hair_en": "ponytail", "outfit_en": "black outfit"},
                    ],
                },
            })
            app = PortraitGalleryApp.__new__(PortraitGalleryApp)
            app.data_dir = str(data_dir)
            app._today = lambda: date(2026, 7, 16)
            app._now = lambda: datetime(2026, 7, 16, 12, 0)

            hair_result = app.update_outfit_plan("发型", "低双马尾")
            outfit_result = app.update_outfit_plan("穿搭", "白衬衫搭配黑色百褶裙")
            saved = ScheduleStore(str(data_dir)).load()["2026-07-16"]

        self.assertEqual("ok", hair_result["status"])
        self.assertEqual("ok", outfit_result["status"])
        self.assertIn("发型：低双马尾", saved["outfit"])
        self.assertIn("穿搭：白衬衫搭配黑色百褶裙", saved["outfit"])
        self.assertEqual("白衬衫搭配黑色百褶裙", saved["outfit_keywords"])
        self.assertTrue(all(item["hair_en"] == "低双马尾" for item in saved["schedule_details"]))
        self.assertTrue(
            all(item["outfit_en"] == "白衬衫搭配黑色百褶裙" for item in saved["schedule_details"])
        )


class OutfitPlanEditEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_accepts_hair_and_rejects_style(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "data"
            data_dir.mkdir()
            config_path = root / "config" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text("gallery:\n  port: 18889\n", encoding="utf-8")
            (root / "app" / "references").mkdir(parents=True)
            server = GalleryServer(
                {"paths": {"project_root": str(root)}, "gallery": {"port": 18889}},
                str(data_dir),
                str(config_path),
            )
            server.on_update_outfit_plan = Mock(return_value={
                "status": "ok",
                "field": "发型",
                "value": "低双马尾",
            })
            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                valid = await client.patch(
                    "/api/schedule-detail/outfit",
                    json={"field": "发型", "value": "低双马尾"},
                )
                invalid = await client.patch(
                    "/api/schedule-detail/outfit",
                    json={"field": "风格", "value": "清新风"},
                )
            finally:
                await client.close()

        self.assertEqual(200, valid.status)
        self.assertEqual(400, invalid.status)
        server.on_update_outfit_plan.assert_called_once_with("发型", "低双马尾")


class OutfitPlanEditFrontendContractTest(unittest.TestCase):
    def test_hair_and_outfit_use_inline_double_click_editor(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('data-outfit-field="', html)
        self.assertIn('ondblclick="startOutfitPlanEdit(this)"', html)
        self.assertIn("function startOutfitPlanEdit(el)", html)
        self.assertIn("function saveOutfitPlanValue(field, value, holder, previousValue)", html)
        self.assertIn("fetch('/api/schedule-detail/outfit'", html)
        self.assertIn("event.key === 'Enter' && !event.shiftKey", html)
        self.assertIn("event.key === 'Escape'", html)


if __name__ == "__main__":
    unittest.main()
