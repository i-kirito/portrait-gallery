import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from web_server import GalleryServer  # noqa: E402


class DummyTransport:
    def __init__(self, peername=None):
        self.peername = peername

    def get_extra_info(self, name):
        if name == "peername":
            return self.peername
        return None


class DummyRequest:
    def __init__(self, host, remote="", peername=None, path="/api/gallery"):
        self.host = host
        self.remote = remote
        self.transport = DummyTransport(peername) if peername else None
        self.path = path


class WebServerAuthTest(unittest.TestCase):
    @staticmethod
    def _make_server():
        return GalleryServer.__new__(GalleryServer)

    def test_lan_host_and_lan_remote_are_not_local(self):
        request = DummyRequest("192.168.31.216:18889", "192.168.31.88")

        self.assertFalse(self._make_server()._is_local_request(request))

    def test_localhost_with_private_bridge_remote_is_local(self):
        request = DummyRequest("localhost:18889", "172.17.0.1")

        self.assertTrue(self._make_server()._is_local_request(request))

    def test_public_remote_cannot_become_local_by_spoofing_lan_host(self):
        request = DummyRequest("192.168.31.216:18889", "8.8.8.8")

        self.assertFalse(self._make_server()._is_local_request(request))

    def test_lan_remote_is_not_local_for_public_host(self):
        request = DummyRequest("gallery.example.com", "192.168.31.88")

        self.assertFalse(self._make_server()._is_local_request(request))

    def test_configured_lan_hostname_with_public_remote_is_not_local(self):
        request = DummyRequest("gallery.home.arpa:18889", "8.8.8.8")

        server = self._make_server()

        self.assertFalse(server._is_local_request(request))

    def test_configured_lan_hostname_with_lan_remote_is_not_local(self):
        request = DummyRequest("gallery.home.arpa:18889", "192.168.31.88")

        self.assertFalse(self._make_server()._is_local_request(request))

    def test_documentation_address_is_not_treated_as_trusted_lan(self):
        request = DummyRequest("203.0.113.10:18889", "203.0.113.11")

        self.assertFalse(self._make_server()._is_local_request(request))


class WebServerPasswordAuthTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_server(root: Path) -> GalleryServer:
        server = GalleryServer.__new__(GalleryServer)
        server.data_dir = str(root)
        server.auth_store_path = str(root / "gallery_auth.json")
        return server

    async def test_nonlocal_api_requires_password_setup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            request = DummyRequest("gallery.example.com", "203.0.113.8")

            async def handler(_request):
                return web.json_response({"ok": True})

            with patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
                response = await server.gallery_auth_middleware(request, handler)

            self.assertEqual(401, response.status)
            self.assertIn("password_setup_required", response.text)

    async def test_authorized_nonlocal_ip_can_access_protected_image(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            server._save_auth_store({"password_hash": server._hash_gallery_password("secret123")})
            request = DummyRequest("gallery.example.com", "203.0.113.8", path="/images/today.jpg")
            server._authorize_client_ip(request)

            async def handler(_request):
                return web.Response(text="ok")

            with patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
                response = await server.gallery_auth_middleware(request, handler)

            self.assertEqual(200, response.status)
            self.assertTrue(server._client_ip_authorized(request))

    def test_env_password_verifies_without_stored_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))

            with patch.dict(os.environ, {"GALLERY_PASSWORD": "secret123"}):
                self.assertEqual("env", server._configured_gallery_password_source())
                self.assertTrue(server._verify_gallery_password("secret123"))
                self.assertFalse(server._verify_gallery_password("wrong"))

    def test_local_setup_hash_authorizes_current_ip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            request = DummyRequest("localhost:18889", "127.0.0.1")

            with patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
                server._save_auth_store({"password_hash": server._hash_gallery_password("secret123")})
                server._authorize_client_ip(request)

                data = json.loads((Path(tmpdir) / "gallery_auth.json").read_text(encoding="utf-8"))
                self.assertIn("127.0.0.1", data.get("authorized_ips", {}))
                self.assertTrue(server._verify_gallery_password("secret123"))


class WebServerReferenceSelectionTest(unittest.TestCase):
    def _make_server(self, root: Path) -> GalleryServer:
        data_dir = root / "data"
        config_path = root / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("gallery:\n  port: 18889\n", encoding="utf-8")
        (root / "app" / "references").mkdir(parents=True, exist_ok=True)
        return GalleryServer(
            {"paths": {"project_root": str(root)}, "gallery": {"port": 18889}},
            str(data_dir),
            str(config_path),
        )

    def test_default_custom_reference_prefers_style_base_models(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ref_dir = root / "data" / "references"
            ref_dir.mkdir(parents=True, exist_ok=True)
            for filename in ("reference_face.jpg", "ref_style_girly.jpg", "ref_style_sweet.jpg"):
                (ref_dir / filename).write_bytes(b"placeholder")

            server = self._make_server(root)
            selected = server._select_default_custom_reference_sync()

            self.assertEqual("default", selected.get("source"))
            self.assertIn(selected.get("style"), {"cool", "girly", "sweet"})
            self.assertEqual("custom_default_style", selected.get("selection_mode"))
            self.assertTrue(Path(selected.get("path", "")).is_file())

    def test_default_custom_reference_falls_back_to_uploaded_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            upload_dir = root / "data" / "references" / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            (upload_dir / "manual_ref.jpg").write_bytes(b"placeholder")

            server = self._make_server(root)
            selected = server._select_default_custom_reference_sync()

            self.assertEqual("upload", selected.get("source"))
            self.assertEqual("manual_ref.jpg", selected.get("filename"))
            self.assertEqual("custom_random_reference", selected.get("selection_mode"))
            self.assertTrue(Path(selected.get("path", "")).is_file())

    def test_default_upload_styles_map_to_fixed_reference_names(self):
        expected = {
            "cool": "reference_face.jpg",
            "girly": "ref_style_girly.jpg",
            "sweet": "ref_style_sweet.jpg",
        }

        for style, filename in expected.items():
            with self.subTest(style=style):
                self.assertEqual(
                    filename,
                    GalleryServer._default_reference_upload_spec(style).get("filename"),
                )
        self.assertEqual({}, GalleryServer._default_reference_upload_spec("unknown"))


class WebServerImageFallbackSettingsTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_server(root: Path) -> GalleryServer:
        data_dir = root / "data"
        config_path = root / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("gallery:\n  port: 18889\n", encoding="utf-8")
        (root / "app" / "references").mkdir(parents=True, exist_ok=True)
        return GalleryServer(
            {"paths": {"project_root": str(root)}, "gallery": {"port": 18889}},
            str(data_dir),
            str(config_path),
        )

    async def test_chat_fallback_defaults_off_and_can_be_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                with patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
                    initial_response = await client.get("/api/config/keys")
                    initial = await initial_response.json()
                    save_response = await client.post(
                        "/api/config/keys",
                        json={"gpt_chat_fallback_enabled": True},
                    )
                    saved = await save_response.json()
                    current_response = await client.get("/api/config/keys")
                    current = await current_response.json()
            finally:
                await client.close()

            self.assertEqual(200, initial_response.status)
            self.assertFalse(initial.get("gpt_chat_fallback_enabled"))
            self.assertEqual(200, save_response.status)
            self.assertTrue(saved.get("success"))
            self.assertEqual(200, current_response.status)
            self.assertTrue(current.get("gpt_chat_fallback_enabled"))
            plugin = json.loads((root / "data" / "plugin_config.json").read_text(encoding="utf-8"))
            self.assertTrue(plugin.get("gpt_chat_fallback_enabled"))


class WebServerReferenceUploadTest(unittest.IsolatedAsyncioTestCase):
    def _make_server(self, root: Path) -> GalleryServer:
        data_dir = root / "data"
        config_path = root / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("gallery:\n  port: 18889\n", encoding="utf-8")
        (root / "app" / "references").mkdir(parents=True, exist_ok=True)
        return GalleryServer(
            {"paths": {"project_root": str(root)}, "gallery": {"port": 18889}},
            str(data_dir),
            str(config_path),
        )

    @staticmethod
    def _png_bytes() -> bytes:
        buffer = io.BytesIO()
        Image.new("RGBA", (12, 16), (255, 80, 140, 180)).save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    async def _start_client(server: GalleryServer) -> TestClient:
        test_server = TestServer(server.app)
        # aiohttp ignores runner options passed to TestServer.__init__.
        await test_server.start_server(access_log=None)
        return TestClient(test_server)

    async def _post_upload(self, server: GalleryServer, form: FormData) -> tuple[int, dict]:
        client = await self._start_client(server)
        try:
            with patch.dict(os.environ, {"GALLERY_PASSWORD": ""}):
                response = await client.post("/api/upload-ref", data=form)
                return response.status, await response.json()
        finally:
            await client.close()

    async def test_ref_list_omits_a_default_profile_when_its_file_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            profile_path = root / "data" / "reference_profiles.json"
            profile_path.write_text(json.dumps({
                "version": 1,
                "items": [{
                    "filename": "reference_face.jpg",
                    "url": "/local-refs/reference_face.jpg",
                    "label": "冷御风",
                    "style": "cool",
                    "source": "default",
                    "builtin": True,
                    "active": True,
                }],
            }), encoding="utf-8")
            client = await self._start_client(server)
            try:
                response = await client.get("/api/ref-list")
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status)
            self.assertEqual([], payload)

    async def test_style_upload_replaces_the_matching_builtin_reference(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            form = FormData()
            form.add_field(
                "file",
                self._png_bytes(),
                filename="new-girly.png",
                content_type="image/png",
            )
            form.add_field("style", "girly")

            with patch("web_server.analyze_reference_image") as analyze:
                status, payload = await self._post_upload(server, form)

            self.assertEqual(200, status)
            self.assertEqual("ref_style_girly.jpg", payload.get("filename"))
            self.assertEqual("girly", payload.get("style"))
            self.assertEqual("default", payload.get("source"))
            self.assertTrue(payload.get("builtin"))
            self.assertRegex(
                payload.get("url", ""),
                r"^/local-refs/ref_style_girly\.jpg\?v=\d+$",
            )
            self.assertGreater(payload.get("version", 0), 0)
            analyze.assert_not_called()

            target = root / "data" / "references" / "ref_style_girly.jpg"
            self.assertTrue(target.is_file())
            with Image.open(target) as image:
                self.assertEqual("JPEG", image.format)
                self.assertEqual((12, 16), image.size)

            client = await self._start_client(server)
            try:
                response = await client.get("/api/ref-list")
                refs = await response.json()
            finally:
                await client.close()
            ref = next(item for item in refs if item.get("style") == "girly")
            self.assertEqual(payload.get("url"), ref.get("url"))
            self.assertEqual(payload.get("version"), ref.get("version"))

    async def test_invalid_style_is_rejected_and_temp_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            form = FormData()
            form.add_field("style", "unknown")
            form.add_field(
                "file",
                self._png_bytes(),
                filename="unknown.png",
                content_type="image/png",
            )

            status, payload = await self._post_upload(server, form)

            self.assertEqual(400, status)
            self.assertEqual("invalid_style", payload.get("error"))
            upload_dir = root / "data" / "references" / "uploads"
            self.assertEqual([], list(upload_dir.glob(".reference_upload_*")))

    async def test_regular_reference_upload_keeps_the_upload_profile_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            form = FormData()
            form.add_field(
                "file",
                self._png_bytes(),
                filename="manual.png",
                content_type="image/png",
            )
            analysis = {
                "label": "手动参考",
                "prompt": "manual reference prompt",
                "tags": ["manual"],
                "analysis_status": "ok",
                "analysis_error": "",
            }

            with patch("web_server.analyze_reference_image", return_value=analysis) as analyze:
                status, payload = await self._post_upload(server, form)

            self.assertEqual(200, status)
            self.assertEqual("upload", payload.get("source"))
            self.assertFalse(payload.get("builtin"))
            self.assertEqual("手动参考", payload.get("label"))
            self.assertTrue((root / "data" / "references" / "uploads" / payload["filename"]).is_file())
            analyze.assert_called_once()


if __name__ == "__main__":
    unittest.main()
