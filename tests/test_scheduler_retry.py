import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import requests


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import scheduler as scheduler_module  # noqa: E402
from scheduler import DailyScheduler  # noqa: E402


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class SchedulerRetryTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def request_config():
        return {
            "chat_url": "https://example.test/v1/chat/completions",
            "api_key": "secret",
            "models": ["deepseek-test"],
        }

    async def test_network_failure_uses_only_configured_attempt_count(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({}, tmpdir)
            with (
                patch.object(
                    scheduler_module,
                    "llm_request_config",
                    return_value=self.request_config(),
                ),
                patch(
                    "requests.post",
                    side_effect=requests.exceptions.ConnectionError("offline"),
                ) as post,
                patch.object(scheduler_module.asyncio, "sleep", new=AsyncMock()),
            ):
                result = await scheduler._call_llm("probe", timeout=1, json_mode=True)

            self.assertIsNone(result)
            self.assertEqual(2, post.call_count)
            self.assertTrue(all("thinking" in call.kwargs["json"] for call in post.call_args_list))

    async def test_explicit_thinking_400_gets_one_compatibility_retry(self):
        rejected = FakeResponse(400, {
            "error": {"message": "Unsupported parameter: thinking"},
        })
        accepted = FakeResponse(200, {
            "choices": [{"message": {"content": "{\"ok\": true}"}}],
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({}, tmpdir)
            with (
                patch.object(
                    scheduler_module,
                    "llm_request_config",
                    return_value=self.request_config(),
                ),
                patch("requests.post", side_effect=[rejected, accepted]) as post,
            ):
                result = await scheduler._call_llm("probe", timeout=1, json_mode=True)

            self.assertEqual('{"ok": true}', result)
            self.assertEqual(2, post.call_count)
            self.assertIn("thinking", post.call_args_list[0].kwargs["json"])
            self.assertNotIn("thinking", post.call_args_list[1].kwargs["json"])

    async def test_temperature_and_model_overrides_are_sent(self):
        accepted = FakeResponse(200, {
            "choices": [{"message": {"content": "{\"ok\": true}"}}],
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({}, tmpdir)
            with (
                patch.object(
                    scheduler_module,
                    "llm_request_config",
                    return_value=self.request_config(),
                ),
                patch("requests.post", return_value=accepted) as post,
            ):
                result = await scheduler._call_llm(
                    "probe",
                    timeout=1,
                    json_mode=True,
                    models_override=["gemini-3.5-flash"],
                    temperature=0.8,
                )

            self.assertEqual('{"ok": true}', result)
            payload = post.call_args.kwargs["json"]
            self.assertEqual("gemini-3.5-flash", payload["model"])
            self.assertEqual(0.8, payload["temperature"])
            self.assertEqual(8192, payload["max_tokens"])

    async def test_streaming_chunks_are_reassembled_as_final_json(self):
        streamed = requests.Response()
        streamed.status_code = 200
        streamed.iter_lines = lambda decode_unicode=False: iter([
            'data: {"model":"grok-4.5","choices":[{"delta":{"reasoning_content":"thinking"}}]}'.encode("utf-8"),
            'data: {"choices":[{"delta":{"content":"{\\"文字\\":\\"中"}}]}'.encode("utf-8"),
            'data: {"choices":[{"delta":{"content":"文\\"}"},"finish_reason":"stop"}]}'.encode("utf-8"),
            b'data: [DONE]',
        ])
        request_config = {
            "chat_url": "https://example.test/v1/chat/completions",
            "api_key": "secret",
            "models": ["grok-4.5"],
            "stream": True,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({}, tmpdir)
            with (
                patch.object(scheduler_module, "llm_request_config", return_value=request_config),
                patch("requests.post", return_value=streamed) as post,
            ):
                result = await scheduler._call_llm("probe", timeout=1, json_mode=True)

        self.assertEqual('{"文字":"中文"}', result)
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertTrue(post.call_args.kwargs["json"]["stream"])
        self.assertEqual(8192, post.call_args.kwargs["json"]["max_tokens"])
        self.assertEqual(
            {"type": "disabled"},
            post.call_args.kwargs["json"]["thinking"],
        )

    async def test_streaming_upstream_mojibake_is_repaired(self):
        mojibake = '{"文字":"中文"}'.encode("utf-8").decode("latin-1")
        streamed = requests.Response()
        streamed.status_code = 200
        streamed.iter_lines = lambda decode_unicode=False: iter([
            (
                'data: {"choices":[{"delta":{"content":'
                + scheduler_module.json.dumps(mojibake, ensure_ascii=False)
                + '},"finish_reason":"stop"}]}'
            ).encode("utf-8"),
            b'data: [DONE]',
        ])
        request_config = {
            "chat_url": "https://example.test/v1/chat/completions",
            "api_key": "secret",
            "models": ["grok-4.5"],
            "stream": True,
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({}, tmpdir)
            with (
                patch.object(scheduler_module, "llm_request_config", return_value=request_config),
                patch("requests.post", return_value=streamed),
            ):
                result = await scheduler._call_llm("probe", timeout=1, json_mode=True)

        self.assertEqual('{"文字":"中文"}', result)

    async def test_xiaohongshu_vision_selects_only_strict_full_body_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({}, tmpdir)
            scheduler._call_llm = AsyncMock(return_value=(
                '{"selected_index":2,"is_real_photo":true,"is_collage":false,'
                '"person_count":1,"single_outfit":true,"full_body_visible":true,'
                '"clothing_clear":true,"quality_sufficient":true,'
                '"keyword_match":true,"quality_score":93,"reason":"头脚完整且穿搭清楚"}'
            ))

            result = await scheduler.select_xiaohongshu_outfit_image(
                "/tmp/contact-sheet.jpg",
                "夏季通勤穿搭",
                3,
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(2, result["selected_index"])
        self.assertEqual(93, result["quality_score"])

    async def test_xiaohongshu_vision_rejects_collage_even_if_model_selects_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({}, tmpdir)
            scheduler._call_llm = AsyncMock(return_value=(
                '{"selected_index":1,"is_real_photo":true,"is_collage":true,'
                '"person_count":1,"single_outfit":false,"full_body_visible":true,'
                '"clothing_clear":true,"quality_sufficient":true,'
                '"keyword_match":true,"quality_score":90,"reason":"包含多套穿搭拼图"}'
            ))

            result = await scheduler.select_xiaohongshu_outfit_image(
                "/tmp/contact-sheet.jpg",
                "夏季通勤穿搭",
                2,
            )

        self.assertFalse(result["accepted"])
        self.assertEqual(0, result["selected_index"])

    async def test_xiaohongshu_vision_rejects_missing_required_field(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({}, tmpdir)
            scheduler._call_llm = AsyncMock(return_value=(
                '{"selected_index":1,"is_real_photo":true,'
                '"person_count":1,"single_outfit":true,"full_body_visible":true,'
                '"clothing_clear":true,"quality_sufficient":true,'
                '"keyword_match":true,"quality_score":95,"reason":"漏了拼图字段"}'
            ))

            result = await scheduler.select_xiaohongshu_outfit_image(
                "/tmp/contact-sheet.jpg",
                "夏季通勤穿搭",
                1,
            )

        self.assertFalse(result["accepted"])
        self.assertEqual(0, result["selected_index"])

    async def test_required_vision_image_never_falls_back_to_text_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DailyScheduler({}, tmpdir)
            with (
                patch.object(
                    scheduler_module,
                    "llm_request_config",
                    return_value=self.request_config(),
                ),
                patch("requests.post") as post,
            ):
                result = await scheduler._call_llm(
                    "vision probe",
                    timeout=1,
                    json_mode=True,
                    image_path=str(Path(tmpdir) / "missing.jpg"),
                    require_image=True,
                )

        self.assertIsNone(result)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
