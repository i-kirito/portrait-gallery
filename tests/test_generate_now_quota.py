import asyncio
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo
from pathlib import Path
import sys

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from web_server import GalleryServer  # noqa: E402


class DummyRequest:
    can_read_body = False

    async def json(self):
        return {}


class GenerateNowQuotaTest(unittest.IsolatedAsyncioTestCase):
    async def test_generate_now_ignores_a_full_daily_plan(self):
        server = GalleryServer.__new__(GalleryServer)
        server.on_photo_quota_snapshot = Mock(
            return_value=(6, 6, 0, 0, 0, 6, 0)
        )
        server._today_completed_photo_count = Mock(return_value=6)
        server.config = {}
        server._now = Mock(return_value=datetime(2026, 7, 23, 20, 48, tzinfo=ZoneInfo("Asia/Shanghai")))
        server._today_schedule_entry = Mock(
            return_value={"schedule": "08:12 起床\n20:48 河边散步", "status": "ok"}
        )
        server._schedule_items_for_inference = Mock(return_value=[("20:48", "河边散步")])
        server._infer_generate_now_detail = AsyncMock(
            return_value={"activity_zh": "河边散步"}
        )
        server._refresh_schedule_singleflight = AsyncMock()
        server._theme_for_schedule_time = Mock(return_value="bedtime")
        server._load_api_keys_config = Mock(return_value={})
        server._load_plugin_config = Mock(return_value={})
        server._effective_gpt_image_base_url = Mock(return_value="")
        server._effective_gitee_api_key = Mock(return_value="")
        server._effective_gitee_image_url = Mock(return_value="")

        response = await server.handle_generate_now(DummyRequest())
        payload = json.loads(response.text)

        self.assertEqual(400, response.status)
        self.assertEqual("missing_image_config", payload.get("error"))
        server._infer_generate_now_detail.assert_awaited_once()
        server.on_photo_quota_snapshot.assert_not_called()
        server._today_completed_photo_count.assert_not_called()


if __name__ == "__main__":
    unittest.main()
