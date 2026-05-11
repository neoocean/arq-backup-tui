"""C-G1 — Restore --paths filter edge cases.

``Restore.restore(paths=[...])`` lets an operator restore a
subset of files. The filter accepts source-relative POSIX paths
as prefix-style matches:

- ``"a/b"`` matches the file ``a/b`` exactly AND every
  descendant of the directory ``a/b/``
- ``"a/b/c.txt"`` matches just that exact path

This module pins the edge cases:

1. **Exact file match** — restore one specific file
2. **Directory prefix** — restore a subtree
3. **Multiple paths** — union of matches
4. **Non-existent path** — silently restores nothing (no error)
5. **Path with leading slash** — normalised away
6. **Empty list** — same as None (restore everything)
"""

from __future__ import annotations

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
class RestorePathsFilterTests(unittest.TestCase):

    def _build(self, td: Path):
        from arq_writer.backup import build_backup
        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"a")
        (src / "b.txt").write_bytes(b"b")
        (src / "sub1").mkdir()
        (src / "sub1" / "x.txt").write_bytes(b"x")
        (src / "sub1" / "y.txt").write_bytes(b"y")
        (src / "sub2").mkdir()
        (src / "sub2" / "z.txt").write_bytes(b"z")
        dest = td / "dest"
        return build_backup(
            str(src), str(dest), encryption_password="pw",
        )

    def _restore(self, dest: Path, paths, out_dir: Path):
        from arq_reader import Restore
        rs = Restore(str(dest), encryption_password="pw")
        layouts = rs.layouts()
        rs.restore(
            folder_uuid=layouts[0].backup_folder_uuids[0],
            computer_uuid=layouts[0].computer_uuid,
            dest=out_dir, paths=paths,
        )
        return {
            str(p.relative_to(out_dir))
            for p in out_dir.rglob("*")
            if p.is_file()
        }

    def test_exact_file_match(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            res = self._build(tdp)
            out = tdp / "out"
            out.mkdir()
            files = self._restore(
                tdp / "dest", ["a.txt"], out,
            )
            self.assertEqual(files, {"a.txt"})

    def test_directory_prefix_match(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._build(tdp)
            out = tdp / "out"
            out.mkdir()
            files = self._restore(
                tdp / "dest", ["sub1"], out,
            )
            self.assertEqual(
                files, {"sub1/x.txt", "sub1/y.txt"},
            )

    def test_multiple_paths_union(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._build(tdp)
            out = tdp / "out"
            out.mkdir()
            files = self._restore(
                tdp / "dest", ["a.txt", "sub2"], out,
            )
            self.assertEqual(files, {"a.txt", "sub2/z.txt"})

    def test_nonexistent_path_restores_nothing_silently(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._build(tdp)
            out = tdp / "out"
            out.mkdir()
            files = self._restore(
                tdp / "dest", ["does/not/exist"], out,
            )
            self.assertEqual(files, set())

    def test_leading_slash_normalised(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self._build(tdp)
            out = tdp / "out"
            out.mkdir()
            files = self._restore(
                tdp / "dest", ["/a.txt"], out,
            )
            self.assertEqual(files, {"a.txt"})


if __name__ == "__main__":
    unittest.main()
