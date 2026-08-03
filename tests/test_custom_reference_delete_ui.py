from pathlib import Path
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"


class CustomReferenceDeleteDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

    def test_uploaded_reference_delete_uses_in_app_confirmation(self):
        start = self.html.index("async function deleteUploadedRef")
        end = self.html.index("async function deleteXiaohongshuRef", start)
        handler = self.html[start:end]

        self.assertIn("await confirmDeleteAction({", handler)
        self.assertIn('title: "删除参考图？"', handler)
        self.assertIn('confirmLabel: "删除参考图"', handler)
        self.assertNotIn("confirm(", handler)
        self.assertIn("/api/uploaded-refs/", handler)

    def test_delete_confirmation_card_supports_dynamic_copy_and_accessibility(self):
        start = self.html.index('const deleteConfirmOverlay = document.createElement("div")')
        end = self.html.index("// Fullscreen image viewer", start)
        card = self.html[start:end]
        helper_start = self.html.index("function confirmDeleteAction")
        helper_end = self.html.index("function confirmDeleteImage", helper_start)
        helper = self.html[helper_start:helper_end]

        self.assertIn('role="alertdialog"', card)
        self.assertIn('aria-describedby="deleteConfirmDescription"', card)
        self.assertIn("function confirmDeleteAction(options = {})", helper)
        self.assertIn(".textContent = title", helper)
        self.assertIn(".textContent = description", helper)
        self.assertIn("activateAccessibleDialog(", helper)


if __name__ == "__main__":
    unittest.main()
