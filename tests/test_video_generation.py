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


    async def test_new_video_archives_previous_instead_of_deleting(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            filename = self._register_image(server)

            first = root / "first.mp4"
            second = root / "second.mp4"
            first.write_bytes(b"first-video")
            second.write_bytes(b"second-video")

            async def register(client, source, prompt, resolution, duration):
                with patch.object(
                    server,
                    "_allowed_video_import_path",
                    return_value=str(source),
                ), patch.object(
                    server,
                    "_probe_video_file",
                    return_value={
                        "duration": duration,
                        "width": 720 if resolution == "720p" else 480,
                        "height": 1280 if resolution == "720p" else 720,
                        "codec": "h264",
                        "file_size_bytes": source.stat().st_size,
                    },
                ):
                    response = await client.post(
                        f"/api/images/{filename}/video/register",
                        json={
                            "source_path": str(source),
                            "prompt": prompt,
                            "resolution": resolution,
                            "aspect_ratio": "auto",
                            "source": "hermes_skill",
                        },
                    )
                    return response, await response.json()

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                first_response, first_payload = await register(
                    client, first, "第一次", "480p", 5.0
                )
                second_response, second_payload = await register(
                    client, second, "第二次", "720p", 10.0
                )
                status_response = await client.get(f"/api/images/{filename}/video")
                status_payload = await status_response.json()
                first_video_response = await client.get(first_payload["video_url"])
                first_video_bytes = await first_video_response.read()
                second_video_response = await client.get(second_payload["video_url"])
                second_video_bytes = await second_video_response.read()
            finally:
                await client.close()

            self.assertEqual(201, first_response.status, first_payload)
            self.assertEqual(201, second_response.status, second_payload)
            self.assertEqual("ready", second_payload["video_status"])
            self.assertEqual(second_payload["video_filename"], status_payload["video_filename"])
            self.assertEqual(1, status_payload["video_history_count"])
            self.assertTrue(status_payload["has_video_history"])
            self.assertEqual(
                first_payload["video_filename"],
                status_payload["video_history"][0]["video_filename"],
            )
            self.assertEqual("第一次", status_payload["video_history"][0]["video_prompt"])
            self.assertEqual(200, first_video_response.status)
            self.assertEqual(b"first-video", first_video_bytes)
            self.assertEqual(200, second_video_response.status)
            self.assertEqual(b"second-video", second_video_bytes)

            stored = ImageMetadataStore(server.data_dir).load()[filename]
            self.assertEqual(1, len(stored.get("video_history") or []))
            self.assertEqual(first_payload["video_filename"], stored["video_history"][0]["video_filename"])

    async def test_video_generation_retries_retryable_failures(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            filename = self._register_image(server)
            script = root / "fake_generate.py"
            script.write_text("# fake\n", encoding="utf-8")
            calls = {"count": 0}
            states = []
            output_path = {"value": ""}

            class FakeFailProc:
                returncode = 1
                async def communicate(self):
                    return (
                        b"",
                        b"API Error: video task failed: internal_error 429 Too many requests",
                    )
                def kill(self):
                    return None
                async def wait(self):
                    return 1

            class FakeOkProc:
                returncode = 0
                async def communicate(self):
                    Path(output_path["value"]).write_bytes(b"ok-mp4")
                    return b"ok", b""
                def kill(self):
                    return None
                async def wait(self):
                    return 0

            async def fake_exec(*args, **kwargs):
                calls["count"] += 1
                args_list = list(args)
                if "--output" in args_list:
                    output_path["value"] = args_list[args_list.index("--output") + 1]
                if calls["count"] == 1:
                    return FakeFailProc()
                return FakeOkProc()

            original_set = server._set_video_metadata

            def tracking_set(img_id, values):
                states.append(dict(values))
                return original_set(img_id, values)

            async def immediate_sleep(_delay):
                return None

            async def immediate_wait_for(coro, timeout=None):
                return await coro

            with patch.object(server, "_grok_video_script", return_value=str(script)), patch.object(
                server,
                "_effective_grok_video_settings",
                return_value={
                    "url": "http://127.0.0.1:8100",
                    "model": "grok-imagine-video",
                    "api_key": "secret",
                },
            ), patch.object(
                server,
                "_grok_video_environment",
                return_value={"GROK_API_KEY": "secret"},
            ), patch.object(
                server,
                "_python_executable",
                return_value="python3",
            ), patch.object(
                server,
                "_set_video_metadata",
                side_effect=tracking_set,
            ), patch.object(
                server,
                "_store_ready_video",
                return_value={
                    "video_filename": "ok.mp4",
                    "video_duration": 5.0,
                    "video_file_size_bytes": 12,
                },
            ), patch(
                "web_server.asyncio.create_subprocess_exec",
                side_effect=fake_exec,
            ), patch(
                "web_server.asyncio.sleep",
                side_effect=immediate_sleep,
            ), patch(
                "web_server.asyncio.wait_for",
                side_effect=immediate_wait_for,
            ):
                await server._run_image_video_generation(
                    filename,
                    "轻微点头",
                    duration=5,
                    resolution="480p",
                    aspect_ratio="auto",
                    job_id="job-retry-1",
                )

            self.assertEqual(2, calls["count"])
            self.assertTrue(any("自动重试" in str(item.get("video_error") or "") for item in states))



class VideoGenerationHelpersTest(unittest.TestCase):
    def test_grok_video_settings_follow_environment_local_dotenv_default_priority(self):
        server = GalleryServer.__new__(GalleryServer)
        dotenv_values = {
            "GROK2API_URL": ("https://dotenv.example", "/tmp/hermes.env"),
            "GROK_VIDEO_MODEL": ("dotenv-video-model", "/tmp/hermes.env"),
            "GROK_VIDEO_DURATION": ("10", "/tmp/hermes.env"),
            "GROK_VIDEO_RESOLUTION": ("720p", "/tmp/hermes.env"),
            "GROK_API_KEY": ("dotenv-secret", "/tmp/hermes.env"),
        }

        def dotenv_value(key):
            return dotenv_values.get(key, ("", ""))

        with patch.object(server, "_grok_video_dotenv_value", side_effect=dotenv_value), patch.dict(
            os.environ,
            {
                "GROK2API_URL": "",
                "GROK_VIDEO_MODEL": "",
                "GROK_VIDEO_DURATION": "",
                "GROK_VIDEO_RESOLUTION": "",
                "GROK_API_KEY": "",
            },
            clear=False,
        ):
            dotenv_settings = server._effective_grok_video_settings(
                {
                    "grok_video_url": "https://local.example",
                    "grok_video_model": "local-video-model",
                    "grok_video_duration": 10,
                    "grok_video_resolution": "480p",
                    "grok_api_key": "local-secret",
                }
            )

        self.assertEqual("https://local.example", dotenv_settings["url"])
        self.assertEqual("local-video-model", dotenv_settings["model"])
        self.assertEqual(10, dotenv_settings["duration"])
        self.assertEqual("480p", dotenv_settings["resolution"])
        self.assertEqual("local-secret", dotenv_settings["api_key"])
        self.assertEqual("本机设置", dotenv_settings["url_source"])
        self.assertEqual("本机设置", dotenv_settings["model_source"])
        self.assertEqual("本机设置", dotenv_settings["duration_source"])
        self.assertEqual("本机设置", dotenv_settings["resolution_source"])
        self.assertEqual("本机设置", dotenv_settings["key_source"])

        with patch.object(server, "_grok_video_dotenv_value", side_effect=dotenv_value), patch.dict(
            os.environ,
            {
                "GROK2API_URL": "https://env.example",
                "GROK_VIDEO_MODEL": "env-video-model",
                "GROK_VIDEO_DURATION": "15",
                "GROK_VIDEO_RESOLUTION": "720p",
                "GROK_API_KEY": "env-secret",
            },
            clear=False,
        ):
            env_settings = server._effective_grok_video_settings(
                {
                    "grok_video_url": "https://local.example",
                    "grok_video_model": "local-video-model",
                    "grok_video_duration": 10,
                    "grok_video_resolution": "480p",
                    "grok_api_key": "local-secret",
                }
            )

        self.assertEqual("https://env.example", env_settings["url"])
        self.assertEqual("env-video-model", env_settings["model"])
        self.assertEqual(15, env_settings["duration"])
        self.assertEqual("720p", env_settings["resolution"])
        self.assertEqual("env-secret", env_settings["api_key"])
        self.assertEqual("环境变量", env_settings["url_source"])
        self.assertEqual("环境变量", env_settings["model_source"])
        self.assertEqual("环境变量", env_settings["duration_source"])
        self.assertEqual("环境变量", env_settings["resolution_source"])
        self.assertEqual("环境变量", env_settings["key_source"])

        with patch.object(server, "_grok_video_dotenv_value", side_effect=dotenv_value), patch.dict(
            os.environ,
            {
                "GROK2API_URL": "",
                "GROK_VIDEO_MODEL": "",
                "GROK_VIDEO_DURATION": "",
                "GROK_VIDEO_RESOLUTION": "",
                "GROK_API_KEY": "",
            },
            clear=False,
        ):
            dotenv_only = server._effective_grok_video_settings({})

        self.assertEqual("https://dotenv.example", dotenv_only["url"])
        self.assertEqual("dotenv-video-model", dotenv_only["model"])
        self.assertEqual(10, dotenv_only["duration"])
        self.assertEqual("720p", dotenv_only["resolution"])
        self.assertEqual("dotenv-secret", dotenv_only["api_key"])
        self.assertEqual("Hermes/OpenClaw .env", dotenv_only["url_source"])
        self.assertEqual("Hermes/OpenClaw .env", dotenv_only["model_source"])
        self.assertEqual("Hermes/OpenClaw .env", dotenv_only["duration_source"])
        self.assertEqual("Hermes/OpenClaw .env", dotenv_only["resolution_source"])
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



    def test_user_facing_video_error_hides_raw_api_payload(self):
        raw = (
            "API Error: video task failed: {'error': {'code': 'internal_error', "
            "'message': 'Console 媒体上游返回 429: Too many requests for team ...'}}"
        )
        message = GalleryServer._user_facing_video_error(raw)
        self.assertNotIn("API Error", message)
        self.assertNotIn("internal_error", message)
        self.assertNotIn("team", message)
        self.assertTrue("频繁" in message or "稍后" in message)

    def test_retryable_video_errors_cover_rate_limit_and_internal(self):
        self.assertTrue(
            GalleryServer._is_retryable_video_error(
                "API Error: video task failed: {'error': {'code': 'internal_error', "
                "'message': 'Console 媒体上游返回 429: Too many requests'}}"
            )
        )
        self.assertTrue(GalleryServer._is_retryable_video_error("Timed out waiting for video task."))
        self.assertTrue(GalleryServer._is_retryable_video_error("API Error: HTTP 503: upstream"))
        self.assertFalse(GalleryServer._is_retryable_video_error("API Error: HTTP 401 Unauthorized"))
        self.assertFalse(GalleryServer._is_retryable_video_error("Input Error: image missing"))
        self.assertGreaterEqual(GalleryServer._video_retry_delay_seconds(0, "429 too many requests"), 8)

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
        self.assertIn('id="videoPlayCardPlayer" controls playsinline', html)
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
        self.assertNotIn('id="skGrokApiKey"', html)
        self.assertNotIn('Grok API Key', html)
        self.assertIn('id="skGrokVideoDuration"', html)
        self.assertIn('id="skGrokVideoResolution"', html)
        self.assertIn("body.grok_video_duration", html)
        self.assertIn("body.grok_video_resolution", html)
        self.assertIn("cachedGrokVideoDefaults", html)
        self.assertIn("modal-video-history", html)
        self.assertIn("modal-video-stage", html)
        self.assertIn("modal-video-history-empty", html)
        self.assertIn("aspect-ratio:3/4", html)
        self.assertIn("openVideoHistory", html)
        self.assertIn("videoHistoryOverlay", html)
        self.assertIn("modal-video-side", html)
        self.assertIn(">重新生成</span>", html)
        self.assertIn("118px", html)
        self.assertIn("playModalHistoryVideo", html)
        self.assertIn("entry.video_error", html)
        self.assertIn("formatModalVideoMessage", html)
        self.assertIn("height:220px", html)
        self.assertIn("video-play-overlay", html)
        self.assertIn("playModalVideoPreview", html)
        self.assertIn("modal-video-player-preview", html)
        self.assertIn("object-fit:contain", html)
        self.assertIn("object-fit:cover", html)
        self.assertIn("align-items:start", html)
        self.assertIn("GROK_VIDEO_DURATION_OPTIONS = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]", html)
        self.assertIn("可选 5-15 秒", html)
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
                        "GROK_VIDEO_DURATION": "",
                        "GROK_VIDEO_RESOLUTION": "",
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
                            "grok_video_duration": 10,
                            "grok_video_resolution": "720p",
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
            self.assertEqual(5, initial["grok_video_duration"])
            self.assertEqual("720p", initial["grok_video_resolution"])
            self.assertEqual(list(range(5, 16)), initial["grok_video_duration_options"])
            self.assertEqual(["480p", "720p"], initial["grok_video_resolution_options"])
            self.assertTrue(initial["grok_video_configured"])
            self.assertFalse(initial["grok_video_api_key_configured"])
            self.assertEqual(200, save_response.status, saved)
            self.assertTrue(saved.get("success"))
            self.assertEqual(200, current_response.status)
            self.assertEqual("https://video.example", current["grok_video_url"])
            self.assertEqual("grok-imagine-video", current["grok_video_model"])
            self.assertEqual(10, current["grok_video_duration"])
            self.assertEqual("720p", current["grok_video_resolution"])
            self.assertEqual("本机设置", current["grok_video_duration_source"])
            self.assertEqual("本机设置", current["grok_video_resolution_source"])
            self.assertTrue(current["grok_video_configured"])
            self.assertTrue(current["grok_video_api_key_configured"])
            self.assertTrue(current["grok_api_key"])
            self.assertNotEqual("video-secret", current["grok_api_key"])

            stored = json.loads(
                (Path(tmpdir) / "data" / "api_keys_config.json").read_text(encoding="utf-8")
            )
            self.assertEqual("https://video.example", stored["grok_video_url"])
            self.assertEqual("grok-imagine-video", stored["grok_video_model"])
            self.assertEqual(10, stored["grok_video_duration"])
            self.assertEqual("720p", stored["grok_video_resolution"])
            self.assertEqual("video-secret", stored["grok_api_key"])


    async def test_video_generation_uses_configured_duration_and_resolution_defaults(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            filename = VideoGenerationEndpointTest._register_image(server, "clip.png")
            keys_path = Path(server.data_dir) / "api_keys_config.json"
            keys_path.write_text(
                json.dumps(
                    {
                        "grok_video_duration": 15,
                        "grok_video_resolution": "720p",
                        "grok_api_key": "video-secret",
                    }
                ),
                encoding="utf-8",
            )

            captured = {}

            async def fake_run(img_id, prompt, *, duration, resolution, aspect_ratio, job_id):
                captured.update(
                    {
                        "img_id": img_id,
                        "prompt": prompt,
                        "duration": duration,
                        "resolution": resolution,
                        "aspect_ratio": aspect_ratio,
                        "job_id": job_id,
                    }
                )

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                with patch.object(server, "_run_image_video_generation", side_effect=fake_run), patch.dict(
                    os.environ,
                    {
                        "GALLERY_PASSWORD": "",
                        "GROK2API_URL": "",
                        "GROK_VIDEO_MODEL": "",
                        "GROK_VIDEO_DURATION": "",
                        "GROK_VIDEO_RESOLUTION": "",
                        "GROK_API_KEY": "",
                    },
                    clear=False,
                ):
                    response = await client.post(
                        f"/api/images/{filename}/video",
                        json={"prompt": "轻微点头"},
                    )
                    payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(202, response.status, payload)
            self.assertEqual(15, captured["duration"])
            self.assertEqual("720p", captured["resolution"])
            self.assertEqual("720p", payload["video_resolution"])


if __name__ == "__main__":
    unittest.main()
