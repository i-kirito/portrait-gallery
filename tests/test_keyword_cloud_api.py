import json
import sys
import tempfile
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from web_server import GalleryServer  # noqa: E402


class _JsonRequest:
    def __init__(self, payload=None, query=None):
        self.payload = payload
        self.query = query or {}

    async def json(self):
        return self.payload


class KeywordCloudApiTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _make_server(data_dir: Path) -> GalleryServer:
        server = GalleryServer.__new__(GalleryServer)
        server.data_dir = str(data_dir)
        return server

    async def test_hide_endpoint_returns_refreshed_cloud_and_persists_filter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            (data_dir / "schedule_data.json").write_text(
                json.dumps({
                    "custom_1.png": {
                        "status": "ok",
                        "source": "custom",
                        "image_filename": "custom_1.png",
                        "custom_prompt": "紫色针织衫，河边步道",
                    },
                    "custom_2.png": {
                        "status": "ok",
                        "source": "custom",
                        "image_filename": "custom_2.png",
                        "custom_prompt": "紫色针织衫，复古相机",
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            server = self._make_server(data_dir)

            hide_response = await server.handle_hide_keyword_cloud_term(
                _JsonRequest({"keyword": "紫色针织衫"})
            )
            hidden_payload = json.loads(hide_response.text)
            get_response = await server.handle_keyword_cloud(
                _JsonRequest(query={"limit": "10"})
            )
            get_payload = json.loads(get_response.text)

            self.assertEqual(200, hide_response.status)
            self.assertTrue(hidden_payload["success"])
            self.assertEqual("紫色针织衫", hidden_payload["hidden_keyword"])
            self.assertEqual(1, hidden_payload["hidden_count"])
            self.assertNotIn(
                "紫色针织衫",
                {item["text"] for item in hidden_payload["keywords"]},
            )
            self.assertNotIn(
                "紫色针织衫",
                {item["text"] for item in get_payload["keywords"]},
            )

    async def test_hide_endpoint_rejects_missing_keyword(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            server = self._make_server(Path(tmpdir))

            response = await server.handle_hide_keyword_cloud_term(_JsonRequest({}))
            payload = json.loads(response.text)

            self.assertEqual(400, response.status)
            self.assertEqual("keyword_required", payload["error"])


if __name__ == "__main__":
    unittest.main()
