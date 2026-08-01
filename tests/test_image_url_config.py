import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from settings import DEFAULT_GITEE_IMAGE_URL  # noqa: E402
from web_server import GalleryServer  # noqa: E402
from zhuzhu import core as zhuzhu_core  # noqa: E402


class ImageUrlConfigTest(unittest.IsolatedAsyncioTestCase):
    GPT_URL = "https://gpt-image.example/v1"
    CPA_URL = "https://llm.example/v1"

    @classmethod
    def _make_server(cls, root: Path, gitee_url: str = "") -> GalleryServer:
        data_dir = root / "data"
        config_path = root / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("gallery:\n  port: 18889\n", encoding="utf-8")
        (root / "app" / "references").mkdir(parents=True, exist_ok=True)
        config = {
            "paths": {"project_root": str(root)},
            "gallery": {"port": 18889},
            "image_gen": {
                "gitee_url": gitee_url,
                "gpt_base_url": cls.GPT_URL,
            },
            "llm": {"base_url": cls.CPA_URL},
        }
        return GalleryServer(config, str(data_dir), str(config_path))

    async def test_default_urls_validate_without_local_overrides_or_github_proxy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            data_dir = root / "data"
            (data_dir / "api_keys_config.json").write_text(
                json.dumps({"gpt_key": "gpt-secret", "cpa_key": "cpa-secret"}),
                encoding="utf-8",
            )
            (data_dir / "plugin_config.json").write_text(
                json.dumps({"gitee_config": {"api_keys": ["gitee-secret"]}}),
                encoding="utf-8",
            )

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                with patch.dict(os.environ, {
                    "GALLERY_PASSWORD": "",
                    "GITEE_API_URL": "",
                    "GPT_IMAGE_BASE_URL": "",
                    "CPA_BASE_URL": "",
                }):
                    response = await client.post("/api/config/keys", json={
                        "validate_required_config": True,
                        "gitee_url": DEFAULT_GITEE_IMAGE_URL,
                        "gpt_base_url": self.GPT_URL,
                        "cpa_url": self.CPA_URL,
                        "github_proxy": "",
                    })
                    payload = await response.json()
                    current_response = await client.get("/api/config/keys")
                    current = await current_response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status, payload)
            self.assertTrue(payload.get("success"))
            self.assertEqual(200, current_response.status)
            self.assertEqual(DEFAULT_GITEE_IMAGE_URL, current.get("gitee_url"))
            self.assertEqual(self.GPT_URL, current.get("gpt_base_url"))

            stored = json.loads((data_dir / "api_keys_config.json").read_text(encoding="utf-8"))
            self.assertNotIn("gitee_url", stored)
            self.assertNotIn("gpt_base_url", stored)
            self.assertNotIn("cpa_url", stored)
            self.assertEqual("", stored.get("github_proxy"))

    async def test_config_write_failure_returns_500_without_false_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            data_dir = root / "data"
            original_keys = {"cpa_key": "keep-me"}
            (data_dir / "api_keys_config.json").write_text(
                json.dumps(original_keys),
                encoding="utf-8",
            )

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                with patch.object(
                    server,
                    "_atomic_replace_text_files",
                    side_effect=OSError("read-only config"),
                ):
                    response = await client.post("/api/config/keys", json={
                        "llm_model": "new-model",
                        "llm_models": ["new-model"],
                    })
                    payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(500, response.status)
            self.assertNotIn("success", payload)
            self.assertIn("read-only config", payload.get("error", ""))
            stored = json.loads(
                (data_dir / "api_keys_config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(original_keys, stored)
            self.assertNotEqual("new-model", server.config.get("llm", {}).get("model"))

    async def test_llm_stream_switch_persists_without_clearing_models(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            server.config["llm"].update({
                "model": "grok-4.5",
                "models": ["grok-4.5", "fallback-model"],
            })
            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                response = await client.post("/api/config/keys", json={
                    "llm_stream_enabled": True,
                })
                payload = await response.json()
                current_response = await client.get("/api/config/keys")
                current = await current_response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status, payload)
            self.assertTrue(payload.get("success"))
            self.assertTrue(current.get("llm_stream_enabled"))
            self.assertEqual(
                ["grok-4.5", "fallback-model"],
                server.config["llm"]["models"],
            )
            runtime = json.loads(
                (root / "data" / "runtime_config.json").read_text(encoding="utf-8")
            )
            self.assertTrue(runtime["llm"]["stream"])

    async def test_gitee_environment_overrides_local_url_and_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root, gitee_url="")
            local_plugin = {"gitee_config": {"api_keys": ["local-key"]}}

            with patch.dict(os.environ, {
                "GITEE_API_URL": "https://env.example/images/generations",
                "GITEE_API_KEY": "env-key",
            }):
                self.assertEqual(
                    "https://env.example/images/generations",
                    server._effective_gitee_image_url({
                        "gitee_url": "https://local.example/images/generations",
                    }),
                )
                self.assertEqual(
                    "env-key",
                    server._effective_gitee_api_key(local_plugin),
                )

            with patch.dict(os.environ, {"GITEE_API_URL": "", "GITEE_API_KEY": ""}):
                self.assertEqual(
                    "https://local.example/images/generations",
                    server._effective_gitee_image_url({
                        "gitee_url": "https://local.example/images/generations",
                    }),
                )
                self.assertEqual("local-key", server._effective_gitee_api_key(local_plugin))
                self.assertEqual(DEFAULT_GITEE_IMAGE_URL, server._effective_gitee_image_url({}))

    async def test_gitee_generator_keeps_default_when_legacy_yaml_url_is_blank(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "missing-api-keys.json"
            with (
                patch.object(zhuzhu_core, "_API_KEYS_CONFIG_PATH", str(config_path)),
                patch.object(zhuzhu_core, "_GALLERY_CONFIG", {"image_gen": {"gitee_url": ""}}),
                patch.dict(os.environ, {"GITEE_API_URL": ""}),
            ):
                self.assertEqual(DEFAULT_GITEE_IMAGE_URL, zhuzhu_core.get_image_model("gitee_url"))


if __name__ == "__main__":
    unittest.main()
