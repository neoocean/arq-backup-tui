"""Tests for uid/gid restore (PR-B9 first slice).

The writer was already capturing ``mac_st_uid`` + ``mac_st_gid``
on every Node (PR #21 added the ``userName`` / ``groupName``
strings; the numeric ids have been there since the first format
pass). What was missing on restore: actually calling
``os.chown(path, uid, gid)``.

These tests pin:

- The metadata travels through the round-trip (writer captures →
  reader sees the same uid/gid on the parsed Node).
- The restore actually invokes os.chown with the right values
  (mocked out so we don't need to be root to verify).
- A failed chown emits ``chown_failed`` on the callback but
  doesn't abort the rest of the restore.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
class UidGidPreservationTests(unittest.TestCase):

    def _backup_and_restore(self, td: Path):
        from arq_reader import Restore
        from arq_writer import build_backup
        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_text("hello")
        dst = td / "dst"
        dst.mkdir()
        build_backup(src, dst, "pw")
        cu = next(p.name for p in dst.iterdir() if p.is_dir())
        folder_uuid = next(
            p.name
            for p in (dst / cu / "backupfolders").iterdir()
            if p.is_dir()
        )
        return Restore(str(dst), encryption_password="pw"), cu, folder_uuid

    def test_writer_captures_uid_and_gid_on_filenode(self) -> None:
        """The Node binary the writer emits must carry the source
        file's uid/gid so any restore (root or otherwise) can see
        the metadata."""
        from arq_reader.parse import parse_tree
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_validator.backend import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_validator.layout import (
            keyset_path, list_backuprecords,
        )
        from arq_validator import discover_layout
        from arq_writer.backuprecord import parse_backuprecord
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            rs, cu, fu = self._backup_and_restore(td)
            backend = LocalBackend(td / "dst")
            keyset = decrypt_keyset(
                backend.read_all(keyset_path("/", cu)), "pw",
            )
            recs = list_backuprecords(backend, "/", cu, fu)
            rec_blob = backend.read_all(recs[-1])
            rec = parse_backuprecord(decrypt_lz4_arqo(
                rec_blob,
                keyset.encryption_key, keyset.hmac_key,
            ))
            tree_loc = rec["node"]["treeBlobLoc"]
            tree_arqo = backend.read_all(tree_loc["relativePath"])
            tree = parse_tree(decrypt_lz4_arqo(
                tree_arqo,
                keyset.encryption_key, keyset.hmac_key,
            ))
            child = tree.children[0]
            # Source file was written by the test process — uid
            # should match os.getuid().
            expected_uid = os.getuid()
            self.assertEqual(
                int(child.node.mac_st_uid), expected_uid,
                f"uid not preserved through writer: "
                f"got {child.node.mac_st_uid}",
            )
            # gid: macOS-tmpdir gid often differs from getgid()
            # (it's the dir's gid, not the process'); just sanity
            # that some non-zero value was captured.
            self.assertGreater(int(child.node.mac_st_gid), 0)

    def test_restore_calls_chown_with_correct_uid_gid(self) -> None:
        """Mock os.chown so we can confirm the restore invokes it
        with the captured uid/gid even when running non-root."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            rs, cu, fu = self._backup_and_restore(td)
            out = td / "out"
            chown_calls = []
            real_chown = os.chown

            def _spy_chown(path, uid, gid):
                chown_calls.append((str(path), uid, gid))
                # Don't actually try the real chown (would
                # likely EPERM as non-root); just record.

            with patch("os.chown", _spy_chown):
                rs.restore(
                    folder_uuid=fu, computer_uuid=cu, dest=out,
                )
            # The restored file is a.txt — its chown should have
            # been called with the writer-captured uid/gid.
            file_calls = [
                c for c in chown_calls
                if c[0].endswith("a.txt")
            ]
            self.assertEqual(
                len(file_calls), 1,
                f"expected one chown for a.txt; "
                f"all calls: {chown_calls!r}",
            )
            _, uid, gid = file_calls[0]
            self.assertEqual(uid, os.getuid())
            self.assertGreater(gid, 0)

    def test_chown_failure_emits_event_and_does_not_abort(self) -> None:
        """When chown raises (e.g. running non-root attempting to
        chown TO a different uid), the failure must surface as a
        ``chown_failed`` event and the rest of the file's
        metadata pipeline continues."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            rs, cu, fu = self._backup_and_restore(td)
            out = td / "out"
            events = []

            def _failing_chown(path, uid, gid):
                raise PermissionError(
                    f"can't chown to uid={uid} as non-root"
                )

            with patch("os.chown", _failing_chown):
                rs.restore(
                    folder_uuid=fu, computer_uuid=cu, dest=out,
                    callback=lambda k, p: events.append((k, p)),
                )
            kinds = [k for k, _ in events]
            # The chown_failed event fired …
            self.assertIn("chown_failed", kinds)
            # … and the file_restored event ALSO fired (i.e. the
            # restore continued past the chown failure).
            self.assertIn("file_restored", kinds)
            # The restored file actually exists.
            self.assertTrue((out / "a.txt").is_file())


if __name__ == "__main__":
    unittest.main()
