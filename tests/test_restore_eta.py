"""Tests for restore-side ETA computation.

The reader emits two ProgressCb events that drive ETA:

- ``restore_planning`` — periodic drip from the pre-walk so the
  TUI can show "Planning: N files, M bytes so far…" instead of
  a silent stall during a slow tree fetch on a remote backend.
- ``restore_planned`` — terminal event from the pre-walk carrying
  ``total_files`` + ``total_bytes`` that the ProgressPanel locks
  in as the ETA budget.

Per-file ``file_restored`` events then increment the running
``bytes_plaintext`` counter; the panel's sliding-window throughput
formula yields the ETA.

These tests exercise the full chain end-to-end: build a small
backup, restore it through ``Restore.restore(callback=…)``,
collect every event, and assert ``restore_planning`` +
``restore_planned`` + ``file_restored`` all fire with consistent
numbers.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class RestoreEventStreamTests(unittest.TestCase):

    def _run_round_trip(self, td: Path, *, file_count: int):
        from arq_writer import build_backup
        from arq_reader import Restore
        src = td / "src"
        src.mkdir()
        # Create enough files to ensure at least one
        # ``restore_planning`` tick fires (the cadence is one
        # event per _PLANNING_TICK_FILES files; with the default
        # 200 we'd need 200+ files to see even one tick — for a
        # smoke test we lower the bar by patching the constant).
        # Files split across a sub-dir so the pre-walk recurses.
        sub = src / "sub"
        sub.mkdir()
        for i in range(file_count):
            d = src if i % 2 == 0 else sub
            (d / f"f{i:04d}.txt").write_bytes(
                f"file-{i}-content\n".encode("utf-8") * 4
            )
        dst = td / "dst"
        dst.mkdir()
        build_backup(src, dst, "secret", backup_name="eta-test")

        out = td / "out"
        rs = Restore(str(dst), encryption_password="secret")
        cu = next(p.name for p in dst.iterdir() if p.is_dir())
        folder_uuid = next(
            p.name
            for p in (dst / cu / "backupfolders").iterdir()
            if p.is_dir()
        )

        events: List[Tuple[str, Dict[str, Any]]] = []

        def cb(kind: str, payload: Dict[str, Any]) -> None:
            events.append((kind, dict(payload)))

        rs.restore(
            folder_uuid=folder_uuid, computer_uuid=cu, dest=out,
            callback=cb, plan_totals=True,
        )
        return events, out

    def test_restore_emits_planned_with_correct_totals(self) -> None:
        """``restore_planned`` is the terminal pre-walk event;
        its totals must match the actual files + bytes restored."""
        with tempfile.TemporaryDirectory() as td:
            events, out = self._run_round_trip(
                Path(td), file_count=8,
            )
            planned = [
                p for k, p in events if k == "restore_planned"
            ]
            self.assertEqual(
                len(planned), 1,
                f"expected exactly one restore_planned, got "
                f"{len(planned)}: {planned!r}",
            )
            total_files = planned[0]["total_files"]
            total_bytes = planned[0]["total_bytes"]
            # Totals must be > 0 and match the file_restored
            # event count + sum.
            restored_events = [
                p for k, p in events if k == "file_restored"
            ]
            self.assertEqual(total_files, len(restored_events))
            sum_restored_bytes = sum(
                int(p.get("size") or 0) for p in restored_events
            )
            self.assertEqual(total_bytes, sum_restored_bytes)

    def test_restore_planning_ticks_for_large_tree(self) -> None:
        """When the planning cadence threshold is lowered, the
        pre-walk should emit ``restore_planning`` events with
        cumulative file + byte counts."""
        # Patch _PLANNING_TICK_FILES down to 3 so even a tiny test
        # sees ticks fire — proves the threading is correct.
        from arq_reader import restore as rmod
        original = rmod._PLANNING_TICK_FILES
        rmod._PLANNING_TICK_FILES = 3
        try:
            with tempfile.TemporaryDirectory() as td:
                events, _ = self._run_round_trip(
                    Path(td), file_count=10,
                )
        finally:
            rmod._PLANNING_TICK_FILES = original
        plannings = [
            p for k, p in events if k == "restore_planning"
        ]
        self.assertGreater(
            len(plannings), 0,
            f"no restore_planning events fired; "
            f"got kinds {set(k for k,_ in events)}",
        )
        # Cumulative counts should be non-decreasing.
        last = (0, 0)
        for p in plannings:
            cur = (int(p["files"]), int(p["bytes"]))
            self.assertGreaterEqual(
                cur[0], last[0],
                "files counter went backwards",
            )
            self.assertGreaterEqual(
                cur[1], last[1],
                "bytes counter went backwards",
            )
            last = cur


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class ProgressPanelETARenderTests(unittest.IsolatedAsyncioTestCase):
    """The panel's _update_eta lives in arq_tui; here we drive
    consume_event with hand-built events and assert the ETA line
    renders something sensible.

    Skips on hosts without textual.
    """

    def setUp(self):
        try:
            import textual  # noqa: F401
        except ImportError:
            self.skipTest("textual not installed")

    async def test_panel_locks_in_total_on_restore_planned(self) -> None:
        from arq_tui.widgets.progress_panel import ProgressPanel
        from textual.app import App

        class _A(App):
            def compose(self):
                yield ProgressPanel()

        async with _A().run_test() as pilot:
            panel = pilot.app.query_one(ProgressPanel)
            panel.consume_event(
                "restore_planning",
                {"files": 5, "bytes": 1024},
            )
            # Planning event doesn't lock totals.
            self.assertEqual(panel.total_bytes, 0)
            panel.consume_event(
                "restore_planned",
                {"total_files": 10, "total_bytes": 4096},
            )
            self.assertEqual(panel.total_bytes, 4096)
            self.assertEqual(panel.total_files, 10)


if __name__ == "__main__":
    unittest.main()
