"""Tests for the dual-mode SubprocessBackupWorker.

The TUI's BackupRunScreen can drive a backup in two ways:

1. **In-process** — :class:`arq_tui.workers.BackupWorker` runs
   the writer on a sibling Python thread inside the TUI process.

2. **Subprocess** — :class:`arq_tui.subprocess_workers.SubprocessBackupWorker`
   spawns ``python3 -m arq_writer create --state-file …`` and
   polls the resulting JSON state file for progress events.

These tests focus on (2): we want to exercise the actual fork
of the writer CLI and prove the polling thread forwards events
back to the screen as Textual messages of the *same* class as
the in-process worker emits — that's why both code paths can
share a single set of message handlers on the screen.

The :func:`subprocess_eligible` rule is also pinned here so a
future "let's allow SFTP destinations" change has to update both
the rule and the test, not just the rule.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import List, Tuple

# Tests rely on Textual being installed for Message construction
# (the same dependency the TUI itself has).
try:
    from textual.message import Message  # noqa: F401
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class SubprocessEligibilityTests(unittest.TestCase):
    """The screen calls :func:`subprocess_eligible` to decide
    between the two modes; we pin its current rules here."""

    def test_local_single_source_is_eligible(self) -> None:
        from arq_tui.state import Plan
        from arq_tui.subprocess_workers import subprocess_eligible
        plan = Plan(
            plan_id="p", name="t",
            sources=["/srv/data"],
            destination_kind="local",
            destination={"path": "/mnt/backup"},
        )
        self.assertTrue(subprocess_eligible(plan, "local"))

    def test_sftp_falls_back_to_in_process(self) -> None:
        from arq_tui.state import Plan
        from arq_tui.subprocess_workers import subprocess_eligible
        plan = Plan(
            plan_id="p", name="t", sources=["/srv/data"],
            destination_kind="sftp",
            destination={"host": "u-x.your-storagebox.de"},
        )
        # The CLI's --dest is a local Path — SFTP can't be
        # expressed without a CLI extension.
        self.assertFalse(subprocess_eligible(plan, "sftp"))

    def test_multi_source_falls_back_to_in_process(self) -> None:
        from arq_tui.state import Plan
        from arq_tui.subprocess_workers import subprocess_eligible
        plan = Plan(
            plan_id="p", name="t",
            sources=["/srv/a", "/srv/b"],
            destination_kind="local",
            destination={"path": "/mnt/backup"},
        )
        # CLI takes one positional source; multi-source needs the
        # in-process worker until we add a wrapper subcommand.
        self.assertFalse(subprocess_eligible(plan, "local"))


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class SubprocessArgvTests(unittest.TestCase):
    """The argv builder is the contract between Plan and CLI flags;
    pin a few common shapes so a Plan-field rename can't silently
    drop a flag."""

    def _build_worker(self, plan):
        from arq_tui.subprocess_workers import SubprocessBackupWorker

        class _FakeApp:
            def call_from_thread(self, fn, *args, **kw):
                return fn(*args, **kw)

        class _FakeTarget:
            app = _FakeApp()

            def post_message(self, msg):
                pass

        return SubprocessBackupWorker(
            _FakeTarget(), plan=plan, password="pw",
        )

    def test_argv_passes_required_create_flags(self) -> None:
        from arq_tui.state import Plan
        plan = Plan(
            plan_id="p", name="MyBackup",
            sources=["/srv/x"],
            destination_kind="local",
            destination={"path": "/mnt/dst"},
            chunker="default",
            use_packs=True,
            dedup_against_existing=True,
        )
        argv = self._build_worker(plan)._build_argv()
        # Subcommand at the right position
        self.assertIn("create", argv)
        # Source + dest landed exactly where the CLI expects them
        self.assertIn("/srv/x", argv)
        self.assertIn(str(Path("/mnt/dst")), argv)
        # Password never goes through argv (would leak through ps)
        self.assertNotIn("pw", argv)
        # Password is read from an env var the worker primes on spawn
        self.assertIn("--password-env", argv)
        self.assertIn("ARQ_BACKUP_PW_TUI", argv)
        # Booleans are present-or-absent (no =true serialization)
        self.assertIn("--use-packs", argv)
        self.assertIn("--dedup-against-existing", argv)
        # Backup name + chunker round-trip
        self.assertIn("--backup-name", argv)
        self.assertIn("MyBackup", argv)
        self.assertIn("--chunker", argv)
        self.assertIn("default", argv)
        # State file path is set so the polling loop can find it
        self.assertIn("--state-file", argv)

    def test_argv_includes_exclusions_and_max_size(self) -> None:
        from arq_tui.state import Plan
        plan = Plan(
            plan_id="p", name="t",
            sources=["/srv/x"],
            destination_kind="local",
            destination={"path": "/mnt/dst"},
            exclude_globs=["*.tmp", "node_modules"],
            exclude_regexes=[r".*\.cache$"],
            max_file_bytes=1024 * 1024,
            use_apfs_snapshot=True,
        )
        argv = self._build_worker(plan)._build_argv()
        self.assertEqual(argv.count("--exclude-glob"), 2)
        self.assertIn("*.tmp", argv)
        self.assertIn("node_modules", argv)
        self.assertEqual(argv.count("--exclude-regex"), 1)
        self.assertIn(r".*\.cache$", argv)
        self.assertIn("--max-file-bytes", argv)
        self.assertIn("1048576", argv)
        self.assertIn("--use-apfs-snapshot", argv)

    def test_argv_omits_optional_flags_when_disabled(self) -> None:
        from arq_tui.state import Plan
        plan = Plan(
            plan_id="p", name="t",
            sources=["/srv/x"],
            destination_kind="local",
            destination={"path": "/mnt/dst"},
            use_packs=False,
            dedup_against_existing=False,
            use_apfs_snapshot=False,
        )
        argv = self._build_worker(plan)._build_argv()
        # When the flag is off, it must NOT appear: action=store_true
        # CLI args treat presence as the on signal.
        self.assertNotIn("--use-packs", argv)
        self.assertNotIn("--dedup-against-existing", argv)
        self.assertNotIn("--use-apfs-snapshot", argv)


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class SubprocessEndToEndTests(unittest.TestCase):
    """Spawn the real ``python3 -m arq_writer create`` against a
    tiny source tree and assert the polling loop forwards each
    state-file event to the screen as a WorkerEvent message,
    finishing with a WorkerFinished.

    Skips on environments missing pyca/cryptography or the
    OpenSSL CLI the writer relies on for AES-256-CBC.
    """

    def test_subprocess_run_emits_finished_for_local_backup(self) -> None:
        from arq_tui.runs import default_state_dir
        from arq_tui.state import Plan
        from arq_tui.subprocess_workers import SubprocessBackupWorker
        from arq_tui.workers import (
            WorkerEvent, WorkerFailed, WorkerFinished,
        )

        # Sanity-check that the writer subprocess can be imported
        # at all in this environment. If not, skip — we don't want
        # to fail in dependency-light CI configurations.
        try:
            import arq_writer  # noqa: F401
        except Exception as exc:
            self.skipTest(f"arq_writer not importable: {exc}")

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            (src / "hello.txt").write_text("hello\n", encoding="utf-8")
            (src / "world.bin").write_bytes(b"\x00" * 16)
            dst = Path(td) / "dst"
            dst.mkdir()
            state_dir = Path(td) / "state"

            # Capture every message the worker posts. The fake
            # target stand-in calls _post in-thread (call_from_thread
            # short-circuits to direct invocation) so we don't need
            # a Textual app instance to observe events.
            received: List[Tuple[str, object]] = []

            class _FakeApp:
                def call_from_thread(self, fn, *args, **kw):
                    fn(*args, **kw)

            class _FakeTarget:
                app = _FakeApp()

                def post_message(self, msg):
                    if isinstance(msg, WorkerEvent):
                        received.append(("event", msg))
                    elif isinstance(msg, WorkerFinished):
                        received.append(("finished", msg))
                    elif isinstance(msg, WorkerFailed):
                        received.append(("failed", msg))

            plan = Plan(
                plan_id="test-plan",
                name="subproc-smoke",
                sources=[str(src)],
                destination_kind="local",
                destination={"path": str(dst)},
                chunker="none",
                use_packs=False,
                dedup_against_existing=False,
            )
            worker = SubprocessBackupWorker(
                _FakeTarget(), plan=plan, password="hunter2",
                state_dir=state_dir,
                executable=sys.executable,
            )
            worker.start()
            # Subprocess + tiny tree should finish well within
            # 30 seconds even on a slow CI runner.
            deadline = time.time() + 30.0
            while time.time() < deadline:
                if any(t in ("finished", "failed") for t, _ in received):
                    break
                time.sleep(0.2)
            worker.join(timeout=5.0)

            terminal = [t for t, _ in received if t in ("finished", "failed")]
            self.assertTrue(
                terminal, f"no terminal message; got {received!r}",
            )
            # Real backup must finish, not fail. If the writer
            # exited non-zero we want the failure message visible
            # in the assertion output for triage.
            if terminal[0] == "failed":
                msg = next(
                    m for t, m in received if t == "failed"
                )
                self.fail(f"backup CLI failed: {msg.error!r}")
            # Some progress events must have streamed through —
            # not just the final finished message.
            event_count = sum(1 for t, _ in received if t == "event")
            self.assertGreater(
                event_count, 0,
                "expected at least one mid-run WorkerEvent, "
                "got only the terminal message",
            )


if __name__ == "__main__":
    unittest.main()
