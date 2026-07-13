import json
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from store import ScheduleStore  # noqa: E402
from web_server import GalleryServer  # noqa: E402


class GroupChatImageDeleteTest(unittest.IsolatedAsyncioTestCase):
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
    async def _start_client(server: GalleryServer) -> TestClient:
        test_server = TestServer(server.app)
        await test_server.start_server(access_log=None)
        return TestClient(test_server)

    @staticmethod
    def _create_room(server: GalleryServer) -> dict:
        return server.group_chat_store.create_room(name="测试群聊")

    async def test_deleting_image_message_also_removes_gallery_image(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            room = self._create_room(server)
            filename = "group_chat_photo.png"
            image_path = Path(server.image_dir) / filename
            image_path.write_bytes(b"image")
            ScheduleStore(server.data_dir).save({
                filename: {"image_filename": filename, "status": "ok"},
                "duplicate": {"image_filename": filename, "status": "ok"},
                "keep.png": {"image_filename": "keep.png", "status": "ok"},
            })
            server._save_image_metadata({
                filename: {"caption": "删除我"},
                "keep.png": {"caption": "保留我"},
            })
            message = server.group_chat_store.add_message(
                room["id"],
                {
                    "content": "生成图片",
                    "type": "image",
                    "metadata": {
                        "image_filename": filename,
                        "image_url": f"/images/{filename}",
                    },
                },
                message_type="image",
            )

            client = await self._start_client(server)
            try:
                response = await client.delete(
                    f"/api/group-chat/rooms/{room['id']}/messages/{message['id']}"
                )
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status)
            self.assertTrue(payload["success"])
            self.assertTrue(payload["gallery_deleted"])
            self.assertEqual(filename, payload["image_filename"])
            self.assertFalse(image_path.exists())
            self.assertEqual(
                {"keep.png": {"image_filename": "keep.png", "status": "ok"}},
                ScheduleStore(server.data_dir).load(),
            )
            metadata = json.loads(
                (Path(server.data_dir) / "image_metadata.json").read_text(encoding="utf-8")
            )
            self.assertNotIn(filename, metadata)
            self.assertIn("keep.png", metadata)
            self.assertEqual([], server.group_chat_store.list_messages(room["id"]))

    async def test_deleting_text_message_does_not_touch_gallery(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            room = self._create_room(server)
            filename = "keep.png"
            image_path = Path(server.image_dir) / filename
            image_path.write_bytes(b"image")
            ScheduleStore(server.data_dir).save({
                filename: {"image_filename": filename, "status": "ok"},
            })
            message = server.group_chat_store.add_message(
                room["id"],
                {"content": "只删除文字", "type": "text"},
            )

            client = await self._start_client(server)
            try:
                response = await client.delete(
                    f"/api/group-chat/rooms/{room['id']}/messages/{message['id']}"
                )
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status)
            self.assertFalse(payload["gallery_deleted"])
            self.assertEqual("", payload["image_filename"])
            self.assertTrue(image_path.exists())
            self.assertIn(filename, ScheduleStore(server.data_dir).load())

    async def test_invalid_message_filename_cannot_delete_gallery_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            room = self._create_room(server)
            filename = "outside.png"
            image_path = Path(server.image_dir) / filename
            image_path.write_bytes(b"image")
            ScheduleStore(server.data_dir).save({
                filename: {"image_filename": filename, "status": "ok"},
            })
            message = server.group_chat_store.add_message(
                room["id"],
                {
                    "content": "异常图片",
                    "type": "image",
                    "metadata": {"image_filename": f"../{filename}"},
                },
                message_type="image",
            )

            client = await self._start_client(server)
            try:
                response = await client.delete(
                    f"/api/group-chat/rooms/{room['id']}/messages/{message['id']}"
                )
                payload = await response.json()
            finally:
                await client.close()

            self.assertEqual(200, response.status)
            self.assertEqual("invalid_filename", payload["gallery_error"])
            self.assertFalse(payload["gallery_deleted"])
            self.assertEqual("", payload["image_filename"])
            self.assertTrue(image_path.exists())
            self.assertIn(filename, ScheduleStore(server.data_dir).load())
            self.assertEqual([], server.group_chat_store.list_messages(room["id"]))


if __name__ == "__main__":
    unittest.main()
