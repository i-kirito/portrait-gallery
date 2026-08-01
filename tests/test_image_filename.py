import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image


APP_DIR = Path(__file__).resolve().parents[1] / "app"
ZHUZHU_DIR = APP_DIR / "zhuzhu"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(ZHUZHU_DIR))

import core  # noqa: E402
from core import save_image, schedule_filename_theme  # noqa: E402


class ImageFilenameTest(unittest.TestCase):
    def test_schedule_time_uses_schedule_prefix(self):
        self.assertEqual(
            "schedule_0830",
            schedule_filename_theme("morning", "8:30 去买咖啡"),
        )
        self.assertEqual("schedule_2015", schedule_filename_theme("evening", "20:15"))

    def test_missing_schedule_time_keeps_theme_label(self):
        self.assertEqual("custom", schedule_filename_theme("custom", ""))
        self.assertEqual("noon", schedule_filename_theme("noon", "午饭后散步"))

    def test_invalid_schedule_time_keeps_theme_label(self):
        self.assertEqual("morning", schedule_filename_theme("morning", "29:80 睡觉"))

    def test_saved_filename_uses_compact_unique_suffix(self):
        image_buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (255, 255, 255)).save(image_buffer, format="PNG")

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            core,
            "WORKSPACE_MEDIA",
            tmpdir,
        ), patch.object(
            core.time,
            "time",
            return_value=1783952093,
        ), patch.object(
            core.uuid,
            "uuid4",
            return_value=SimpleNamespace(hex="b0d94705abcdef"),
        ):
            _path, filename, _timestamp = save_image(
                image_buffer.getvalue(),
                "bedtime",
                "gpt-image-2",
                filename_theme="schedule_2211",
            )

        self.assertEqual("schedule_2211_b0d947_1783952093.png", filename)

    def test_scheduled_save_preserves_upstream_bytes_and_dimensions(self):
        source = Image.new("RGB", (180, 120))
        for y in range(120):
            for x in range(180):
                source.putpixel((x, y), ((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256))
        image_buffer = io.BytesIO()
        source.save(image_buffer, format="PNG")
        upstream_bytes = image_buffer.getvalue()

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(
            core,
            "WORKSPACE_MEDIA",
            tmpdir,
        ):
            path, _filename, _timestamp = save_image(
                image_buffer.getvalue(),
                "morning",
                "gpt-image-2",
                target_size="120x160",
                filename_theme="schedule_0825",
            )
            with Image.open(path) as output:
                self.assertEqual((180, 120), output.size)

            self.assertEqual(upstream_bytes, Path(path).read_bytes())


if __name__ == "__main__":
    unittest.main()
