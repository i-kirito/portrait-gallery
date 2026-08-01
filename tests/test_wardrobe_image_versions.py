"""Wardrobe hanger images keep history versions on regen."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

from image_versions import find_image_version, normalize_image_versions  # noqa: E402
from web_server import GalleryServer  # noqa: E402


class WardrobeImageVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.ref_dir = self.data_dir / "references"
        self.wardrobe_dir = self.ref_dir / "wardrobe"
        self.wardrobe_dir.mkdir(parents=True)
        (self.data_dir / "images").mkdir(parents=True)
        self.server = GalleryServer.__new__(GalleryServer)
        self.server.data_dir = str(self.data_dir)
        self.server.reference_dir = str(self.ref_dir)
        self.server.wardrobe_reference_dir = str(self.wardrobe_dir)
        self.server.image_dir = str(self.data_dir / "images")
        self.server._wardrobe_image_locks = {}

        outfit = {
            "id": "abcd1234efgh5678",
            "date": "2026-07-23",
            "outfit_style": "甜妹风",
            "outfit": {"风格": "甜妹风", "穿搭": "测试裙子"},
            "created_at": 1,
            "wardrobe_image": {
                "filename": "wardrobe_abcd1234_old.png",
                "url": "/local-refs/wardrobe/wardrobe_abcd1234_old.png",
                "prompt": "old",
                "created_at": 1,
            },
        }
        old = self.wardrobe_dir / "wardrobe_abcd1234_old.png"
        Image.new("RGB", (32, 48), color=(200, 100, 120)).save(old)
        (self.data_dir / "favorite_outfits.json").write_text(
            json.dumps({"items": [outfit]}, ensure_ascii=False),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_regen_archives_previous_instead_of_delete_only(self) -> None:
        new_path = self.wardrobe_dir / "wardrobe_abcd1234_new.png"
        Image.new("RGB", (40, 60), color=(90, 140, 200)).save(new_path)

        def fake_run(*args, **kwargs):
            return {
                "filename": new_path.name,
                "path": str(new_path),
                "url": f"/local-refs/wardrobe/{new_path.name}",
                "source": "wardrobe",
                "model_name": "gpt-image-2",
                "file_size_bytes": new_path.stat().st_size,
                "width": 40,
                "height": 60,
            }

        with patch.object(self.server, "_run_hermes_image_generation", side_effect=fake_run):
            result = asyncio.run(
                self.server._generate_and_store_favorite_outfit_wardrobe_image(
                    "abcd1234efgh5678",
                    prompt="new hanger",
                    size="1024x1536",
                    replace_existing=True,
                )
            )

        self.assertEqual(result.get("filename"), new_path.name)
        item = self.server._favorite_outfit_by_id("abcd1234efgh5678")
        versions = normalize_image_versions(item.get("wardrobe_image_versions"))
        self.assertEqual(len(versions), 1)
        resolved = find_image_version(str(self.data_dir), versions, versions[0]["id"])
        self.assertIsNotNone(resolved)
        # previous live file should be removed after successful archive
        self.assertFalse((self.wardrobe_dir / "wardrobe_abcd1234_old.png").exists())
        self.assertTrue(new_path.exists())

    def test_activate_restores_archived_and_keeps_current(self) -> None:
        # first create an archive by regenerating
        new_path = self.wardrobe_dir / "wardrobe_abcd1234_new.png"
        Image.new("RGB", (40, 60), color=(90, 140, 200)).save(new_path)

        def fake_run(*args, **kwargs):
            return {
                "filename": new_path.name,
                "path": str(new_path),
                "url": f"/local-refs/wardrobe/{new_path.name}",
                "source": "wardrobe",
                "model_name": "gpt-image-2",
                "file_size_bytes": new_path.stat().st_size,
                "width": 40,
                "height": 60,
            }

        with patch.object(self.server, "_run_hermes_image_generation", side_effect=fake_run):
            asyncio.run(
                self.server._generate_and_store_favorite_outfit_wardrobe_image(
                    "abcd1234efgh5678",
                    prompt="new hanger",
                    size="1024x1536",
                    replace_existing=True,
                )
            )

        item = self.server._favorite_outfit_by_id("abcd1234efgh5678")
        version_id = normalize_image_versions(item.get("wardrobe_image_versions"))[0]["id"]

        class FakeRequest:
            def __init__(self, outfit_id, version_id):
                self.match_info = {"outfit_id": outfit_id, "version_id": version_id}

        response = asyncio.run(
            self.server.handle_activate_favorite_outfit_wardrobe_version(
                FakeRequest("abcd1234efgh5678", version_id)
            )
        )
        self.assertEqual(response.status, 200)
        payload = json.loads(response.text)
        self.assertTrue(payload.get("success"))
        # current image file still exists
        current_name = payload["wardrobe_image"]["filename"]
        self.assertTrue((self.wardrobe_dir / current_name).exists())

    def test_activate_save_failure_rolls_back_switched_file(self) -> None:
        # Build one archived version by regenerating the hanger image.
        new_path = self.wardrobe_dir / "wardrobe_abcd1234_new.png"
        Image.new("RGB", (40, 60), color=(90, 140, 200)).save(new_path)

        def fake_run(*args, **kwargs):
            return {
                "filename": new_path.name,
                "path": str(new_path),
                "url": f"/local-refs/wardrobe/{new_path.name}",
                "source": "wardrobe",
                "model_name": "gpt-image-2",
                "file_size_bytes": new_path.stat().st_size,
                "width": 40,
                "height": 60,
            }

        with patch.object(self.server, "_run_hermes_image_generation", side_effect=fake_run):
            asyncio.run(
                self.server._generate_and_store_favorite_outfit_wardrobe_image(
                    "abcd1234efgh5678",
                    prompt="new hanger",
                    size="1024x1536",
                    replace_existing=True,
                )
            )

        item = self.server._favorite_outfit_by_id("abcd1234efgh5678")
        records = normalize_image_versions(item.get("wardrobe_image_versions"))
        version_id = records[0]["id"]
        resolved = find_image_version(str(self.data_dir), records, version_id)
        self.assertIsNotNone(resolved)
        version_path = resolved[1]

        target_path = self.wardrobe_dir / item["wardrobe_image"]["filename"]
        json_path = self.data_dir / "favorite_outfits.json"
        archive_dir = self.data_dir / "image_versions"
        target_bytes_before = target_path.read_bytes()
        version_bytes_before = version_path.read_bytes()
        json_bytes_before = json_path.read_bytes()
        archive_names_before = sorted(p.name for p in archive_dir.iterdir())

        class FakeRequest:
            def __init__(self, outfit_id, version_id):
                self.match_info = {"outfit_id": outfit_id, "version_id": version_id}

        with patch.object(
            self.server, "_update_favorite_outfits", side_effect=OSError("disk full")
        ):
            response = asyncio.run(
                self.server.handle_activate_favorite_outfit_wardrobe_version(
                    FakeRequest("abcd1234efgh5678", version_id)
                )
            )

        self.assertEqual(response.status, 500)
        self.assertEqual(json.loads(response.text).get("error"), "save_failed")
        # target file restored byte-for-byte to the pre-switch content
        self.assertEqual(target_path.read_bytes(), target_bytes_before)
        # favorites JSON untouched
        self.assertEqual(json_path.read_bytes(), json_bytes_before)
        # selected history version file and record remain usable
        self.assertEqual(version_path.read_bytes(), version_bytes_before)
        item_after = self.server._favorite_outfit_by_id("abcd1234efgh5678")
        self.assertIsNotNone(
            find_image_version(
                str(self.data_dir),
                item_after.get("wardrobe_image_versions"),
                version_id,
            )
        )
        # no orphaned pre-switch archive copies are left behind
        self.assertEqual(
            sorted(p.name for p in archive_dir.iterdir()), archive_names_before
        )


if __name__ == "__main__":
    unittest.main()
