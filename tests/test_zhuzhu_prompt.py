import os
import sys
import unittest
from unittest.mock import patch


APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
ZHUZHU_DIR = os.path.join(APP_DIR, "zhuzhu")
for path in (APP_DIR, ZHUZHU_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from zhuzhu.core import build_prompt
from zhuzhu import generate as generate_module


class ScheduledPromptTests(unittest.TestCase):
    def test_structured_schedule_keeps_full_scene_guidance(self):
        schedule = (
            "Activity: open curtains. Action: standing by the window. "
            "Scene: sunlit study. Outfit: purple silk blouse. "
            "Hair: low side-parted updo. Props: curtains and notebook. "
            "Lighting: soft morning daylight"
        )
        time_constraint = (
            "The scheduled clock time is 07:18, early morning. "
            "Use soft early-morning natural daylight"
        )

        prompt = build_prompt(
            "morning",
            schedule_activity=schedule,
            outfit_keywords="purple silk blouse",
            scene_keywords="sunlit study, curtains and notebook",
            hair_keywords="low side-parted updo",
            time_constraint=time_constraint,
        )

        self.assertIn("Activity: open curtains", prompt)
        self.assertIn("Action: standing by the window", prompt)
        self.assertIn("Scene: sunlit study", prompt)
        self.assertIn("Outfit: purple silk blouse", prompt)
        self.assertIn("Hair: low side-parted updo", prompt)
        self.assertIn("Props: curtains and notebook", prompt)
        self.assertIn("Lighting: soft morning daylight", prompt)
        self.assertIn("The scheduled clock time is 07:18", prompt)
        self.assertIn("Use this schedule text as the source of truth", prompt)
        self.assertIn("Let the model infer the most natural eye line", prompt)

    def test_plain_schedule_adds_scene_outfit_and_time(self):
        prompt = build_prompt(
            "morning",
            schedule_activity="open curtains",
            outfit_keywords="purple silk blouse",
            scene_keywords="sunlit study",
            hair_keywords="low side-parted updo",
            time_constraint="early morning daylight",
        )

        self.assertIn("Current scheduled scene from today's LLM plan: open curtains", prompt)
        self.assertIn("purple silk blouse", prompt)
        self.assertIn("sunlit study", prompt)
        self.assertIn("low side-parted updo", prompt)
        self.assertIn("early morning daylight", prompt)

    def test_generation_entrypoint_passes_schedule_once(self):
        schedule = (
            "Activity: open curtains. Action: standing by the window. "
            "Scene: sunlit study. Outfit: purple silk blouse. "
            "Hair: low side-parted updo. Props: notebook. "
            "Lighting: soft morning daylight"
        )
        context = (
            f"Today's plan: {schedule}. Time: early morning daylight. "
            "Style: elegant style"
        )
        captured = {}

        def fake_generate(_theme, **kwargs):
            captured["prompt"] = kwargs["prompt_override"]
            return "/tmp/generated.png"

        with (
            patch.object(generate_module, "resolve_prompt", return_value="unused"),
            patch.object(
                generate_module,
                "_get_schedule_context",
                return_value=(
                    context,
                    "07:18 open curtains",
                    "purple silk blouse",
                    "sunlit study",
                    "low side-parted updo",
                ),
            ),
            patch.object(generate_module, "generate_with_gptimage", side_effect=fake_generate),
            patch("core.sync_to_gallery"),
        ):
            result = generate_module.generate(
                "morning",
                schedule_time="07:18 open curtains",
                no_auto_style=True,
            )

        self.assertEqual(result, "/tmp/generated.png")
        self.assertIn("Activity: open curtains", captured["prompt"])
        self.assertIn("Action: standing by the window", captured["prompt"])
        self.assertIn("Today's plan:", captured["prompt"])
        self.assertIn("Outfit: purple silk blouse", captured["prompt"])
        self.assertIn("early morning daylight", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
