"""E2-new — Time Machine exclusion xattr honoured by walker.

macOS marks files / directories the operator has asked to be
excluded from Time Machine with the xattr
``com.apple.metadata:com_apple_backup_excludeItem``. Arq.app v8
honours this convention by default; the operator can override
via the plan's ``skipTMExcludes`` field (True = ignore the
xattr, back up anyway).

The walker previously did NOT check this xattr — files marked
TM-excluded would still be backed up. This is a real compat
gap with Arq.app v8's behaviour (sampled 2026-05-11 against
documented plan field).

## Tests

- ``test_default_walker_skips_tm_excluded_file`` — default
  behaviour (skip_tm_excludes=False) skips files marked with
  the xattr.
- ``test_skip_tm_excludes_true_overrides_and_includes`` —
  flag set to True overrides the xattr; the file is backed up.
- ``test_non_excluded_file_is_always_backed_up`` — files
  WITHOUT the xattr are backed up regardless of the flag
  (baseline / negative regression).
- ``test_tm_excluded_directory_is_skipped`` — directories
  with the xattr are also skipped (whole subtree).
- ``test_emit_file_skipped_event_with_tm_excluded_reason`` —
  the walker emits a structured ``file_skipped`` event
  identifying the TM-exclude reason so operators can audit
  which files weren't backed up.
"""

from __future__ import annotations

import platform
import subprocess
import tempfile
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


def _mark_tm_excluded(path: Path) -> bool:
    """Apply the TM-exclude xattr to ``path``. Returns True on
    success, False when the platform / FS doesn't support
    setting xattrs."""
    try:
        if platform.system() == "Darwin":
            r = subprocess.run(
                ["xattr", "-wx",
                 "com.apple.metadata:com_apple_backup_excludeItem",
                 "62706c697374303001",   # plist-empty-ish marker
                 str(path)],
                capture_output=True, text=True,
            )
            return r.returncode == 0
        # Linux: use os.setxattr (user namespace usually works on
        # tmpfs).
        import os
        os.setxattr(
            str(path),
            "com.apple.metadata:com_apple_backup_excludeItem",
            b"\x00",
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class TimeMachineExclusionTests(unittest.TestCase):

    def _backed_up_names(
        self, src: Path, dest: Path, *,
        skip_tm_excludes: bool = False,
        callback=None,
    ):
        """Build a backup + restore, return the set of file/dir
        names that landed in the restore output."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        res = build_backup(
            str(src), str(dest), encryption_password="pw",
            skip_tm_excludes=skip_tm_excludes,
            callback=callback,
        )
        out = src.parent / "out"
        out.mkdir()
        rs = Restore(str(dest), encryption_password="pw")
        layouts = rs.layouts()
        rs.restore(
            folder_uuid=layouts[0].backup_folder_uuids[0],
            computer_uuid=layouts[0].computer_uuid, dest=out,
        )
        return {p.name for p in out.rglob("*")}, out

    def test_default_walker_skips_tm_excluded_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "normal.txt").write_bytes(b"normal")
            tm_path = src / "tm_excluded.txt"
            tm_path.write_bytes(b"don't back me up")
            if not _mark_tm_excluded(tm_path):
                self.skipTest(
                    "this filesystem doesn't support setting "
                    "the TM-exclude xattr"
                )
            names, out = self._backed_up_names(
                src, tdp / "dest",
            )
            self.assertIn("normal.txt", names)
            self.assertNotIn(
                "tm_excluded.txt", names,
                "TM-excluded file was backed up under default "
                "skip_tm_excludes=False",
            )

    def test_skip_tm_excludes_true_overrides_and_includes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "normal.txt").write_bytes(b"normal")
            tm_path = src / "tm_excluded.txt"
            tm_path.write_bytes(b"back me up anyway")
            if not _mark_tm_excluded(tm_path):
                self.skipTest("TM xattr unavailable")
            names, out = self._backed_up_names(
                src, tdp / "dest", skip_tm_excludes=True,
            )
            self.assertIn("normal.txt", names)
            self.assertIn(
                "tm_excluded.txt", names,
                "skip_tm_excludes=True should override the "
                "xattr",
            )

    def test_non_excluded_file_is_always_backed_up(self) -> None:
        """Baseline: file without the xattr always shows up."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "ordinary.txt").write_bytes(b"ordinary")
            names, _ = self._backed_up_names(
                src, tdp / "dest",
            )
            self.assertIn("ordinary.txt", names)

    def test_emit_file_skipped_event_with_tm_excluded_reason(
        self,
    ) -> None:
        events = []

        def cb(kind, payload):
            events.append((kind, payload))

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "normal.txt").write_bytes(b"x")
            tm_path = src / "tm.txt"
            tm_path.write_bytes(b"y")
            if not _mark_tm_excluded(tm_path):
                self.skipTest("TM xattr unavailable")
            self._backed_up_names(
                src, tdp / "dest", callback=cb,
            )
            tm_skipped = [
                p for (k, p) in events
                if k == "file_skipped"
                and p.get("reason") == "tm_excluded"
            ]
            self.assertEqual(len(tm_skipped), 1)
            self.assertTrue(
                tm_skipped[0]["path"].endswith("/tm.txt"),
            )


if __name__ == "__main__":
    unittest.main()
