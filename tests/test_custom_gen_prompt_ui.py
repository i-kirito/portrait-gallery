from pathlib import Path
import re
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"


class CustomGenerationPromptUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

    def test_prompt_uses_compact_two_line_control(self):
        prompt_markup = re.search(r'<textarea\s+id="cgPrompt"[^>]*>', self.html)
        self.assertIsNotNone(prompt_markup)
        self.assertIn('rows="2"', prompt_markup.group(0))

        prompt_style = re.search(r"#cgPrompt\s*\{(?P<body>[^}]*)\}", self.html)
        self.assertIsNotNone(prompt_style)
        self.assertIn("height: 76px;", prompt_style.group("body"))
        self.assertIn("min-height: 76px;", prompt_style.group("body"))


if __name__ == "__main__":
    unittest.main()
