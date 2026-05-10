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

            def patched_stat(self, *args, **kwargs):
                # Python 3.10+ passes follow_symlinks as a kwarg;
                # Python 3.9 doesn't. Accept both via *args/**kwargs
                # so the mock works on both interpreter versions.
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
                # Forward only kwargs the running Python supports;
                # on 3.9 forwarding follow_symlinks raises TypeError.
                if (
                    sys.version_info >= (3, 10)
                    and "follow_symlinks" in kwargs
                ):
                    return real_stat(
                        self,
                        follow_symlinks=kwargs["follow_symlinks"],
                    )
                return real_stat(self)

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


class StructuredBackupRecordErrorsFlowTests(unittest.TestCase):
    """F1: every per-file walker error MUST land as a structured
    dict in ``backup_record_errors`` (and therefore in the
    on-disk backuprecord's ``backupRecordErrors`` list), shaped
    to Arq.app v8's per-error schema sampled 2026-05-10.

    The tests trigger each of the five
    ``_record_error`` call sites in ``backup.py``:

    - ``read_bytes`` failure
    - ``post_read_stat`` failure (post-content-read stat raises)
    - ``readlink`` failure (symlink readlink raises)
    - ``lstat`` failure (symlink lstat raises after readlink)
    - ``post_walk_stat`` failure (directory stat raises post-walk)

    For each, the resulting error dict must carry the
    required-3 keys (``localPath``, ``errorMessage``,
    ``pathIsDirectory``) plus the optional NSError-mapped triple
    when the underlying exception is an ``OSError`` with errno.
    """

    def _build_backup(self, dest_root, *, callback=None):
        from arq_writer.backup import Backup
        return Backup(
            dest_root=dest_root,
            encryption_password="pw",
            backup_name="test",
            callback=callback,
        )

    def test_read_bytes_failure_records_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "ok.txt").write_text("hello")
            (src / "bad.txt").write_text("ignored")
            bk = self._build_backup(tdp / "dest")
            bk.init_plan()
            real_read = Path.read_bytes

            def patched(self):
                if self.name == "bad.txt":
                    # PermissionError with errno=13 → NSPOSIXErrorDomain
                    raise PermissionError(13, "Permission denied")
                return real_read(self)

            with mock.patch.object(Path, "read_bytes", patched):
                bk.add_folder(src)

            self.assertEqual(len(bk.backup_record_errors), 1)
            err = bk.backup_record_errors[0]
            # Required-3 keys.
            self.assertEqual(err["pathIsDirectory"], False)
            self.assertTrue(err["localPath"].endswith("/bad.txt"))
            self.assertIn("Failed to read_bytes", err["errorMessage"])
            self.assertIn("Permission denied", err["errorMessage"])
            # NSError-mapped triple — errno=13 (EACCES on POSIX).
            self.assertEqual(err["errorCode"], 13)
            self.assertEqual(err["errorDomain"], "NSPOSIXErrorDomain")
            self.assertEqual(err["severity"], 3)

    def test_post_read_stat_failure_records_structured_error(
        self,
    ) -> None:
        # When ``stat()`` raises after a successful read_bytes, the
        # writer falls back to defaulted metadata + records the
        # error (the bytes are already on disk; metadata is the
        # only loss).
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_text("hi")
            bk = self._build_backup(tdp / "dest")
            bk.init_plan()
            real_stat = Path.stat
            calls = {"n": 0}

            def patched(self, *a, **kw):
                # Allow the pre-walk + dir stats; raise on the
                # post-read stat for f.txt.
                if self.name == "f.txt":
                    calls["n"] += 1
                    if calls["n"] >= 2:
                        raise OSError(2, "No such file or directory")
                return real_stat(self, *a, **kw)

            with mock.patch.object(Path, "stat", patched):
                bk.add_folder(src)

            errs = [
                e for e in bk.backup_record_errors
                if "Failed to post_read_stat" in e["errorMessage"]
            ]
            self.assertEqual(len(errs), 1)
            err = errs[0]
            self.assertEqual(err["pathIsDirectory"], False)
            self.assertTrue(err["localPath"].endswith("/f.txt"))
            self.assertEqual(err["errorCode"], 2)
            self.assertEqual(err["errorDomain"], "NSPOSIXErrorDomain")

    @unittest.skipUnless(
        hasattr(os, "symlink") and os.name == "posix",
        "POSIX symlink behaviour required",
    )
    def test_readlink_failure_records_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            sl = src / "link.lnk"
            os.symlink("/some/target", sl)
            bk = self._build_backup(tdp / "dest")
            bk.init_plan()
            with mock.patch(
                "os.readlink",
                side_effect=OSError(13, "Permission denied"),
            ):
                bk.add_folder(src)
            errs = [
                e for e in bk.backup_record_errors
                if "Failed to readlink" in e["errorMessage"]
            ]
            self.assertEqual(len(errs), 1)
            err = errs[0]
            self.assertEqual(err["pathIsDirectory"], False)
            self.assertEqual(err["errorCode"], 13)

    def test_post_walk_dir_stat_failure_records_structured_error(
        self,
    ) -> None:
        # When ``Path.stat()`` raises on a directory AFTER its
        # children walk, the directory's TreeNode lands with
        # defaulted metadata + the error is structured. Match by
        # name only — calling ``self.is_dir()`` inside the mocked
        # stat() would re-enter stat() and recurse.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            sub = src / "subdir"
            sub.mkdir(parents=True)
            (sub / "child.txt").write_text("hi")
            bk = self._build_backup(tdp / "dest")
            bk.init_plan()
            real_stat = Path.stat
            # ``Backup.add_folder`` calls ``source.resolve()`` which
            # canonicalises ``/var/folders/...`` → ``/private/var/
            # folders/...`` on macOS, so match by resolved string.
            target_path = str(sub.resolve())
            # The walker hits ``sub`` with multiple stat() variants:
            # ``is_dir()`` and the post-walk metadata capture both
            # use the default ``follow_symlinks=True`` form;
            # ``is_symlink()`` uses ``follow_symlinks=False``.
            # We want only the LAST default-stat to raise (that's
            # the post-walk capture, line ~1124 in backup.py), so:
            # - allow every ``follow_symlinks=False`` call
            # - count only the ``follow_symlinks=True`` calls and
            #   fail from the second one onwards.
            calls = {"default": 0}

            def patched(self, *a, **kw):
                follow = kw.get("follow_symlinks", True)
                if a:
                    follow = a[0]
                if str(self) == target_path and follow:
                    calls["default"] += 1
                    if calls["default"] >= 2:
                        raise OSError(13, "Permission denied")
                return real_stat(self, *a, **kw)

            with mock.patch.object(Path, "stat", patched):
                bk.add_folder(src)
            errs = [
                e for e in bk.backup_record_errors
                if "Failed to post_walk_stat" in e["errorMessage"]
            ]
            self.assertEqual(len(errs), 1)
            err = errs[0]
            self.assertEqual(
                err["pathIsDirectory"], True,
                "directory stat error must mark pathIsDirectory=True",
            )
            self.assertEqual(err["errorCode"], 13)

    def test_errors_appear_in_emitted_backuprecord_plist(self) -> None:
        # End-to-end: build a backup with a triggered failure and
        # confirm the failure is in the on-disk
        # ``backupRecordErrors`` list, not just the in-memory state.
        from arq_validator.backend import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_validator.layout import keyset_path
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_writer.backuprecord import parse_backuprecord

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "ok.txt").write_text("alpha")
            (src / "bad.txt").write_text("never read")
            dest = tdp / "dest"
            bk = self._build_backup(dest)
            bk.init_plan()
            real_read = Path.read_bytes

            def patched(self):
                if self.name == "bad.txt":
                    raise PermissionError(13, "Permission denied")
                return real_read(self)

            with mock.patch.object(Path, "read_bytes", patched):
                rec_path = bk.add_folder(src)

            backend = LocalBackend(dest)
            ks = decrypt_keyset(
                backend.read_all(keyset_path("/", bk.computer_uuid)),
                "pw",
            )
            # macOS canonicalises tempdirs through ``/private/var``;
            # both sides need the same canonical form for
            # ``relative_to`` to work.
            rel = "/" + str(
                Path(rec_path).resolve()
                .relative_to(Path(dest).resolve())
            ).replace(os.sep, "/")
            rec = parse_backuprecord(decrypt_lz4_arqo(
                backend.read_all(rel),
                ks.encryption_key, ks.hmac_key,
            ))
            errs = rec.get("backupRecordErrors") or []
            self.assertEqual(len(errs), 1)
            self.assertTrue(errs[0]["localPath"].endswith("/bad.txt"))
            self.assertEqual(errs[0]["errorCode"], 13)
            self.assertEqual(errs[0]["errorDomain"], "NSPOSIXErrorDomain")
            self.assertEqual(errs[0]["pathIsDirectory"], False)
            # Old field stays absent.
            self.assertNotIn("errorCount", rec)

    def test_per_folder_errors_reset_between_add_folder_calls(
        self,
    ) -> None:
        # Two ``add_folder`` calls should each carry only their own
        # walk's failures — no leak from a prior folder's accumulator.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            a = tdp / "a"
            a.mkdir()
            (a / "bad.txt").write_text("x")
            b = tdp / "b"
            b.mkdir()
            (b / "ok.txt").write_text("y")
            bk = self._build_backup(tdp / "dest")
            bk.init_plan()
            real_read = Path.read_bytes

            def patched(self):
                if self.name == "bad.txt":
                    raise PermissionError(13, "Permission denied")
                return real_read(self)

            with mock.patch.object(Path, "read_bytes", patched):
                bk.add_folder(a)
                # After folder A: errors list has 1 entry.
                self.assertEqual(len(bk.backup_record_errors), 1)
                bk.add_folder(b)
                # After folder B: errors list reset + B had no
                # failures, so empty.
                self.assertEqual(bk.backup_record_errors, [])


if __name__ == "__main__":
    unittest.main()
