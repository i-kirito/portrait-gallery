import os
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from web_server import GalleryServer


class GalleryHtmlCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "index.html").write_text("<!doctype html><title>current</title>")
        (root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        app = web.Application()
        app.on_response_prepare.append(GalleryServer._set_html_cache_headers)
        server = GalleryServer.__new__(GalleryServer)
        app.router.add_get("/", server.handle_index)
        app.router.add_static("/static/", root)

        async def api(_request):
            return web.json_response({"status": "ok"})

        app.router.add_get("/api/health", api)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_index_must_revalidate(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-cache, private")
        self.assertIn("qwen_reference_required", await response.text())

    async def test_static_html_alias_must_revalidate(self):
        response = await self.client.get("/static/index.html")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-cache, private")
        await response.read()

    async def test_conditional_html_also_revalidates(self):
        for route in ("/", "/static/index.html"):
            with self.subTest(route=route):
                response = await self.client.get(route)
                etag = response.headers["ETag"]
                await response.read()
                unchanged = await self.client.get(route, headers={"If-None-Match": etag})
                self.assertEqual(unchanged.status, 304)
                self.assertEqual(unchanged.headers["Cache-Control"], "no-cache, private")
                await unchanged.read()

    async def test_head_also_revalidates(self):
        response = await self.client.head("/")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-cache, private")
        self.assertEqual(await response.read(), b"")

    async def test_image_cache_policy_is_unchanged(self):
        response = await self.client.get("/static/image.png")
        self.assertEqual(response.status, 200)
        self.assertNotIn("Cache-Control", response.headers)
        await response.read()

    async def test_json_cache_policy_is_unchanged(self):
        response = await self.client.get("/api/health")
        self.assertEqual(response.status, 200)
        self.assertNotIn("Cache-Control", response.headers)
        self.assertEqual(await response.json(), {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
