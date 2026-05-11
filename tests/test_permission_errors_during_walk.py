"""C-I1 — Permission errors during walk.

A source tree where some files are unreadable (chmod 000)
should NOT crash the walker. The writer's contract:

- File-level read error → emit ``file_read_error`` event,
  record structured error, skip the file, continue
- Directory-level read error → emit ``dir_read_error``, skip
  the subtree, continue
- The backuprecord includes the error in ``backupRecordErrors``
  list per F1 (per-file structured errors)

This module pins the graceful-degradation behaviour: backup
COMPLETES even with unreadable entries, producing a
backuprecord whose error list documents what was skipped.
"""

from __future__ import annotations

import os
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


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
@unittest.skipIf(
    os.geteuid() == 0,
    "running as root bypasses permission checks",
)
@unittest.skipIf(
    platform.system().startswith("Win"),
    "POSIX permission semantics required",
)
class PermissionErrorsTests(unittest.TestCase):

    def test_unreadable_file_recorded_as_error(self) -> None:
        """A chmod-000 file in source — walker emits
        file_read_error, records the structured error, continues."""
        from arq_writer.backup import build_backup
        events = []
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "good.txt").write_bytes(b"good")
            bad = src / "bad.txt"
            bad.write_bytes(b"unreadable")
            os.chmod(bad, 0o000)
            try:
                dest = tdp / "dest"
                res = build_backup(
                    str(src), str(dest), encryption_password="pw",
                    callback=lambda k, p: events.append((k, p)),
                )
                # Backup completed.
                self.assertGreater(res.files_written, 0)
                # files_with_errors counts at least 1.
                self.assertGreaterEqual(res.files_with_errors, 1)
                # file_read_error event surfaced.
                read_errors = [
                    p for (k, p) in events if k == "file_read_error"
                ]
                self.assertGreater(len(read_errors), 0)
                bad_path = [
                    p for p in read_errors
                    if p["path"].endswith("/bad.txt")
                ]
                self.assertEqual(len(bad_path), 1)
            finally:
                # Restore perms so tempdir cleanup works.
                os.chmod(bad, 0o644)

    def test_good_files_still_backed_up_despite_unreadable_sibling(
        self,
    ) -> None:
        """The good file in the same dir restores correctly even
        when a sibling was unreadable."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "good.txt").write_bytes(b"survives")
            bad = src / "bad.txt"
            bad.write_bytes(b"won't make it")
            os.chmod(bad, 0o000)
            try:
                dest = tdp / "dest"
                build_backup(
                    str(src), str(dest), encryption_password="pw",
                )
                out = tdp / "out"
                out.mkdir()
                rs = Restore(str(dest), encryption_password="pw")
                layouts = rs.layouts()
                rs.restore(
                    folder_uuid=layouts[0].backup_folder_uuids[0],
                    computer_uuid=layouts[0].computer_uuid, dest=out,
                )
                self.assertEqual(
                    (out / "good.txt").read_bytes(), b"survives",
                )
                # bad.txt should not be in the restore (it was
                # skipped at backup time).
                self.assertFalse((out / "bad.txt").exists())
            finally:
                os.chmod(bad, 0o644)


if __name__ == "__main__":
    unittest.main()
