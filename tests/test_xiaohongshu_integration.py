import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from web_server import GalleryServer  # noqa: E402
from xiaohongshu_client import XiaohongshuClient, XiaohongshuError  # noqa: E402
from data import DailyEntry  # noqa: E402


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
                        "user": {"nickname": "作者"},
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
        client._request.assert_awaited_once_with(
            "POST",
            "/api/v1/feeds/search",
            json_body={
                "keyword": "通勤",
                "max_results": 30,
                "filters": {"note_type": "图文"},
            },
            timeout_seconds=90,
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
