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
            'class="egg-theme-day-preview-nav"',
            'class="egg-theme-day-target-group" role="group" aria-label="日程预览日期"',
            'id="eggThemeDayManualToggle" type="button" aria-expanded="false"',
            'aria-controls="eggThemeDayManualSection"',
            'id="eggThemeDayManualSection" hidden',
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

        self.assertIn("panel.classList.toggle('is-enabled', themeDayManualMode)", render)
        self.assertIn("panel.classList.toggle('is-manual-expanded', manualExpanded)", render)
        self.assertIn("panel.classList.toggle('is-busy', interactionLocked)", render)
        self.assertIn("panel.setAttribute('aria-busy'", render)
        self.assertIn("toggle.checked = !themeDayManualMode", render)
        self.assertIn("manualSection.hidden = !manualExpanded", render)
        self.assertIn("manualToggle.setAttribute('aria-expanded'", render)
        self.assertIn("btn.hasAttribute('data-theme-target')", render)
        self.assertIn("btn.disabled = interactionLocked", render)
        self.assertIn("button.setAttribute('aria-pressed'", select)
        self.assertIn("if (themeDayManualMode) loadThemeDayManualReference()", select)

    def test_schedule_preview_navigation_stays_outside_manual_disclosure(self):
        controls_start = self.html.index('<div class="egg-theme-day-controls"')
        controls_end = self.html.index('<div class="egg-body"', controls_start)
        controls = self.html[controls_start:controls_end]

        preview_pos = controls.index('class="egg-theme-day-preview-nav"')
        disclosure_pos = controls.index('id="eggThemeDayManualToggle"')
        manual_section_pos = controls.index('id="eggThemeDayManualSection"')
        self.assertLess(preview_pos, disclosure_pos)
        self.assertLess(disclosure_pos, manual_section_pos)
        self.assertEqual(controls.count('data-theme-target="today"'), 1)
        self.assertEqual(controls.count('data-theme-target="tomorrow"'), 1)

    def test_manual_theme_settings_require_explicit_disclosure(self):
        self.assertIn("let themeDayManualExpanded = false", self.html)
        self.assertIn("function toggleThemeDayManualSection()", self.html)
        self.assertIn(
            "themeDayManualExpanded = !themeDayManualExpanded",
            self.html,
        )
        self.assertIn(
            ".egg-theme-day-manual-section[hidden] { display: none; }",
            self.html,
        )
        self.assertIn("themeDayManualExpanded = false", self.html)

    def test_schedule_egg_reopens_with_manual_settings_collapsed(self):
        open_start = self.html.index("function openScheduleEgg()")
        open_end = self.html.index("function refreshScheduleEgg()", open_start)
        open_handler = self.html[open_start:open_end]
        close_start = self.html.index("function closeScheduleEgg()")
        close_end = self.html.index("function bindScheduleEggEvents()", close_start)
        close_handler = self.html[close_start:close_end]

        self.assertLess(
            open_handler.index("themeDayManualExpanded = false"),
            open_handler.index("overlay.classList.add('show')"),
        )
        self.assertIn("themeDayManualExpanded = false", close_handler)
        self.assertIn("renderThemeDayEnabled()", close_handler)

    def test_schedule_refresh_targets_active_preview_day(self):
        self.assertIn(
            'class="egg-refresh-btn" title="重新生成当天日程" aria-label="重新生成当天日程"',
            self.html,
        )

        render_start = self.html.index("function renderScheduleEggRefreshButton()")
        render_end = self.html.index("function renderThemeDayEnabled()", render_start)
        refresh_button_render = self.html[render_start:render_end]
        self.assertIn(
            "selectedThemeDayTarget === 'tomorrow' ? '第二天' : '当天'",
            refresh_button_render,
        )
        self.assertIn("button.setAttribute('aria-label', label)", refresh_button_render)

        select_start = self.html.index("function selectThemeDayTarget(target)")
        select_end = self.html.index("async function generateThemeDay", select_start)
        select_handler = self.html[select_start:select_end]
        self.assertIn("renderScheduleEggRefreshButton()", select_handler)

        open_start = self.html.index("function openScheduleEgg()")
        open_end = self.html.index("function refreshScheduleEgg()", open_start)
        open_handler = self.html[open_start:open_end]
        self.assertIn("renderScheduleEggRefreshButton()", open_handler)

        refresh_start = self.html.index("function refreshScheduleEgg()")
        refresh_end = self.html.index("function closeScheduleEgg()", refresh_start)
        refresh_handler = self.html[refresh_start:refresh_end]
        for marker in (
            "var target = selectedThemeDayTarget === 'tomorrow' ? 'tomorrow' : 'today'",
            "var targetDate = themeDayScheduleDate()",
            "body: JSON.stringify({target: target, target_date: targetDate})",
            "btn.disabled = true",
            "btn.classList.add('spinning')",
            "if (selectedThemeDayTarget !== target) return",
            "scheduleEggCache = { html: '', dateText: '', updatedAt: 0 }",
            "loadScheduleEggData({ force: true })",
            "btn.disabled = false",
            "btn.classList.remove('spinning')",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, refresh_handler)

    def test_empty_tomorrow_preview_uses_target_date_without_today_jobs(self):
        load_start = self.html.index("function loadScheduleEggData(options)")
        load_end = self.html.index("function toggleFavoriteOutfitPlan", load_start)
        loader = self.html[load_start:load_end]

        for marker in (
            "var target = selectedThemeDayTarget === 'tomorrow' ? 'tomorrow' : 'today'",
            "var targetDate = themeDayScheduleDate()",
            "if (selectedThemeDayTarget !== target) return",
            "var isFutureSchedule = target === 'tomorrow'",
            "if (detail.status === 'no_schedule')",
            "String(detail.date || targetDate || '').trim()",
            "第二天还没有日程哦",
            "点击右上角刷新按钮生成 ' + escapeHtml(emptyDateText) + ' 的日程",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, loader)

    def test_theme_day_switch_maps_legacy_state_to_auto_and_manual_modes(self):
        self.assertIn('id="eggThemeDayState">自动主题已开启</span>', self.html)
        self.assertIn("enabled：false=自动主题，true=手动主题", self.html)
        self.assertIn(
            "themeDayManualMode ? '手动主题' : '自动主题已开启'",
            self.html,
        )
        self.assertIn(
            "自动主题已开启，每天随机选择一条主题主线并生成完整日程",
            self.html,
        )
        self.assertIn(
            "mode: themeDayManualMode ? 'manual' : 'auto'",
            self.html,
        )
        self.assertIn(
            'checked onchange="setThemeDayAutomatic(this.checked)"',
            self.html,
        )

    def test_theme_day_mode_save_rolls_back_to_previous_state(self):
        start = self.html.index("async function setThemeDayAutomatic(automaticEnabled)")
        end = self.html.index("function renderThemeDayManualReference()", start)
        setter = self.html[start:end]

        self.assertIn("var previousManualMode = themeDayManualMode", setter)
        self.assertIn("var previousManualExpanded = themeDayManualExpanded", setter)
        self.assertIn("themeDayStateBusy = true", setter)
        self.assertIn("themeDayManualMode = previousManualMode", setter)
        self.assertIn("themeDayManualExpanded = previousManualExpanded", setter)
        self.assertIn("toggle.checked = !themeDayManualMode", setter)
        self.assertIn("themeDayStateBusy = false", setter)


if __name__ == "__main__":
    unittest.main()
