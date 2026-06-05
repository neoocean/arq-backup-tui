"""Tests for the Sidebar widget (PR-B8 first slice).

The sidebar mirrors Arq.app's macOS GUI sidebar: a fixed-width
column with one section per row, the active section highlighted.
The widget is opt-in per-screen — these tests exercise its
behaviour without requiring a full screen rebuild.
"""

from __future__ import annotations

import unittest

try:
    from textual.app import App
    from textual.widgets import Static
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class SidebarTests(unittest.IsolatedAsyncioTestCase):

    def _app_with_sidebar(self, **kwargs):
        from arq_tui.widgets.sidebar import Sidebar

        class _A(App):
            def compose(self_):
                yield Sidebar(**kwargs)

        return _A()

    async def test_default_sections_render(self) -> None:
        from arq_tui.widgets.sidebar import (
            DEFAULT_SECTIONS, Sidebar,
        )
        async with self._app_with_sidebar().run_test() as pilot:
            sidebar = pilot.app.query_one(Sidebar)
            for key, _ in DEFAULT_SECTIONS:
                row = sidebar.query_one(
                    f"#sidebar-{key}", Static,
                )
                self.assertTrue(row.has_class("sidebar-section"))

    async def test_initial_active_section_carries_class(self) -> None:
        from arq_tui.widgets.sidebar import Sidebar
        async with self._app_with_sidebar(
            active="activity",
        ).run_test() as pilot:
            sidebar = pilot.app.query_one(Sidebar)
            active_row = sidebar.query_one(
                "#sidebar-activity", Static,
            )
            self.assertTrue(active_row.has_class("-active"))
            other_row = sidebar.query_one(
                "#sidebar-plans", Static,
            )
            self.assertFalse(other_row.has_class("-active"))

    async def test_set_active_moves_highlight(self) -> None:
        from arq_tui.widgets.sidebar import Sidebar
        async with self._app_with_sidebar().run_test() as pilot:
            sidebar = pilot.app.query_one(Sidebar)
            sidebar.set_active("validate")
            await pilot.pause()
            # New section is active.
            self.assertTrue(
                sidebar.query_one(
                    "#sidebar-validate", Static,
                ).has_class("-active")
            )
            # Old default ("plans") is no longer active.
            self.assertFalse(
                sidebar.query_one(
                    "#sidebar-plans", Static,
                ).has_class("-active")
            )

    async def test_custom_sections_render(self) -> None:
        from arq_tui.widgets.sidebar import Sidebar
        custom = [("a", "Alpha"), ("b", "Beta")]
        async with self._app_with_sidebar(
            sections=custom, active="b",
        ).run_test() as pilot:
            sidebar = pilot.app.query_one(Sidebar)
            self.assertTrue(
                sidebar.query_one(
                    "#sidebar-a", Static,
                ).has_class("sidebar-section")
            )
            self.assertTrue(
                sidebar.query_one(
                    "#sidebar-b", Static,
                ).has_class("-active")
            )

    # ------------------------------------------------------------------
    # Keyboard navigation (focusable + arrow cursor + Enter select)
    # ------------------------------------------------------------------

    async def test_sidebar_is_focusable(self) -> None:
        from arq_tui.widgets.sidebar import Sidebar
        self.assertTrue(Sidebar.can_focus)
        async with self._app_with_sidebar().run_test() as pilot:
            sidebar = pilot.app.query_one(Sidebar)
            sidebar.focus()
            await pilot.pause()
            self.assertIs(pilot.app.focused, sidebar)

    async def test_tab_reaches_sidebar(self) -> None:
        # With the sidebar the only focusable widget, Tab lands on it.
        from arq_tui.widgets.sidebar import Sidebar
        async with self._app_with_sidebar().run_test() as pilot:
            await pilot.press("tab")
            await pilot.pause()
            self.assertIsInstance(pilot.app.focused, Sidebar)

    async def test_arrow_keys_move_cursor_not_active(self) -> None:
        from arq_tui.widgets.sidebar import Sidebar
        async with self._app_with_sidebar(active="plans").run_test() as pilot:
            sidebar = pilot.app.query_one(Sidebar)
            sidebar.focus()
            await pilot.pause()
            self.assertEqual(sidebar._cursor, 0)
            await pilot.press("down")
            await pilot.pause()
            # Cursor moved to the 2nd section (Summary)…
            self.assertEqual(sidebar._cursor, 1)
            second_key = sidebar._sections[1][0]
            self.assertTrue(
                sidebar.query_one(f"#sidebar-{second_key}", Static)
                .has_class("-cursor")
            )
            # …but the active section has NOT changed yet.
            self.assertEqual(sidebar._active, "plans")
            self.assertTrue(
                sidebar.query_one("#sidebar-plans", Static)
                .has_class("-active")
            )

    async def test_cursor_clamps_at_ends(self) -> None:
        from arq_tui.widgets.sidebar import Sidebar
        async with self._app_with_sidebar(active="plans").run_test() as pilot:
            sidebar = pilot.app.query_one(Sidebar)
            sidebar.focus()
            await pilot.pause()
            await pilot.press("up")  # already at top
            self.assertEqual(sidebar._cursor, 0)
            for _ in range(20):
                await pilot.press("down")
            self.assertEqual(sidebar._cursor, len(sidebar._sections) - 1)

    async def test_enter_selects_cursor_row(self) -> None:
        from arq_tui.widgets.sidebar import Sidebar, SidebarNavigation
        captured = []

        class _A(App):
            def compose(self_):
                yield Sidebar(active="plans")

            def on_sidebar_navigation(self_, msg: SidebarNavigation) -> None:
                captured.append(msg.section)

        async with _A().run_test() as pilot:
            sidebar = pilot.app.query_one(Sidebar)
            sidebar.focus()
            await pilot.pause()
            second_key = sidebar._sections[1][0]   # 2nd section
            await pilot.press("down")   # cursor → 2nd section
            await pilot.press("enter")  # commit
            await pilot.pause()
            self.assertEqual(captured, [second_key])
            self.assertEqual(sidebar._active, second_key)


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class CommandPaletteDisabledTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_palette_disabled(self) -> None:
        # The generic Textual command palette (Ctrl+P) must be off.
        from arq_tui import ArqTuiApp
        self.assertFalse(ArqTuiApp.ENABLE_COMMAND_PALETTE)


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class ImeBindingTests(unittest.IsolatedAsyncioTestCase):
    """Shortcuts must fire regardless of input-method state — a Korean
    jamo from the IME is translated back to its Latin key."""

    def test_hangul_map_covers_shortcut_keys(self) -> None:
        from arq_tui.app import _HANGUL_TO_LATIN
        for jamo, latin in [
            ("ㅜ", "n"), ("ㄷ", "e"), ("ㄱ", "r"), ("ㅠ", "b"),
            ("ㅍ", "v"), ("ㅁ", "a"), ("ㄴ", "s"), ("ㅂ", "q"),
        ]:
            self.assertEqual(_HANGUL_TO_LATIN[jamo], latin)

    async def test_hangul_key_triggers_latin_binding(self) -> None:
        import tempfile
        from pathlib import Path
        from textual import events
        from textual.widgets import ContentSwitcher
        from arq_tui import ArqTuiApp
        with tempfile.TemporaryDirectory() as cfg:
            app = ArqTuiApp(config_dir=Path(cfg), arq_app=None)
            async with app.run_test() as pilot:
                await pilot.pause()
                # "ㅁ" is the jamo on the physical "a" key (Activity Log).
                app.on_key(events.Key("ㅁ", "ㅁ"))
                await pilot.pause()
                await pilot.pause()
                cs = app.screen.query_one("#home-content", ContentSwitcher)
                self.assertEqual(cs.current, "panel-activity")
                await pilot.press("q")

    async def test_hangul_ignored_in_text_field(self) -> None:
        # While typing in a field the raw Hangul must pass through (not
        # be hijacked as a shortcut).
        import tempfile
        from pathlib import Path
        from textual import events
        from textual.widgets import Input
        from arq_tui import ArqTuiApp
        with tempfile.TemporaryDirectory() as cfg:
            app = ArqTuiApp(config_dir=Path(cfg), arq_app=None)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("n")   # open the plan wizard (has Inputs)
                await pilot.pause()
                inp = None
                for w in app.screen.query(Input):
                    inp = w
                    break
                if inp is not None:
                    inp.focus()
                    await pilot.pause()
                    # Not translated → no exception, stays on wizard.
                    app.on_key(events.Key("ㅁ", "ㅁ"))
                    await pilot.pause()
                self.assertNotEqual(
                    app.screen.__class__.__name__, "HomeScreen",
                )
                await pilot.press("escape")


if __name__ == "__main__":
    unittest.main()
