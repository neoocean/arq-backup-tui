"""Smoke tests for the M1 TUI skeleton.

Uses Textual's ``pilot`` (the headless test driver) — no real
terminal is involved, so this test runs in CI exactly the same as
on a developer machine.

The test suite is auto-skipped when textual isn't installed, so a
default ``pip install -e .`` (without the ``tui`` extra) still
runs the rest of the test suite cleanly.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    import textual  # noqa: F401  (just for the import-availability test)
    HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    HAS_TEXTUAL = False


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed; install with .[tui]")
class TuiSkeletonTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_starts_and_quits_cleanly(self) -> None:
        # Boot the app pointed at an empty config dir, then send "q"
        # to quit. If anything in the boot path raises, the await on
        # pilot.press will surface it.
        from arq_tui import ArqTuiApp

        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()        # let on_mount finish
                # The Home screen should be on top of the stack.
                self.assertEqual(
                    app.screen.__class__.__name__, "HomeScreen"
                )
                await pilot.press("q")
                await pilot.pause()
            self.assertEqual(app.return_code, 0)

    async def test_home_screen_renders_empty_plans_hint(self) -> None:
        # With an empty config dir, the Home screen must render the
        # "No plans yet" hint rather than crashing on a missing
        # plans/ directory.
        from arq_tui import ArqTuiApp

        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                # Empty state placeholder must be present (not the
                # populated ListView).
                app.screen.query_one("#plans-empty")
                with self.assertRaises(Exception):
                    app.screen.query_one("#plans-list")
                await pilot.press("q")

    async def test_quick_action_keys_emit_notifications(self) -> None:
        # The placeholder actions (n / b / v) all post a notification
        # rather than crashing. We just verify they don't raise.
        from arq_tui import ArqTuiApp

        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                for key in ("n", "b", "v"):
                    await pilot.press(key)
                    await pilot.pause()
                await pilot.press("q")


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed; install with .[tui]")
class PlanRegistryShapeTests(unittest.TestCase):
    def test_empty_dir_returns_empty_list(self) -> None:
        from arq_tui.state import PlanRegistry
        with tempfile.TemporaryDirectory() as td:
            reg = PlanRegistry(config_dir=Path(td))
            self.assertEqual(reg.list_plans(), [])

    def test_loads_well_formed_plan_files(self) -> None:
        import json
        from arq_tui.state import PlanRegistry
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            plans_dir = tdp / "plans"
            plans_dir.mkdir()
            (plans_dir / "p1.json").write_text(json.dumps({
                "plan_id": "abc",
                "name": "home-laptop-to-nas",
                "sources": ["/home/me/Documents"],
                "destination_kind": "local",
                "destination": {"path": "/Volumes/arqbackup1"},
                "chunker": "arq_v7_41",
                "use_packs": True,
                "dedup_against_existing": True,
                "last_run_iso": "2026-05-08T03:14:22Z",
            }))
            plans = PlanRegistry(config_dir=tdp).list_plans()
            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0].name, "home-laptop-to-nas")
            self.assertEqual(plans[0].sources, ["/home/me/Documents"])

    def test_malformed_plan_files_are_skipped(self) -> None:
        from arq_tui.state import PlanRegistry
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            plans_dir = tdp / "plans"
            plans_dir.mkdir()
            (plans_dir / "broken.json").write_text("{not json")
            (plans_dir / "wrong-shape.json").write_text("[1, 2, 3]")
            self.assertEqual(
                PlanRegistry(config_dir=tdp).list_plans(), [],
            )


if __name__ == "__main__":
    unittest.main()
