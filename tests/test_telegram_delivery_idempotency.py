import asyncio
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import main as main_module  # noqa: E402
import web_server as web_server_module  # noqa: E402
from main import PortraitGalleryApp  # noqa: E402
from web_server import GalleryServer  # noqa: E402


AMBIGUOUS_TIMEOUT_OUTPUT = json.dumps({
    "error": "No deliverable text or media remained after processing MEDIA tags",
    "warnings": ["Failed to send media /tmp/photo.png: Timed out"],
})


class TelegramDeliveryIdempotencyTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_app() -> PortraitGalleryApp:
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app.config = {"integrations": {}}
        app._hermes_send_lock = asyncio.Lock()
        app._hermes_send_cooldown_until = 0.0
        app._last_delivery_error = ""
        return app

    @staticmethod
    def _make_server() -> GalleryServer:
        server = GalleryServer.__new__(GalleryServer)
        server.config = {"integrations": {}}
        server._manual_send_lock = asyncio.Lock()
        server._manual_send_cooldown_until = 0.0
        server._manual_send_last_error = ""
        return server

    async def test_automatic_telegram_media_timeout_output_is_not_retried(self):
        app = self._make_app()
        process = subprocess.CompletedProcess([], 1, stdout=AMBIGUOUS_TIMEOUT_OUTPUT, stderr="")

        with patch.object(main_module.subprocess, "run", return_value=process) as run:
            delivered = await app._run_hermes_send(
                "hermes",
                "telegram",
                "MEDIA:/tmp/photo.png",
                "TG图片",
                assume_delivered_on_timeout=True,
            )

        self.assertTrue(delivered)
        self.assertEqual(1, run.call_count)
        self.assertEqual("", app._last_delivery_error)

    async def test_automatic_telegram_media_process_timeout_is_not_retried(self):
        app = self._make_app()

        with patch.object(
            main_module.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("hermes", 90),
        ) as run:
            delivered = await app._run_hermes_send(
                "hermes",
                "telegram",
                "MEDIA:/tmp/photo.png",
                "TG图片",
                assume_delivered_on_timeout=True,
            )

        self.assertTrue(delivered)
        self.assertEqual(1, run.call_count)

    async def test_definite_telegram_rejection_can_still_retry(self):
        app = self._make_app()
        rate_limited = subprocess.CompletedProcess([], 1, stdout="HTTP 429 Too Many Requests", stderr="")
        delivered = subprocess.CompletedProcess([], 0, stdout='{"ok": true}', stderr="")

        with (
            patch.object(main_module.subprocess, "run", side_effect=[rate_limited, delivered]) as run,
            patch.object(main_module.asyncio, "sleep", new=AsyncMock()),
        ):
            result = await app._run_hermes_send(
                "hermes",
                "telegram",
                "MEDIA:/tmp/photo.png",
                "TG图片",
                assume_delivered_on_timeout=True,
            )

        self.assertTrue(result)
        self.assertEqual(2, run.call_count)

    async def test_automatic_telegram_image_enables_at_most_once_timeout_handling(self):
        app = self._make_app()
        app.config["integrations"]["hermes_cli"] = "hermes"
        app._run_hermes_send = AsyncMock(return_value=True)

        delivered = await app._send_to_hermes_channel(
            "telegram",
            "/tmp/photo.png",
            "",
            {"telegram_target": "telegram"},
        )

        self.assertTrue(delivered)
        self.assertTrue(
            app._run_hermes_send.await_args.kwargs["assume_delivered_on_timeout"]
        )

    async def test_manual_telegram_media_timeout_output_is_not_retried(self):
        server = self._make_server()
        process = subprocess.CompletedProcess([], 1, stdout=AMBIGUOUS_TIMEOUT_OUTPUT, stderr="")

        with patch.object(web_server_module.subprocess, "run", return_value=process) as run:
            delivered = await server._run_manual_hermes_send(
                "hermes",
                "telegram",
                "MEDIA:/tmp/photo.png",
                "TG图片",
                assume_delivered_on_timeout=True,
            )

        self.assertTrue(delivered)
        self.assertEqual(1, run.call_count)

    async def test_openclaw_telegram_timeout_does_not_fall_back(self):
        app = self._make_app()
        app.config["integrations"]["openclaw_cli"] = "openclaw"
        app._push_delivery_config = lambda: {
            "channel": "telegram",
            "agent": "openclaw",
            "telegram_target": "12345",
            "telegram_account": "default",
        }
        app._send_to_hermes_channel = AsyncMock(return_value=True)
        process = subprocess.CompletedProcess([], 1, stdout=AMBIGUOUS_TIMEOUT_OUTPUT, stderr="")

        with patch.object(main_module.subprocess, "run", return_value=process):
            delivered = await app._send_generated_photo("/tmp/photo.png", "caption")

        self.assertTrue(delivered)
        app._send_to_hermes_channel.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
