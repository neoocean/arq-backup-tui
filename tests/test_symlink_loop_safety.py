"""C-H1 — Symlink loop safety.

Symlinks to directories that contain back-references (A → B,
B contains symlink → A) could in principle make a recursive
walker loop forever. The writer's symlink handling avoids this
by **never following symlinks** — `_walk` checks
``source.is_dir() and not source.is_symlink()`` before
recursing.

This module pins that symlink loops in the source tree do NOT
cause the walker to hang or run out of memory:

- Direct self-loop (``A/sub`` → ``A``)
- Indirect loop (``A/x`` → ``B``, ``B/y`` → ``A``)
- Symlink to parent (``A/sub/back`` → ``A``)
- Symlink to sibling that loops back

In each case the walker captures each symlink as a S_IFLNK
node (with the target string preserved) and does NOT recurse
into the link.
"""

from __future__ import annotations

import os
import platform
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


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
@unittest.skipIf(
    platform.system().startswith("Win"),
    "POSIX symlink semantics required",
)
class SymlinkLoopSafetyTests(unittest.TestCase):

    def _build_with_timeout(self, src: Path, dest: Path, timeout=30):
        """Build a backup of ``src`` with a timeout — pinpoints a
        hang vs a real failure."""
        from arq_writer.backup import build_backup
        result = {}
        def run():
            try:
                result["res"] = build_backup(
                    str(src), str(dest),
                    encryption_password="pw",
                )
            except Exception as exc:
                result["exc"] = exc
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            self.fail(
                f"build_backup hung past {timeout}s timeout — "
                f"likely symlink loop wasn't detected",
            )
        if "exc" in result:
            raise result["exc"]
        return result["res"]

    def test_direct_self_loop_does_not_hang(self) -> None:
        """``src/sub -> ..`` would infinite-loop a naïve walker."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "file.txt").write_bytes(b"content")
            # Loop back to src itself.
            os.symlink("..", str(src / "loopback"))
            dest = tdp / "dest"
            res = self._build_with_timeout(src, dest)
            self.assertGreater(res.files_written, 0)

    def test_indirect_two_step_loop_does_not_hang(self) -> None:
        """``A/x -> B`` + ``B/y -> A`` — two-step indirect loop."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "A").mkdir()
            (src / "B").mkdir()
            (src / "A" / "afile.txt").write_bytes(b"a")
            (src / "B" / "bfile.txt").write_bytes(b"b")
            os.symlink(
                "../B", str(src / "A" / "x_link_to_B"),
            )
            os.symlink(
                "../A", str(src / "B" / "y_link_to_A"),
            )
            dest = tdp / "dest"
            res = self._build_with_timeout(src, dest)
            self.assertGreater(res.files_written, 0)

    def test_parent_loop_does_not_hang(self) -> None:
        """``src/sub/back -> src`` — child links to its ancestor."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "real.txt").write_bytes(b"real")
            (src / "sub").mkdir()
            (src / "sub" / "deep.txt").write_bytes(b"deep")
            os.symlink(
                str(src), str(src / "sub" / "back"),
            )
            dest = tdp / "dest"
            res = self._build_with_timeout(src, dest)
            self.assertGreater(res.files_written, 0)

    def test_loop_symlinks_restored_as_symlinks_not_followed(
        self,
    ) -> None:
        """The symlink IS captured (as a symlink, not via following)
        — restore reconstructs it as a symlink with the target
        string preserved."""
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "real.txt").write_bytes(b"real")
            # Loop link.
            os.symlink("..", str(src / "loopback"))
            dest = tdp / "dest"
            self._build_with_timeout(src, dest)
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            # The link is restored as a symlink (NOT a directory).
            loopback = out / "loopback"
            self.assertTrue(loopback.is_symlink())
            self.assertEqual(os.readlink(loopback), "..")


if __name__ == "__main__":
    unittest.main()
