"""Custom injected prompts stay short and scene-first."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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

    def test_xiaohongshu_multi_ref_uses_configured_body_profile(self) -> None:
        appearance = (
            "dusty rose hair, dark brown eyes, fair skin, 168 cm tall, "
            "petite slender figure, narrow shoulders, balanced leg proportions"
        )
        with patch("main.load_runtime_persona", return_value={"appearance": appearance}):
            prompt = self.app._apply_custom_reference_role_guard(
                "summer street outfit.",
                ["/local-refs/xiaohongshu/look.webp", "/refs/face.jpg"],
                {"source": "xiaohongshu"},
            )

        self.assertIn("[XHS_OUTFIT_REFERENCE_MODE]", prompt)
        self.assertIn("168 cm tall", prompt)
        self.assertIn("petite slender figure", prompt)
        self.assertIn("narrow shoulders", prompt)
        self.assertIn("ONLY source for the target physique", prompt)
        self.assertIn("Image 2", prompt)
        self.assertIn("sole and authoritative facial identity source", prompt)
        self.assertIn("generic influencer face", prompt)
        self.assertIn("outside Image 2's facial region as transparent", prompt)
        self.assertIn("must never come from Image 2", prompt)

    def test_xiaohongshu_prompt_can_omit_generic_identity_cues(self) -> None:
        with patch(
            "main.load_runtime_persona",
            return_value={"appearance": "long black hair, fair skin, expressive eyes"},
        ):
            prompt = self.app._build_light_custom_prompt(
                "summer outfit",
                "full-body photo",
                include_identity=False,
            )

        self.assertNotIn("expressive eyes", prompt.lower())
        self.assertNotIn("long black hair", prompt.lower())
        self.assertIn("summer outfit", prompt)

    def test_non_xiaohongshu_multi_ref_keeps_normal_prompt(self) -> None:
        prompt = self.app._apply_custom_reference_role_guard(
            "portrait scene.",
            ["/refs/base.jpg", "/refs/face.jpg"],
            {"source": "default"},
        )

        self.assertEqual("portrait scene.", prompt)


class CustomImageCaptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_caption_uses_generated_image_without_prompt_text(self) -> None:
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "generated.png"
            image_path.write_bytes(b"image")
            app.image_gen = SimpleNamespace(output_dir=tmpdir)
            with patch(
                "main.build_caption_for_image",
                return_value="雪枫坐在窗边，准备把手里的书再看两页。",
            ) as build_caption:
                caption = await app._generate_custom_image_caption("generated.png")

        self.assertEqual("雪枫坐在窗边，准备把手里的书再看两页。", caption)
        build_caption.assert_called_once_with(
            "custom",
            str(image_path),
            request_timeout=60,
            llm_attempts=1,
            require_image=True,
            allow_fallback=False,
        )

    async def test_custom_caption_failure_never_uses_generic_fallback(self) -> None:
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app.image_gen = SimpleNamespace(output_dir="/missing")
        with patch("main.build_caption_fallback") as fallback:
            caption = await app._generate_custom_image_caption("missing.png")

        self.assertEqual("", caption)
        fallback.assert_not_called()

    async def test_background_caption_persists_ready_only_after_visual_success(self) -> None:
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app._generate_custom_image_caption = AsyncMock(return_value="红色针织衫在草地阳光下很醒目。")
        app._persist_custom_caption_state = Mock()

        await app._complete_custom_image_caption("generated.png")

        app._persist_custom_caption_state.assert_called_once_with(
            "generated.png",
            "红色针织衫在草地阳光下很醒目。",
            "ready",
        )

    async def test_background_caption_marks_failed_without_fake_copy(self) -> None:
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app._generate_custom_image_caption = AsyncMock(return_value="")
        app._persist_custom_caption_state = Mock()

        await app._complete_custom_image_caption("generated.png")

        app._persist_custom_caption_state.assert_called_once_with(
            "generated.png",
            "",
            "failed",
        )


if __name__ == "__main__":
    unittest.main()
