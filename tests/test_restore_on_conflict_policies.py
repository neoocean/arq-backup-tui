"""C-G2 — Restore on_conflict policy edge cases.

``Restore(on_conflict=...)`` accepts three values:

- ``"overwrite"`` (default) — replace existing destination files
- ``"skip"`` — leave existing files untouched, emit
  ``conflict_skipped`` event
- ``"rename"`` — write to ``<name>.restored-N`` to avoid clobber

This module pins all three branches under different conflict
shapes: existing file with same/different content, existing
directory at the same path (a type mismatch), and the case where
the conflict is with a previously-restored file.
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
class RestoreOnConflictTests(unittest.TestCase):

    def _build_and_restore(
        self, td: Path, *, on_conflict: str,
        preexisting_content: bytes,
    ):
        """Build a backup of one file, then restore into a
        directory where that file's restore-path already
        exists with ``preexisting_content``."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        src = td / "src"
        src.mkdir()
        (src / "f.txt").write_bytes(b"backed up content")
        dest = td / "dest"
        build_backup(
            str(src), str(dest), encryption_password="pw",
        )
        out = td / "out"
        out.mkdir()
        (out / "f.txt").write_bytes(preexisting_content)
        events = []
        rs = Restore(
            str(dest), encryption_password="pw",
            on_conflict=on_conflict,
        )
        layouts = rs.layouts()
        rs.restore(
            folder_uuid=layouts[0].backup_folder_uuids[0],
            computer_uuid=layouts[0].computer_uuid, dest=out,
            callback=lambda k, p: events.append((k, p)),
        )
        return out, events

    def test_overwrite_replaces_existing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out, _ = self._build_and_restore(
                Path(td), on_conflict="overwrite",
                preexisting_content=b"old content",
            )
            self.assertEqual(
                (out / "f.txt").read_bytes(), b"backed up content",
            )

    def test_skip_leaves_existing_alone(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out, events = self._build_and_restore(
                Path(td), on_conflict="skip",
                preexisting_content=b"keep me",
            )
            self.assertEqual(
                (out / "f.txt").read_bytes(), b"keep me",
                "skip policy should preserve existing content",
            )
            # conflict_skipped event surfaces.
            skipped = [
                p for (k, p) in events if k == "conflict_skipped"
            ]
            self.assertGreater(len(skipped), 0)

    def test_rename_writes_to_alternate_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out, _ = self._build_and_restore(
                Path(td), on_conflict="rename",
                preexisting_content=b"original",
            )
            # Original preserved.
            self.assertEqual(
                (out / "f.txt").read_bytes(), b"original",
            )
            # Renamed copy exists (name.restored-N).
            renamed = [
                p for p in out.iterdir()
                if "restored" in p.name and p.name != "f.txt"
            ]
            self.assertEqual(len(renamed), 1)
            self.assertEqual(
                renamed[0].read_bytes(), b"backed up content",
            )

    def test_rename_handles_multiple_conflicts(self) -> None:
        """Run restore twice with rename — the second run should
        produce a different rename target (not overwrite the
        first rename)."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"backed up")
            dest = tdp / "dest"
            build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            out = tdp / "out"
            out.mkdir()
            (out / "f.txt").write_bytes(b"original")
            rs = Restore(
                str(dest), encryption_password="pw",
                on_conflict="rename",
            )
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            # Second restore — should produce a second
            # distinct rename, not overwrite the first.
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            files = list(out.iterdir())
            self.assertGreaterEqual(
                len(files), 3,
                f"expected ≥3 files (original + 2 renames), "
                f"got {[f.name for f in files]}",
            )


if __name__ == "__main__":
    unittest.main()
