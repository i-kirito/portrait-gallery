import json
import sys
import tempfile
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from web_server import GalleryServer  # noqa: E402


class JsonRequest:
    method = "POST"

    def __init__(self, payload: dict):
        self.payload = payload

    async def json(self):
        return self.payload


class DislikedOutfitStorageTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_server(data_dir: str) -> GalleryServer:
        server = GalleryServer.__new__(GalleryServer)
        server.data_dir = data_dir
        return server

    async def test_adding_51st_dislike_preserves_oldest_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            existing = [
                {
                    "id": f"existing-{index}",
                    "date": "2026-06-01",
                    "outfit_style": "测试风格",
                    "outfit": {"穿搭": f"历史穿搭 {index}"},
                    "created_at": index + 1,
                }
                for index in range(50)
            ]
            Path(tmpdir, "disliked_outfits.json").write_text(
                json.dumps({"items": existing}, ensure_ascii=False),
                encoding="utf-8",
            )
            server = self.make_server(tmpdir)
            request = JsonRequest({
                "date": "2026-07-14",
                "outfit_style": "新风格",
                "outfit": {
                    "风格": "新风格",
                    "发型": "低马尾",
                    "穿搭": "新加入的不喜欢穿搭",
                },
                "disliked": True,
            })

            response = await server.handle_disliked_outfits(request)
            saved = json.loads(
                Path(tmpdir, "disliked_outfits.json").read_text(encoding="utf-8")
            )["items"]

            self.assertEqual(200, response.status)
            self.assertEqual(51, len(saved))
            self.assertIn("existing-0", {item.get("id") for item in saved})


if __name__ == "__main__":
    unittest.main()
