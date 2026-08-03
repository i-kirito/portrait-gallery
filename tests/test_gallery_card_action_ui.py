from pathlib import Path
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"


class GalleryCardActionUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

    def test_gallery_card_actions_hide_visual_text_labels(self):
        start = self.html.index(
            ".gallery-card .card-body .card-actions .ca-btn .ca-label"
        )
        end = self.html.index("}", start) + 1
        rule = self.html[start:end]

        self.assertIn("display: none;", rule)

    def test_gallery_card_actions_retain_accessible_button_names(self):
        start = self.html.index("function renderGallery()")
        end = self.html.index("// ===== Fullscreen Image Viewer =====", start)
        renderer = self.html[start:end]

        for label in ("编辑图片", "发推", "删除"):
            with self.subTest(label=label):
                self.assertIn(f'aria-label="{label}"', renderer)
                self.assertIn(f'title="{label}"', renderer)
        self.assertIn("button.setAttribute('title', isFavorite ? '取消收藏' : '收藏');", self.html)


if __name__ == "__main__":
    unittest.main()
