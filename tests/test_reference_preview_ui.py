from pathlib import Path
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"


class ReferencePreviewUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

    def test_reference_tag_supports_hover_and_fullscreen_click(self):
        render_start = self.html.index("function renderReferenceMetaTag")
        render_end = self.html.index("function renderEntryTagsMeta", render_start)
        renderer = self.html[render_start:render_end]

        self.assertIn('data-ref-src="${escapeAttr(src)}"', renderer)
        self.assertIn('onclick="openReferenceImage(event,this)"', renderer)
        self.assertIn('title="悬停预览，点击查看大图"', renderer)
        self.assertNotIn('class="meta-ref-preview"', renderer)

        open_start = self.html.index("function openReferenceImage")
        open_end = self.html.index("function bindReferencePreviewHover", open_start)
        opener = self.html[open_start:open_end]
        self.assertIn("closeActiveReferencePreview()", opener)
        self.assertIn("openFullscreenImg(src, sourceTag)", opener)

    def test_reference_tag_keyboard_activation_matches_click(self):
        bind_start = self.html.index("function bindReferencePreviewHover")
        bind_end = self.html.index("bindReferencePreviewHover();", bind_start)
        binding = self.html[bind_start:bind_end]

        self.assertIn('event.key !== "Enter" && event.key !== " "', binding)
        self.assertIn("openReferenceImage(event, tag)", binding)
        self.assertIn("if (isFullscreenImageOpen()) return;", binding)
        self.assertIn("closeActiveReferencePreview();", binding)

    def test_preview_is_portaled_and_fullscreen_restores_page_state(self):
        preview_start = self.html.index("function ensureReferencePreviewNode")
        preview_end = self.html.index("function bindReferencePreviewHover", preview_start)
        preview_code = self.html[preview_start:preview_end]
        self.assertIn("document.body.appendChild(preview)", preview_code)
        self.assertIn("isFullscreenImageOpen()", preview_code)
        self.assertIn("activeReferencePreviewTag", preview_code)

        viewer_start = self.html.index("// Fullscreen image viewer")
        viewer_end = self.html.index("let currentModalImg", viewer_start)
        viewer = self.html[viewer_start:viewer_end]
        self.assertIn('role", "dialog"', viewer)
        self.assertIn('aria-modal", "true"', viewer)
        self.assertIn('document.body.style.overflow = "hidden"', viewer)
        self.assertIn("imgFullscreenPreviousOverflow", viewer)
        self.assertIn("imgFullscreenReturnFocus", viewer)
        self.assertIn("function closeFullscreenImg()", viewer)


if __name__ == "__main__":
    unittest.main()
