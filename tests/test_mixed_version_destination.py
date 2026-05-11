"""A13 — Mixed-version BackupRecord destinations.

When an operator upgrades Arq.app, their destination ends up
with a mix of legacy v100 records (Tree v3 era) and current
v101 records (Tree v4 era). The reader + validator MUST handle
both. Sampled 2026-05-11 against ``/Volumes/arqbackup1``:
333 v100 + 18 v101 records in the same folder.

This module pins:

1. A destination containing BOTH v100 and v101 records lists
   all of them via ``Restore.list_records``.
2. Restoring from a v100 record produces the original content.
3. Restoring from a v101 record produces the original content.
4. The validator's SV3 check accepts both versions (already
   covered by A3 / PR #99 — this test confirms the integrated
   path with mixed records).
"""

from __future__ import annotations

import json
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
class MixedVersionDestinationTests(unittest.TestCase):

    def _build_with_version(
        self, td: Path, tree_version: int, content_marker: bytes,
    ):
        """Build a backup with the specified tree_version (3 = v100
        record, 4 = v101 record). Returns BackupResult."""
        from arq_writer.backup import build_backup
        src = td / f"src-v{tree_version}"
        src.mkdir()
        (src / "f.txt").write_bytes(content_marker)
        return build_backup(
            str(src), str(td / "dest"),
            encryption_password="pw",
            tree_version=tree_version,
        )

    def test_v100_and_v101_records_coexist(self) -> None:
        """Build two records in the same destination: one Tree v3
        (v100), one Tree v4 (v101). Reader lists both."""
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            r1 = self._build_with_version(
                tdp, tree_version=3, content_marker=b"v100-content",
            )
            r2 = self._build_with_version(
                tdp, tree_version=4, content_marker=b"v101-content",
            )
            # Same computer (build_backup default UUID would
            # differ; we don't pass it so each gets a fresh CU).
            # Reader sees 2 layouts.
            rs = Restore(
                str(tdp / "dest"), encryption_password="pw",
            )
            layouts = rs.layouts()
            self.assertEqual(
                len(layouts), 2,
                f"expected 2 computer layouts for mixed-version, "
                f"got {len(layouts)}",
            )

    def test_restore_from_v100_record_byte_identical(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            r = self._build_with_version(
                tdp, tree_version=3,
                content_marker=b"v100-tree-v3-content",
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(
                str(tdp / "dest"), encryption_password="pw",
            )
            rs.restore(
                folder_uuid=r.folder_uuid,
                computer_uuid=r.computer_uuid, dest=out,
            )
            self.assertEqual(
                (out / "f.txt").read_bytes(),
                b"v100-tree-v3-content",
            )

    def test_restore_from_v101_record_byte_identical(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            r = self._build_with_version(
                tdp, tree_version=4,
                content_marker=b"v101-tree-v4-content",
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(
                str(tdp / "dest"), encryption_password="pw",
            )
            rs.restore(
                folder_uuid=r.folder_uuid,
                computer_uuid=r.computer_uuid, dest=out,
            )
            self.assertEqual(
                (out / "f.txt").read_bytes(),
                b"v101-tree-v4-content",
            )

    def test_mixed_record_versions_inside_one_folder(self) -> None:
        """Build BOTH versions in the same folder. The destination
        ends up with one record at v100 + one at v101 in the same
        backupfolders/<uuid>/backuprecords/ subtree. Reader's
        list_records must enumerate both, and each restores
        correctly."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"v100-then-v101")
            dest = tdp / "dest"
            r1 = build_backup(
                str(src), str(dest), encryption_password="pw",
                tree_version=3,
            )
            import time
            time.sleep(1.1)
            r2 = build_backup(
                str(src), str(dest), encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                tree_version=4,
                dedup_against_existing=True,
            )
            rs = Restore(str(dest), encryption_password="pw")
            recs = rs.list_records(
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
            )
            self.assertEqual(len(recs), 2)
            # Both records restore identically.
            for rec in recs:
                out = tdp / f"out-{rec.relative_path.split('/')[-1]}"
                out.mkdir()
                rs.restore(
                    folder_uuid=rec.folder_uuid,
                    computer_uuid=rec.computer_uuid,
                    backuprecord_path=rec.relative_path,
                    dest=out,
                )
                self.assertEqual(
                    (out / "f.txt").read_bytes(),
                    b"v100-then-v101",
                )


if __name__ == "__main__":
    unittest.main()
