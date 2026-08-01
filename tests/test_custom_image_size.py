import sys
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from settings import normalize_custom_image_size  # noqa: E402


class CustomImageSizeTest(unittest.TestCase):
    def test_new_common_ratios_map_to_expected_sizes(self):
        cases = {
            ("3:2", "1k"): "1536x1024",
            ("16:9", "2k"): "2048x1152",
            ("21:9", "4k"): "4096x1755",
        }
        for (aspect, resolution), expected in cases.items():
            with self.subTest(aspect=aspect, resolution=resolution):
                self.assertEqual(
                    expected,
                    normalize_custom_image_size("", aspect, resolution),
                )

    def test_auto_mode_omits_explicit_dimensions(self):
        self.assertEqual("", normalize_custom_image_size("auto", "auto", "auto"))
        self.assertEqual("", normalize_custom_image_size("自动", "", ""))

    def test_custom_dimensions_are_normalized(self):
        self.assertEqual(
            "1280x1920",
            normalize_custom_image_size(" 1280 × 1920 ", "custom", "custom"),
        )
        self.assertEqual(
            "4096x4096",
            normalize_custom_image_size("4096x4096", "custom", "custom"),
        )

    def test_unsafe_custom_dimensions_fall_back(self):
        for size in ("255x1024", "4097x1024", "1024x9000", "not-a-size"):
            with self.subTest(size=size):
                self.assertEqual(
                    "1024x1024",
                    normalize_custom_image_size(size, "custom", "custom"),
                )

    def test_explicit_ratio_takes_priority_over_auto_size_token(self):
        self.assertEqual(
            "768x1024",
            normalize_custom_image_size("auto", "3:4", "1k"),
        )


if __name__ == "__main__":
    unittest.main()
