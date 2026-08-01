import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from web_server import GalleryServer  # noqa: E402
from xiaohongshu_client import XiaohongshuClient, XiaohongshuError  # noqa: E402
from data import DailyEntry  # noqa: E402
from store import ScheduleStore  # noqa: E402


class XiaohongshuClientTest(unittest.IsolatedAsyncioTestCase):
    def test_binary_is_discovered_from_runtime_workdir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            binary = Path(tmpdir) / "xiaohongshu-mcp-darwin-arm64"
            binary.write_bytes(b"test")
            binary.chmod(0o755)

            client = XiaohongshuClient(workdir=tmpdir)

            self.assertEqual(str(binary), client.binary_path)
            self.assertTrue(client.configured)

    def test_service_url_must_be_loopback(self):
        with self.assertRaisesRegex(ValueError, "回环地址"):
            XiaohongshuClient(base_url="http://192.168.1.20:18060")

    async def test_login_qrcode_parses_go_duration_timeout(self):
        client = XiaohongshuClient()
        client._request = AsyncMock(return_value={
            "is_logged_in": False,
            "timeout": "4m0s",
            "img": "data:image/png;base64,cXJjb2Rl",
        })

        payload = await client.login_qrcode()

        self.assertFalse(payload["is_logged_in"])
        self.assertEqual(240, payload["timeout"])
        self.assertTrue(payload["image"].startswith("data:image/png;base64,"))
        client._request.assert_awaited_once_with(
            "GET",
            "/api/v1/login/qrcode",
            timeout_seconds=60,
        )

    async def test_search_filters_video_and_incomplete_results(self):
        client = XiaohongshuClient()
        client._request = AsyncMock(return_value={
            "feeds": [
                {
                    "id": "image-note",
                    "xsecToken": "token-1",
                    "noteCard": {
                        "type": "normal",
                        "displayTitle": "通勤穿搭",
                        "user": {
                            "userId": "creator-1",
                            "nickname": "作者",
                            "avatar": "https://sns-avatar-qc.xhscdn.com/avatar.webp",
                        },
                        "cover": {"urlDefault": "https://sns-webpic-qc.xhscdn.com/a.webp"},
                    },
                },
                {
                    "id": "video-note",
                    "xsecToken": "token-2",
                    "noteCard": {
                        "type": "video",
                        "displayTitle": "视频",
                        "cover": {"urlDefault": "https://sns-webpic-qc.xhscdn.com/b.webp"},
                    },
                },
                {
                    "id": "missing-token",
                    "noteCard": {
                        "type": "normal",
                        "cover": {"urlDefault": "https://sns-webpic-qc.xhscdn.com/c.webp"},
                    },
                },
            ]
        })

        items = await client.search("通勤")

        self.assertEqual(1, len(items))
        self.assertEqual("image-note", items[0]["id"])
        self.assertEqual("通勤穿搭", items[0]["title"])
        self.assertEqual("creator-1", items[0]["user_id"])
        self.assertEqual("https://sns-avatar-qc.xhscdn.com/avatar.webp", items[0]["avatar_url"])
        client._request.assert_awaited_once_with(
            "POST",
            "/api/v1/feeds/search",
            json_body={
                "keyword": "通勤",
                "max_results": 30,
            },
            timeout_seconds=70,
        )

    async def test_search_creators_groups_notes_by_author(self):
        client = XiaohongshuClient()
        client.search = AsyncMock(return_value=[
            {
                "id": "note-1", "user_id": "creator-1", "author": "小雪穿搭",
                "avatar_url": "https://sns-avatar-qc.xhscdn.com/a.webp",
                "xsec_token": "token-1", "title": "法式穿搭", "cover_url": "cover-1",
            },
            {
                "id": "note-2", "user_id": "creator-1", "author": "小雪穿搭",
                "avatar_url": "", "xsec_token": "token-2", "title": "通勤穿搭",
                "cover_url": "cover-2",
            },
            {
                "id": "note-3", "user_id": "creator-2", "author": "小雪日记",
                "avatar_url": "avatar-2", "xsec_token": "token-3", "title": "日常",
                "cover_url": "cover-3",
            },
        ])

        creators = await client.search_creators("小雪穿搭")

        self.assertEqual(2, len(creators))
        self.assertEqual("creator-1", creators[0]["user_id"])
        self.assertEqual(2, creators[0]["matched_note_count"])
        self.assertEqual("token-2", creators[0]["xsec_token"])

    async def test_profile_normalizes_creator_and_image_notes(self):
        client = XiaohongshuClient()
        client._request = AsyncMock(return_value={
            "data": {
                "userBasicInfo": {
                    "nickname": "小雪穿搭",
                    "desc": "分享日常穿搭",
                    "imageb": "https://sns-avatar-qc.xhscdn.com/a.webp",
                    "redId": "xiaoxue",
                },
                "interactions": [{"type": "fans", "count": "12.3万"}],
                "feeds": [{
                    "id": "note-1",
                    "xsecToken": "note-token",
                    "noteCard": {
                        "type": "normal",
                        "displayTitle": "法式温柔风",
                        "user": {"userId": "creator-1", "nickname": "小雪穿搭"},
                        "cover": {
                            "urlDefault": "https://sns-webpic-qc.xhscdn.com/a.webp",
                            "width": 900,
                            "height": 1200,
                        },
                    },
                }],
            }
        })

        profile = await client.profile("creator-1", "profile-token")

        self.assertEqual("小雪穿搭", profile["creator"]["nickname"])
        self.assertEqual("12.3万", profile["creator"]["stats"]["fans"])
        self.assertEqual("note-1", profile["notes"][0]["id"])
        client._request.assert_awaited_once_with(
            "POST",
            "/api/v1/user/profile",
            json_body={"user_id": "creator-1", "xsec_token": "profile-token"},
            timeout_seconds=80,
        )
    async def test_search_rejects_result_limit_outside_supported_range(self):
        client = XiaohongshuClient()

        with self.assertRaises(XiaohongshuError) as raised:
            await client.search("通勤", max_results=51)

        self.assertEqual("invalid_max_results", raised.exception.code)

    async def test_search_retries_one_transient_upstream_error(self):
        client = XiaohongshuClient()
        client._request = AsyncMock(side_effect=[
            XiaohongshuError("upstream_error", "服务器内部错误"),
            {"feeds": []},
        ])

        items = await client.search("通勤")

        self.assertEqual([], items)
        self.assertEqual(2, client._request.await_count)

    async def test_image_import_rejects_non_xiaohongshu_host(self):
        client = XiaohongshuClient()

        with self.assertRaises(XiaohongshuError) as raised:
            await client.import_image("https://example.com/outfit.jpg", "/tmp")

        self.assertEqual("image_host_not_allowed", raised.exception.code)

    async def test_xiaohongshu_hostname_cannot_resolve_to_loopback(self):
        client = XiaohongshuClient()
        fake_dns = [(2, 1, 6, "", ("127.0.0.1", 443))]

        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=fake_dns)):
            with self.assertRaises(XiaohongshuError) as raised:
                await client._validate_image_url("https://sns-webpic-qc.xhscdn.com/a.webp")

        self.assertEqual("private_image_host", raised.exception.code)


class XiaohongshuFrontendContractTest(unittest.TestCase):
    def test_qrcode_countdown_auto_refresh_and_request_races(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

        countdown_start = html.index("function updateXiaohongshuQrCountdown()")
        countdown_end = html.index("function getXiaohongshuStatusRequest", countdown_start)
        countdown = html[countdown_start:countdown_end]
        self.assertIn("Math.ceil((xhsLoginPollDeadline - Date.now()) / 1000)", countdown)
        self.assertIn("`请在 ${remaining} 秒内扫码`", countdown)

        stop_start = html.index("function stopXiaohongshuLoginPolling()")
        stop_end = html.index("function cancelXiaohongshuLoginFlow", stop_start)
        stop = html[stop_start:stop_end]
        self.assertIn("window.clearInterval(xhsLoginPollTimer)", stop)
        self.assertIn("window.clearInterval(xhsLoginCountdownTimer)", stop)
        self.assertIn("window.clearTimeout(xhsLoginQrRetryTimer)", stop)

        status_request_start = html.index("function getXiaohongshuStatusRequest")
        status_request_end = html.index("async function loadXiaohongshuStatus", status_request_start)
        status_request = html[status_request_start:status_request_end]
        self.assertIn("if (xhsLoginStatusRequestInFlight)", status_request)
        self.assertIn("xhsLoginStatusRequestInFlight = request", status_request)

        status_start = status_request_end
        status_end = html.index("async function refreshExpiredXiaohongshuQrcode", status_start)
        status = html[status_start:status_end]
        self.assertIn("const statusRequestGeneration = ++xhsLoginStatusRequestGeneration", status)
        self.assertIn("statusRequestGeneration !== xhsLoginStatusRequestGeneration", status)

        refresh_start = status_end
        refresh_end = html.index("function startXiaohongshuLoginPolling", refresh_start)
        refresh = html[refresh_start:refresh_end]
        self.assertIn("requestGeneration !== xhsLoginRequestGeneration", refresh)
        self.assertIn("xhsLoginExpiryCheckBusy", refresh)
        self.assertIn("await loadXiaohongshuStatus(true, requestGeneration)", refresh)
        self.assertIn("await requestXiaohongshuQrcode({ autoRefresh: true })", refresh)

        retry_start = html.index("function scheduleXiaohongshuQrcodeRetry")
        retry_end = html.index("function updateXiaohongshuQrCountdown", retry_start)
        retry = html[retry_start:retry_end]
        self.assertIn("XHS_QR_RETRY_MAX_DELAY_MS", retry)
        self.assertIn("window.setTimeout", retry)
        self.assertIn("isXiaohongshuLoginPanelActive()", retry)

        polling_start = html.index("function startXiaohongshuLoginPolling")
        polling_end = html.index("async function requestXiaohongshuQrcode", polling_start)
        polling = html[polling_start:polling_end]
        self.assertGreaterEqual(
            polling.count("void refreshExpiredXiaohongshuQrcode(requestGeneration)"),
            2,
        )

        request_start = html.index("async function requestXiaohongshuQrcode(options = {})")
        request_end = html.index("function setSettingsPanel", request_start)
        request = html[request_start:request_end]
        self.assertIn("const autoRefresh = Boolean(options.autoRefresh)", request)
        self.assertIn('"二维码已过期，正在自动刷新"', request)
        self.assertIn("const requestGeneration = ++xhsLoginRequestGeneration", request)
        self.assertIn("requestGeneration !== xhsLoginRequestGeneration", request)
        self.assertIn("await previousRequest.catch(() => null)", request)
        self.assertIn("xhsLoginQrRequestInFlight = activeQrRequest", request)
        self.assertIn("xhsLoginQrRequestInFlight === activeQrRequest", request)
        self.assertIn("scheduleXiaohongshuQrcodeRetry(requestGeneration, error.message)", request)
        self.assertIn("startXiaohongshuLoginPolling(data.timeout, requestGeneration)", request)
        logged_in_start = request.index("if (data.is_logged_in)")
        logged_in_end = request.index("if (!data.image)", logged_in_start)
        logged_in = request[logged_in_start:logged_in_end]
        self.assertIn("completeXiaohongshuLogin()", logged_in)
        self.assertNotIn("loadXiaohongshuStatus", logged_in)

        complete_start = html.index("function completeXiaohongshuLogin")
        complete_end = html.index("function stopXiaohongshuLoginPolling", complete_start)
        complete = html[complete_start:complete_end]
        self.assertIn("stopXiaohongshuLoginPolling()", complete)
        self.assertIn("clearXiaohongshuQrcode()", complete)
        self.assertIn("button.disabled = false", complete)

        panel_start = html.index("function setSettingsPanel")
        panel_end = html.index("function openSettings", panel_start)
        panel = html[panel_start:panel_end]
        self.assertIn('activeSettingsPanel === "xiaohongshu"', panel)
        self.assertIn("cancelXiaohongshuLoginFlow()", panel)

        cancel_start = html.index("function cancelXiaohongshuLoginFlow")
        cancel_end = html.index("function scheduleXiaohongshuQrcodeRetry", cancel_start)
        cancel = html[cancel_start:cancel_end]
        self.assertIn("xhsLoginRequestGeneration += 1", cancel)
        self.assertIn("xhsLoginStatusRequestGeneration += 1", cancel)
        self.assertIn("stopXiaohongshuLoginPolling()", cancel)
        self.assertIn("clearXiaohongshuQrcode()", cancel)

        close_start = html.index("function closeSettings()")
        close_end = html.index("function setPersonaSource", close_start)
        close = html[close_start:close_end]
        self.assertIn("cancelXiaohongshuLoginFlow()", close)


class XiaohongshuApiTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_server(root: Path) -> GalleryServer:
        data_dir = root / "data"
        config_path = root / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("gallery:\n  port: 18899\n", encoding="utf-8")
        (root / "app" / "references").mkdir(parents=True, exist_ok=True)
        config = {
            "paths": {"project_root": str(root)},
            "gallery": {"port": 18899},
        }
        return GalleryServer(config, str(data_dir), str(config_path))

    async def _start_client(self, server: GalleryServer) -> TestClient:
        test_server = TestServer(server.app)
        await test_server.start_server(access_log=None)
        client = TestClient(test_server)
        await client.start_server()
        return client

    def test_schedule_query_ignores_llm_garment_description(self):
        query = GalleryServer._xiaohongshu_schedule_query({
            "outfit_style": "温柔风",
            "reference_query": (
                "成年女性温柔风白色心情居家日常穿搭参考，"
                "米白不透肤全覆盖棉质圆领短袖针织上衣搭配"
                "浅燕麦色高腰阔腿家居裤，室内自然光单人摄影"
            ),
        })

        self.assertEqual(
            "温柔风居家穿搭",
            query,
        )
        self.assertNotIn("针织上衣", query)
        self.assertNotIn("家居裤", query)
        self.assertLessEqual(len(query), 64)

    def test_schedule_query_prefers_preselected_keyword(self):
        query = GalleryServer._xiaohongshu_schedule_query({
            "xiaohongshu_search_query": "夏季温柔居家穿搭",
            "outfit_style": "LLM 不应覆盖",
            "outfit": "穿搭：LLM 设计的具体衣服不应进入搜索",
        })

        self.assertEqual("夏季温柔居家穿搭", query)

    def test_schedule_query_ignores_bare_generic_keyword(self):
        query = GalleryServer._xiaohongshu_schedule_query({
            "xiaohongshu_search_query": "穿搭",
            "outfit_style": "温柔风",
        })

        self.assertEqual("温柔风穿搭", query)

    async def test_status_and_search_use_read_only_client(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            server.xiaohongshu_client.status = AsyncMock(return_value={
                "configured": True,
                "service_running": True,
                "is_logged_in": True,
            })
            server.xiaohongshu_client.search = AsyncMock(return_value=[{
                "id": "note-1",
                "xsec_token": "token",
                "title": "夏季穿搭",
                "cover_url": "https://sns-webpic-qc.xhscdn.com/a.webp",
            }])
            client = await self._start_client(server)
            try:
                status_response = await client.get("/api/xiaohongshu/status")
                status = await status_response.json()
                search_response = await client.post(
                    "/api/xiaohongshu/search",
                    json={"keyword": "夏季穿搭"},
                )
                search = await search_response.json()
            finally:
                await client.close()

            self.assertEqual(200, status_response.status)
            self.assertTrue(status["is_logged_in"])
            self.assertEqual(200, search_response.status)
            self.assertEqual(1, search["count"])
            server.xiaohongshu_client.search.assert_awaited_once_with(
                "夏季穿搭",
                max_results=30,
            )

    async def test_login_qrcode_route_returns_client_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            server.xiaohongshu_client.login_qrcode = AsyncMock(return_value={
                "is_logged_in": False,
                "timeout": 240,
                "image": "data:image/png;base64,cXJjb2Rl",
            })
            client = await self._start_client(server)
            try:
                response = await client.post("/api/xiaohongshu/login/qrcode")
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status, payload)
            self.assertEqual(240, payload["timeout"])
            self.assertTrue(payload["image"].startswith("data:image/png;base64,"))
            server.xiaohongshu_client.login_qrcode.assert_awaited_once_with()

    async def test_creator_search_profile_favorite_and_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            server.xiaohongshu_client.search_creators = AsyncMock(return_value=[{
                "user_id": "creator-1",
                "nickname": "小雪穿搭",
                "avatar_url": "https://sns-avatar-qc.xhscdn.com/a.webp",
                "xsec_token": "token-1",
                "matched_note_count": 2,
            }])
            server.xiaohongshu_client.profile = AsyncMock(return_value={
                "creator": {
                    "user_id": "creator-1",
                    "nickname": "小雪穿搭",
                    "avatar_url": "https://sns-avatar-qc.xhscdn.com/a.webp",
                    "description": "分享通勤穿搭",
                    "xsec_token": "token-1",
                },
                "notes": [{
                    "id": "note-1",
                    "xsec_token": "note-token",
                    "title": "法式温柔风",
                    "user_id": "creator-1",
                    "author": "小雪穿搭",
                    "cover_url": "https://sns-webpic-qc.xhscdn.com/look.webp",
                }],
            })
            client = await self._start_client(server)
            try:
                search_response = await client.post(
                    "/api/xiaohongshu/creators/search",
                    json={"keyword": "小雪穿搭"},
                )
                search_payload = await search_response.json()
                profile_response = await client.post(
                    "/api/xiaohongshu/creators/profile",
                    json={
                        "user_id": "creator-1",
                        "nickname": "小雪穿搭",
                        "xsec_token": "token-1",
                    },
                )
                profile_payload = await profile_response.json()
                favorite_response = await client.post(
                    "/api/xiaohongshu/creators",
                    json={
                        **profile_payload["creator"],
                        "note_count": len(profile_payload["notes"]),
                    },
                )
                favorite_payload = await favorite_response.json()
                list_response = await client.get("/api/xiaohongshu/creators")
                list_payload = await list_response.json()
                delete_response = await client.delete(
                    "/api/xiaohongshu/creators/creator-1"
                )
                delete_payload = await delete_response.json()
            finally:
                await client.close()

            self.assertEqual(200, search_response.status, search_payload)
            self.assertFalse(search_payload["items"][0]["favorited"])
            self.assertEqual(200, profile_response.status, profile_payload)
            self.assertEqual("note-1", profile_payload["notes"][0]["id"])
            self.assertTrue(favorite_payload["favorited"])
            self.assertEqual(1, list_payload["count"])
            self.assertNotIn("xsec_token", list_payload["items"][0])
            self.assertTrue(delete_payload["removed"])
            self.assertEqual({}, server.xiaohongshu_creator_store.load())

    async def test_favorite_creator_refreshes_stale_profile_token(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            server.xiaohongshu_creator_store.save({
                "creator-1": {
                    "nickname": "小雪穿搭",
                    "xsec_token": "stale-token",
                    "added_at": "2026-07-30T10:00:00",
                }
            })
            server.xiaohongshu_client.profile = AsyncMock(side_effect=[
                XiaohongshuError("upstream_error", "令牌失效"),
                {
                    "creator": {"user_id": "creator-1", "nickname": "小雪穿搭"},
                    "notes": [],
                },
            ])
            server.xiaohongshu_client.search_creators = AsyncMock(return_value=[{
                "user_id": "creator-1",
                "nickname": "小雪穿搭",
                "xsec_token": "fresh-token",
            }])

            profile = await server._load_xiaohongshu_creator_profile("creator-1")

            self.assertTrue(profile["creator"]["favorited"])
            self.assertEqual("fresh-token", server.xiaohongshu_creator_store.load()["creator-1"]["xsec_token"])
            self.assertEqual(
                [("creator-1", "stale-token"), ("creator-1", "fresh-token")],
                [call.args for call in server.xiaohongshu_client.profile.await_args_list],
            )
    async def test_import_persists_verified_local_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            root = Path(tmpdir)
            server = self._make_server(root)

            async def fake_import(_url, output_dir):
                path = Path(output_dir) / "xhs_test.png"
                Image.new("RGB", (32, 48), "white").save(path)
                return {
                    "filename": path.name,
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                }

            server.xiaohongshu_client.import_image = AsyncMock(side_effect=fake_import)
            client = await self._start_client(server)
            try:
                response = await client.post("/api/xiaohongshu/import", json={
                    "url": "https://sns-webpic-qc.xhscdn.com/outfit.webp",
                    "title": "白色连衣裙",
                    "author": "作者",
                })
                payload = await response.json()
                list_response = await client.get("/api/xiaohongshu/references")
                references = await list_response.json()
                delete_response = await client.delete(
                    "/api/xiaohongshu/references/xhs_test.png"
                )
                delete_payload = await delete_response.json()
                after_delete_response = await client.get("/api/xiaohongshu/references")
                references_after_delete = await after_delete_response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status, payload)
            self.assertEqual("/local-refs/xiaohongshu/xhs_test.png", payload["url"])
            self.assertEqual(32, payload["width"])
            self.assertEqual(48, payload["height"])
            self.assertEqual(1, len(references))
            self.assertEqual("xiaohongshu", references[0]["source"])
            self.assertEqual(200, delete_response.status, delete_payload)
            self.assertTrue(delete_payload["success"])
            self.assertEqual([], references_after_delete)
            self.assertFalse(
                (root / "data" / "references" / "xiaohongshu" / "xhs_test.png").exists()
            )

    async def test_schedule_mode_selects_hidden_persistent_daily_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            root = Path(tmpdir)
            server = self._make_server(root)
            server._now = lambda: datetime(2026, 7, 30, 10, 0)
            ScheduleStore(str(root / "data")).save({
                "2026-07-30": {
                    "date": "2026-07-30",
                    "reference_query": "白色衬衫 蓝色半身裙",
                    "outfit_style": "清新通勤风",
                    "schedule": "10:30 咖啡店办公",
                }
            })
            server.xiaohongshu_client.status = AsyncMock(return_value={
                "service_running": True,
                "is_logged_in": True,
            })
            server.xiaohongshu_client.search = AsyncMock(return_value=[{
                "id": "note-1",
                "xsec_token": "token-1",
                "title": "清新通勤穿搭 OOTD",
                "author": "作者",
                "cover_url": "https://sns-webpic-qc.xhscdn.com/daily.webp",
                "width": 900,
                "height": 1200,
                "liked_count": "2.1万",
            }])
            server.xiaohongshu_client.detail = AsyncMock(return_value={
                "id": "note-1",
                "title": "清新通勤穿搭 OOTD",
                "author": "作者",
                "images": [
                    {
                        "index": 0,
                        "url": "https://sns-webpic-qc.xhscdn.com/daily.webp",
                        "width": 900,
                        "height": 1200,
                    },
                    {
                        "index": 1,
                        "url": "https://sns-webpic-qc.xhscdn.com/full-body.webp",
                        "width": 900,
                        "height": 1200,
                    },
                ],
            })

            async def fake_import(_url, output_dir):
                path = Path(output_dir) / "xhs_daily.png"
                Image.new("RGB", (900, 1200), "white").save(path)
                return {"filename": path.name, "path": str(path), "size_bytes": path.stat().st_size}

            server.xiaohongshu_client.import_image = AsyncMock(side_effect=fake_import)
            server.on_validate_xiaohongshu_outfit = AsyncMock(return_value={
                "accepted": True,
                "selected_index": 1,
                "quality_score": 92,
                "reason": "单人单套且头脚完整可见",
                "person_count": 1,
                "is_real_photo": True,
                "is_collage": False,
                "single_outfit": True,
                "full_body_visible": True,
                "clothing_clear": True,
                "quality_sufficient": True,
                "keyword_match": True,
            })
            client = await self._start_client(server)
            try:
                response = await client.post(
                    "/api/xiaohongshu/schedule-mode",
                    json={"enabled": True},
                )
                payload = await response.json()
                references_response = await client.get("/api/xiaohongshu/references")
                references = await references_response.json()
                disabled_response = await client.post(
                    "/api/xiaohongshu/schedule-mode",
                    json={"enabled": False},
                )
                disabled = await disabled_response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status, payload)
            self.assertTrue(payload["enabled"])
            self.assertEqual("ready", payload["status"])
            self.assertEqual("daily_schedule", payload["today_reference"]["scope"])
            self.assertEqual([], references)
            schedule_filename = payload["today_reference"]["filename"]
            schedule_path = root / "data" / "references" / "xiaohongshu" / schedule_filename
            indexed = server.xiaohongshu_reference_store.load()[schedule_filename]
            self.assertEqual("daily_schedule", indexed["scope"])
            server.xiaohongshu_client.search.assert_awaited_once_with(
                "清新通勤风通勤穿搭",
                max_results=10,
            )
            server.xiaohongshu_client.detail.assert_awaited_once_with("note-1", "token-1")
            self.assertEqual(
                "https://sns-webpic-qc.xhscdn.com/full-body.webp",
                server.xiaohongshu_client.import_image.await_args.args[0],
            )
            self.assertEqual(1, indexed["image_index"])
            self.assertEqual(92, indexed["validation_score"])
            server.on_validate_xiaohongshu_outfit.assert_awaited_once()
            self.assertEqual("disabled", disabled["status"])
            self.assertFalse(disabled["enabled"])
            self.assertEqual([], server._xiaohongshu_reference_filenames_for_paths([
                str(schedule_path)
            ]))
            self.assertFalse(server._delete_xiaohongshu_reference_file(schedule_filename))
            self.assertTrue(schedule_path.exists())
            self.assertFalse((root / "data" / "references" / "xiaohongshu" / "xhs_daily.png").exists())

    async def test_schedule_prefers_favorite_creator_profile_notes(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            root = Path(tmpdir)
            server = self._make_server(root)
            server._now = lambda: datetime(2026, 7, 30, 10, 0)
            server.xiaohongshu_schedule_store.save({
                "enabled": True,
                "prefer_creators": True,
            })
            server.xiaohongshu_creator_store.save({
                "creator-1": {
                    "nickname": "小雪穿搭",
                    "xsec_token": "profile-token",
                    "added_at": "2026-07-30T09:00:00",
                }
            })
            server.xiaohongshu_client.status = AsyncMock(return_value={
                "service_running": True,
                "is_logged_in": True,
            })
            server.xiaohongshu_client.profile = AsyncMock(return_value={
                "creator": {"user_id": "creator-1", "nickname": "小雪穿搭"},
                "notes": [{
                    "id": "favorite-note",
                    "xsec_token": "note-token",
                    "title": "法式温柔风全身穿搭",
                    "user_id": "creator-1",
                    "author": "小雪穿搭",
                    "cover_url": "https://sns-webpic-qc.xhscdn.com/cover.webp",
                    "width": 900,
                    "height": 1200,
                }],
            })
            server.xiaohongshu_client.search = AsyncMock(return_value=[])
            server.xiaohongshu_client.detail = AsyncMock(return_value={
                "id": "favorite-note",
                "title": "法式温柔风全身穿搭",
                "author": "小雪穿搭",
                "images": [
                    {"index": 0, "url": "https://sns-webpic-qc.xhscdn.com/cover.webp", "width": 900, "height": 1200},
                    {"index": 1, "url": "https://sns-webpic-qc.xhscdn.com/full.webp", "width": 900, "height": 1200},
                ],
            })

            async def fake_import(_url, output_dir):
                path = Path(output_dir) / "creator_full.png"
                Image.new("RGB", (900, 1200), "white").save(path)
                return {"filename": path.name, "path": str(path), "size_bytes": path.stat().st_size}

            server.xiaohongshu_client.import_image = AsyncMock(side_effect=fake_import)
            server.on_validate_xiaohongshu_outfit = AsyncMock(return_value={
                "accepted": True,
                "selected_index": 1,
                "quality_score": 94,
                "reason": "单人全身穿搭照",
                "person_count": 1,
                "is_real_photo": True,
                "is_collage": False,
                "single_outfit": True,
                "full_body_visible": True,
                "clothing_clear": True,
                "quality_sufficient": True,
                "keyword_match": True,
            })

            result = await server.ensure_xiaohongshu_schedule_reference(
                "2026-07-30",
                {"xiaohongshu_search_query": "法式温柔风"},
                force=True,
            )

            self.assertEqual("favorite_creator", result["selection_source"])
            self.assertEqual("creator-1", result["creator_id"])
            self.assertEqual("小雪穿搭", result["creator_name"])
            server.xiaohongshu_client.profile.assert_awaited_once_with(
                "creator-1", "profile-token"
            )
            server.xiaohongshu_client.search.assert_awaited_once_with(
                "法式温柔风穿搭", max_results=10
            )
            server.xiaohongshu_client.detail.assert_awaited_once_with(
                "favorite-note", "note-token"
            )
    async def test_schedule_mode_login_failure_keeps_enabled_for_runtime_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            root = Path(tmpdir)
            server = self._make_server(root)
            server._now = lambda: datetime(2026, 7, 30, 10, 0)
            ScheduleStore(str(root / "data")).save({
                "2026-07-30": {
                    "date": "2026-07-30",
                    "reference_query": "夏季通勤穿搭",
                }
            })
            server.xiaohongshu_client.status = AsyncMock(return_value={
                "service_running": True,
                "is_logged_in": False,
            })
            server.xiaohongshu_client.search = AsyncMock()
            client = await self._start_client(server)
            try:
                response = await client.post(
                    "/api/xiaohongshu/schedule-mode",
                    json={"enabled": True},
                )
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status, payload)
            self.assertTrue(payload["enabled"])
            self.assertEqual("error", payload["status"])
            self.assertIn("未登录", payload["last_error"])
            server.xiaohongshu_client.search.assert_not_called()

    async def test_schedule_mode_rejects_unqualified_inner_images_and_never_uses_cover(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            root = Path(tmpdir)
            server = self._make_server(root)
            server._now = lambda: datetime(2026, 7, 30, 10, 0)
            ScheduleStore(str(root / "data")).save({
                "2026-07-30": {
                    "date": "2026-07-30",
                    "outfit_style": "清新通勤风",
                    "schedule": "10:30 咖啡店办公",
                }
            })
            server.xiaohongshu_client.status = AsyncMock(return_value={
                "service_running": True,
                "is_logged_in": True,
            })
            cover_url = "https://sns-webpic-qc.xhscdn.com/collage-cover.webp"
            inner_url = "https://sns-webpic-qc.xhscdn.com/half-body.webp"
            server.xiaohongshu_client.search = AsyncMock(return_value=[{
                "id": "note-rejected",
                "xsec_token": "token-rejected",
                "title": "通勤穿搭合集",
                "author": "作者",
                "cover_url": cover_url,
                "width": 900,
                "height": 1200,
            }])
            server.xiaohongshu_client.detail = AsyncMock(return_value={
                "id": "note-rejected",
                "title": "通勤穿搭合集",
                "author": "作者",
                "images": [
                    {
                        "index": 0,
                        "url": "https://sns-webpic-qc.xhscdn.com/first-page.webp",
                        "width": 900,
                        "height": 1200,
                    },
                    {
                        "index": 1,
                        "url": "https://sns-webpic-qc.xhscdn.net/path/collage-cover.jpg?format=webp",
                        "width": 900,
                        "height": 1200,
                    },
                    {"index": 2, "url": inner_url, "width": 900, "height": 1200},
                ],
            })

            async def fake_import(url, output_dir):
                self.assertEqual(inner_url, url)
                path = Path(output_dir) / "xhs_rejected.png"
                Image.new("RGB", (900, 1200), "white").save(path)
                return {"filename": path.name, "path": str(path), "size_bytes": path.stat().st_size}

            server.xiaohongshu_client.import_image = AsyncMock(side_effect=fake_import)
            server.on_validate_xiaohongshu_outfit = AsyncMock(
                side_effect=RuntimeError("vision backend unavailable")
            )
            client = await self._start_client(server)
            try:
                response = await client.post(
                    "/api/xiaohongshu/schedule-mode",
                    json={"enabled": True},
                )
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status, payload)
            self.assertEqual("error", payload["status"])
            self.assertIn("全身", payload["last_error"])
            self.assertFalse((root / "data" / "references" / "xiaohongshu" / "xhs_rejected.png").exists())
            self.assertNotIn(
                cover_url,
                [call.args[0] for call in server.xiaohongshu_client.import_image.await_args_list],
            )

    async def test_schedule_mode_checks_all_inner_images_after_collage_cover(self):
        """A collage cover must not hide a later valid outfit photo in the same note."""
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            root = Path(tmpdir)
            server = self._make_server(root)
            server._now = lambda: datetime(2026, 7, 30, 10, 0)
            ScheduleStore(str(root / "data")).save({
                "2026-07-30": {
                    "date": "2026-07-30",
                    "xiaohongshu_search_query": "法式温柔风穿搭",
                }
            })
            server.xiaohongshu_client.status = AsyncMock(return_value={
                "service_running": True,
                "is_logged_in": True,
            })
            cover_url = "https://sns-webpic-qc.xhscdn.com/french-cover.webp"
            inner_urls = [
                f"https://sns-webpic-qc.xhscdn.com/french-inner-{index}.webp"
                for index in range(1, 7)
            ]
            server.xiaohongshu_client.search = AsyncMock(return_value=[{
                "id": "note-french",
                "xsec_token": "token-french",
                "title": "法式温柔风穿搭合集",
                "author": "作者",
                "cover_url": cover_url,
                "width": 900,
                "height": 1200,
            }])
            server.xiaohongshu_client.detail = AsyncMock(return_value={
                "id": "note-french",
                "title": "法式温柔风穿搭合集",
                "author": "作者",
                "images": [
                    {"index": 0, "url": cover_url, "width": 900, "height": 1200},
                    *[
                        {"index": index, "url": url, "width": 900, "height": 1200}
                        for index, url in enumerate(inner_urls, start=1)
                    ],
                ],
            })

            async def fake_import(url, output_dir):
                index = inner_urls.index(url) + 1
                path = Path(output_dir) / f"xhs_french_inner_{index}.png"
                Image.new("RGB", (900, 1200), "white").save(path)
                return {"filename": path.name, "path": str(path), "size_bytes": path.stat().st_size}

            server.xiaohongshu_client.import_image = AsyncMock(side_effect=fake_import)

            async def validate(_sheet, _query, candidate_count):
                if candidate_count == 4:
                    return {
                        "accepted": False,
                        "selected_index": 0,
                        "quality_score": 0,
                        "reason": "前四张没有合格全身照",
                    }
                return {
                    "accepted": True,
                    "selected_index": 2,
                    "quality_score": 93,
                    "reason": "第六张为单人单套全身照",
                    "person_count": 1,
                    "is_real_photo": True,
                    "is_collage": False,
                    "single_outfit": True,
                    "full_body_visible": True,
                    "clothing_clear": True,
                    "quality_sufficient": True,
                    "keyword_match": True,
                }

            server.on_validate_xiaohongshu_outfit = AsyncMock(side_effect=validate)
            client = await self._start_client(server)
            try:
                response = await client.post(
                    "/api/xiaohongshu/schedule-mode",
                    json={"enabled": True},
                )
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status, payload)
            self.assertEqual("ready", payload["status"])
            imported_urls = [call.args[0] for call in server.xiaohongshu_client.import_image.await_args_list]
            self.assertEqual(len(inner_urls), len(imported_urls))
            self.assertCountEqual(inner_urls, imported_urls)
            self.assertNotIn(cover_url, imported_urls)
            self.assertEqual(2, server.on_validate_xiaohongshu_outfit.await_count)

    async def test_schedule_mode_search_timeout_keeps_runtime_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            root = Path(tmpdir)
            server = self._make_server(root)
            server._now = lambda: datetime(2026, 7, 30, 10, 0)
            ScheduleStore(str(root / "data")).save({
                "2026-07-30": {
                    "date": "2026-07-30",
                    "reference_query": "夏季通勤穿搭",
                }
            })
            server.xiaohongshu_client.status = AsyncMock(return_value={
                "service_running": True,
                "is_logged_in": True,
            })
            server.xiaohongshu_client.search = AsyncMock(side_effect=XiaohongshuError(
                "timeout",
                "小红书请求超时，请稍后重试。",
            ))
            server.xiaohongshu_client.import_image = AsyncMock()
            client = await self._start_client(server)
            try:
                response = await client.post(
                    "/api/xiaohongshu/schedule-mode",
                    json={"enabled": True},
                )
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status, payload)
            self.assertTrue(payload["enabled"])
            self.assertEqual("error", payload["status"])
            self.assertIn("超时", payload["last_error"])
            server.xiaohongshu_client.search.assert_awaited_once_with(
                "通勤穿搭",
                max_results=10,
            )
            server.xiaohongshu_client.import_image.assert_not_called()

    async def test_schedule_selection_timeout_is_reported_without_starting_search(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            server._now = lambda: datetime(2026, 7, 30, 10, 0)
            server.xiaohongshu_schedule_store.update(
                lambda state: {**state, "enabled": True}
            )
            server.xiaohongshu_client.status = AsyncMock(side_effect=asyncio.TimeoutError())
            server.xiaohongshu_client.search = AsyncMock()

            result = await server.ensure_xiaohongshu_schedule_reference(
                "2026-07-30",
                {"xiaohongshu_search_query": "夏季通勤穿搭"},
                force=True,
            )
            state = server.xiaohongshu_schedule_state("2026-07-30")

            self.assertEqual({}, result)
            self.assertEqual("error", state["status"])
            self.assertIn("240 秒", state["last_error"])
            server.xiaohongshu_client.search.assert_not_called()

    def test_failed_refresh_keeps_old_reference_but_exposes_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            root = Path(tmpdir)
            server = self._make_server(root)
            filename = "xhs_schedule_20260730_old.png"
            path = root / "data" / "references" / "xiaohongshu" / filename
            Image.new("RGB", (900, 1200), "white").save(path)
            record = {
                "filename": filename,
                "scope": "daily_schedule",
                "schedule_date": "2026-07-30",
                "created_at": "2026-07-30T10:00:00",
                "title": "旧的合格穿搭",
            }
            server.xiaohongshu_reference_store.update(
                lambda records: {**records, filename: record}
            )
            server.xiaohongshu_schedule_store.update(lambda state: {
                **state,
                "enabled": True,
                "references": {"2026-07-30": record},
                "last_error": "本次刷新没有找到合格全身照",
                "last_error_at": "2026-07-30T10:05:00",
                "updated_at": "2026-07-30T10:05:00",
            })

            state = server.xiaohongshu_schedule_state("2026-07-30")

            self.assertEqual("stale", state["status"])
            self.assertEqual("旧的合格穿搭", state["today_reference"]["title"])
            self.assertIn("本次刷新", state["last_error"])

    async def test_delete_rejects_non_image_filename(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            server = self._make_server(Path(tmpdir))
            client = await self._start_client(server)
            try:
                response = await client.delete(
                    "/api/xiaohongshu/references/not-an-image.txt"
                )
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(400, response.status, payload)
            self.assertEqual("invalid_filename", payload["error"])

    async def test_generate_custom_auto_appends_face_only_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            root = Path(tmpdir)
            server = self._make_server(root)
            xhs_path = Path(server.xiaohongshu_reference_dir) / "xhs_outfit.webp"
            face_path = Path(server.app_reference_dir) / "reference_face_faceonly.jpg"
            Image.new("RGB", (32, 48), "white").save(xhs_path)
            Image.new("RGB", (48, 48), "white").save(face_path)
            server.xiaohongshu_reference_store.update(lambda records: {
                **records,
                xhs_path.name: {
                    "filename": xhs_path.name,
                    "label": "小红书 · 夏季穿搭",
                    "source": "xiaohongshu",
                },
            })
            server.on_generate_custom = AsyncMock(return_value=DailyEntry(
                date="2026-07-30",
                image_filename="generated.png",
                image_path="/images/generated.png",
                status="ok",
            ))
            client = await self._start_client(server)
            try:
                response = await client.post("/api/generate-custom", json={
                    "prompt": "生成一张自然的全身照",
                    "ref_image": "/local-refs/xiaohongshu/xhs_outfit.webp",
                    "source": "custom_ui",
                })
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status, payload)
            call = server.on_generate_custom.await_args
            self.assertEqual(str(xhs_path), call.args[2])
            self.assertEqual([str(xhs_path), str(face_path)], call.args[10])
            self.assertEqual("xiaohongshu", call.args[9]["source"])

    async def test_generate_custom_replaces_builtin_face_with_face_only_crop(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            root = Path(tmpdir)
            server = self._make_server(root)
            xhs_path = Path(server.xiaohongshu_reference_dir) / "xhs_outfit.webp"
            full_face_path = Path(server.app_reference_dir) / "reference_face.jpg"
            face_only_path = Path(server.app_reference_dir) / "reference_face_faceonly.jpg"
            Image.new("RGB", (32, 48), "white").save(xhs_path)
            Image.new("RGB", (64, 64), "white").save(full_face_path)
            Image.new("RGB", (48, 48), "white").save(face_only_path)
            server.xiaohongshu_reference_store.update(lambda records: {
                **records,
                xhs_path.name: {
                    "filename": xhs_path.name,
                    "label": "小红书 · 夏季穿搭",
                    "source": "xiaohongshu",
                },
            })
            server.on_generate_custom = AsyncMock(return_value=DailyEntry(
                date="2026-07-30",
                image_filename="generated.png",
                image_path="/images/generated.png",
                status="ok",
            ))
            client = await self._start_client(server)
            try:
                response = await client.post("/api/generate-custom", json={
                    "prompt": "生成一张自然的全身照",
                    "ref_image": "/local-refs/xiaohongshu/xhs_outfit.webp",
                    "ref_images": [
                        "/local-refs/xiaohongshu/xhs_outfit.webp",
                        "/refs/reference_face.jpg",
                    ],
                    "source": "custom_ui",
                })
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status, payload)
            call = server.on_generate_custom.await_args
            self.assertEqual([str(xhs_path), str(face_only_path)], call.args[10])

    async def test_custom_generation_cleans_temporary_xiaohongshu_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
            root = Path(tmpdir)
            server = self._make_server(root)

            async def fake_import(_url, output_dir):
                path = Path(output_dir) / "xhs_temporary.png"
                Image.new("RGB", (32, 48), "white").save(path)
                return {
                    "filename": path.name,
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                }

            server.xiaohongshu_client.import_image = AsyncMock(side_effect=fake_import)
            server.on_generate_custom = AsyncMock(return_value=DailyEntry(
                date="2026-07-30",
                image_filename="generated.png",
                image_path="/images/generated.png",
                status="ok",
            ))
            client = await self._start_client(server)
            try:
                import_response = await client.post("/api/xiaohongshu/import", json={
                    "url": "https://sns-webpic-qc.xhscdn.com/outfit.webp",
                    "title": "临时穿搭",
                })
                imported = await import_response.json()
                temporary_path = root / "data" / "references" / "xiaohongshu" / "xhs_temporary.png"
                self.assertTrue(temporary_path.exists())

                generate_response = await client.post("/api/generate-custom", json={
                    "prompt": "参考这套穿搭",
                    "ref_image": imported["url"],
                })
                generated = await generate_response.json()
                list_response = await client.get("/api/xiaohongshu/references")
                references = await list_response.json()
            finally:
                await client.close()

            self.assertEqual(200, generate_response.status, generated)
            self.assertEqual([], references)
            self.assertFalse(temporary_path.exists())
            server.on_generate_custom.assert_awaited_once()
            generated_args = server.on_generate_custom.await_args.args
            resolved_temporary_path = str(temporary_path.resolve())
            self.assertEqual(resolved_temporary_path, generated_args[2])
            self.assertEqual([resolved_temporary_path], generated_args[-1])


if __name__ == "__main__":
    unittest.main()
