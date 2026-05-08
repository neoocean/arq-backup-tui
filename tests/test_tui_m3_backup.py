"""Tests for the M3 plan-wizard + backup-run screens.

The wizard test populates each step's inputs through the pilot
and checks that ``PlanRegistry.save`` was called with the right
shape. The backup-run test drives a real ``Backup`` to completion
through the worker bridge and verifies the destination is
restorable afterward.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any, List

try:
    import textual  # noqa: F401
    HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    HAS_TEXTUAL = False

from arq_reader import Restore


def _make_tree(root: Path) -> None:
    (root / "subdir").mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"alpha\n")
    (root / "subdir" / "gamma.txt").write_bytes(b"gamma\n")
    (root / "한글.txt").write_bytes("내용".encode("utf-8"))


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class PlanRegistrySaveTests(unittest.TestCase):
    def test_save_then_list_round_trip(self) -> None:
        from arq_tui.state import Plan, PlanRegistry
        with tempfile.TemporaryDirectory() as td:
            reg = PlanRegistry(config_dir=Path(td))
            p = Plan(
                plan_id="abc",
                name="hello",
                sources=["/home/me/Documents", "/home/me/Pictures"],
                destination_kind="local",
                destination={"path": "/Volumes/arqbackup1"},
                chunker="arq_v7_41",
                use_packs=True,
                dedup_against_existing=True,
            )
            reg.save(p)
            loaded = reg.list_plans()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].name, "hello")
            self.assertEqual(
                loaded[0].sources,
                ["/home/me/Documents", "/home/me/Pictures"],
            )

    def test_save_requires_plan_id(self) -> None:
        from arq_tui.state import Plan, PlanRegistry
        with tempfile.TemporaryDirectory() as td:
            reg = PlanRegistry(config_dir=Path(td))
            with self.assertRaises(ValueError):
                reg.save(Plan(name="no-id"))

    def test_save_preserves_unicode_in_paths(self) -> None:
        from arq_tui.state import Plan, PlanRegistry
        with tempfile.TemporaryDirectory() as td:
            reg = PlanRegistry(config_dir=Path(td))
            p = Plan(
                plan_id="u",
                name="unicode-name-한글",
                sources=["/home/me/문서/계획.txt"],
                destination_kind="local",
                destination={"path": "/Volumes/한글-디스크"},
            )
            reg.save(p)
            loaded = reg.list_plans()
            self.assertEqual(loaded[0].name, "unicode-name-한글")
            self.assertEqual(
                loaded[0].sources, ["/home/me/문서/계획.txt"],
            )
            self.assertEqual(
                loaded[0].destination["path"], "/Volumes/한글-디스크",
            )


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class PlanWizardTests(unittest.IsolatedAsyncioTestCase):
    async def test_wizard_full_walkthrough_saves_plan(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.screens.plan_wizard import PlanWizardScreen
        from arq_tui.widgets.source_picker import SourcePicker
        from textual.widgets import Input

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "cfg"
            app = ArqTuiApp(config_dir=cfg)
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(PlanWizardScreen())
                await pilot.pause()
                wizard = app.screen
                self.assertIsInstance(wizard, PlanWizardScreen)

                # Step 1 — sources: programmatically populate the
                # source picker (Pilot can drive it but the widget
                # exposes its `paths` list directly).
                picker = wizard.query_one(SourcePicker)
                picker.paths = ["/data/photos", "/data/docs"]
                # Step 1 next:
                wizard._handle_next()
                await pilot.pause()
                # Step 2 — destination defaults to local; fill path.
                wizard.query_one("#dest-local-path", Input).value = (
                    "/Volumes/arqbackup1"
                )
                wizard._handle_next()
                await pilot.pause()
                # Step 3 — encryption.
                wizard.query_one("#enc-password", Input).value = "pw"
                wizard.query_one("#enc-password2", Input).value = "pw"
                wizard._handle_next()
                await pilot.pause()
                # Step 4 — chunker (defaults are fine).
                wizard._handle_next()
                await pilot.pause()
                # Step 5 — advanced (defaults are fine; all fields
                # optional and default-empty equates to M3 behaviour).
                wizard._handle_next()
                await pilot.pause()
                # Step 6 — review + save.
                wizard.query_one("#plan-name", Input).value = "test-plan"
                wizard._handle_next()
                await pilot.pause()

            plans = app.plan_registry.list_plans()
            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0].name, "test-plan")
            self.assertEqual(
                plans[0].sources, ["/data/photos", "/data/docs"],
            )
            self.assertEqual(plans[0].destination_kind, "local")
            self.assertEqual(
                plans[0].destination["path"], "/Volumes/arqbackup1",
            )


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class BackupRunScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_screen_drives_worker_to_completion(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.screens.backup_run import BackupRunScreen
        from arq_tui.state import Destination, Plan
        from arq_tui.widgets.progress_panel import ProgressPanel

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            cfg = tdp / "cfg"
            plan = Plan(
                plan_id="p1",
                name="testplan",
                sources=[str(src)],
                destination_kind="local",
                destination={"path": str(dest)},
                chunker="default",
                use_packs=True,
                dedup_against_existing=False,
            )

            app = ArqTuiApp(config_dir=cfg)
            target = Destination(kind="local", path=str(dest))
            app.credential_cache.set_encryption_password(target, "pw")
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(BackupRunScreen(plan=plan, password="pw"))
                # Wait up to 30s for the worker to finish.
                panel = None
                for _ in range(300):
                    await pilot.pause()
                    await asyncio.sleep(0.1)
                    if panel is None:
                        try:
                            panel = app.screen.query_one(ProgressPanel)
                        except Exception:
                            continue
                    if panel.finished or panel.failed:
                        break
                self.assertIsNotNone(panel)
                self.assertTrue(panel.finished, msg=f"panel state: failed={panel.failed} err={panel.error_message}")
                # Files were written.
                self.assertGreater(panel.files_written, 0)

            # Verify destination is restorable.
            out = tdp / "out"
            out.mkdir()
            rs = Restore(dest, encryption_password="pw")
            layouts = rs.layouts()
            self.assertEqual(len(layouts), 1)
            folder_uuid = layouts[0].backup_folder_uuids[0]
            rs.restore(
                folder_uuid=folder_uuid,
                computer_uuid=layouts[0].computer_uuid,
                dest=out,
            )
            self.assertEqual(
                (out / "alpha.txt").read_bytes(), b"alpha\n",
            )
            self.assertEqual(
                (out / "한글.txt").read_bytes(),
                "내용".encode("utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
