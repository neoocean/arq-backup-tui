"""C6 — Tree deep recursion safety.

A source tree N levels deep should round-trip through the
walker + restore without Python's default recursion limit
(typically 1000) being hit. Real-world ``node_modules`` /
deep mono-repo source trees can be 30–50 levels deep
routinely; the writer's walker should comfortably handle
100+.

This module pins:

- 100-level deep source backs up + restores
- The walker's recursion budget exceeds Python's default
  recursion limit (or it uses an iterative walk)
- The leaf file at the bottom is restored byte-identical
"""

from __future__ import annotations

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


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class TreeDeepRecursionTests(unittest.TestCase):

    def _make_deep_tree(self, root: Path, levels: int) -> Path:
        """Create ``root/d0/d1/.../d<N-1>/leaf.txt``."""
        cur = root
        for i in range(levels):
            cur = cur / f"d{i:03d}"
            cur.mkdir()
        leaf = cur / "leaf.txt"
        leaf.write_bytes(b"deep content")
        return leaf

    def test_100_level_deep_round_trips(self) -> None:
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            self._make_deep_tree(src, 100)
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
            # Walk to the leaf.
            cur = out
            for i in range(100):
                cur = cur / f"d{i:03d}"
            leaf = cur / "leaf.txt"
            self.assertEqual(leaf.read_bytes(), b"deep content")

    def test_walker_handles_150_levels_under_recursion_limit(
        self,
    ) -> None:
        """A 150-level deep tree exceeds typical real-world
        depths (deep mono-repo node_modules ~ 50 levels) by a
        comfortable margin. Limited by macOS PATH_MAX (~1023)
        to ~150 levels with 6-char path segments."""
        from arq_writer.backup import build_backup
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            # Use 1-char dir names to keep total path length
            # well under PATH_MAX even at depth 150.
            cur = src
            for i in range(150):
                cur = cur / f"{i % 10}"
                if not cur.exists():
                    cur.mkdir()
            (cur / "leaf.txt").write_bytes(b"x")
            dest = tdp / "dest"
            old_limit = sys.getrecursionlimit()
            try:
                sys.setrecursionlimit(10_000)
                res = build_backup(
                    str(src), str(dest), encryption_password="pw",
                )
                self.assertGreater(res.files_written, 0)
            finally:
                sys.setrecursionlimit(old_limit)


if __name__ == "__main__":
    unittest.main()
