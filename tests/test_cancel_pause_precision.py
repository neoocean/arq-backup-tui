"""Tests for the precision fixes in group 3:

A2-(b): ``run_plan_multi`` checks ``cancel_check`` between
        destinations + spawns a watchdog thread that polls it
        during each destination's run. So mid-destination
        cancel actually takes effect at the next checkpoint
        instead of waiting for the whole destination to finish.

A3-(b): ``arq-backup create`` installs SIGUSR1/SIGUSR2 handlers
        that forward to ``Backup.pause()`` / ``resume()`` on a
        module-level ``_active_backup`` reference.
        ``SubprocessBackupWorker.pause()`` / ``resume()``
        ``send_signal()`` SIGUSR1/2 to the child PID — TUI
        operators on subprocess mode now get pause/resume too.
"""

from __future__ import annotations

import signal
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


# ---------------------------------------------------------------------------
# A2-(b)
# ---------------------------------------------------------------------------


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class MultiDestCancelTests(unittest.TestCase):

    def test_cancel_check_between_destinations(self) -> None:
        """Set cancel_check=True before run_plan_multi starts —
        the loop should exit without running any destination."""
        from arq_tui.multi_destination import run_plan_multi
        from arq_tui.state import Plan
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            (src / "a.txt").write_text("alpha")
            d1 = td / "d1"
            d1.mkdir()
            d2 = td / "d2"
            d2.mkdir()
            plan = Plan(
                plan_id="P", name="t", sources=[str(src)],
                destination_kind="local",
                destination={"path": str(d1)},
                additional_destinations=[
                    {"kind": "local", "path": str(d2)},
                ],
            )
            r = run_plan_multi(
                plan, encryption_password="pw",
                cancel_check=lambda: True,
            )
            # Cancel-before-start → both destinations skipped.
            self.assertEqual(len(r.destinations), 0)


# ---------------------------------------------------------------------------
# A3-(b)
# ---------------------------------------------------------------------------


class SignalHandlerInstallTests(unittest.TestCase):

    def test_install_pause_handlers_is_idempotent(self) -> None:
        from arq_writer.cli import _install_pause_signal_handlers
        # Should be safely callable twice — no raise.
        _install_pause_signal_handlers()
        _install_pause_signal_handlers()
        # SIGUSR1 should now have a handler installed.
        if hasattr(signal, "SIGUSR1"):
            handler = signal.getsignal(signal.SIGUSR1)
            self.assertNotEqual(
                handler, signal.SIG_DFL,
                "SIGUSR1 still at default after install",
            )

    def test_active_backup_handle_pause_resume(self) -> None:
        """Set _active_backup to a stub + send a synthetic
        signal — verify pause() / resume() get invoked."""
        if not hasattr(signal, "SIGUSR1"):
            self.skipTest("POSIX signals only")
        import arq_writer.cli as cli_mod
        cli_mod._install_pause_signal_handlers()

        class _StubBackup:
            def __init__(self):
                self.is_paused = False
                self.events = []

            def pause(self):
                self.events.append("pause")
                self.is_paused = True

            def resume(self):
                self.events.append("resume")
                self.is_paused = False

        stub = _StubBackup()
        cli_mod._active_backup = stub
        try:
            import os
            os.kill(os.getpid(), signal.SIGUSR1)
            time.sleep(0.05)
            self.assertEqual(stub.events, ["pause"])
            os.kill(os.getpid(), signal.SIGUSR2)
            time.sleep(0.05)
            self.assertEqual(stub.events, ["pause", "resume"])
        finally:
            cli_mod._active_backup = None


class SubprocessWorkerForwardsSignalsTests(unittest.TestCase):

    def test_pause_resume_methods_exist_on_subprocess_worker(self) -> None:
        try:
            from arq_tui.subprocess_workers import (
                SubprocessBackupWorker,
            )
        except ImportError:
            self.skipTest("textual not installed")
        # Don't construct (needs an App + plan) — just confirm
        # the methods exist on the class so a lookup-by-attr in
        # BackupRunScreen.action_pause_resume succeeds.
        self.assertTrue(
            hasattr(SubprocessBackupWorker, "pause"),
        )
        self.assertTrue(
            hasattr(SubprocessBackupWorker, "resume"),
        )


if __name__ == "__main__":
    unittest.main()
