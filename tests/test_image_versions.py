import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from image_versions import (  # noqa: E402
    archive_image_version,
    image_version_path,
    normalize_image_versions,
)
from store import ScheduleStore  # noqa: E402
from web_server import GalleryServer  # noqa: E402


class ImageVersionStorageTest(unittest.TestCase):
    def test_archive_uses_opaque_filename_and_rejects_tampered_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            source = Path(tmpdir) / "a-very-long-original-image-name.png"
            Image.new("RGB", (30, 40), (120, 80, 160)).save(source)

            record = archive_image_version(
                str(data_dir),
                str(source),
                original_image_filename=source.name,
                target="background",
                target_label="背景",
                instruction="改成雨后街道",
            )
            archived = image_version_path(str(data_dir), record)

            self.assertRegex(record["id"], r"^[a-f0-9]{32}$")
            self.assertEqual(f"{record['id']}.png", record["archive_filename"])
            self.assertTrue(archived.is_file())
            self.assertEqual((30, 40), (record["width"], record["height"]))
            tampered = {**record, "archive_filename": "../outside.png"}
            self.assertEqual([], normalize_image_versions([tampered]))
            self.assertIsNone(image_version_path(str(data_dir), tampered))


class ImageVersionEndpointTest(unittest.IsolatedAsyncioTestCase):
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

    async def test_history_api_hides_archive_name_and_delete_removes_version(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            current_filename = "current.png"
            current_path = Path(server.image_dir) / current_filename
            Image.new("RGB", (48, 64), (230, 220, 210)).save(current_path)
            old_path = root / "old.png"
            Image.new("RGB", (48, 64), (90, 100, 110)).save(old_path)
            old_bytes = old_path.read_bytes()
            version = archive_image_version(
                server.data_dir,
                str(old_path),
                original_image_filename="original.png",
                archived_at="2026-07-16T16:12:00",
                target="background",
                target_label="背景",
                instruction="改成商业街",
                date="2026-07-16",
                time="16:12",
            )
            archived_path = image_version_path(server.data_dir, version)
            ScheduleStore(server.data_dir).save({
                "card": {
                    "id": current_filename,
                    "date": "2026-07-16",
                    "time": "16:12",
                    "image_filename": current_filename,
                    "image_path": f"/images/{current_filename}",
                    "status": "ok",
                    "source": "cron",
                    "edit_history": [{"target": "background"}],
                    "image_versions": [version],
                },
            })

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                gallery_response = await client.get("/api/gallery?limit=10")
                gallery = await gallery_response.json()
                detail_response = await client.get(f"/api/images/{current_filename}")
                detail = await detail_response.json()
                versions_response = await client.get(
                    f"/api/images/{current_filename}/versions"
                )
                versions = await versions_response.json()
                version_response = await client.get(versions["items"][0]["image_url"])
                version_bytes = await version_response.read()
                missing_response = await client.get(
                    f"/api/images/{current_filename}/versions/{'0' * 32}"
                )
                delete_response = await client.delete(f"/api/images/{current_filename}")
                deleted = await delete_response.json()
            finally:
                await client.close()

            self.assertEqual(200, gallery_response.status)
            self.assertEqual(1, gallery["items"][0]["version_count"])
            self.assertTrue(gallery["items"][0]["has_image_history"])
            self.assertNotIn("image_versions", gallery["items"][0])
            self.assertEqual(1, detail["version_count"])
            self.assertNotIn("image_versions", detail)
            self.assertEqual(200, versions_response.status)
            self.assertEqual(1, versions["version_count"])
            self.assertEqual(0, versions["unavailable_count"])
            self.assertNotIn("archive_filename", versions["items"][0])
            self.assertEqual("背景", versions["items"][0]["target_label"])
            self.assertEqual(200, version_response.status)
            self.assertEqual(old_bytes, version_bytes)
            self.assertEqual(404, missing_response.status)
            self.assertEqual(1, deleted["deleted_version_count"])
            self.assertFalse(archived_path.exists())

    async def test_legacy_edit_is_visible_even_when_old_file_was_not_preserved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            filename = "legacy-edited.png"
            Image.new("RGB", (24, 32), (180, 170, 160)).save(
                Path(server.image_dir) / filename
            )
            ScheduleStore(server.data_dir).save({
                "card": {
                    "image_filename": filename,
                    "status": "ok",
                    "edit_history": [{"target": "outfit"}],
                },
            })

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                detail_response = await client.get(f"/api/images/{filename}")
                detail = await detail_response.json()
                versions_response = await client.get(f"/api/images/{filename}/versions")
                versions = await versions_response.json()
            finally:
                await client.close()

            self.assertEqual(200, detail_response.status)
            self.assertTrue(detail["has_image_history"])
            self.assertEqual(0, detail["version_count"])
            self.assertEqual(1, versions["unavailable_count"])
            self.assertEqual([], versions["items"])


if __name__ == "__main__":
    unittest.main()
