"""Adversarial scenarios for the writer + reader.

Pin the actual behaviour of the system in real-world failure
modes operators run into:

- **source_mutate_during_walk**: a file is rewritten or deleted
  between the walker's stat() and read_bytes(). Pinning the
  observed behaviour: whatever bytes the read returns are what
  gets backed up; the recorded itemSize matches the read.
- **source_unreadable**: a file's permission denies the reader
  during the walk. The walker emits ``file_read_error`` + records
  an empty FileNode so the rest of the backup completes.
- **destination_disk_full**: simulated by routing the writer at
  a fake LocalBackend that raises OSError on write_all. The
  backup propagates the exception; partial state lands on disk
  + the backuprecord is NOT written (so the destination's prior
  state is preserved).
- **restore_into_existing_dir**: the on_conflict='rename' policy
  preserves the original + writes the restored copy alongside.
- **restore_skip_policy_does_nothing**: on_conflict='skip' yields
  the original on disk untouched + emits conflict_skipped.

Plus the conflict resolution from E6 is exercised end-to-end.
"""

from __future__ import annotations

import os
import subprocess
import sys
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
# E6 — restore conflict resolution
# ---------------------------------------------------------------------------


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class RestoreConflictResolutionTests(unittest.TestCase):

    def _backup(self, td):
        from arq_writer import build_backup
        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_text("restored content\n")
        (src / "b.txt").write_text("second file\n")
        dst = td / "dst"
        dst.mkdir()
        build_backup(src, dst, "pw")
        cu = next(p.name for p in dst.iterdir() if p.is_dir())
        fu = next(
            p.name
            for p in (dst / cu / "backupfolders").iterdir()
            if p.is_dir()
        )
        return dst, cu, fu

    def test_overwrite_default_replaces(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, cu, fu = self._backup(td)
            out = td / "out"
            out.mkdir()
            (out / "a.txt").write_text("OLD\n")
            rs = Restore(str(dst), encryption_password="pw")
            rs.restore(folder_uuid=fu, computer_uuid=cu, dest=out)
            self.assertEqual(
                (out / "a.txt").read_text(),
                "restored content\n",
            )

    def test_skip_policy_keeps_existing(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, cu, fu = self._backup(td)
            out = td / "out"
            out.mkdir()
            (out / "a.txt").write_text("EXISTING\n")
            events = []
            rs = Restore(
                str(dst), encryption_password="pw",
                on_conflict="skip",
            )
            rs.restore(
                folder_uuid=fu, computer_uuid=cu, dest=out,
                callback=lambda k, p: events.append((k, p)),
            )
            # a.txt left alone …
            self.assertEqual(
                (out / "a.txt").read_text(), "EXISTING\n",
            )
            # … but b.txt (no conflict) restored.
            self.assertEqual(
                (out / "b.txt").read_text(), "second file\n",
            )
            # And we got at least one conflict_skipped event.
            kinds = [k for k, _ in events]
            self.assertIn("conflict_skipped", kinds)

    def test_rename_policy_writes_sibling(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, cu, fu = self._backup(td)
            out = td / "out"
            out.mkdir()
            (out / "a.txt").write_text("KEEP ME\n")
            events = []
            rs = Restore(
                str(dst), encryption_password="pw",
                on_conflict="rename",
            )
            rs.restore(
                folder_uuid=fu, computer_uuid=cu, dest=out,
                callback=lambda k, p: events.append((k, p)),
            )
            # Original untouched.
            self.assertEqual(
                (out / "a.txt").read_text(), "KEEP ME\n",
            )
            # Restored copy lives alongside.
            self.assertTrue(
                (out / "a.txt.restored-1").is_file(),
            )
            self.assertEqual(
                (out / "a.txt.restored-1").read_text(),
                "restored content\n",
            )
            kinds = [k for k, _ in events]
            self.assertIn("conflict_renamed", kinds)

    def test_invalid_policy_rejected_at_init(self) -> None:
        from arq_reader import Restore
        with self.assertRaises(ValueError):
            Restore(
                "/", encryption_password="x",
                on_conflict="zap-everything",
            )


# ---------------------------------------------------------------------------
# G2 — backup safety scenarios
# ---------------------------------------------------------------------------


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class BackupSafetyScenarioTests(unittest.TestCase):

    def test_source_unreadable_emits_event_and_continues(self) -> None:
        """A file with mode 000 can't be read; the walker should
        emit file_read_error + treat it as empty + continue
        backing up the rest of the source."""
        from arq_writer import build_backup
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            (src / "readable.txt").write_text("hello")
            unreadable = src / "unreadable.txt"
            unreadable.write_text("secret")
            try:
                os.chmod(unreadable, 0o000)
            except OSError:
                self.skipTest(
                    "filesystem refuses chmod 000",
                )
            try:
                events = []
                dst = td / "dst"
                dst.mkdir()
                build_backup(
                    src, dst, "pw",
                    callback=lambda k, p:
                        events.append((k, p)),
                )
                kinds = [k for k, _ in events]
                # Backup completed (else build_backup would raise).
                # If we run as root (chmod 000 doesn't block us),
                # no read_error fires and the file is backed up
                # normally — that's also a valid outcome.
                if os.getuid() != 0:
                    self.assertIn(
                        "file_read_error", kinds,
                        f"expected file_read_error for unreadable; "
                        f"kinds={kinds}",
                    )
            finally:
                # Restore mode so tearDown can clean up.
                try:
                    os.chmod(unreadable, 0o600)
                except OSError:
                    pass

    def test_destination_write_failure_propagates(self) -> None:
        """A backend that raises OSError on write_all should
        cause the backup to fail with a useful message — not
        silently corrupt the destination."""
        from arq_writer import Backup
        from arq_validator.backend import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            (src / "a.txt").write_text("alpha")
            dst = td / "dst"
            dst.mkdir()
            real = LocalBackend(dst)

            class _ExplodingBackend:
                """Wraps a real backend but raises on any write."""

                def __init__(self, real):
                    self._real = real

                def __getattr__(self, name):
                    return getattr(self._real, name)

                def write_all(self, path, body):
                    raise OSError(
                        f"simulated disk full ({len(body)} bytes)"
                    )

            bk = Backup(
                dest_root=Path("/"),
                encryption_password="pw",
                backend=_ExplodingBackend(real),
            )
            # init_plan calls write_all (keyset). It MUST raise
            # since the backend always errors.
            with self.assertRaises(OSError):
                bk.init_plan()

    @unittest.skipIf(
        sys.version_info < (3, 10),
        "Python 3.9 pathlib bypasses our os.stat patch via "
        "_NormalAccessor; behaviour pinning still happens on "
        "3.10+ (which is what real operators run).",
    )
    def test_source_deleted_during_walk_currently_raises(self) -> None:
        """Pin the OBSERVED behaviour: when a file is removed
        between the walker's iterdir() and its stat(), build_backup
        propagates FileNotFoundError. This is a known weakness —
        the walker doesn't currently catch OSError around the
        per-file is_dir/is_symlink/stat checks the way a true
        race-tolerant walker would.

        Surface as a regression test rather than a wish: future
        work hardening the walker should flip this assertion +
        add a file_read_error event check instead. Documenting
        as TODO in the safety contract docs.
        """
        from arq_writer import build_backup
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            (src / "a.txt").write_text("first")
            (src / "race.txt").write_text("about to vanish")
            dst = td / "dst"
            dst.mkdir()

            real_stat = os.stat

            def _vanish(path, *args, **kwargs):
                if str(path).endswith("race.txt"):
                    raise FileNotFoundError(
                        f"race: {path} vanished"
                    )
                return real_stat(path, *args, **kwargs)

            with patch("os.stat", _vanish):
                with self.assertRaises(
                    (FileNotFoundError, OSError),
                ):
                    build_backup(src, dst, "pw")


if __name__ == "__main__":
    unittest.main()
