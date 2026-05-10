"""Walker safety hardening tests.

Earlier behaviour: the walker silently swallowed OSError on
``read_bytes`` + substituted empty bytes — producing a backup
that LOOKED successful but had 0-byte content for failing
files. This was invisible damage: the operator only discovered
it on restore.

New behaviour:
- Per-file read / readlink / stat errors emit a ``*_read_error``
  / ``*_stat_error`` event AND increment ``files_with_errors``,
  AND skip the entry from the resulting tree (so the backup
  honestly omits files it couldn't read instead of silently
  including 0-byte versions).
- Post-walk stat() failures (the file was deleted between read
  + stat) fall back to defaulted metadata BUT also count
  toward ``files_with_errors``, so the operator can distinguish
  "all files written cleanly" from "some files written with
  defaulted mtime/mode because the source was racing".

These tests pin the new behaviour so a future refactor can't
accidentally re-introduce the silent-corruption path.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class WalkerReadErrorEmitsAndSkipsTests(unittest.TestCase):
    """``Backup._walk_file`` emits a file_read_error event AND
    skips the file (returns None) when read_bytes raises."""

    def _build_backup(self, dest_root, *, callback):
        from arq_writer.backup import Backup
        return Backup(
            dest_root=dest_root,
            encryption_password="pw",
            backup_name="test",
            callback=callback,
        )

    def test_read_bytes_oserror_emits_and_increments_counter(self) -> None:
        events = []

        def cb(kind, payload):
            events.append((kind, payload))

        with tempfile.TemporaryDirectory(prefix="arq-walker-") as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            ok = src / "ok.txt"
            ok.write_text("hello")
            bad = src / "bad.txt"
            bad.write_text("ignored — patched read_bytes raises")
            dest = tdp / "dest"

            bk = self._build_backup(dest, callback=cb)
            bk.init_plan()

            # Patch read_bytes to raise PermissionError for bad.txt
            # only. Other files (ok.txt) pass through normally.
            real_read = Path.read_bytes

            def patched(self):
                if self.name == "bad.txt":
                    raise PermissionError("simulated EACCES")
                return real_read(self)

            with mock.patch.object(Path, "read_bytes", patched):
                bk.add_folder(src)

            # The ok file landed; the bad file was skipped + reported.
            kinds = [k for k, _ in events]
            self.assertIn("file_read_error", kinds)
            self.assertEqual(bk.files_with_errors, 1)
            # files_written should reflect ok.txt only (bad was
            # skipped). The dir tree node also counts as a tree, not
            # a file, so files_written == 1.
            self.assertEqual(bk.files_written, 1)

    def test_readlink_failure_skips_symlink(self) -> None:
        events = []

        def cb(kind, payload):
            events.append((kind, payload))

        with tempfile.TemporaryDirectory(prefix="arq-symlink-") as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            link = src / "broken_link"
            link.symlink_to("/nonexistent/target")
            dest = tdp / "dest"
            bk = self._build_backup(dest, callback=cb)
            bk.init_plan()

            # Patch os.readlink to raise ONLY when called for our
            # broken_link basename. Other os.readlink calls inside
            # Path.resolve() (for /var → /private/var on macOS,
            # etc.) must pass through unchanged so the surrounding
            # plumbing keeps working.
            real_readlink = os.readlink

            def patched_readlink(target):
                if os.path.basename(os.fspath(target)) == "broken_link":
                    raise OSError("simulated readlink fail")
                return real_readlink(target)

            with mock.patch(
                "arq_writer.backup.os.readlink",
                side_effect=patched_readlink,
            ):
                bk.add_folder(src)

            ops = [
                p.get("op")
                for k, p in events
                if k == "file_read_error"
            ]
            self.assertIn(
                "readlink", ops,
                f"expected file_read_error(op=readlink), got {ops}",
            )
            self.assertEqual(bk.files_with_errors, 1)


class WalkerStatRaceFallbackTests(unittest.TestCase):
    """Post-read stat() race → defaulted metadata + counter
    increment + diagnostic event. The blob bytes still land
    successfully; only the metadata is sentinel-defaulted."""

    def test_post_read_stat_failure_uses_defaults(self) -> None:
        events = []

        def cb(kind, payload):
            events.append((kind, payload))

        with tempfile.TemporaryDirectory(prefix="arq-stat-race-") as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            f = src / "racey.txt"
            f.write_text("content captured")
            dest = tdp / "dest"

            from arq_writer.backup import Backup
            bk = Backup(
                dest_root=dest, encryption_password="pw",
                backup_name="t", callback=cb,
            )
            bk.init_plan()

            # Strategy: flag-based mocking. read_bytes sets the
            # flag after returning the file content; the patched
            # stat() raises iff the flag is set AND the call
            # targets racey.txt. This pinpoints exactly the
            # post-read stat without needing to know how many
            # other stat calls happen for racey.txt.
            real_stat = Path.stat
            real_read = Path.read_bytes
            post_read_for_racey = {"flag": False}

            def patched_read(self):
                content = real_read(self)
                if self.name == "racey.txt":
                    post_read_for_racey["flag"] = True
                return content

            def patched_stat(self, *, follow_symlinks=True):
                if (
                    self.name == "racey.txt"
                    and post_read_for_racey["flag"]
                ):
                    # Reset so the FILE-level stat that lives
                    # AFTER FileNode build (e.g. cache/inode
                    # registration) doesn't keep raising.
                    post_read_for_racey["flag"] = False
                    raise FileNotFoundError(
                        "simulated source-deleted-mid-walk",
                    )
                return real_stat(self, follow_symlinks=follow_symlinks)

            with mock.patch.object(Path, "read_bytes", patched_read), \
                    mock.patch.object(Path, "stat", patched_stat):
                bk.add_folder(src)

            kinds = [k for k, _ in events]
            # The file_stat_error event should have fired with
            # the recovery=defaulted_metadata payload.
            self.assertIn("file_stat_error", kinds)
            for k, p in events:
                if k == "file_stat_error":
                    self.assertEqual(
                        p.get("recovery"), "defaulted_metadata",
                    )
                    self.assertEqual(p.get("op"), "post_read_stat")
                    break
            self.assertEqual(bk.files_with_errors, 1)
            # The blob bytes WERE captured — files_written should
            # still tick (1 file successfully written, with
            # defaulted metadata).
            self.assertGreaterEqual(bk.files_written, 1)


class BackupResultSurfacesErrorCountTests(unittest.TestCase):
    """``BackupResult.files_with_errors`` is exposed in the
    result dataclass + the build_backup convenience returns
    it. Operator-facing diagnostics depend on this."""

    def test_field_present_in_dataclass(self) -> None:
        from arq_writer.backup import BackupResult
        from dataclasses import fields
        names = {f.name for f in fields(BackupResult)}
        self.assertIn("files_with_errors", names)

    def test_default_is_zero_for_clean_backup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="arq-clean-") as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_text("alpha")
            (src / "b.txt").write_text("beta")
            dest = tdp / "dest"
            from arq_writer.backup import build_backup
            res = build_backup(
                str(src), str(dest),
                encryption_password="pw", backup_name="clean",
            )
            self.assertEqual(res.files_with_errors, 0)
            self.assertEqual(res.files_written, 2)


if __name__ == "__main__":
    unittest.main()
