import asyncio
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
    replace_image_from_version,
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

    def test_version_replacement_keeps_target_format_and_dimensions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "old.jpg"
            target = Path(tmpdir) / "current.png"
            Image.new("RGB", (36, 24), (20, 80, 140)).save(source, quality=95)
            Image.new("RGB", (20, 30), (220, 210, 200)).save(target)

            info = replace_image_from_version(source, target)

            self.assertEqual((36, 24), (info["width"], info["height"]))
            with Image.open(target) as image:
                self.assertEqual("PNG", image.format)
                self.assertEqual((36, 24), image.size)


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

    async def _assert_activation_waits_for_mutation(self, operation: str):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            filename = f"serialized-{operation}.png"
            current_path = Path(server.image_dir) / filename
            Image.new("RGB", (48, 64), (230, 210, 190)).save(current_path)
            current_bytes = current_path.read_bytes()
            old_path = root / "old.png"
            Image.new("RGB", (36, 54), (70, 100, 150)).save(old_path)
            old_bytes = old_path.read_bytes()
            old_version = archive_image_version(
                server.data_dir,
                str(old_path),
                original_image_filename=filename,
                target="custom",
                target_label="其他",
                instruction="切换并发测试",
            )
            stored_entry = {
                "id": filename,
                "date": "2026-07-18",
                "time": "13:42",
                "image_filename": filename,
                "image_path": f"/images/{filename}",
                "status": "ok",
                "image_versions": [old_version],
            }
            ScheduleStore(server.data_dir).save({"card": stored_entry})

            mutation_started = asyncio.Event()
            release_mutation = asyncio.Event()

            async def blocking_mutation(*_args):
                mutation_started.set()
                await release_mutation.wait()
                return dict(stored_entry)

            if operation == "edit":
                server.on_edit_image = blocking_mutation
                mutation_path = f"/api/images/{filename}/edit"
                mutation_kwargs = {
                    "json": {"target": "background", "instruction": "改成雨后街道"}
                }
            else:
                server.on_reroll_image = blocking_mutation
                mutation_path = f"/api/images/{filename}/reroll"
                mutation_kwargs = {"json": {}}

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            mutation_task = None
            activation_task = None
            try:
                mutation_task = asyncio.create_task(
                    client.post(mutation_path, **mutation_kwargs)
                )
                await asyncio.wait_for(mutation_started.wait(), timeout=1)
                activation_task = asyncio.create_task(client.post(
                    f"/api/images/{filename}/versions/{old_version['id']}/activate"
                ))
                activation_response = await asyncio.wait_for(activation_task, timeout=3)
                activation_payload = await activation_response.json()
                # Version switch fails fast while another mutation holds the lock.
                self.assertEqual(409, activation_response.status)
                self.assertEqual("image_busy", activation_payload.get("error"))
                release_mutation.set()
                mutation_response = await asyncio.wait_for(mutation_task, timeout=1)
                mutation_payload = await mutation_response.json()
            finally:
                release_mutation.set()
                pending = [
                    task for task in (mutation_task, activation_task)
                    if task is not None and not task.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                await client.close()

            self.assertEqual(200, mutation_response.status)
            self.assertEqual(filename, mutation_payload["image_filename"])
            # Failed activation must not replace the current card while mutation runs.
            self.assertEqual(current_bytes, current_path.read_bytes())
            self.assertNotEqual(old_bytes, current_path.read_bytes())
            self.assertNotIn(filename, server._image_mutation_locks)
            self.assertNotIn(filename, server._image_mutation_lock_users)

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
            self.assertTrue(gallery["items"][0]["image_revision"])
            self.assertNotIn("image_versions", gallery["items"][0])
            self.assertEqual(1, detail["version_count"])
            self.assertEqual(
                gallery["items"][0]["image_revision"],
                detail["image_revision"],
            )
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

    async def test_replaced_reference_preview_uses_archived_version_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            current_filename = "schedule_0816_current.png"
            replaced_filename = "zhuzhu_schedule_0816_old.png"
            Image.new("RGB", (24, 32), (180, 170, 160)).save(
                Path(server.image_dir) / current_filename
            )
            old_path = root / replaced_filename
            Image.new("RGB", (24, 32), (90, 100, 110)).save(old_path)
            old_version = archive_image_version(
                server.data_dir,
                str(old_path),
                original_image_filename=replaced_filename,
                target="outfit",
                target_label="穿搭",
                instruction="替换穿搭",
            )
            ScheduleStore(server.data_dir).save({
                "card": {
                    "image_filename": current_filename,
                    "image_path": f"/images/{current_filename}",
                    "status": "ok",
                    "selected_reference": {
                        "filename": replaced_filename,
                        "url": f"/images/{replaced_filename}",
                        "label": "原图",
                        "source": "gallery",
                    },
                    "requested_ref_image": replaced_filename,
                    "requested_ref_image_path": f"/images/{replaced_filename}",
                    "image_versions": [old_version],
                },
            })

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                response = await client.get("/api/gallery?limit=10")
                payload = await response.json()
                item = payload["items"][0]
                preview_url = item["selected_reference"]["url"]
                preview_response = await client.get(preview_url)
            finally:
                await client.close()

            self.assertEqual(200, response.status)
            self.assertIn(f"/api/images/{current_filename}/versions/", preview_url)
            self.assertEqual("", item["requested_ref_image"])
            self.assertEqual("", item["requested_ref_image_path"])
            self.assertEqual(200, preview_response.status)

    async def test_missing_replaced_reference_keeps_label_without_broken_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            current_filename = "schedule_0816_current.png"
            replaced_filename = "zhuzhu_schedule_0816_missing.png"
            Image.new("RGB", (24, 32), (180, 170, 160)).save(
                Path(server.image_dir) / current_filename
            )
            ScheduleStore(server.data_dir).save({
                "card": {
                    "image_filename": current_filename,
                    "image_path": f"/images/{current_filename}",
                    "status": "ok",
                    "selected_reference": {
                        "filename": replaced_filename,
                        "url": f"/images/{replaced_filename}",
                        "label": "原图",
                        "source": "gallery",
                    },
                    "requested_ref_image": replaced_filename,
                    "requested_ref_image_path": f"/images/{replaced_filename}",
                },
            })

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                response = await client.get("/api/gallery?limit=10")
                payload = await response.json()
            finally:
                await client.close()

            item = payload["items"][0]
            self.assertEqual(200, response.status)
            self.assertEqual("原图", item["selected_reference"]["label"])
            self.assertNotIn("url", item["selected_reference"])
            self.assertNotIn("filename", item["selected_reference"])
            self.assertEqual("", item["requested_ref_image"])
            self.assertEqual("", item["requested_ref_image_path"])

    async def test_versions_can_switch_back_and_forth_without_creating_a_new_card(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            server = self._make_server(root)
            filename = "switchable.png"
            current_path = Path(server.image_dir) / filename
            Image.new("RGB", (48, 64), (230, 210, 190)).save(current_path)
            current_bytes = current_path.read_bytes()
            old_path = root / "old.png"
            Image.new("RGB", (36, 54), (70, 100, 150)).save(old_path)
            old_bytes = old_path.read_bytes()
            old_version = archive_image_version(
                server.data_dir,
                str(old_path),
                original_image_filename=filename,
                archived_at="2026-07-17T13:59:00",
                target="custom",
                target_label="其他",
                instruction="脸上不要有油腻感",
            )
            old_archive_path = image_version_path(server.data_dir, old_version)
            ScheduleStore(server.data_dir).save({
                "card": {
                    "id": filename,
                    "date": "2026-07-17",
                    "time": "13:42",
                    "image_filename": filename,
                    "image_path": f"/images/{filename}",
                    "status": "ok",
                    "image_versions": [old_version],
                },
            })

            test_server = TestServer(server.app)
            await test_server.start_server(access_log=None)
            client = TestClient(test_server)
            try:
                first_switch = await client.post(
                    f"/api/images/{filename}/versions/{old_version['id']}/activate"
                )
                first_payload = await first_switch.json()
                first_versions_response = await client.get(
                    f"/api/images/{filename}/versions"
                )
                first_versions = await first_versions_response.json()
                first_gallery_response = await client.get("/api/gallery?limit=10")
                first_gallery = await first_gallery_response.json()
                new_version_id = first_versions["items"][0]["id"]
                archived_new_response = await client.get(
                    first_versions["items"][0]["image_url"]
                )
                archived_new_bytes = await archived_new_response.read()
                first_current_bytes = current_path.read_bytes()

                second_switch = await client.post(
                    f"/api/images/{filename}/versions/{new_version_id}/activate"
                )
                second_payload = await second_switch.json()
                second_versions_response = await client.get(
                    f"/api/images/{filename}/versions"
                )
                second_versions = await second_versions_response.json()
                archived_old_response = await client.get(
                    second_versions["items"][0]["image_url"]
                )
                archived_old_bytes = await archived_old_response.read()
            finally:
                await client.close()

            self.assertEqual(200, first_switch.status)
            self.assertEqual(filename, first_payload["image_filename"])
            self.assertEqual(1, first_payload["version_count"])
            self.assertTrue(first_payload["image_revision"])
            self.assertEqual(
                first_payload["image_revision"],
                first_gallery["items"][0]["image_revision"],
            )
            self.assertEqual((36, 54), (first_payload["width"], first_payload["height"]))
            self.assertEqual("36x54", first_payload["size"])
            self.assertEqual(old_bytes, first_current_bytes)
            self.assertFalse(old_archive_path.exists())
            self.assertEqual(1, first_versions["version_count"])
            self.assertNotEqual(old_version["id"], new_version_id)
            self.assertEqual(current_bytes, archived_new_bytes)

            self.assertEqual(200, second_switch.status)
            self.assertEqual(filename, second_payload["image_filename"])
            self.assertEqual(1, second_payload["version_count"])
            self.assertEqual((48, 64), (second_payload["width"], second_payload["height"]))
            self.assertEqual("48x64", second_payload["size"])
            self.assertEqual(current_bytes, current_path.read_bytes())
            self.assertEqual(1, second_versions["version_count"])
            self.assertEqual(old_bytes, archived_old_bytes)
            self.assertEqual("custom", second_versions["items"][0]["target"])
            self.assertEqual("其他", second_versions["items"][0]["target_label"])
            self.assertEqual("脸上不要有油腻感", second_versions["items"][0]["instruction"])
            self.assertEqual("2026-07-17T13:59:00", second_versions["items"][0]["archived_at"])

    async def test_version_activation_waits_for_background_edit(self):
        await self._assert_activation_waits_for_mutation("edit")

    async def test_version_activation_waits_for_background_reroll(self):
        await self._assert_activation_waits_for_mutation("reroll")


if __name__ == "__main__":
    unittest.main()
