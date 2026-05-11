"""C-L1 — Empty / missing destination Restore behaviour.

When the operator instantiates ``Restore`` against an empty
directory or a path that doesn't exist, the reader should:

- Empty directory → ``layouts()`` returns empty list (no
  computer-UUID subtrees found); no crash
- Missing directory → ``Restore.__init__`` either raises a
  clean OSError-style exception OR ``layouts()`` raises;
  no AttributeError-style internal crash
- A path that's a FILE (not a directory) → clean error

These pin the operator-facing error surface so a wrong-path
typo doesn't produce a confusing traceback.
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
class EmptyDestinationRestoreTests(unittest.TestCase):

    def test_empty_dir_layouts_returns_empty_list(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            empty = tdp / "empty"
            empty.mkdir()
            rs = Restore(str(empty), encryption_password="pw")
            try:
                layouts = rs.layouts()
            except Exception as exc:
                err = type(exc).__name__
                self.assertNotIn(
                    err, ("AttributeError", "TypeError"),
                    f"empty dir produced internal crash: {exc}",
                )
                return
            self.assertEqual(
                layouts, [],
                "empty directory should yield no layouts",
            )

    def test_missing_directory_clean_error(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            missing = tdp / "does_not_exist"
            try:
                rs = Restore(
                    str(missing), encryption_password="pw",
                )
                rs.layouts()
            except Exception as exc:
                err = type(exc).__name__
                self.assertNotIn(
                    err, ("AttributeError", "TypeError"),
                    f"missing dir produced internal crash: {exc}",
                )

    def test_file_as_destination_clean_error(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            file_path = tdp / "not_a_dir"
            file_path.write_bytes(b"oops")
            try:
                rs = Restore(
                    str(file_path), encryption_password="pw",
                )
                rs.layouts()
            except Exception as exc:
                err = type(exc).__name__
                self.assertNotIn(
                    err, ("AttributeError", "TypeError"),
                    f"file-as-dir produced internal crash: {exc}",
                )

    def test_dir_with_garbage_files_yields_no_layouts(self) -> None:
        """A directory containing non-UUID files/dirs (random
        clutter) — layouts() ignores them."""
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            garbage_dir = tdp / "garbage"
            garbage_dir.mkdir()
            (garbage_dir / "not-a-uuid").write_bytes(b"clutter")
            (garbage_dir / "also-not").mkdir()
            (garbage_dir / "README.txt").write_bytes(b"readme")
            rs = Restore(
                str(garbage_dir), encryption_password="pw",
            )
            layouts = rs.layouts()
            self.assertEqual(
                layouts, [],
                "garbage-only directory should yield no layouts",
            )


if __name__ == "__main__":
    unittest.main()
