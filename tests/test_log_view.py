import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
_TEST_LOG_DIR = tempfile.TemporaryDirectory(prefix="portrait-gallery-tests-")
os.environ["HERMES_GALLERY_LOG"] = str(Path(_TEST_LOG_DIR.name) / "gallery.log")

from web_server import GalleryServer  # noqa: E402
from image_gen import ImageGenerator  # noqa: E402
from main import PortraitGalleryApp, _UsefulAccessLogFilter  # noqa: E402


class LogViewFormattingTest(unittest.TestCase):
    def test_access_filter_keeps_failures_and_drops_success_noise(self):
        access_filter = _UsefulAccessLogFilter()

        success = logging.LogRecord(
            "aiohttp.access", logging.INFO, "", 0,
            '127.0.0.1 "GET /api/logs HTTP/1.1" 200 120', (), None,
        )
        failure = logging.LogRecord(
            "aiohttp.access", logging.INFO, "", 0,
            '127.0.0.1 "POST /api/generate HTTP/1.1" 500 120', (), None,
        )
        missing_api = logging.LogRecord(
            "aiohttp.access", logging.INFO, "", 0,
            '127.0.0.1 "GET /api/missing HTTP/1.1" 404 120', (), None,
        )
        favicon_probe = logging.LogRecord(
            "aiohttp.access", logging.INFO, "", 0,
            '127.0.0.1 "GET /assets/favicon.svg HTTP/1.1" 404 120', (), None,
        )

        self.assertFalse(access_filter.filter(success))
        self.assertTrue(access_filter.filter(failure))
        self.assertTrue(access_filter.filter(missing_api))
        self.assertFalse(access_filter.filter(favicon_probe))

    def test_structured_logs_hide_access_and_collapse_repeated_noise(self):
        text = "\n".join(
            [
                '2026-07-10 09:00:00,000 [INFO] aiohttp.access: 127.0.0.1 "GET /api/health HTTP/1.1" 200 16',
                "2026-07-10 09:00:01,000 [INFO] portrait_gallery: 动态任务调度器已启动",
                "2026-07-10 09:00:11,000 [INFO] portrait_gallery: 动态任务调度器已启动",
                "2026-07-10 09:00:12,000 [WARNING] web_server: 微信发送最终失败: ret=-2 unknown error",
            ]
        )

        payload = GalleryServer._format_level_logs(text, max_items=100)

        self.assertEqual(1, payload["hidden_access_count"])
        self.assertEqual(2, len(payload["entries"]))
        startup = next(item for item in payload["entries"] if "调度器已启动" in item["message"])
        self.assertEqual("system", startup["category"])
        self.assertEqual(2, startup["repeat_count"])
        self.assertIn("重复 1 次", payload["text"])
        self.assertFalse(startup["important"])

        delivery = next(item for item in payload["entries"] if item["category"] == "delivery")
        self.assertEqual("WARN", delivery["level"])
        self.assertEqual("推送", delivery["category_label"])
        self.assertTrue(delivery["important"])
        self.assertIn("微信图片最终发送失败", delivery["message"])

    def test_manual_send_progress_is_translated_for_the_log_view(self):
        start = GalleryServer._translate_log_message(
            "手动发送图片: channel=wechat agent=hermes image=demo.png",
            "INFO",
            "web_server",
        )
        retry = GalleryServer._translate_log_message(
            "微信图片发送重试等待 60.0s (2/3)",
            "INFO",
            "web_server",
        )

        self.assertEqual("开始手动发送图片到微信：demo.png。", start)
        self.assertEqual("微信图片发送将在 60.0 秒后重试：下一次为第 2/3 次。", retry)

    def test_weixin_ret_minus_two_has_an_actionable_explanation(self):
        message = GalleryServer._diagnose_error_text(
            "Weixin media send failed: ret=-2 errmsg=unknown error"
        )

        self.assertIn("并非已发送次数过多", message)
        self.assertIn("刷新上下文", message)

    def test_weixin_missing_context_is_not_reported_as_rate_limit(self):
        message = GalleryServer._diagnose_error_text(
            "iLink media sendmessage requires a fresh WeChat conversation context; "
            "send the bot a WeChat message first"
        )

        self.assertIn("会话上下文已失效", message)
        self.assertIn("任意一条消息", message)

    def test_multiline_weixin_error_updates_the_visible_diagnosis(self):
        text = "\n".join(
            [
                "2026-07-10 11:23:08,213 [ERROR] web_server: "
                "微信图片发送失败: attempt=3/3 exit=1 retryable=True output={",
                '  "error": "Weixin media send failed: iLink media sendmessage '
                'rate limited: ret=-2 errcode=None errmsg=unknown error"',
                "}",
            ]
        )

        payload = GalleryServer._format_level_logs(text, max_items=100)

        self.assertEqual(1, len(payload["entries"]))
        entry = payload["entries"][0]
        self.assertIn("会话上下文已失效", entry["message"])
        self.assertIn("并非已发送次数过多", entry["message"])
        self.assertNotIn('"error"', entry["message"])
        self.assertIn('"error"', entry["detail"])
        self.assertTrue(entry["detail"].endswith("}"))

    def test_log_startup_path_is_not_misclassified_as_delivery(self):
        category, label = GalleryServer._log_category(
            "portrait_gallery",
            "持久化日志已启用: /Volumes/ikirito/hermes-portrait-gallery/logs/gallery.log",
        )

        self.assertEqual(("system", "系统"), (category, label))

    def test_error_access_is_kept_as_readable_api_event(self):
        text = (
            '2026-07-10 09:01:00,000 [INFO] aiohttp.access: '
            '127.0.0.1 "POST /api/generate-now HTTP/1.1" 500 120'
        )

        payload = GalleryServer._format_level_logs(text, max_items=100)

        self.assertEqual(1, len(payload["entries"]))
        entry = payload["entries"][0]
        self.assertEqual("ERROR", entry["level"])
        self.assertEqual("api", entry["category"])
        self.assertIn("POST /api/generate-now", entry["message"])
        self.assertTrue(entry["important"])

    def test_favicon_probes_are_hidden_but_real_api_404_is_kept(self):
        text = "\n".join(
            [
                '2026-07-10 09:01:00,000 [INFO] aiohttp.access: '
                '127.0.0.1 "GET /favicon.ico HTTP/1.1" 404 120',
                '2026-07-10 09:01:01,000 [INFO] aiohttp.access: '
                '127.0.0.1 "GET /public/icon-maskable-192.png HTTP/1.1" 404 120',
                '2026-07-10 09:01:02,000 [INFO] aiohttp.access: '
                '127.0.0.1 "GET /api/missing HTTP/1.1" 404 120',
            ]
        )

        payload = GalleryServer._format_level_logs(text, max_items=100)

        self.assertEqual(2, payload["hidden_access_count"])
        self.assertEqual(1, len(payload["entries"]))
        self.assertIn("GET /api/missing", payload["entries"][0]["message"])

    def test_weixin_context_failure_is_not_retried_as_rate_limit(self):
        old_hermes_output = (
            "Weixin media send failed: iLink media sendmessage rate limited: "
            "ret=-2 errcode=None errmsg=unknown error"
        )
        current_hermes_output = (
            "iLink media sendmessage requires a fresh WeChat conversation context; "
            "send the bot a WeChat message first"
        )
        genuine_rate_limit = (
            "Weixin media send failed: iLink media sendmessage rate limited: "
            "ret=-2 errcode=None errmsg=freq limit"
        )

        for output in (old_hermes_output, current_hermes_output):
            self.assertTrue(GalleryServer._is_wechat_context_error(output))
            self.assertFalse(GalleryServer._is_retryable_send_error(output))
            self.assertTrue(PortraitGalleryApp._is_wechat_context_error(output))
            self.assertFalse(PortraitGalleryApp._is_retryable_wechat_error(output))

        self.assertFalse(GalleryServer._is_wechat_context_error(genuine_rate_limit))
        self.assertTrue(GalleryServer._is_retryable_send_error(genuine_rate_limit))
        self.assertFalse(PortraitGalleryApp._is_wechat_context_error(genuine_rate_limit))
        self.assertTrue(PortraitGalleryApp._is_retryable_wechat_error(genuine_rate_limit))

    def test_structured_log_detail_keeps_the_complete_image_prompt(self):
        long_prompt = "卧室飘窗，柔和自然光，" + ("完整提示词内容" * 30) + "结尾标记"
        text = (
            "2026-07-10 09:02:00,000 [INFO] image_gen: "
            "开始生图: theme=custom, engine=gptimage, model=-, "
            f"style=None, size=1024x1024, prompt={long_prompt}"
        )

        payload = GalleryServer._format_level_logs(text, max_items=100)

        self.assertEqual(1, len(payload["entries"]))
        detail = payload["entries"][0]["detail"]
        self.assertIn(long_prompt, detail)
        self.assertTrue(detail.endswith("结尾标记"))

    def test_image_fallback_log_is_explained_in_plain_language(self):
        message = GalleryServer._translate_log_message(
            "生图引擎自动回退: requested=gptimage, failed=gptimage, actual=gitee, "
            "reason=上游没有可用的 GPT Image 渠道；上游内容安全策略拒绝了请求",
            "WARNING",
            "image_gen",
        )

        self.assertIn("GPT Image 生图失败", message)
        self.assertIn("最终图片由 Gitee 生成", message)
        self.assertIn("没有可用的 GPT Image 渠道", message)

    def test_metadata_model_field_populates_display_model_name(self):
        server = GalleryServer.__new__(GalleryServer)
        server._image_file_info = lambda _filename: {}
        entry = {
            "image_filename": "fallback.png",
            "source": "character",
            "outfit": "风格：自定义 穿搭：JK 制服",
        }

        normalized = server._normalize_entry_display(
            entry,
            {"fallback.png": {"model": "z-image-turbo"}},
        )

        self.assertEqual("Gitee", normalized["model_name"])


class ManualSendFailureHandlingTest(unittest.IsolatedAsyncioTestCase):
    async def test_context_failure_stops_after_one_attempt(self):
        server = GalleryServer.__new__(GalleryServer)
        server._manual_send_cooldown_until = 0.0
        server._manual_send_last_error = ""
        result = SimpleNamespace(
            returncode=1,
            stdout=(
                '{"error":"Weixin media send failed: '
                'ret=-2 errcode=None errmsg=unknown error"}'
            ),
            stderr="",
        )

        with patch("web_server.subprocess.run", return_value=result) as run:
            ok = await server._run_manual_hermes_send(
                "hermes",
                "weixin",
                "MEDIA:/tmp/example.png",
                "微信图片",
            )

        self.assertFalse(ok)
        self.assertEqual(1, run.call_count)
        self.assertIn("会话上下文已失效", server._manual_send_last_error)


class ImageGenerationLoggingTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_log_keeps_the_complete_prompt(self):
        long_prompt = "靠在窗边听音乐，" + ("保留全部提示词" * 30) + "最终内容"
        with tempfile.TemporaryDirectory(prefix="portrait-gallery-image-log-") as temp_dir:
            generator = ImageGenerator(
                script_dir=temp_dir,
                data_dir=temp_dir,
                default_engine="gptimage",
            )
            with patch("image_gen.logger.info") as info:
                result = await generator.generate(
                    long_prompt,
                    size="1024x1024",
                    timeout=1,
                )

        self.assertIsNone(result)
        logged_message = info.call_args.args[0]
        self.assertIn(long_prompt, logged_message)
        self.assertTrue(logged_message.endswith("最终内容"))

    async def test_successful_fallback_is_logged_with_the_real_engine(self):
        with tempfile.TemporaryDirectory(prefix="portrait-gallery-image-fallback-") as temp_dir:
            script_path = Path(temp_dir) / "generate.py"
            script_path.write_text("# test fixture\n", encoding="utf-8")
            output_path = Path(temp_dir) / "fallback.png"
            output_path.write_bytes(b"test image")
            generator = ImageGenerator(
                script_dir=temp_dir,
                data_dir=temp_dir,
                default_engine="gptimage",
            )
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=f"SUCCESS:{output_path}\n",
                stderr=(
                    "GPT Image Images API failed [axonhub] (attempt 1/3): unexpected EOF\n"
                    "GPT Image Images API error 503 [axonhub] (attempt 2/3): "
                    "没有可用的 gpt-image-2 渠道\n"
                    "GPT Image failed, falling back to Gitee\n"
                ),
            )

            with patch("image_gen.subprocess.run", return_value=completed), patch(
                "image_gen.logger.warning"
            ) as warning:
                result = await generator.generate("测试提示词", timeout=1)

        self.assertEqual("fallback.png", result)
        warning_args = warning.call_args.args
        rendered = warning_args[0] % warning_args[1:]
        self.assertIn("requested=gptimage", rendered)
        self.assertIn("actual=gitee", rendered)
        self.assertIn("上游连接意外中断", rendered)
        self.assertIn("没有可用的 GPT Image 渠道", rendered)

    def test_quota_fallback_reason_is_actionable(self):
        reason = ImageGenerator._fallback_reason(
            "GPT Image Images API error 429 [axonhub]: "
            '{"error":{"code":"insufficient_quota","message":"图片账号额度已用完"}}\n'
            "GPT Image failed, falling back to Gitee\n",
            "gptimage",
        )

        self.assertIn("GPT Image 图片账号额度已用完", reason)


if __name__ == "__main__":
    unittest.main()
