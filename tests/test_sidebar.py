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


if __name__ == "__main__":
    unittest.main()
