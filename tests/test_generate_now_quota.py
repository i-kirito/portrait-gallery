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
    async def test_generate_now_blocked_when_daily_plan_full(self):
        server = GalleryServer.__new__(GalleryServer)
        server.on_photo_quota_snapshot = Mock(
            return_value=(6, 6, 0, 0, 0, 6, 0)
        )
        server.get_photo_job_limit = Mock(return_value=6)
        server._today_completed_photo_count = Mock(return_value=6)
        server._now = Mock(return_value=datetime(2026, 7, 23, 20, 48, tzinfo=ZoneInfo("Asia/Shanghai")))
        server._today_schedule_entry = Mock(
            return_value={"schedule": "08:12 起床\n20:48 河边散步", "status": "ok"}
        )
        server._schedule_items_for_inference = Mock(return_value=[("20:48", "河边散步")])
        server._infer_generate_now_detail = AsyncMock()
        server._refresh_schedule_singleflight = AsyncMock()

        response = await server.handle_generate_now(DummyRequest())
        payload = json.loads(response.text)

        self.assertEqual(409, response.status)
        self.assertEqual("limit_reached", payload.get("error"))
        self.assertIn("今日生图计划已达上限", payload.get("message", ""))
        server._infer_generate_now_detail.assert_not_awaited()
        server.on_photo_quota_snapshot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
