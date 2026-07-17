import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))
_TEST_LOG_DIR = tempfile.TemporaryDirectory(prefix="portrait-gallery-size-tests-")
os.environ["HERMES_GALLERY_LOG"] = str(Path(_TEST_LOG_DIR.name) / "gallery.log")

import main as main_module  # noqa: E402
from main import PortraitGalleryApp  # noqa: E402
from settings import (  # noqa: E402
    LEGACY_SCHEDULE_IMAGE_FRAMING_RULE,
    SCHEDULE_IMAGE_FRAMING_RULE,
    SCHEDULE_IMAGE_FRAMING_MARKER,
    SCHEDULE_IMAGE_OOTD_FRAMING_RULE,
    SCHEDULE_IMAGE_SCENERY_FRAMING_RULE,
    apply_schedule_image_framing,
    schedule_image_size,
)


class ScheduleImageSizeTest(unittest.TestCase):
    def test_schedule_images_default_to_gallery_ratio(self):
        self.assertEqual("1536x2048", schedule_image_size({}))
        self.assertEqual(
            "1536x2048",
            schedule_image_size({"image_gen": {"metadata_size": "1536x2048"}}),
        )

    def test_explicit_valid_schedule_size_can_override_default(self):
        self.assertEqual(
            "768x1024",
            schedule_image_size({"image_gen": {"schedule_size": "768x1024"}}),
        )

    def test_invalid_schedule_size_falls_back_to_three_by_four(self):
        self.assertEqual(
            "1536x2048",
            schedule_image_size({"image_gen": {"schedule_size": "941x1672"}}),
        )

    def test_non_three_by_four_known_sizes_also_fall_back(self):
        for size in ("1024x1024", "2048x1536", "1365x2048", "1152x2048"):
            with self.subTest(size=size):
                self.assertEqual(
                    "1536x2048",
                    schedule_image_size({"image_gen": {"schedule_size": size}}),
                )

    def test_scheduled_reroll_repairs_size_but_custom_reroll_preserves_it(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app.config = {}
        metadata = {"size": "941x1672"}

        self.assertEqual("1536x2048", app._reroll_image_size(True, metadata))
        self.assertEqual("941x1672", app._reroll_image_size(False, metadata))

    def test_schedule_prompt_uses_adaptive_photographic_framing(self):
        prompt = apply_schedule_image_framing("watering plants by the desk")

        self.assertIn(SCHEDULE_IMAGE_FRAMING_RULE, prompt)
        self.assertIn("FRAMING MODE: ORDINARY DAILY PORTRAIT", prompt)
        self.assertIn("waist, hips, or mid-thigh", prompt)
        self.assertIn("feet, shoes, and most of the floor outside the frame", prompt)
        self.assertIn("50-85mm-equivalent portrait perspective", prompt)
        self.assertIn("No high-angle, overhead", prompt)
        self.assertIn("make the subject look small or squat", prompt)
        self.assertIn("no black bars", prompt)
        self.assertIn("no black bars", SCHEDULE_IMAGE_FRAMING_RULE)
        self.assertNotIn("Mandatory 3:4 full-body", prompt)
        self.assertEqual(prompt, apply_schedule_image_framing(prompt))

    def test_schedule_prompt_uses_ordinary_mode_despite_full_outfit_and_standing(self):
        prompt = apply_schedule_image_framing(
            "Today's plan: Activity: trim leaves beside the desk. "
            "Action: stand by the plant and use small scissors. "
            "Scene: bright study. Outfit: cardigan, midi skirt, and Mary Jane shoes."
        )

        self.assertIn(SCHEDULE_IMAGE_FRAMING_RULE, prompt)
        self.assertNotIn(SCHEDULE_IMAGE_OOTD_FRAMING_RULE, prompt)
        self.assertNotIn(SCHEDULE_IMAGE_SCENERY_FRAMING_RULE, prompt)

    def test_schedule_prompt_uses_full_body_only_for_explicit_ootd_intent(self):
        prompt = apply_schedule_image_framing(
            "Today's plan: Activity: photograph an OOTD clothing showcase. "
            "Action: check and show the complete outfit in a mirror. "
            "Scene: dressing room. Outfit: coordinated look."
        )

        self.assertIn(SCHEDULE_IMAGE_OOTD_FRAMING_RULE, prompt)
        self.assertIn("level, eye-height full-body fashion photograph", prompt)
        self.assertNotIn(SCHEDULE_IMAGE_FRAMING_RULE, prompt)

    def test_schedule_prompt_uses_environmental_mode_for_explicit_scenery_intent(self):
        prompt = apply_schedule_image_framing(
            "Today's plan: Activity: share the city skyline scenery. "
            "Action: photograph the landscape from an overlook. "
            "Scene: hilltop viewing deck. Outfit: casual jacket."
        )

        self.assertIn(SCHEDULE_IMAGE_SCENERY_FRAMING_RULE, prompt)
        self.assertIn("wider environmental composition", prompt)
        self.assertNotIn(SCHEDULE_IMAGE_FRAMING_RULE, prompt)

    def test_schedule_prompt_replaces_legacy_mandatory_full_body_rule(self):
        legacy_prompt = f"watering plants by the desk. {LEGACY_SCHEDULE_IMAGE_FRAMING_RULE}"

        prompt = apply_schedule_image_framing(legacy_prompt)

        self.assertNotIn(LEGACY_SCHEDULE_IMAGE_FRAMING_RULE, prompt)
        self.assertIn(SCHEDULE_IMAGE_FRAMING_RULE, prompt)
        self.assertEqual(1, prompt.count(SCHEDULE_IMAGE_FRAMING_MARKER))

    def test_schedule_prompt_replaces_previous_adaptive_framing_version(self):
        old_rule = f"{SCHEDULE_IMAGE_FRAMING_MARKER} old conditional framing rule"
        old_prompt = (
            "Today's plan: Activity: water plants. Action: trim leaves. "
            f"Scene: study room. {old_rule}"
        )

        prompt = apply_schedule_image_framing(old_prompt)

        self.assertNotIn(old_rule, prompt)
        self.assertIn(SCHEDULE_IMAGE_FRAMING_RULE, prompt)
        self.assertEqual(1, prompt.count(SCHEDULE_IMAGE_FRAMING_MARKER))


class PhotoJobSizeCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_photo_job_passes_stable_schedule_size(self):
        app = PortraitGalleryApp.__new__(PortraitGalleryApp)
        app.config = {"image_gen": {"metadata_size": "1536x2048"}}
        app._photo_job_schedule_meta = {}
        app._slot_key_for_schedule_time = lambda _value: ("", "", "")
        app._photo_job_id_for_time = lambda _value: ""
        app._is_photo_quiet_now = lambda: False
        app._today_schedule_entry = lambda: {}

        async def select_reference(_context):
            return {}

        app._select_reference_for_generation = select_reference
        app.image_gen = SimpleNamespace(
            python_executable=sys.executable,
            generate_script="/tmp/generate.py",
            script_dir="/tmp",
            build_env=lambda: {},
        )
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(main_module.subprocess, "run", return_value=completed) as run:
            result = await app.photo_job("morning")

        self.assertTrue(result)
        command = run.call_args.args[0]
        size_index = command.index("--size")
        self.assertEqual("1536x2048", command[size_index + 1])


if __name__ == "__main__":
    unittest.main()
