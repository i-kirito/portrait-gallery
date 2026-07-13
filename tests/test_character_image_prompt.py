import sys
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from characters import (  # noqa: E402
    build_character_image_prompt,
    build_group_image_prompt,
    sanitize_image_prompt,
)


class CharacterImagePromptTest(unittest.TestCase):
    def setUp(self):
        self.character = {
            "id": "hermes",
            "name": "猪猪",
            "persona": '18岁虚拟主播，自称"猪猪"，称呼"主人"。',
            "appearance": (
                "18-year-old Chinese girl, dusty rose pink hair, wispy air bangs, "
                "large round deep-set dark brown eyes, hourglass figure, slim waist, "
                "large natural breasts."
            ),
            "voice": "魅魔体质，喜欢诱惑主人和说色情挑逗的话。",
            "reference_image": "/images/reference.png",
        }

    def test_character_image_prompt_keeps_visual_identity_without_chat_persona(self):
        prompt = build_character_image_prompt(self.character)
        lowered = prompt.lower()

        self.assertIn("dusty rose pink hair", lowered)
        self.assertIn("deep-set dark brown eyes", lowered)
        self.assertIn("reference image hint", lowered)
        self.assertIn("adults aged 21 or older", lowered)
        for forbidden in (
            "identity and persona",
            "voice or mood",
            "主人",
            "18-year-old",
            "breasts",
            "seductive",
            "魅魔",
            "色情",
            "诱惑",
        ):
            self.assertNotIn(forbidden.lower(), lowered)

    def test_scene_prompt_is_rewritten_as_adult_everyday_photography(self):
        prompt = sanitize_image_prompt(
            "18-year-old Chinese girl in a classic Japanese JK school uniform, "
            "large natural breasts, playful seductive smile."
        )
        lowered = prompt.lower()

        self.assertIn("age 21 or older", lowered)
        self.assertIn("adult woman", lowered)
        self.assertIn("sailor-inspired fashion outfit", lowered)
        self.assertIn("warm and confident smile", lowered)
        for forbidden in ("18-year-old", "school uniform", "breasts", "seductive"):
            self.assertNotIn(forbidden, lowered)

    def test_group_image_prompt_omits_configured_relationship_and_voice(self):
        second = {
            "id": "zifeng",
            "name": "紫风",
            "appearance": "adult Chinese man with short black hair and a navy shirt",
            "relationship": "给主人暧昧地贴在一起",
            "voice": "挑逗另一个角色",
        }

        prompt = build_group_image_prompt(
            [self.character, second],
            "两个人穿校服，在卧室露出 seductive smile",
        )
        lowered = prompt.lower()

        self.assertIn("猪猪", prompt)
        self.assertIn("紫风", prompt)
        self.assertIn("natural, non-sexual interaction", lowered)
        self.assertNotIn("主人", prompt)
        self.assertNotIn("暧昧", prompt)
        self.assertNotIn("挑逗", prompt)
        self.assertNotIn("校服", prompt)
        self.assertNotIn("seductive", lowered)


if __name__ == "__main__":
    unittest.main()
