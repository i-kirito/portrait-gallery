import re
import unittest
from pathlib import Path


class GalleryScopeUiTests(unittest.TestCase):
    html = (Path(__file__).resolve().parents[1] / "app" / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    def test_group_chat_image_tool_state_is_closed_before_gallery_helpers(self):
        match = re.search(
            r"function updateGroupChatImageToolState\(\) \{(?P<body>.*?)\n\}\n\nfunction socialCharacterById",
            self.html,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertIn("if (!sendsToChat) loadSocialSchedulePanel();", body)

    def test_generated_image_url_helper_remains_top_level(self):
        # The helper is used by gallery rendering, so it must not be nested in
        # the group-chat state function where callers cannot resolve it.
        before_helper = self.html.split("function generatedEntryImageUrl", 1)[0]
        self.assertEqual(before_helper.count("function updateGroupChatImageToolState"), 1)
        self.assertIn("\n}\n\nfunction socialCharacterById", before_helper)

    def test_local_image_cleanup_body_stays_inside_its_function(self):
        section = self.html.split("function removeLocalImages", 1)[1].split(
            "function removeLocalImage", 1
        )[0]
        self.assertIn("\n  return ids.size;\n}\n", section)


if __name__ == "__main__":
    unittest.main()
