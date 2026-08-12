import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from store import ImageMetadataStore  # noqa: E402
from web_server import GalleryServer  # noqa: E402


class VideoGenerationEndpointTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_server(root: Path) -> GalleryServer:
        config_path = root / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("gallery:\n  port: 18889\n", encoding="utf-8")
        (root / "app" / "references").mkdir(parents=True, exist_ok=True)
        return GalleryServer(
            {"paths": {"project_root": str(root)}, "gallery": {"port": 18889}},
            str(root / "data"),
            str(config_path),
        )

    @staticmethod
    def _register_image(server: GalleryServer, filename: str = "hermes-image.png") -> str:
        Image.new("RGB", (32, 48), (220, 210, 200)).save(
            Path(server.image_dir) / filename
        )
        ImageMetadataStore(server.data_dir).save(
            {
                filename: {
                    "source": "hermes_api",
                    "prompt": "portrait prompt",
                    "created_at": 1786454767,
                }
            }
        )
        return filename

    async def test_local_skill_video_registration_persists_and_serves_mp4(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            filename = self._register_image(server)
            source = root / "skill-output.mp4"
            source.write_bytes(b"valid-test-mp4")

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                with patch.object(
                    server,
                    "_allowed_video_import_path",
                    return_value=str(source),
                ), patch.object(
                    server,
                    "_probe_video_file",
                    return_value={
                        "duration": 6.04,
                        "width": 480,
                        "height": 720,
                        "codec": "h264",
                        "file_size_bytes": source.stat().st_size,
                    },
                ):
                    response = await client.post(
                        f"/api/images/{filename}/video/register",
                        json={
                            "source_path": str(source),
                            "prompt": "自然眨眼并看向镜头",
                            "resolution": "480p",
                            "aspect_ratio": "2:3",
                            "source": "hermes_skill",
                        },
                    )
                    payload = await response.json()
                video_response = await client.get(payload["video_url"])
                video_bytes = await video_response.read()
                status_response = await client.get(
                    f"/api/images/{filename}/video"
                )
                status_payload = await status_response.json()
            finally:
                await client.close()

            stored = ImageMetadataStore(server.data_dir).load()[filename]
            self.assertEqual(201, response.status)
            self.assertEqual("ready", payload["video_status"])
            self.assertEqual("hermes_skill", stored["video_source"])
            self.assertEqual("grok-imagine-video", stored["video_model"])
            self.assertEqual(200, video_response.status)
            self.assertEqual(b"valid-test-mp4", video_bytes)
            self.assertEqual(payload["video_filename"], status_payload["video_filename"])

    async def test_duplicate_video_generation_is_rejected_while_task_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            filename = self._register_image(server)
            started = asyncio.Event()
            release = asyncio.Event()

            async def blocked_generation(*_args, **_kwargs):
                started.set()
                await release.wait()

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                with patch.object(
                    server,
                    "_run_image_video_generation",
                    side_effect=blocked_generation,
                ):
                    first = await client.post(
                        f"/api/images/{filename}/video",
                        json={"prompt": "轻轻转头"},
                    )
                    await asyncio.wait_for(started.wait(), timeout=1)
                    second = await client.post(
                        f"/api/images/{filename}/video",
                        json={"prompt": "慢慢挥手"},
                    )
                    second_payload = await second.json()
                    release.set()
                    await asyncio.sleep(0)
            finally:
                release.set()
                await client.close()

            self.assertEqual(202, first.status)
            self.assertEqual(409, second.status)
            self.assertEqual("video_generating", second_payload["error"])

    async def test_restart_marks_in_progress_video_as_interrupted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "data"
            ImageMetadataStore(str(data_dir)).save(
                {
                    "hermes.png": {
                        "video_status": "generating",
                        "video_job_id": "job-1",
                    }
                }
            )

            server = self._make_server(root)
            stored = ImageMetadataStore(server.data_dir).load()["hermes.png"]

            self.assertEqual("error", stored["video_status"])
            self.assertIn("服务重启", stored["video_error"])


class VideoGenerationHelpersTest(unittest.TestCase):
    def test_grok_video_settings_follow_environment_local_dotenv_default_priority(self):
        server = GalleryServer.__new__(GalleryServer)
        dotenv_values = {
            "GROK2API_URL": ("https://dotenv.example", "/tmp/hermes.env"),
            "GROK_VIDEO_MODEL": ("dotenv-video-model", "/tmp/hermes.env"),
            "GROK_API_KEY": ("dotenv-secret", "/tmp/hermes.env"),
        }

        def dotenv_value(key):
            return dotenv_values.get(key, ("", ""))

        with patch.object(server, "_grok_video_dotenv_value", side_effect=dotenv_value), patch.dict(
            os.environ,
            {
                "GROK2API_URL": "",
                "GROK_VIDEO_MODEL": "",
                "GROK_API_KEY": "",
            },
            clear=False,
        ):
            dotenv_settings = server._effective_grok_video_settings(
                {
                    "grok_video_url": "https://local.example",
                    "grok_video_model": "local-video-model",
                    "grok_api_key": "local-secret",
                }
            )

        self.assertEqual("https://local.example", dotenv_settings["url"])
        self.assertEqual("local-video-model", dotenv_settings["model"])
        self.assertEqual("local-secret", dotenv_settings["api_key"])
        self.assertEqual("本机设置", dotenv_settings["url_source"])
        self.assertEqual("本机设置", dotenv_settings["model_source"])
        self.assertEqual("本机设置", dotenv_settings["key_source"])

        with patch.object(server, "_grok_video_dotenv_value", side_effect=dotenv_value), patch.dict(
            os.environ,
            {
                "GROK2API_URL": "https://env.example",
                "GROK_VIDEO_MODEL": "env-video-model",
                "GROK_API_KEY": "env-secret",
            },
            clear=False,
        ):
            env_settings = server._effective_grok_video_settings(
                {
                    "grok_video_url": "https://local.example",
                    "grok_video_model": "local-video-model",
                    "grok_api_key": "local-secret",
                }
            )

        self.assertEqual("https://env.example", env_settings["url"])
        self.assertEqual("env-video-model", env_settings["model"])
        self.assertEqual("env-secret", env_settings["api_key"])
        self.assertEqual("环境变量", env_settings["url_source"])
        self.assertEqual("环境变量", env_settings["model_source"])
        self.assertEqual("环境变量", env_settings["key_source"])

        with patch.object(server, "_grok_video_dotenv_value", side_effect=dotenv_value), patch.dict(
            os.environ,
            {
                "GROK2API_URL": "",
                "GROK_VIDEO_MODEL": "",
                "GROK_API_KEY": "",
            },
            clear=False,
        ):
            dotenv_only = server._effective_grok_video_settings({})

        self.assertEqual("https://dotenv.example", dotenv_only["url"])
        self.assertEqual("dotenv-video-model", dotenv_only["model"])
        self.assertEqual("dotenv-secret", dotenv_only["api_key"])
        self.assertEqual("Hermes/OpenClaw .env", dotenv_only["url_source"])
        self.assertEqual("Hermes/OpenClaw .env", dotenv_only["model_source"])
        self.assertEqual("Hermes/OpenClaw .env", dotenv_only["key_source"])

    def test_grok_environment_uses_existing_process_secret_without_logging_it(self):
        server = GalleryServer.__new__(GalleryServer)
        server._child_env = lambda extra: dict(extra)
        with patch.dict(os.environ, {"GROK_API_KEY": "secret-value"}, clear=False):
            env = server._grok_video_environment()

        self.assertEqual("secret-value", env["GROK_API_KEY"])
        self.assertEqual("http://127.0.0.1:8100", env["GROK2API_URL"])
        self.assertEqual("grok-imagine-video", env["GROK_VIDEO_MODEL"])

    def test_hermes_api_logs_are_categorized_and_translated(self):
        text = "\n".join(
            [
                "2026-08-11 12:00:00,000 [INFO] web_server: "
                "Hermes image API call started: request=abcdef123456 mode=text-to-image "
                "engine=gptimage size=1536x2048 prompt_chars=42",
                "2026-08-11 12:00:01,000 [INFO] web_server: "
                "Hermes video API call started: request=9876543210 image=hermes.png "
                "route=http://127.0.0.1:8100 model=grok-imagine-video "
                "prompt_chars=12 duration=6 resolution=480p aspect_ratio=auto",
            ]
        )

        payload = GalleryServer._format_level_logs(text, max_items=20)

        self.assertEqual(2, len(payload["entries"]))
        self.assertTrue(all(item["category"] == "hermes_api" for item in payload["entries"]))
        self.assertTrue(all(item["category_label"] == "Hermes API" for item in payload["entries"]))
        self.assertIn("已接收", payload["entries"][0]["message"])
        self.assertIn("本机 8100 Grok", payload["entries"][1]["message"])

    def test_hermes_api_rejections_keep_request_id_and_actionable_reason(self):
        text = "\n".join(
            [
                "2026-08-11 12:01:00,000 [WARNING] web_server: "
                "Hermes image API call rejected: request=prompt0001 mode=text-to-image "
                "stage=validation error=prompt_required",
                "2026-08-11 12:01:01,000 [WARNING] web_server: "
                "Hermes image API call rejected: request=engine002 mode=text-to-image "
                "stage=validation error=invalid_engine",
            ]
        )

        payload = GalleryServer._format_level_logs(text, max_items=20)

        self.assertEqual(2, len(payload["entries"]))
        prompt_entry, engine_entry = payload["entries"]
        self.assertIn("请求 prompt00", prompt_entry["message"])
        self.assertIn("阶段 参数校验", prompt_entry["message"])
        self.assertIn("缺少 prompt", prompt_entry["message"])
        self.assertIn("请求 engine00", engine_entry["message"])
        self.assertIn("仅支持 gptimage 或 gitee", engine_entry["message"])
        self.assertNotEqual(prompt_entry["message"], engine_entry["message"])

    def test_hermes_video_failure_exposes_8100_auth_diagnosis(self):
        message = GalleryServer._translate_log_message(
            "Hermes video API call failed: request=video123456 image=hermes.png "
            "stage=submit error=HTTP 401 Unauthorized",
            "ERROR",
            "web_server",
        )

        self.assertIn("请求 video123", message)
        self.assertIn("阶段 提交 8100 任务", message)
        self.assertIn("鉴权失败", message)

    def test_frontend_contains_video_entry_player_and_hermes_log_filter(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="modalVideo"', html)
        self.assertIn("generateVideoFromModal(event)", html)
        self.assertIn("<video controls playsinline", html)
        skill_script = (
            Path.home()
            / ".hermes"
            / "workspace"
            / "skills"
            / "grok-video-gen"
            / "scripts"
            / "generate.py"
        ).read_text(encoding="utf-8")
        self.assertIn("register_gallery_video(", skill_script)
        self.assertIn('"/register"', skill_script)
        self.assertIn("GALLERY_VIDEO_URL:", skill_script)
        self.assertIn('data-log-filter="hermes"', html)
        self.assertIn("item.category === 'hermes_api'", html)
        self.assertIn('id="skLlmGroupTitle"', html)
        self.assertIn('id="skImageGroupTitle"', html)
        self.assertIn('id="skVideoGroupTitle"', html)
        self.assertIn('id="skGrokVideoUrl"', html)
        self.assertIn('id="skGrokVideoModel"', html)
        self.assertIn('id="skGrokApiKey"', html)
        self.assertIn('class="sk-gpt-option-grid"', html)
        self.assertGreaterEqual(html.count("sk-engine-grid--three"), 3)
        self.assertIn('class="sk-field sk-engine-option-field"', html)
        self.assertIn('id="skGptChatFallbackRow"', html)
        self.assertIn('id="skGptPromptCompactRow"', html)
        self.assertIn("body.grok_video_url", html)
        self.assertIn("body.grok_video_model", html)


class VideoConfigurationEndpointTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_server(root: Path) -> GalleryServer:
        config_path = root / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("gallery:\n  port: 18889\n", encoding="utf-8")
        (root / "app" / "references").mkdir(parents=True, exist_ok=True)
        return GalleryServer(
            {"paths": {"project_root": str(root)}, "gallery": {"port": 18889}},
            str(root / "data"),
            str(config_path),
        )

    async def test_get_and_save_video_settings_without_required_image_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                with patch.object(server, "_grok_video_dotenv_value", return_value=("", "")), patch.dict(
                    os.environ,
                    {
                        "GALLERY_PASSWORD": "",
                        "GROK2API_URL": "",
                        "GROK_VIDEO_MODEL": "",
                        "GROK_API_KEY": "",
                    },
                    clear=False,
                ):
                    initial_response = await client.get("/api/config/keys")
                    initial = await initial_response.json()
                    save_response = await client.post(
                        "/api/config/keys",
                        json={
                            "grok_video_url": "https://video.example/v1",
                            "grok_video_model": "grok-imagine-video",
                            "grok_api_key": "video-secret",
                        },
                    )
                    saved = await save_response.json()
                    current_response = await client.get("/api/config/keys")
                    current = await current_response.json()
            finally:
                await client.close()

            self.assertEqual(200, initial_response.status)
            self.assertEqual("http://127.0.0.1:8100", initial["grok_video_url"])
            self.assertEqual("grok-imagine-video", initial["grok_video_model"])
            self.assertFalse(initial["grok_video_configured"])
            self.assertEqual(200, save_response.status, saved)
            self.assertTrue(saved.get("success"))
            self.assertEqual(200, current_response.status)
            self.assertEqual("https://video.example", current["grok_video_url"])
            self.assertEqual("grok-imagine-video", current["grok_video_model"])
            self.assertTrue(current["grok_video_configured"])
            self.assertTrue(current["grok_api_key"])
            self.assertNotEqual("video-secret", current["grok_api_key"])

            stored = json.loads(
                (Path(tmpdir) / "data" / "api_keys_config.json").read_text(encoding="utf-8")
            )
            self.assertEqual("https://video.example", stored["grok_video_url"])
            self.assertEqual("grok-imagine-video", stored["grok_video_model"])
            self.assertEqual("video-secret", stored["grok_api_key"])


if __name__ == "__main__":
    unittest.main()
