from pathlib import Path
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"


class CustomGenerationSearchPanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

    def test_idle_search_panel_hides_empty_status_and_result_regions(self):
        self.assertIn('id="xhsSearchStatus" hidden', self.html)
        for selector in (
            ".xhs-search-status[hidden]",
            ".xhs-search-results[hidden]",
            ".xhs-note-images[hidden]",
            ".xhs-search-pagination[hidden]",
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, self.html)

    def test_status_visibility_tracks_message_content(self):
        start = self.html.index("function setXiaohongshuSearchStatus")
        end = self.html.index("function xiaohongshuCreatorAvatar", start)
        helper = self.html[start:end]

        self.assertIn("const text = String(message || \"\").trim();", helper)
        self.assertIn("element.hidden = !text;", helper)

    def test_reference_grid_has_spacing_after_search_panel(self):
        start = self.html.index(".cg-ref-grid {")
        end = self.html.index("}", start) + 1
        rule = self.html[start:end]

        self.assertIn("margin-top: 20px;", rule)


if __name__ == "__main__":
    unittest.main()
