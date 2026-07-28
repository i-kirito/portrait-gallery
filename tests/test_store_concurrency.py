import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
ZHUZHU_DIR = APP_DIR / "zhuzhu"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(ZHUZHU_DIR))

from store import ImageMetadataStore  # noqa: E402
from zhuzhu import core  # noqa: E402


class StoreConcurrencyTest(unittest.TestCase):
    def test_concurrent_gallery_sync_preserves_both_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            gallery_dir = root / "images"
            schedule_path = root / "schedule_data.json"
            sources = []
            for name in ("image_a.jpg", "image_b.jpg"):
                source = root / f"source_{name}"
                source.write_bytes(name.encode("ascii"))
                sources.append((source, name))

            barrier = threading.Barrier(2)

            def sync(source_and_name):
                source, name = source_and_name
                barrier.wait()
                core.sync_to_gallery(
                    str(source),
                    name,
                    "custom",
                    prompt="",
                    source="custom",
                )

            with (
                patch.object(core, "SECRETARY_GALLERY_DIR", str(gallery_dir)),
                patch.object(core, "SECRETARY_SCHEDULE_PATH", str(schedule_path)),
            ):
                threads = [threading.Thread(target=sync, args=(item,)) for item in sources]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            data = json.loads(schedule_path.read_text(encoding="utf-8"))
            self.assertIn("image_a.jpg", data)
            self.assertIn("image_b.jpg", data)

    def test_concurrent_metadata_updates_preserve_every_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ImageMetadataStore(tmpdir)
            barrier = threading.Barrier(12)

            def update(index):
                barrier.wait()

                def merge(metadata):
                    metadata[f"image_{index}.jpg"] = {"index": index}
                    return metadata

                store.update(merge)

            threads = [threading.Thread(target=update, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            metadata = store.load()
            self.assertEqual(12, len(metadata))
            self.assertEqual(set(range(12)), {item["index"] for item in metadata.values()})

    def test_cron_gallery_sync_persists_delivery_pending_before_parent_send(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.jpg"
            source.write_bytes(b"image")
            schedule_path = root / "schedule_data.json"

            with (
                patch.object(core, "SECRETARY_GALLERY_DIR", str(root / "images")),
                patch.object(core, "SECRETARY_SCHEDULE_PATH", str(schedule_path)),
                patch.dict("os.environ", {"ZHUZHU_DELIVERY_PENDING": "1"}),
            ):
                core.sync_to_gallery(
                    str(source),
                    "scheduled.jpg",
                    "noon",
                    source="cron",
                    schedule_time="15:12 测试活动",
                )

            entry = json.loads(schedule_path.read_text(encoding="utf-8"))["scheduled.jpg"]
            self.assertEqual("pending", entry.get("delivery_status"))
            self.assertTrue(entry.get("delivery_updated_at"))

    def test_cross_midnight_gallery_sync_persists_schedule_date(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.jpg"
            source.write_bytes(b"image")
            schedule_path = root / "schedule_data.json"

            with (
                patch.object(core, "SECRETARY_GALLERY_DIR", str(root / "images")),
                patch.object(core, "SECRETARY_SCHEDULE_PATH", str(schedule_path)),
            ):
                core.sync_to_gallery(
                    str(source),
                    "overnight.jpg",
                    "bedtime",
                    source="cron",
                    schedule_time="00:30 昨日尾部活动",
                    schedule_date="2026-07-14",
                )

            entry = json.loads(schedule_path.read_text(encoding="utf-8"))["overnight.jpg"]
            self.assertEqual("2026-07-14", entry["date"])
            self.assertEqual("00:30 昨日尾部活动", entry["schedule_time"])

    def test_gallery_sync_persistence_failure_is_propagated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "source.jpg"
            source.write_bytes(b"image")

            with (
                patch.object(core, "SECRETARY_GALLERY_DIR", str(root / "images")),
                patch.object(core, "SECRETARY_SCHEDULE_PATH", str(root / "schedule_data.json")),
                patch.object(core.ScheduleStore, "update", side_effect=OSError("disk full")),
            ):
                with self.assertRaisesRegex(RuntimeError, "gallery_sync_failed"):
                    core.sync_to_gallery(
                        str(source),
                        "failed.jpg",
                        "custom",
                        prompt="",
                        source="custom",
                    )


if __name__ == "__main__":
    unittest.main()
