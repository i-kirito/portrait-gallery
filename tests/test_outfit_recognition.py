import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from reference_profiles import analyze_image_outfit  # noqa: E402
from store import ImageMetadataStore, ScheduleStore  # noqa: E402
from web_server import GalleryServer  # noqa: E402


class OutfitAnalysisTest(unittest.TestCase):
    def test_analyzer_returns_compact_visible_outfit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "portrait.png"
            Image.new("RGB", (24, 32), (220, 210, 200)).save(image_path)
            with patch(
                "reference_profiles._post_llm",
                return_value=json.dumps(
                    {"outfit_cn": "米色针织毛衣搭配浅色长裤，佩戴细链项链"}
                ),
            ) as post_llm:
                result = analyze_image_outfit({}, tmpdir, str(image_path))

        self.assertEqual("ok", result["analysis_status"])
        self.assertEqual(
            "米色针织毛衣搭配浅色长裤，佩戴细链项链",
            result["outfit"],
        )
        image_part = post_llm.call_args.args[2][1]["content"][1]
        self.assertTrue(image_part["image_url"]["url"].startswith("data:image/png;base64,"))


class OutfitRecognitionEndpointTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_legacy_metadata_only_chat_image_is_recognized_and_merged(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            filename = "zhuzhu_custom_1786454767.png"
            Image.new("RGB", (24, 32), (220, 210, 200)).save(
                Path(server.image_dir) / filename
            )
            ImageMetadataStore(server.data_dir).save(
                {
                    filename: {
                        "prompt": "original prompt",
                        "model": "gpt-image-2",
                        "created_at": 1786454767,
                    }
                }
            )

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                with patch(
                    "web_server.analyze_image_outfit",
                    return_value={
                        "outfit": "米色针织毛衣搭配浅色长裤",
                        "analysis_status": "ok",
                        "analysis_error": "",
                    },
                ):
                    response = await client.post(
                        f"/api/images/{filename}/recognize-outfit"
                    )
                    payload = await response.json()
            finally:
                await client.close()

            stored = ImageMetadataStore(server.data_dir).load()[filename]
            self.assertEqual(200, response.status)
            self.assertEqual("米色针织毛衣搭配浅色长裤", payload["recognized_outfit"])
            self.assertIn("米色针织毛衣搭配浅色长裤", payload["outfit"])
            self.assertEqual("original prompt", stored["prompt"])
            self.assertEqual("米色针织毛衣搭配浅色长裤", stored["outfit"])
            self.assertEqual("米色针织毛衣搭配浅色长裤", stored["display_outfit"])
            self.assertEqual("米色针织毛衣搭配浅色长裤", stored["outfit_description"])

    async def test_non_hermes_image_is_rejected_without_calling_vision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))
            filename = "custom.png"
            Image.new("RGB", (24, 32), (220, 210, 200)).save(
                Path(server.image_dir) / filename
            )
            ScheduleStore(server.data_dir).save(
                {
                    "card": {
                        "id": filename,
                        "date": "2026-08-11",
                        "image_filename": filename,
                        "image_path": f"/images/{filename}",
                        "outfit": "风格：自定义 穿搭：白色连衣裙",
                        "status": "ok",
                        "source": "custom",
                    }
                }
            )

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                with patch("web_server.analyze_image_outfit") as analyze:
                    response = await client.post(
                        f"/api/images/{filename}/recognize-outfit"
                    )
                    payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(403, response.status)
            self.assertEqual("outfit_recognition_not_available", payload["error"])
            analyze.assert_not_called()


class OutfitRecognitionFrontendTest(unittest.TestCase):
    def test_modal_only_renders_recognition_for_hermes_chat_entries(self):
        html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("function canRecognizeHermesChatOutfit(e)", html)
        self.assertIn('source === "chat" && e.metadata_only === true', html)
        self.assertIn('title="识别穿搭"', html)
        self.assertIn("/recognize-outfit", html)
        self.assertIn("recognizeOutfitFromModal(event,this)", html)


if __name__ == "__main__":
    unittest.main()
