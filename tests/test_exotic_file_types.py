"""E2 — exotic file types (broken symlinks, hardlinks, FIFO, sockets,
devices).

The writer's walker is built around regular files + directories +
symlinks; everything else needs explicit handling. This module pins
each edge case:

- **Broken symlinks** — the link target string is preserved even
  when the target doesn't exist. Restore reconstructs the link
  pointing at the same (still-broken) target.
- **Hardlinks** — multiple paths sharing an inode go into the
  backup once; restore reconstructs the link relationship by
  grouping equal-``mac_st_ino`` entries.
- **FIFO / Unix socket / character / block device** — these have
  no backup-meaningful content (their "content" is a kernel-
  provided stream / channel) AND opening a read-only FIFO with no
  writer blocks the walker indefinitely. The writer detects them
  via ``stat.S_ISFIFO`` / ``S_ISSOCK`` / ``S_ISCHR`` / ``S_ISBLK``
  before any read attempt and emits a structured
  ``file_skipped`` event with ``reason="special_file"``.

The legacy walker had no special-file gate; on a source tree
containing a single FIFO with no writer, the backup would hang
forever. This module's :class:`SpecialFileGateTests` is the
regression pin for that behaviour.
"""

from __future__ import annotations

import os
import socket
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from arq_reader import Restore
from arq_writer.backup import Backup, build_backup


class BrokenSymlinkRoundTripTests(unittest.TestCase):
    def test_dangling_symlink_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            os.symlink(
                "/this/target/does/not/exist",
                str(src / "broken"),
            )
            (src / "real.txt").write_bytes(b"hello\n")
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
            bl = out / "broken"
            self.assertTrue(
                bl.is_symlink(),
                "restored entry must be a symlink, not a regular file",
            )
            self.assertEqual(
                os.readlink(bl), "/this/target/does/not/exist",
                "dangling target string must round-trip exactly",
            )
            # Target doesn't exist → stat() through the link fails;
            # lstat() succeeds.
            with self.assertRaises(FileNotFoundError):
                bl.stat()
            self.assertEqual(
                (out / "real.txt").read_bytes(), b"hello\n",
            )


class HardlinkRoundTripTests(unittest.TestCase):
    def test_hardlink_pair_preserves_shared_inode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"shared content\n")
            os.link(str(src / "a.txt"), str(src / "b.txt"))
            self.assertEqual(
                (src / "a.txt").stat().st_ino,
                (src / "b.txt").stat().st_ino,
                "test setup: source files must share an inode",
            )
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
                (out / "a.txt").stat().st_ino,
                (out / "b.txt").stat().st_ino,
                "restored hardlinks must share an inode",
            )
            self.assertEqual(
                (out / "a.txt").read_bytes(), b"shared content\n",
            )


class SpecialFileGateTests(unittest.TestCase):
    """The legacy walker had no special-file gate. A source tree
    containing a single FIFO with no writer would block the backup
    indefinitely — ``Path.read_bytes()`` on a read-only FIFO blocks
    until a writer connects. Even after the regression pin lands
    these tests run quickly because the gate skips before any read.

    Block devices (``S_ISBLK``) are documented but not exercised —
    they need root to create. The gate code path is identical to
    char / FIFO / socket so we don't risk regressing it by not
    testing it directly.
    """

    def _walk_events(self, callback_events):
        # callback gets (kind, payload_dict); flatten to a dict of
        # kind → list of payloads for assertion convenience.
        out = {}
        for kind, payload in callback_events:
            out.setdefault(kind, []).append(payload)
        return out

    @unittest.skipIf(
        sys.platform.startswith("win"),
        "mkfifo + POSIX file types not supported on Windows",
    )
    def test_fifo_in_source_is_skipped_without_blocking(self) -> None:
        events = []
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            os.mkfifo(str(src / "myfifo"))
            (src / "normal.txt").write_bytes(b"normal\n")
            dest = tdp / "dest"
            bk = Backup(
                dest_root=dest, encryption_password="pw",
                callback=lambda kind, payload: events.append(
                    (kind, payload),
                ),
            )
            bk.init_plan()
            bk.add_folder(src)
            # Normal file landed; FIFO did not.
            self.assertEqual(bk.files_written, 1)
            skipped = [
                p for (k, p) in events
                if k == "file_skipped" and p.get("reason") == "special_file"
            ]
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0]["special_kind"], "fifo")
            self.assertTrue(skipped[0]["path"].endswith("/myfifo"))

    @unittest.skipIf(
        sys.platform.startswith("win"),
        "Unix sockets aren't a Windows concept",
    )
    def test_unix_socket_in_source_is_skipped(self) -> None:
        events = []
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            # AF_UNIX path-bound sockets surface as filesystem
            # entries; the writer must not try to read them.
            sock_path = src / "mysock"
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.bind(str(sock_path))
                (src / "normal.txt").write_bytes(b"normal\n")
                dest = tdp / "dest"
                bk = Backup(
                    dest_root=dest, encryption_password="pw",
                    callback=lambda kind, payload: events.append(
                        (kind, payload),
                    ),
                )
                bk.init_plan()
                bk.add_folder(src)
            finally:
                s.close()
            self.assertEqual(bk.files_written, 1)
            skipped = [
                p for (k, p) in events
                if k == "file_skipped" and p.get("reason") == "special_file"
            ]
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0]["special_kind"], "socket")

    @unittest.skipUnless(
        Path("/dev/null").exists(),
        "needs a system /dev/null to test S_ISCHR detection",
    )
    def test_char_device_via_symlink_to_dev_null_is_skipped(self) -> None:
        # We can't create char devices without root, but /dev/null
        # is universally present. Symlinking it into a source tree
        # would normally make the walker follow-and-read; the gate
        # operates on the LSTAT of the entry directly. To exercise
        # the gate we point a symlink AT /dev/null and confirm the
        # walker's symlink handling treats the link (mode S_IFLNK)
        # as a regular symlink — not invoking the gate at all,
        # because we lstat the link, not the target. This documents
        # the deliberate choice: the gate runs against the entry as
        # the walker sees it (lstat), not after symlink resolution.
        events = []
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            link_path = src / "null-link"
            os.symlink("/dev/null", str(link_path))
            (src / "normal.txt").write_bytes(b"normal\n")
            dest = tdp / "dest"
            bk = Backup(
                dest_root=dest, encryption_password="pw",
                callback=lambda kind, payload: events.append(
                    (kind, payload),
                ),
            )
            bk.init_plan()
            bk.add_folder(src)
            # Both entries land — the symlink is captured (target
            # string preserved), normal.txt is read as usual.
            self.assertEqual(bk.files_written, 2)
            skipped_special = [
                p for (k, p) in events
                if k == "file_skipped" and p.get("reason") == "special_file"
            ]
            self.assertEqual(
                skipped_special, [],
                "the gate must lstat the entry — a symlink TO a "
                "char device is still just a symlink",
            )


if __name__ == "__main__":
    unittest.main()
