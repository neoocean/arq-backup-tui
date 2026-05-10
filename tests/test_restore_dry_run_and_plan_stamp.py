"""Restore --list-only dry-run + Plan.last_run_iso stamping.

Two integrations:

1. ``Restore.dry_run_restore()`` walks the backuprecord's tree
   + emits would_restore_file events without touching the
   destination. Operators use this to size a restore + verify
   ``paths=`` filtering before paying the I/O cost.

2. ``PlanRegistry.mark_run(plan_id)`` stamps a plan's
   ``last_run_iso`` to "now" — wired into BackupRunScreen's
   on_worker_finished + on_worker_failed handlers so the plan
   row in HomeScreen shows a fresh timestamp instead of "never
   run" forever.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Restore.dry_run_restore — list without writing
# ---------------------------------------------------------------------------


def _build_small_backup(workdir: Path):
    """Create a real Arq 7 backup against a local destination so
    Restore.dry_run_restore has something real to walk."""
    src = workdir / "src"
    src.mkdir()
    (src / "a.txt").write_text("alpha")
    (src / "b.txt").write_text("beta-content-larger")
    sub = src / "sub"
    sub.mkdir()
    (sub / "c.txt").write_text("c-in-sub")
    dest = workdir / "dest"
    from arq_writer.backup import build_backup
    res = build_backup(
        str(src), str(dest),
        encryption_password="pw", backup_name="dryruntest",
    )
    return src, dest, res


class DryRunRestoreTests(unittest.TestCase):

    def test_dry_run_lists_files_without_writing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arq-dry-") as td:
            tdp = Path(td)
            _src, dest, br = _build_small_backup(tdp)
            from arq_reader.restore import Restore
            r = Restore(dest, encryption_password="pw")
            events = []

            def cb(kind, payload):
                events.append((kind, payload))

            dr = r.dry_run_restore(
                folder_uuid=br.folder_uuid,
                computer_uuid=br.computer_uuid,
                callback=cb,
            )
            self.assertEqual(dr.files_listed, 3)
            # Bytes should sum to 5 + 19 + 8 = 32.
            self.assertEqual(dr.bytes_would_restore, 32)
            # Sample paths should include the three files (or a
            # subset capped at 10).
            sample = set(dr.sample_paths)
            self.assertIn("a.txt", sample)
            self.assertIn("b.txt", sample)
            self.assertIn("sub/c.txt", sample)

            # would_restore_file events fired one per file.
            wrf = [k for k, _ in events if k == "would_restore_file"]
            self.assertEqual(len(wrf), 3)

            # No bytes were written anywhere — destination still
            # has only the original backup, no restored content.
            self.assertFalse(
                any(
                    p.name in {"a.txt", "b.txt", "c.txt"}
                    for p in tdp.rglob("*")
                    if p.is_file() and "src" not in p.parts
                    and "dest" not in p.parts[:tdp.parts.__len__() + 1]
                ),
                "dry-run must not have written any restored files",
            )

    def test_dry_run_honors_paths_filter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arq-dry-flt-") as td:
            tdp = Path(td)
            _src, dest, br = _build_small_backup(tdp)
            from arq_reader.restore import Restore
            r = Restore(dest, encryption_password="pw")
            dr = r.dry_run_restore(
                folder_uuid=br.folder_uuid,
                computer_uuid=br.computer_uuid,
                paths=["sub"],
            )
            # Only c.txt under sub/.
            self.assertEqual(dr.files_listed, 1)
            self.assertEqual(dr.bytes_would_restore, 8)
            self.assertEqual(dr.sample_paths, ["sub/c.txt"])


# ---------------------------------------------------------------------------
# Restore CLI --list-only
# ---------------------------------------------------------------------------


class RestoreCliListOnlyTests(unittest.TestCase):

    def test_cli_list_only_returns_zero_and_emits_summary(self) -> None:
        import io
        import json
        from contextlib import redirect_stdout, redirect_stderr
        with tempfile.TemporaryDirectory(prefix="arq-cli-dry-") as td:
            tdp = Path(td)
            _src, dest, br = _build_small_backup(tdp)
            # The dest argument is positional even with --list-only,
            # but the directory doesn't need to exist (we never
            # write to it). Just point at a temp subpath.
            stub_dest = tdp / "would_restore_here"
            from arq_reader.cli import main
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                rc = main([
                    "restore", str(dest),
                    "--password", "pw",
                    "--list-only",
                    br.folder_uuid, str(stub_dest),
                ])
            self.assertEqual(rc, 0, msg=buf_err.getvalue())
            payload = json.loads(buf_out.getvalue())
            self.assertEqual(payload.get("list_only"), True)
            self.assertEqual(payload.get("files_listed"), 3)
            self.assertEqual(payload.get("bytes_would_restore"), 32)
            # Stub dest dir was never created.
            self.assertFalse(stub_dest.exists())


# ---------------------------------------------------------------------------
# PlanRegistry.mark_run
# ---------------------------------------------------------------------------


class PlanRegistryMarkRunTests(unittest.TestCase):

    def test_mark_run_writes_iso_timestamp(self) -> None:
        from arq_tui.state import Plan, PlanRegistry
        with tempfile.TemporaryDirectory(prefix="arq-plan-stamp-") as td:
            reg = PlanRegistry(config_dir=Path(td))
            plan = Plan(
                plan_id="plan-1", name="t",
                sources=["/tmp"],
                destination_kind="local",
                destination={"path": "/tmp/dest"},
            )
            reg.save(plan)
            self.assertEqual(plan.last_run_iso, "")
            ok = reg.mark_run("plan-1")
            self.assertTrue(ok)
            # Re-load and verify the timestamp is set + parses.
            loaded = reg.list_plans()
            self.assertEqual(len(loaded), 1)
            self.assertNotEqual(loaded[0].last_run_iso, "")
            # Should be parseable as ISO-8601.
            datetime.fromisoformat(loaded[0].last_run_iso)

    def test_mark_run_missing_plan_returns_false(self) -> None:
        from arq_tui.state import PlanRegistry
        with tempfile.TemporaryDirectory(prefix="arq-plan-stamp-") as td:
            reg = PlanRegistry(config_dir=Path(td))
            # No plan saved → mark_run returns False.
            self.assertFalse(reg.mark_run("does-not-exist"))

    def test_mark_run_uses_provided_timestamp(self) -> None:
        from arq_tui.state import Plan, PlanRegistry
        with tempfile.TemporaryDirectory(prefix="arq-plan-stamp-") as td:
            reg = PlanRegistry(config_dir=Path(td))
            plan = Plan(plan_id="p2", name="t2", sources=["/tmp"])
            reg.save(plan)
            specific = "2026-01-15T03:45:00+00:00"
            reg.mark_run("p2", when_iso=specific)
            loaded = reg.list_plans()
            self.assertEqual(loaded[0].last_run_iso, specific)


# ---------------------------------------------------------------------------
# BackupRunScreen wires _stamp_plan_last_run
# ---------------------------------------------------------------------------


class BackupRunScreenStampWireupTests(unittest.TestCase):

    def test_stamp_helper_called_on_finish(self) -> None:
        try:
            from arq_tui.screens.backup_run import BackupRunScreen
        except ImportError:
            self.skipTest("textual not installed")
        # Source-level: both lifecycle handlers reference the
        # _stamp_plan_last_run helper. A regression that drops
        # the call on either path can't pass this test.
        src = (
            REPO_ROOT / "arq_tui" / "screens" / "backup_run.py"
        ).read_text(encoding="utf-8")
        # Helper exists at the class level.
        self.assertTrue(
            hasattr(BackupRunScreen, "_stamp_plan_last_run"),
        )
        # Both lifecycle handlers call the helper.
        self.assertGreaterEqual(
            src.count("_stamp_plan_last_run"), 3,
            "expected _stamp_plan_last_run defined + called on "
            "both finish + fail handlers",
        )


if __name__ == "__main__":
    unittest.main()
