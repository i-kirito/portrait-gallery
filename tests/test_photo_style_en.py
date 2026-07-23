import sys
import unittest
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
ZHUZHU_DIR = APP_DIR / "zhuzhu"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(ZHUZHU_DIR))

import core as zhuzhu_core  # noqa: E402


class PhotoStyleEnTest(unittest.TestCase):
    def test_llm_photo_style_replaces_fixed_quality_prefix(self):
        style = (
            "Candid handheld smartphone snapshot after dinner, practical street ambient light, "
            "true-to-life color, slightly imperfect framing."
        )
        with patch.object(
            zhuzhu_core,
            "_read_custom_appearance",
            return_value="adult woman with natural facial features and realistic body proportions",
        ):
            prompt = zhuzhu_core.build_prompt(
                "evening",
                schedule_activity="walk along the riverside promenade after dinner",
                outfit_keywords="cream knit cardigan, soft jeans, white sneakers",
                scene_keywords="riverside promenade after dinner",
                hair_keywords="low ponytail with soft bangs",
                photo_style_en=style,
            )

        self.assertIn("practical street ambient light", prompt.lower())
        self.assertIn("slightly imperfect framing", prompt.lower())
        self.assertIn("natural skin texture", prompt.lower())
        # fixed hybrid prefix should not be forced when LLM style is present
        self.assertNotIn("no cinematic color grade", prompt.lower())
        self.assertNotIn("subtle sensor noise", prompt.lower())

    def test_missing_photo_style_falls_back_to_quality_prefix(self):
        with patch.object(
            zhuzhu_core,
            "_read_custom_appearance",
            return_value="adult woman with natural facial features and realistic body proportions",
        ):
            prompt = zhuzhu_core.build_prompt(
                "morning",
                schedule_activity="water balcony plants",
                outfit_keywords="soft cardigan, cotton shorts",
                scene_keywords="sunny balcony",
                hair_keywords="half-up with a clip",
            )
        self.assertIn("candid real-life smartphone photograph", prompt.lower())

    def test_photo_style_strips_masterpiece_and_cinematic_drift(self):
        cleaned = zhuzhu_core._normalize_photo_style_en(
            "Masterpiece quality, cinematic lighting, candid phone snapshot"
        )
        self.assertNotIn("masterpiece", cleaned.lower())
        self.assertNotIn("cinematic lighting", cleaned.lower())
        self.assertIn("candid phone snapshot", cleaned.lower())


if __name__ == "__main__":
    unittest.main()
