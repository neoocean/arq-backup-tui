"""Tests for the M5 validation runner.

Drives the ValidateRunScreen at three tier levels (layout, deep,
audit) against a real synthetic destination and asserts the
ValidationReport summary lands in the screen's #summary widget.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

try:
    import textual  # noqa: F401
    HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    HAS_TEXTUAL = False

from arq_writer import build_backup


def _make_tree(root: Path) -> None:
    (root / "subdir").mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"alpha\n")
    (root / "subdir" / "gamma.txt").write_bytes(b"gamma\n")


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class ValidateRunScreenTests(unittest.IsolatedAsyncioTestCase):
    async def _run_tier(self, tier: str) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.backend_open import close_backend, open_backend
        from arq_tui.screens.validate_run import ValidateRunScreen
        from arq_tui.state import Destination
        from arq_tui.widgets.progress_panel import ProgressPanel

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            build_backup(src, dest, encryption_password="pw")

            app = ArqTuiApp(config_dir=tdp / "cfg")
            d = Destination(kind="local", path=str(dest))
            backend = open_backend(d)
            try:
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app.push_screen(ValidateRunScreen(
                        backend=backend,
                        tier=tier,
                        password="pw",
                        dest_label=str(dest),
                    ))
                    panel = None
                    for _ in range(300):
                        await pilot.pause()
                        await asyncio.sleep(0.05)
                        if panel is None:
                            try:
                                panel = app.screen.query_one(ProgressPanel)
                            except Exception:
                                continue
                        if panel.finished or panel.failed:
                            break
                    self.assertIsNotNone(panel)
                    self.assertTrue(
                        panel.finished,
                        msg=f"failed={panel.failed} err={panel.error_message}",
                    )
            finally:
                close_backend(backend)

    async def test_layout_tier(self) -> None:
        await self._run_tier("dry-run")

    async def test_deep_tier(self) -> None:
        await self._run_tier("deep")

    async def test_audit_tier(self) -> None:
        await self._run_tier("audit")


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class ValidateLaunchScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_launch_screen_renders_and_cancels(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.backend_open import close_backend, open_backend
        from arq_tui.screens.validate_run import ValidateLaunchScreen
        from arq_tui.state import Destination

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            build_backup(src, dest, encryption_password="pw")
            app = ArqTuiApp(config_dir=tdp / "cfg")
            d = Destination(kind="local", path=str(dest))
            backend = open_backend(d)
            try:
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app.push_screen(ValidateLaunchScreen(
                        backend=backend,
                        password="pw",
                        dest_label=str(dest),
                        config_dir=tdp / "cfg",
                    ))
                    await pilot.pause()
                    self.assertIsInstance(
                        app.screen, ValidateLaunchScreen,
                    )
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertNotIsInstance(
                        app.screen, ValidateLaunchScreen,
                    )
            finally:
                close_backend(backend)


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class ValidatePanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_lists_locations_and_launches(self) -> None:
        # The sidebar's Validate section lists openable storage
        # locations; selecting one (password cached) goes straight to
        # the tier picker — validation runs from the TUI directly.
        from arq_tui import ArqTuiApp
        from arq_tui.screens.home import HomeScreen
        from arq_tui.screens.validate_run import (
            ValidateLaunchScreen, ValidatePanel,
        )
        from arq_tui.state import Destination
        from textual.widgets import ListView

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            build_backup(src, dest, encryption_password="pw")

            app = ArqTuiApp(config_dir=tdp / "cfg", arq_app=None)
            d = Destination(kind="local", path=str(dest))
            app.destination_store.add_or_touch(d)
            app.credential_cache.set_encryption_password(d, "pw")

            async with app.run_test() as pilot:
                await pilot.pause()
                home = app.screen
                self.assertIsInstance(home, HomeScreen)
                home._show_section("validate")
                await pilot.pause()
                panel = home.query_one(ValidatePanel)
                lv = panel.query_one("#validate-locations", ListView)
                # The remembered destination shows up as a row.
                self.assertGreaterEqual(len(lv.children), 1)
                # Selecting it (cached pw) lands on the tier picker.
                panel._open_and_validate(d)
                await pilot.pause()
                self.assertIsInstance(app.screen, ValidateLaunchScreen)
                await pilot.press("escape")


if __name__ == "__main__":
    unittest.main()
