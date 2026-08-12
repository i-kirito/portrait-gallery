import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"


class KeywordCloudUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

    def test_refresh_prioritizes_terms_missing_from_previous_layout(self):
        self.assertIn("function keywordCloudCandidateOrder(", self.html)
        self.assertIn("const fresh = rotating", self.html)
        self.assertIn(".filter(item => !previous.has(", self.html)
        self.assertIn("return pinned.concat(fresh, familiar);", self.html)

    def test_refresh_and_delete_request_a_rotated_layout(self):
        self.assertIn("drawKeywordCloud(data, {rotate: true});", self.html)
        self.assertIn("openKeywordCloudDialog(true, true)", self.html)
        self.assertIn("本轮换入 ${newlyVisibleCount} 个", self.html)


if __name__ == "__main__":
    unittest.main()
