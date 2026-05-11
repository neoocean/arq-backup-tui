"""C-M1 — macOS default exclusion list.

Pins the default exclusion patterns Arq.app v8 applies +
verifies the writer's exclusion machinery accepts them.
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


class MacOSDefaultExcludesTests(unittest.TestCase):
    """Pure pattern set + ExclusionRules.excludes() behaviour."""

    def test_user_excludes_catch_library_caches(self) -> None:
        from arq_writer.default_excludes import macos_default_excludes
        rules = macos_default_excludes(include_system=False)
        self.assertTrue(
            rules.excludes(
                "Library/Caches/com.apple.Safari/file",
                is_dir=False,
            ),
        )
        self.assertTrue(
            rules.excludes(
                "Library/Caches",
                is_dir=True,
            ),
        )

    def test_user_excludes_catch_trash(self) -> None:
        from arq_writer.default_excludes import macos_default_excludes
        rules = macos_default_excludes(include_system=False)
        self.assertTrue(rules.excludes(".Trash", is_dir=True))
        self.assertTrue(
            rules.excludes(".Trash/deleted.txt", is_dir=False),
        )

    def test_user_excludes_dont_catch_arbitrary_files(self) -> None:
        """Don't over-match — operator's docs / photos / etc. must
        pass through."""
        from arq_writer.default_excludes import macos_default_excludes
        rules = macos_default_excludes(include_system=False)
        self.assertFalse(
            rules.excludes("Documents/notes.txt", is_dir=False),
        )
        self.assertFalse(
            rules.excludes("Pictures/photo.jpg", is_dir=False),
        )
        self.assertFalse(rules.excludes("Library", is_dir=True))
        # ``Library/Application Support`` is fine — only the
        # specific MobileSync subdir is excluded.
        self.assertFalse(
            rules.excludes(
                "Library/Application Support/MyApp",
                is_dir=True,
            ),
        )

    def test_system_excludes_catch_private_var_folders(self) -> None:
        from arq_writer.default_excludes import macos_default_excludes
        rules = macos_default_excludes(include_user=False)
        self.assertTrue(
            rules.excludes(
                "private/var/folders/abc/T/tmp",
                is_dir=False,
            ),
        )

    def test_extra_wildcards_combine(self) -> None:
        from arq_writer.default_excludes import macos_default_excludes
        rules = macos_default_excludes(
            extra_wildcards=("node_modules", "node_modules/**/*"),
        )
        self.assertTrue(rules.excludes("node_modules", is_dir=True))
        self.assertTrue(
            rules.excludes(
                "node_modules/lodash/index.js", is_dir=False,
            ),
        )
        # Default-set still active.
        self.assertTrue(
            rules.excludes(".Trash", is_dir=True),
        )


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class MacOSDefaultExcludesEndToEndTests(unittest.TestCase):

    def test_backup_with_default_excludes_skips_caches(self) -> None:
        """End-to-end: a source tree containing a Library/Caches
        subdir backed up with macos_default_excludes() should
        NOT include the cache content."""
        from arq_writer.backup import build_backup
        from arq_writer.default_excludes import macos_default_excludes
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "Documents").mkdir()
            (src / "Documents" / "note.txt").write_bytes(b"keep me")
            (src / "Library" / "Caches" / "Safari").mkdir(
                parents=True,
            )
            (src / "Library" / "Caches" / "Safari" / "blob.dat").write_bytes(
                b"throwaway",
            )
            dest = tdp / "dest"
            build_backup(
                str(src), str(dest), encryption_password="pw",
                exclusions=macos_default_excludes(
                    include_system=False,
                ),
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            # Documents survived.
            self.assertEqual(
                (out / "Documents" / "note.txt").read_bytes(),
                b"keep me",
            )
            # Caches excluded.
            self.assertFalse(
                (out / "Library" / "Caches" / "Safari" / "blob.dat").exists(),
                "Library/Caches content should be excluded",
            )


if __name__ == "__main__":
    unittest.main()
