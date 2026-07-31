import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image


APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
ZHUZHU_DIR = os.path.join(APP_DIR, "zhuzhu")
for path in (APP_DIR, ZHUZHU_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import generate_gptimage  # noqa: E402
from web_server import GalleryServer  # noqa: E402


class WebImageGenerationTests(unittest.TestCase):
    def test_gptimage_records_submitted_and_original_prompts(self):
        image_buffer = io.BytesIO()
        Image.new("RGB", (2, 2), "white").save(image_buffer, format="PNG")
        captured_metadata = {}

        def fake_direct(_prompt, **kwargs):
            kwargs["request_info"]["submitted_prompt"] = "compact submitted prompt"
            return image_buffer.getvalue(), 1.25

        with tempfile.TemporaryDirectory() as image_dir:
            server = GalleryServer.__new__(GalleryServer)
            server.image_dir = image_dir
            server._display_model_name = lambda value: value
            server._wardrobe_reference_for_value = lambda _value: {}
            server._update_image_metadata_entry = (
                lambda _filename, entry: captured_metadata.update(entry)
            )

            with patch.object(
                generate_gptimage,
                "_generate_via_direct_gpt",
                side_effect=fake_direct,
            ):
                result = server._run_hermes_image_generation(
                    "gptimage",
                    "original long user prompt",
                    output_dir=image_dir,
                    classify_style=False,
                )

        self.assertTrue(result["success"])
        self.assertEqual("compact submitted prompt", captured_metadata["prompt"])
        self.assertEqual("compact submitted prompt", captured_metadata["custom_prompt"])
        self.assertEqual("original long user prompt", captured_metadata["user_prompt"])
        self.assertEqual("compact submitted prompt", result["custom_prompt"])
        self.assertEqual("original long user prompt", result["user_prompt"])


if __name__ == "__main__":
    unittest.main()
