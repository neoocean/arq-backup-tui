"""C-F3 — backup → restore → backup idempotency.

A backup → restore cycle should produce a restored tree that's
byte-identical to the original (within the limits documented in
COVERAGE.md: content yes, sparseness/clones not preserved).
Re-backing-up that restored tree to the SAME destination with
``dedup_against_existing=True`` should produce no new data blobs
— content addressing means identical plaintext + identical
keyset → identical blob_ids.

This module pins:

- Backup → restore → backup with identical source bytes
  produces zero new data blobs in standardobjects (modulo a new
  BackupRecord per run + new tree blobs if Tree v4 trailing
  drifts, which K2/K3 documented)
- The restored tree has the same SHA-256 manifest as the
  original source
"""

from __future__ import annotations

import hashlib
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


def _hash_tree(root: Path) -> dict:
    """Return {relative_path: sha256_hex} for every regular file."""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            with p.open("rb") as f:
                h = hashlib.sha256(f.read()).hexdigest()
            out[str(p.relative_to(root))] = h
    return out


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class BackupRestoreBackupIdempotencyTests(unittest.TestCase):

    def test_round_trip_content_sha256_matches(self) -> None:
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"alpha")
            (src / "b.txt").write_bytes(b"bravo")
            (src / "subdir").mkdir()
            (src / "subdir" / "c.bin").write_bytes(b"charlie\n" * 100)
            dest = tdp / "dest"
            r1 = build_backup(
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
            src_hashes = _hash_tree(src)
            out_hashes = _hash_tree(out)
            self.assertEqual(src_hashes, out_hashes)

    def test_idempotent_rebackup_of_restored_tree(self) -> None:
        """Re-backup the restored tree → no new data blobs added.
        (BackupRecord-level blobs MAY increment by 1 per run; data
        blobs stay constant because content didn't change.)"""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "data.bin").write_bytes(b"X" * 10_000)
            dest = tdp / "dest"
            r1 = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            so_root = dest / r1.computer_uuid / "standardobjects"
            phase1_blob_count = sum(
                1 for _ in so_root.rglob("*") if _.is_file()
            )
            # Restore.
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            # Re-back-up the restored tree to the SAME destination.
            r2 = build_backup(
                str(out), str(dest), encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
            )
            phase2_blob_count = sum(
                1 for _ in so_root.rglob("*") if _.is_file()
            )
            # Data blobs (file content) should not multiply. New
            # blob_ids in r2 reflect the new BackupRecord +
            # potentially new tree blob (K2/K3: tree-walk reuse
            # carries through, so even tree blob may dedup).
            self.assertLessEqual(
                phase2_blob_count - phase1_blob_count, 3,
                f"re-backup added {phase2_blob_count - phase1_blob_count} "
                f"blobs; should be ≤ 3 (BackupRecord + maybe tree)",
            )


if __name__ == "__main__":
    unittest.main()
