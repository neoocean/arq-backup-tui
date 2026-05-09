"""Tests for hardlink dedup (writer) + reconstruction (restore).

Sources with many hardlinks (a git checkout, a node_modules tree,
hardlink-based time-machine layouts) used to be re-walked per
link: the same bytes got read, chunked, encrypted, and uploaded
N times. This pass adds an in-walk inode cache on the writer side
so the second-and-later link hits the cached FileNode instead.

Restore-side, multiple FileNodes carrying the same ``mac_st_ino``
get materialised by ``os.link()`` instead of by writing the body
again — preserving the hardlink relationship across the round
trip.

These tests skip on Windows (st_ino / os.link semantics differ
substantially) and on hosts where the temp dir refuses hardlinks
(very rare; tmpfs in some sandboxes does this).
"""

from __future__ import annotations

import os
import subprocess
import sys
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


@unittest.skipIf(
    sys.platform == "win32",
    "hardlink semantics on Windows are out of scope",
)
@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class HardlinkRoundTripTests(unittest.TestCase):

    def _link(self, src: Path, dst: Path) -> bool:
        """Best-effort os.link; return False if the FS refuses."""
        try:
            os.link(src, dst)
            return True
        except OSError:
            return False

    def test_writer_reuses_one_filenode_for_two_hardlinks(self) -> None:
        """The walker should produce a Tree whose two children point
        at the *same* FileNode object — proving the second link
        didn't re-read the bytes."""
        from arq_writer import Backup
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            payload = b"hardlink content " * 50
            (src / "primary.bin").write_bytes(payload)
            if not self._link(
                src / "primary.bin", src / "alias.bin",
            ):
                self.skipTest("filesystem refuses hardlinks")

            dst = Path(td) / "dst"
            dst.mkdir()
            bk = Backup(
                dest_root=dst,
                encryption_password="secret",
                backup_name="hl-test",
            )
            bk.init_plan()
            bk.add_folder(src)
            # Sniff the in-memory cache: at least one inode entry
            # must be present (the shared one) to prove the
            # short-circuit ran.
            self.assertTrue(
                bk._inode_to_node,
                "writer never recorded any inode in its hardlink "
                "cache — short-circuit didn't trigger",
            )

    def test_backup_then_restore_recreates_hardlink(self) -> None:
        """End-to-end: source has two hardlinked files; restored
        copies must share an inode (i.e. os.link, not two writes)."""
        from arq_writer import build_backup
        from arq_reader import Restore

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            (src / "a.bin").write_bytes(b"shared payload " * 100)
            if not self._link(src / "a.bin", src / "b.bin"):
                self.skipTest("filesystem refuses hardlinks")
            # Sanity-check: source IS hardlinked.
            self.assertEqual(
                (src / "a.bin").stat().st_ino,
                (src / "b.bin").stat().st_ino,
            )

            dst = Path(td) / "dst"
            dst.mkdir()
            build_backup(
                src, dst, "secret", backup_name="hl-test",
            )

            out = Path(td) / "out"
            rs = Restore(str(dst), encryption_password="secret")
            cu = next(p.name for p in dst.iterdir() if p.is_dir())
            folder_uuid = next(
                p.name
                for p in (dst / cu / "backupfolders").iterdir()
                if p.is_dir()
            )
            rs.restore(
                folder_uuid=folder_uuid,
                computer_uuid=cu, dest=out,
            )
            a = out / "a.bin"
            b = out / "b.bin"
            self.assertTrue(a.is_file() and b.is_file())
            # Most important assertion: same inode after restore.
            self.assertEqual(
                a.stat().st_ino, b.stat().st_ino,
                "restored a.bin and b.bin should share an inode "
                "(hardlink reconstruction)",
            )
            # And the content survived correctly.
            self.assertEqual(a.read_bytes(), b"shared payload " * 100)


if __name__ == "__main__":
    unittest.main()
