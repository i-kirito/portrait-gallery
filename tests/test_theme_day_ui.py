from pathlib import Path
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"


class ThemeDayUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

    def test_theme_day_controls_keep_visual_hierarchy_and_accessible_state(self):
        start = self.html.index('<div class="egg-theme-day-controls"')
        end = self.html.index('<div class="egg-body"', start)
        controls = self.html[start:end]

        for marker in (
            'class="egg-theme-day-heading"',
            'class="egg-theme-day-action egg-theme-day-action--primary"',
            'class="egg-theme-day-action egg-theme-day-action--secondary"',
            'class="egg-theme-day-target-group" role="group" aria-label="生成日期"',
            'aria-controls="eggThemeDayXhsPicker" aria-expanded="false"',
            'id="eggThemeDayStatus" role="status" aria-live="polite"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, controls)

    def test_theme_day_rendering_exposes_enabled_busy_and_pressed_states(self):
        render_start = self.html.index("function renderThemeDayEnabled()")
        render_end = self.html.index("async function loadThemeDayState()", render_start)
        render = self.html[render_start:render_end]
        select_start = self.html.index("function selectThemeDayTarget(target)")
        select_end = self.html.index("async function generateThemeDay", select_start)
        select = self.html[select_start:select_end]

        self.assertIn("panel.classList.toggle('is-enabled', themeDayEnabled)", render)
        self.assertIn("panel.classList.toggle('is-busy', interactionLocked)", render)
        self.assertIn("panel.setAttribute('aria-busy'", render)
        self.assertIn("button.setAttribute('aria-pressed'", select)


if __name__ == "__main__":
    unittest.main()
