"""Custom injected prompts stay short and scene-first."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))
os.chdir(APP_DIR)

from main import PortraitGalleryApp  # noqa: E402
from settings import (  # noqa: E402
    DEFAULT_QUALITY_PREFIX,
    custom_shot_prompt,
    load_config,
)


class LightCustomPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        self.app.config = load_config()
        self.app.data_dir = str(ROOT / "data")

    def test_light_custom_prompt_is_short_and_scene_first(self) -> None:
        scene = "Making yogurt oatmeal cup in a bright kitchen with morning window light"
        shot = custom_shot_prompt("half_body", "1024x1536")
        prompt = self.app._build_light_custom_prompt(scene, shot)

        self.assertTrue(prompt.startswith(scene))
        self.assertLess(len(prompt), 900)
        self.assertNotIn("Candid real-life smartphone photograph", prompt)
        self.assertNotIn("sensor noise", prompt.lower())
        self.assertNotIn("twirling", prompt.lower())
        self.assertNotIn("OOTD", prompt)
        # Heavy hybrid quality prefix must not be re-stacked for custom.
        self.assertNotIn(DEFAULT_QUALITY_PREFIX.splitlines()[0], prompt)

        identity = self.app._compact_custom_appearance()
        if identity:
            self.assertIn(identity.split(",")[0].strip(), prompt)

    def test_compact_appearance_keeps_identity_cues(self) -> None:
        identity = self.app._compact_custom_appearance()
        self.assertTrue(identity)
        low = identity.lower()
        self.assertTrue(
            "hair" in low or "eye" in low or "skin" in low,
            identity,
        )
        self.assertNotIn("hourglass figure", low)


if __name__ == "__main__":
    unittest.main()
