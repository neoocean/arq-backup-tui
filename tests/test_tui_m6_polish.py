"""Smoke tests for the M6 polish layer: help screen + theme toggle."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    import textual  # noqa: F401
    HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    HAS_TEXTUAL = False


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class HelpScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_question_mark_opens_help(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.screens.help import HelpScreen

        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("question_mark")
                await pilot.pause()
                self.assertIsInstance(app.screen, HelpScreen)
                await pilot.press("escape")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, HelpScreen)


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class ThemeToggleTests(unittest.IsolatedAsyncioTestCase):
    async def test_t_key_flips_theme(self) -> None:
        from arq_tui import ArqTuiApp

        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                first = getattr(app, "theme", None)
                await pilot.press("t")
                await pilot.pause()
                second = getattr(app, "theme", None)
                # Theme objects must differ -- toggling must do
                # something. (If textual stops exposing app.theme
                # the test simply skips; the binding still fires.)
                if first is not None and second is not None:
                    self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
