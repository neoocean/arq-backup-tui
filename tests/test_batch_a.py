"""Tests for Batch A — Restore ETA + Plan editing + Hetzner detector.

Three independent features, exercised without any TUI dependency
where possible:

- :mod:`arq_reader.restore` — ``Restore.restore(plan_totals=True)``
  emits a single ``restore_planned`` event with accurate
  ``total_files`` / ``total_bytes`` covering the subtree about to
  be restored. Path-filtered restores plan only the filtered
  subtree.
- :class:`arq_tui.screens.plan_wizard.PlanWizardScreen` — the
  ``plan=`` constructor argument pre-fills every input from an
  existing plan, ``Save`` preserves ``plan_id`` + ``last_run_iso``,
  and a blank password during edit doesn't wipe the cached
  credential.
- :class:`arq_validator.sftp._RateLimitTracker` — counts
  consecutive Hetzner-style stderr matches, resets on success,
  and ``threshold_hit()`` flips when the streak crosses the
  threshold.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    import textual  # noqa: F401
    HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    HAS_TEXTUAL = False

from arq_reader import Restore
from arq_writer import build_backup


class _Recorder:
    """Lightweight callback collector — append (kind, payload) tuples."""

    def __init__(self) -> None:
        self.events: list = []

    def __call__(self, kind: str, **payload) -> None:
        # Reader's _emit signature is (kind, **payload); accept both
        # forms for safety.
        self.events.append((kind, dict(payload)))


def _make_tree(root: Path) -> dict:
    (root / "subdir").mkdir(parents=True)
    a = (root / "alpha.txt")
    a.write_bytes(b"alpha\n")
    b = (root / "subdir" / "gamma.txt")
    b.write_bytes(b"gamma\n")
    c = (root / "subdir" / "delta.bin")
    c.write_bytes(b"x" * 4096)
    return {
        "files": 3,
        "total_bytes": (
            a.stat().st_size
            + b.stat().st_size
            + c.stat().st_size
        ),
    }


class RestorePlanTotalsTests(unittest.TestCase):
    def test_restore_emits_planned_event_with_totals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            sizes = _make_tree(src)
            dest = tdp / "dest"
            build_backup(
                source=src, dest_root=dest,
                encryption_password="pw",
                backup_name="test",
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(dest, encryption_password="pw")
            layouts = rs.layouts()
            folder_uuid = layouts[0].backup_folder_uuids[0]

            events = []
            def cb(kind, payload):
                events.append((kind, dict(payload)))
            rs.restore(
                folder_uuid=folder_uuid,
                computer_uuid=layouts[0].computer_uuid,
                dest=out, callback=cb, plan_totals=True,
            )
            planned = [
                p for k, p in events if k == "restore_planned"
            ]
            self.assertEqual(len(planned), 1)
            self.assertEqual(
                planned[0]["total_files"], sizes["files"],
            )
            self.assertEqual(
                planned[0]["total_bytes"], sizes["total_bytes"],
            )

    def test_plan_totals_false_emits_no_planned_event(self) -> None:
        # Headless mode — no extra tree-blob fetch and no event.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            build_backup(
                source=src, dest_root=dest,
                encryption_password="pw", backup_name="test",
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(dest, encryption_password="pw")
            layouts = rs.layouts()
            folder_uuid = layouts[0].backup_folder_uuids[0]

            events = []
            def cb(kind, payload):
                events.append((kind, dict(payload)))
            rs.restore(
                folder_uuid=folder_uuid,
                computer_uuid=layouts[0].computer_uuid,
                dest=out, callback=cb, plan_totals=False,
            )
            self.assertFalse(
                any(k == "restore_planned" for k, _ in events),
                msg=f"unexpected restore_planned event: {events}",
            )

    def test_path_filter_narrows_planned_totals(self) -> None:
        # When `paths=["subdir"]` is set, the planned totals must
        # cover only that subtree, not the full tree.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            build_backup(
                source=src, dest_root=dest,
                encryption_password="pw", backup_name="test",
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(dest, encryption_password="pw")
            layouts = rs.layouts()
            folder_uuid = layouts[0].backup_folder_uuids[0]

            events = []
            def cb(kind, payload):
                events.append((kind, dict(payload)))
            rs.restore(
                folder_uuid=folder_uuid,
                computer_uuid=layouts[0].computer_uuid,
                dest=out, paths=["subdir"], callback=cb,
                plan_totals=True,
            )
            planned = [
                p for k, p in events if k == "restore_planned"
            ]
            self.assertEqual(len(planned), 1)
            # subdir has 2 files (gamma.txt + delta.bin)
            self.assertEqual(planned[0]["total_files"], 2)
            self.assertGreater(planned[0]["total_bytes"], 4000)


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class PlanEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_wizard_prefills_existing_plan(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.screens.plan_wizard import PlanWizardScreen
        from arq_tui.state import Plan
        from arq_tui.widgets.source_picker import SourcePicker
        from textual.widgets import Input, RadioButton, TextArea

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "cfg"
            existing = Plan(
                plan_id="EXISTING-UUID",
                name="legacy-plan",
                sources=["/x", "/y"],
                destination_kind="local",
                destination={"path": "/Volumes/arq"},
                chunker="arq_v7_41",
                use_packs=False,
                dedup_against_existing=False,
                exclude_globs=["*.log"],
                exclude_regexes=[r".*\.tmp$"],
                exclude_gitignore_lines=["build/"],
                max_file_bytes=12345,
                use_apfs_snapshot=True,
                retention={"keep_last_n": 5, "keep_daily": 7},
                last_run_iso="2025-01-01T00:00:00Z",
            )
            app = ArqTuiApp(config_dir=cfg)
            app.plan_registry.save(existing)
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(PlanWizardScreen(plan=existing))
                await pilot.pause()
                wizard = app.screen
                # Sources pre-filled.
                picker = wizard.query_one(SourcePicker)
                self.assertEqual(list(picker.paths), ["/x", "/y"])
                # Local-path pre-filled.
                self.assertEqual(
                    wizard.query_one(
                        "#dest-local-path", Input,
                    ).value,
                    "/Volumes/arq",
                )
                # Chunker radio reflects arq_v7_41.
                self.assertTrue(
                    wizard.query_one(
                        "#chunker-arq", RadioButton,
                    ).value
                )
                # Layout reflects standalone (not packs).
                self.assertTrue(
                    wizard.query_one(
                        "#layout-standalone", RadioButton,
                    ).value
                )
                self.assertFalse(
                    wizard.query_one(
                        "#layout-packs", RadioButton,
                    ).value
                )
                # Advanced fields.
                self.assertEqual(
                    wizard.query_one(
                        "#adv-globs", TextArea,
                    ).text,
                    "*.log",
                )
                self.assertEqual(
                    wizard.query_one(
                        "#adv-max-file-bytes", Input,
                    ).value,
                    "12345",
                )
                self.assertTrue(
                    wizard.query_one(
                        "#adv-apfs-on", RadioButton,
                    ).value
                )
                self.assertEqual(
                    wizard.query_one(
                        "#adv-keep-last-n", Input,
                    ).value,
                    "5",
                )
                # Plan name pre-filled.
                self.assertEqual(
                    wizard.query_one("#plan-name", Input).value,
                    "legacy-plan",
                )

    async def test_edit_save_preserves_plan_id_and_last_run(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.screens.plan_wizard import PlanWizardScreen
        from arq_tui.state import Plan
        from textual.widgets import Input

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "cfg"
            existing = Plan(
                plan_id="STABLE-UUID-123",
                name="legacy",
                sources=["/x"],
                destination_kind="local",
                destination={"path": "/Volumes/arq"},
                last_run_iso="2025-01-01T00:00:00Z",
            )
            app = ArqTuiApp(config_dir=cfg)
            app.plan_registry.save(existing)
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(PlanWizardScreen(plan=existing))
                await pilot.pause()
                wizard = app.screen
                # Walk to review and save with a renamed plan;
                # blank passwords (edit allowed).
                wizard._handle_next()  # sources
                await pilot.pause()
                wizard._handle_next()  # destination
                await pilot.pause()
                # Encryption — leave blank (edit mode permits).
                wizard.query_one("#enc-password", Input).value = ""
                wizard.query_one("#enc-password2", Input).value = ""
                wizard._handle_next()
                await pilot.pause()
                wizard._handle_next()  # chunker
                await pilot.pause()
                wizard._handle_next()  # advanced
                await pilot.pause()
                wizard.query_one(
                    "#plan-name", Input,
                ).value = "renamed"
                wizard._handle_next()  # review → save
                await pilot.pause()
            plans = app.plan_registry.list_plans()
            self.assertEqual(len(plans), 1)
            got = plans[0]
            self.assertEqual(got.plan_id, "STABLE-UUID-123")
            self.assertEqual(got.name, "renamed")
            self.assertEqual(
                got.last_run_iso, "2025-01-01T00:00:00Z",
            )

    async def test_home_screen_e_binding_pushes_wizard_with_plan(self) -> None:
        # [e] key on Home should open the wizard pre-filled for the
        # currently-focused plan.
        from arq_tui import ArqTuiApp
        from arq_tui.screens.plan_wizard import PlanWizardScreen
        from arq_tui.state import Plan

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "cfg"
            existing = Plan(
                plan_id="HOME-EDIT-UUID",
                name="home-test",
                sources=["/x"],
                destination_kind="local",
                destination={"path": "/Volumes/arq"},
            )
            app = ArqTuiApp(config_dir=cfg)
            app.plan_registry.save(existing)
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("e")
                await pilot.pause()
                self.assertIsInstance(app.screen, PlanWizardScreen)
                self.assertIsNotNone(app.screen._editing_plan)
                self.assertEqual(
                    app.screen._editing_plan.plan_id,
                    "HOME-EDIT-UUID",
                )


class HetznerRateLimitTrackerTests(unittest.TestCase):
    def _cp(self, returncode: int, stderr: str):
        # Bypass the real subprocess.run — _track_rate_limit just
        # reads .returncode and .stderr.
        cp = subprocess.CompletedProcess(
            args=["ssh"], returncode=returncode,
        )
        cp.stderr = stderr.encode("utf-8")
        cp.stdout = b""
        return cp

    def test_consecutive_failures_increment_then_reset_on_success(self) -> None:
        from arq_validator.sftp import _RateLimitTracker
        t = _RateLimitTracker(threshold=20)
        for _ in range(5):
            t.record(255, "ssh: connect to host: Connection refused")
        self.assertEqual(t.consecutive_failures, 5)
        self.assertEqual(t.last_pattern, "Connection refused")
        # Success resets the streak.
        t.record(0, "")
        self.assertEqual(t.consecutive_failures, 0)
        self.assertIsNone(t.last_pattern)

    def test_non_rate_limit_failure_resets_streak(self) -> None:
        # An unrelated failure (e.g. file-not-found) breaks the
        # rate-limit streak — the next rate-limit hit starts fresh.
        from arq_validator.sftp import _RateLimitTracker
        t = _RateLimitTracker(threshold=20)
        for _ in range(3):
            t.record(255, "ssh: Connection refused")
        self.assertEqual(t.consecutive_failures, 3)
        t.record(2, "Cannot stat: No such file or directory")
        self.assertEqual(t.consecutive_failures, 0)

    def test_threshold_hit_at_configured_limit(self) -> None:
        from arq_validator.sftp import _RateLimitTracker
        t = _RateLimitTracker(threshold=4)
        for _ in range(3):
            t.record(255, "mux_client_request_session: Channel ...")
        self.assertFalse(t.threshold_hit())
        t.record(255, "mux_client_request_session: Channel ...")
        self.assertTrue(t.threshold_hit())

    def test_track_rate_limit_raises_on_threshold(self) -> None:
        # Drive _track_rate_limit directly without opening a real
        # SSH master.
        from arq_validator.sftp import (
            SftpBackend,
            SftpRateLimitedError,
        )
        b = SftpBackend("example.com", rate_limit_abort_threshold=3)
        cp = self._cp(255, "ssh: Connection refused")
        b._track_rate_limit(cp)
        b._track_rate_limit(cp)
        with self.assertRaises(SftpRateLimitedError):
            b._track_rate_limit(cp)
        self.assertEqual(
            b.consecutive_rate_limit_failures, 3,
        )
        self.assertEqual(b.rate_limit_failures, 3)


if __name__ == "__main__":
    unittest.main()
