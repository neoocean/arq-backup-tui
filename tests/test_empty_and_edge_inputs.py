"""C4 — empty + single-blob edge cases.

Three input shapes that bypass the writer's common path:

- **Empty source directory** — no files, no children Tree.
  Restore should produce an empty target directory.
- **Single-file source** — one tiny file. Pack contains one
  ARQO. BlobLoc offsets align with start-of-pack.
- **Zero-byte file content** — file present but its content is
  empty bytes. ARQO header + LZ4 frame still produced; restore
  yields a zero-byte file.

Each shape's round-trip must succeed without special-casing in
the reader. Catches regressions where the walker emits a
"missing children" sentinel or restore drops the file.
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
class EmptyAndEdgeInputsTests(unittest.TestCase):

    def _round_trip(self, td: Path, populate_src):
        """Build → restore → return restored Path tree."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        src = td / "src"
        src.mkdir()
        populate_src(src)
        dest = td / "dest"
        build_backup(
            str(src), str(dest), encryption_password="pw",
        )
        out = td / "out"
        out.mkdir()
        rs = Restore(str(dest), encryption_password="pw")
        layouts = rs.layouts()
        rs.restore(
            folder_uuid=layouts[0].backup_folder_uuids[0],
            computer_uuid=layouts[0].computer_uuid, dest=out,
        )
        return out

    def test_empty_source_directory_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = self._round_trip(Path(td), lambda src: None)
            # Restored should be an empty directory (or
            # essentially empty — restore may leave .DS_Store-
            # equivalent artefacts; we just assert no real files).
            real_entries = [
                p for p in out.iterdir()
                if not p.name.startswith(".")
            ]
            self.assertEqual(
                real_entries, [],
                "empty source should restore empty target",
            )

    def test_single_file_source_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            def populate(src):
                (src / "only.txt").write_bytes(b"single content")
            out = self._round_trip(Path(td), populate)
            self.assertEqual(
                (out / "only.txt").read_bytes(), b"single content",
            )

    def test_zero_byte_file_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            def populate(src):
                (src / "empty.txt").write_bytes(b"")
            out = self._round_trip(Path(td), populate)
            self.assertEqual(
                (out / "empty.txt").read_bytes(), b"",
            )
            self.assertEqual(
                (out / "empty.txt").stat().st_size, 0,
            )

    def test_empty_subdir_in_otherwise_populated_source(
        self,
    ) -> None:
        """A populated source containing one empty subdirectory.
        The subdir must be re-created on restore even though it
        has no children."""
        with tempfile.TemporaryDirectory() as td:
            def populate(src):
                (src / "a.txt").write_bytes(b"content")
                (src / "emptydir").mkdir()
            out = self._round_trip(Path(td), populate)
            self.assertTrue((out / "a.txt").is_file())
            self.assertTrue((out / "emptydir").is_dir())
            self.assertEqual(
                list((out / "emptydir").iterdir()), [],
                "restored emptydir contains entries",
            )

    def test_single_blob_pack_round_trips_through_packed_mode(
        self,
    ) -> None:
        """One small file with use_packs=True → pack file has
        exactly one ARQO. Restore reads it back."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "one.bin").write_bytes(b"only blob content")
            dest = tdp / "dest"
            from arq_writer.backup import build_backup
            from arq_reader import Restore
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
                use_packs=True,
            )
            # Inspect the blobpacks/ directory for exactly one
            # pack file (after the writer's single .pack flush).
            bp_dir = (
                dest / res.computer_uuid / "blobpacks"
            )
            packs = list(bp_dir.rglob("*.pack"))
            self.assertEqual(
                len(packs), 1,
                f"single-file packed backup produced "
                f"{len(packs)} blobpacks; expected 1",
            )
            # Round-trip the content.
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            self.assertEqual(
                (out / "one.bin").read_bytes(),
                b"only blob content",
            )


if __name__ == "__main__":
    unittest.main()
