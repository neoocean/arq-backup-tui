"""Tests for Backup.pause() / resume() (PR-B2).

Pause cooperatively suspends the walker at the next checkpoint
(every directory boundary + start of every file). Resume lifts
the flag. Cancel takes precedence over pause so a paused backup
can still be cancelled cleanly.

The walker observes ``_paused`` inside ``_check_cancel``, which
is called from ``_walk()`` before each entry. We exercise the
contract by pausing right before a multi-file walk + observing
the timing.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


class PauseResumeStateTests(unittest.TestCase):
    """Pure-state tests on the Backup object — no walking needed."""

    def _make_backup(self, td: Path):
        from arq_writer import Backup
        dst = td / "dst"
        dst.mkdir()
        return Backup(
            dest_root=dst, encryption_password="pw",
            backup_name="pause-test",
        )

    def test_pause_sets_flag_and_emits_event(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            events = []
            from arq_writer import Backup
            dst = Path(td) / "dst"
            dst.mkdir()
            bk = Backup(
                dest_root=dst, encryption_password="pw",
                backup_name="pause-test",
                callback=lambda k, p: events.append((k, p)),
            )
            self.assertFalse(bk.is_paused)
            bk.pause()
            self.assertTrue(bk.is_paused)
            self.assertEqual(
                events[-1][0], "backup_paused",
            )

    def test_resume_clears_flag_and_emits_event(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            events = []
            from arq_writer import Backup
            dst = Path(td) / "dst"
            dst.mkdir()
            bk = Backup(
                dest_root=dst, encryption_password="pw",
                callback=lambda k, p: events.append((k, p)),
            )
            bk.pause()
            bk.resume()
            self.assertFalse(bk.is_paused)
            self.assertEqual(
                events[-1][0], "backup_resumed",
            )

    def test_resume_no_op_when_not_paused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            events = []
            from arq_writer import Backup
            dst = Path(td) / "dst"
            dst.mkdir()
            bk = Backup(
                dest_root=dst, encryption_password="pw",
                callback=lambda k, p: events.append((k, p)),
            )
            bk.resume()  # no-op
            self.assertFalse(bk.is_paused)
            # No backup_resumed event should have fired.
            self.assertNotIn(
                "backup_resumed",
                [k for k, _ in events],
            )

    def test_cancel_lifts_pause_so_walker_wakes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            from arq_writer import Backup
            dst = Path(td) / "dst"
            dst.mkdir()
            bk = Backup(
                dest_root=dst, encryption_password="pw",
            )
            bk.pause()
            self.assertTrue(bk.is_paused)
            bk.cancel()
            # Cancel must clear pause so the walker can observe
            # the cancel exception (otherwise the spin loop
            # would keep checking _paused, which is set, and
            # never see _cancelled).
            self.assertFalse(bk.is_paused)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class PauseDuringWalkTests(unittest.TestCase):
    """Drive a real backup, pause it mid-walk from another thread,
    confirm the walker actually suspends + resumes."""

    def test_pause_blocks_walker_until_resume(self) -> None:
        from arq_writer import Backup
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            # Enough files that the walker definitely hits a
            # checkpoint between them.
            for i in range(40):
                (src / f"f{i:03d}.txt").write_bytes(
                    f"content-{i}\n".encode("utf-8")
                )
            dst = td / "dst"
            dst.mkdir()
            # Slow the pause-poll so the test can observe the
            # walker wedged.
            bk = Backup(
                dest_root=dst, encryption_password="pw",
                backup_name="pause-walk",
            )
            bk._pause_poll_sec = 0.05
            bk.init_plan()

            # Pause IMMEDIATELY after init so the very first
            # add_folder hits the pause at its first checkpoint.
            bk.pause()
            done_event = threading.Event()
            err = []

            def _runner():
                try:
                    bk.add_folder(src)
                except Exception as exc:
                    err.append(exc)
                done_event.set()

            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            # Give the walker time to hit pause.
            time.sleep(0.5)
            # Walker must NOT be done yet.
            self.assertFalse(done_event.is_set())
            self.assertTrue(bk.is_paused)
            # Now resume — walker should finish.
            bk.resume()
            done_event.wait(timeout=10)
            self.assertTrue(
                done_event.is_set(),
                "walker did not resume after Backup.resume()",
            )
            self.assertFalse(err, f"walker errored: {err!r}")

    def test_cancel_during_pause_terminates_walker(self) -> None:
        from arq_writer import Backup
        from arq_writer.backup import BackupCancelled
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            for i in range(20):
                (src / f"f{i:03d}.txt").write_text(f"x{i}")
            dst = td / "dst"
            dst.mkdir()
            bk = Backup(
                dest_root=dst, encryption_password="pw",
            )
            bk._pause_poll_sec = 0.05
            bk.init_plan()
            bk.pause()
            err = []
            done = threading.Event()

            def _runner():
                try:
                    bk.add_folder(src)
                except Exception as exc:
                    err.append(exc)
                done.set()

            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            time.sleep(0.3)
            self.assertFalse(done.is_set())
            # Cancel while paused. _check_cancel's pause-spin
            # exits because cancel cleared _paused; then the
            # cancel branch raises.
            bk.cancel()
            done.wait(timeout=10)
            self.assertTrue(done.is_set())
            self.assertEqual(len(err), 1)
            self.assertIsInstance(err[0], BackupCancelled)


if __name__ == "__main__":
    unittest.main()
