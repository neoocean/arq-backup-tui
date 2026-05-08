"""Tests for M7 — TUI integration of post-M6 features.

Three things land here:

- The ``Plan`` dataclass + ``PlanRegistry`` JSON round-trip carry
  the new ``exclude_*`` / ``max_file_bytes`` / ``use_apfs_snapshot``
  / ``retention`` fields with no loss.
- :class:`arq_tui.screens.plan_wizard.PlanWizardScreen` exposes the
  Advanced step and persists what the operator typed into the
  saved plan.
- :class:`arq_tui.screens.backup_run.BackupRunScreen` honours the
  saved ``exclude_globs`` so excluded files do not reach the
  destination, and the ``BackupWorker`` falls back to a live walk
  on Linux when ``use_apfs_snapshot=True`` (the writer emits an
  ``apfs_snapshot_skipped`` event but the backup still completes).
- :class:`arq_tui.screens.maintenance.MaintenanceScreen` rotates a
  destination's keyset password against an existing backup and the
  cached password updates so subsequent restores work with the new
  one.
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

from arq_reader import Restore


def _make_tree(root: Path) -> None:
    (root / "subdir").mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"alpha\n")
    (root / "subdir" / "gamma.txt").write_bytes(b"gamma\n")
    (root / "ignore.log").write_bytes(b"junk\n")


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class PlanRegistryAdvancedRoundTripTests(unittest.TestCase):
    def test_round_trip_carries_advanced_fields(self) -> None:
        from arq_tui.state import Plan, PlanRegistry
        with tempfile.TemporaryDirectory() as td:
            reg = PlanRegistry(config_dir=Path(td))
            p = Plan(
                plan_id="adv",
                name="advanced-plan",
                sources=["/home/me/Documents"],
                destination_kind="local",
                destination={"path": "/Volumes/arq"},
                exclude_globs=["*.log", "__pycache__"],
                exclude_regexes=[r".*\.tmp$"],
                exclude_gitignore_lines=["build/", "!build/keep.txt"],
                max_file_bytes=1024 * 1024,
                use_apfs_snapshot=True,
                retention={"keep_last_n": 10, "keep_daily": 7},
            )
            reg.save(p)
            loaded = reg.list_plans()
            self.assertEqual(len(loaded), 1)
            got = loaded[0]
            self.assertEqual(got.exclude_globs, ["*.log", "__pycache__"])
            self.assertEqual(got.exclude_regexes, [r".*\.tmp$"])
            self.assertEqual(
                got.exclude_gitignore_lines,
                ["build/", "!build/keep.txt"],
            )
            self.assertEqual(got.max_file_bytes, 1024 * 1024)
            self.assertTrue(got.use_apfs_snapshot)
            self.assertEqual(
                got.retention, {"keep_last_n": 10, "keep_daily": 7},
            )

    def test_legacy_plan_loads_with_default_advanced_fields(self) -> None:
        # Ensure adding fields didn't break loading older plan
        # JSON files that don't include them yet.
        import json
        from arq_tui.state import PlanRegistry
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td)
            plans_dir = cfg / "plans"
            plans_dir.mkdir(parents=True)
            legacy = {
                "plan_id": "legacy",
                "name": "legacy-plan",
                "sources": ["/home/me"],
                "destination_kind": "local",
                "destination": {"path": "/Volumes/arq"},
                "chunker": "default",
                "use_packs": True,
                "dedup_against_existing": True,
            }
            (plans_dir / "legacy.json").write_text(json.dumps(legacy))
            reg = PlanRegistry(config_dir=cfg)
            loaded = reg.list_plans()
            self.assertEqual(len(loaded), 1)
            got = loaded[0]
            # Advanced fields must default to their empty-zero state.
            self.assertEqual(got.exclude_globs, [])
            self.assertEqual(got.exclude_regexes, [])
            self.assertEqual(got.exclude_gitignore_lines, [])
            self.assertIsNone(got.max_file_bytes)
            self.assertFalse(got.use_apfs_snapshot)
            self.assertEqual(got.retention, {})


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class PlanWizardAdvancedStepTests(unittest.IsolatedAsyncioTestCase):
    async def test_advanced_step_persists_into_saved_plan(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.screens.plan_wizard import PlanWizardScreen
        from arq_tui.widgets.source_picker import SourcePicker
        from textual.widgets import Input, RadioButton, TextArea

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "cfg"
            app = ArqTuiApp(config_dir=cfg)
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(PlanWizardScreen())
                await pilot.pause()
                wizard = app.screen
                # Step 1 sources.
                wizard.query_one(SourcePicker).paths = ["/data/photos"]
                wizard._handle_next()
                await pilot.pause()
                # Step 2 destination.
                wizard.query_one("#dest-local-path", Input).value = (
                    "/Volumes/arqbackup1"
                )
                wizard._handle_next()
                await pilot.pause()
                # Step 3 encryption.
                wizard.query_one("#enc-password", Input).value = "pw"
                wizard.query_one("#enc-password2", Input).value = "pw"
                wizard._handle_next()
                await pilot.pause()
                # Step 4 chunker (defaults).
                wizard._handle_next()
                await pilot.pause()
                # Step 5 — advanced.
                wizard.query_one("#adv-globs", TextArea).text = (
                    "*.log\n__pycache__\n"
                )
                wizard.query_one("#adv-regexes", TextArea).text = (
                    r".*\.tmp$" + "\n"
                )
                wizard.query_one("#adv-max-file-bytes", Input).value = (
                    "1048576"
                )
                wizard.query_one("#adv-apfs-on", RadioButton).value = True
                wizard.query_one("#adv-apfs-off", RadioButton).value = False
                wizard.query_one("#adv-keep-last-n", Input).value = "5"
                wizard.query_one("#adv-keep-daily", Input).value = "7"
                wizard._handle_next()
                await pilot.pause()
                # Step 6 review + save.
                wizard.query_one("#plan-name", Input).value = "adv"
                wizard._handle_next()
                await pilot.pause()

            plans = app.plan_registry.list_plans()
            self.assertEqual(len(plans), 1)
            got = plans[0]
            self.assertEqual(got.exclude_globs, ["*.log", "__pycache__"])
            self.assertEqual(got.exclude_regexes, [r".*\.tmp$"])
            self.assertEqual(got.max_file_bytes, 1048576)
            self.assertTrue(got.use_apfs_snapshot)
            self.assertEqual(
                got.retention,
                {"keep_last_n": 5, "keep_daily": 7},
            )

    async def test_advanced_max_file_bytes_validation(self) -> None:
        # Non-positive / non-integer max-file-bytes must surface an
        # error and keep the wizard on the Advanced step.
        from arq_tui import ArqTuiApp
        from arq_tui.screens.plan_wizard import PlanWizardScreen
        from arq_tui.widgets.source_picker import SourcePicker
        from textual.widgets import Input

        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(PlanWizardScreen())
                await pilot.pause()
                wizard = app.screen
                wizard.query_one(SourcePicker).paths = ["/x"]
                wizard._handle_next()
                await pilot.pause()
                wizard.query_one(
                    "#dest-local-path", Input,
                ).value = "/Volumes/x"
                wizard._handle_next()
                await pilot.pause()
                wizard.query_one("#enc-password", Input).value = "pw"
                wizard.query_one("#enc-password2", Input).value = "pw"
                wizard._handle_next()
                await pilot.pause()
                wizard._handle_next()  # past chunker step
                await pilot.pause()
                # Bad max-file-bytes value:
                wizard.query_one(
                    "#adv-max-file-bytes", Input,
                ).value = "abc"
                advanced_idx = wizard._step_index
                wizard._handle_next()
                await pilot.pause()
                self.assertEqual(wizard._step_index, advanced_idx)


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class BackupRunHonorsExclusionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_excluded_glob_does_not_reach_destination(self) -> None:
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
                name="advplan",
                sources=[str(src)],
                destination_kind="local",
                destination={"path": str(dest)},
                use_packs=True,
                dedup_against_existing=False,
                exclude_globs=["*.log"],
            )
            app = ArqTuiApp(config_dir=cfg)
            target = Destination(kind="local", path=str(dest))
            app.credential_cache.set_encryption_password(target, "pw")
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(BackupRunScreen(plan=plan, password="pw"))
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
                self.assertTrue(
                    panel.finished,
                    msg=f"panel state: failed={panel.failed} "
                        f"err={panel.error_message}",
                )
            # Restore + verify the excluded file is missing.
            out = tdp / "out"
            out.mkdir()
            rs = Restore(dest, encryption_password="pw")
            layouts = rs.layouts()
            folder_uuid = layouts[0].backup_folder_uuids[0]
            rs.restore(
                folder_uuid=folder_uuid,
                computer_uuid=layouts[0].computer_uuid,
                dest=out,
            )
            self.assertTrue((out / "alpha.txt").is_file())
            self.assertFalse((out / "ignore.log").exists())

    async def test_use_apfs_snapshot_falls_back_on_linux(self) -> None:
        # On Linux the writer's APFS helper raises NotMacOSError;
        # the worker must catch it, emit apfs_snapshot_skipped, and
        # still produce a working backup via the live walk.
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
                plan_id="p2",
                name="apfsplan",
                sources=[str(src)],
                destination_kind="local",
                destination={"path": str(dest)},
                use_packs=True,
                dedup_against_existing=False,
                use_apfs_snapshot=True,
            )
            app = ArqTuiApp(config_dir=cfg)
            target = Destination(kind="local", path=str(dest))
            app.credential_cache.set_encryption_password(target, "pw")
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(BackupRunScreen(plan=plan, password="pw"))
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
                self.assertTrue(panel.finished)
            # Verify the live-walk fallback produced a real backup.
            rs = Restore(dest, encryption_password="pw")
            layouts = rs.layouts()
            self.assertEqual(len(layouts), 1)


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class MaintenanceRotateTests(unittest.IsolatedAsyncioTestCase):
    async def test_rotate_keyset_password_via_screen(self) -> None:
        # Build a small backup the wizard-style flow can talk to,
        # then push the maintenance screen and trigger rotation.
        from arq_tui import ArqTuiApp
        from arq_tui.screens.maintenance import MaintenanceScreen
        from arq_tui.state import Destination
        from arq_validator.backend import LocalBackend
        from arq_writer import build_backup
        from textual.widgets import Input

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "x.txt").write_bytes(b"x\n")
            dest = tdp / "dest"
            build_backup(
                source=src, dest_root=dest,
                encryption_password="oldpw",
                backup_name="rot",
            )
            backend = LocalBackend(dest)
            target = Destination(kind="local", path=str(dest))
            app = ArqTuiApp(config_dir=tdp / "cfg")
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(MaintenanceScreen(
                    backend=backend,
                    dest=target,
                    password="oldpw",
                ))
                await pilot.pause()
                screen = app.screen
                screen.query_one("#rot-new-password", Input).value = (
                    "newpw"
                )
                screen.query_one("#rot-new-password2", Input).value = (
                    "newpw"
                )
                screen._start_rotate()
                # Wait for rotation thread to land back on the
                # event loop.
                for _ in range(100):
                    await pilot.pause()
                    await asyncio.sleep(0.05)
                    if not screen._busy:
                        break
                self.assertFalse(screen._busy)
            # Old password no longer decrypts; new password does.
            from arq_validator.crypto import decrypt_keyset
            cuuids = [
                e for e in (dest).iterdir() if e.is_dir() and e.name
            ]
            self.assertTrue(cuuids)
            kp = cuuids[0] / "encryptedkeyset.dat"
            with self.assertRaises(Exception):
                decrypt_keyset(kp.read_bytes(), "oldpw")
            # New password works.
            ks = decrypt_keyset(kp.read_bytes(), "newpw")
            self.assertEqual(len(ks.encryption_key), 32)


if __name__ == "__main__":
    unittest.main()
