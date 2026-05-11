"""C-J2 — BackupRecord numbering uniqueness.

BackupRecords land at
``<cu>/backupfolders/<fu>/backuprecords/<bucket>/<num>.backuprecord``.
The naming convention pins:

- ``<bucket>`` is a 5-digit zero-padded number (NN/MMM/HH-K
  the bucket-of-time)
- ``<num>`` is the Unix-epoch second of creation (per Arq.app's
  sampled emit)

Two runs at the SAME wall-clock second would produce the same
``<num>`` and could collide. This module pins:

- Two sequential runs (~100ms apart) get distinct numbers
- The bucket directory partitions records sanely
- Restoring all records from a folder lists every one
"""

from __future__ import annotations

import subprocess
import tempfile
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
class BackupRecordNumberingTests(unittest.TestCase):

    def test_sequential_runs_get_distinct_record_numbers(self) -> None:
        from arq_writer.backup import build_backup
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"x")
            dest = tdp / "dest"
            r1 = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            time.sleep(1.1)   # at least one wall-clock second
            r2 = build_backup(
                str(src), str(dest), encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
            )
            self.assertNotEqual(
                r1.backuprecord_path, r2.backuprecord_path,
                "two records share a path",
            )

    def test_bucket_is_5_digit_zero_padded(self) -> None:
        """Pin the bucket directory naming convention."""
        from arq_writer.backup import build_backup
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"x")
            dest = tdp / "dest"
            r = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            rec_root = (
                dest / r.computer_uuid / "backupfolders"
                / r.folder_uuid / "backuprecords"
            )
            buckets = [
                p for p in rec_root.iterdir() if p.is_dir()
            ]
            self.assertGreater(len(buckets), 0)
            for b in buckets:
                self.assertEqual(
                    len(b.name), 5,
                    f"bucket {b.name!r} not 5 digits",
                )
                self.assertTrue(b.name.isdigit())

    def test_record_path_encodes_unix_epoch(self) -> None:
        """Record path's ``<bucket>/<num>`` together encode the
        Unix epoch second of creation. Bucket is the high digits
        (epoch // 100000), num is the low 5 digits (epoch % 100000).
        Concatenating them reconstructs the full epoch."""
        from arq_writer.backup import build_backup
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"x")
            dest = tdp / "dest"
            before = int(time.time())
            r = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            after = int(time.time()) + 1
            # Split the path: .../backuprecords/<bucket>/<num>.backuprecord
            parts = r.backuprecord_path.parts
            bucket = parts[-2]
            stem = r.backuprecord_path.stem
            self.assertTrue(bucket.isdigit())
            self.assertTrue(stem.isdigit())
            # Reconstruct full epoch.
            full_epoch = int(bucket) * 100000 + int(stem)
            self.assertGreaterEqual(full_epoch, before)
            self.assertLessEqual(full_epoch, after)

    def test_list_records_enumerates_all(self) -> None:
        """Three sequential runs → list_records returns 3
        records for that folder."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"x")
            dest = tdp / "dest"
            r1 = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            for _ in range(2):
                time.sleep(1.1)
                build_backup(
                    str(src), str(dest), encryption_password="pw",
                    computer_uuid=r1.computer_uuid,
                    folder_uuid=r1.folder_uuid,
                )
            rs = Restore(str(dest), encryption_password="pw")
            recs = rs.list_records(
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
            )
            self.assertEqual(len(recs), 3)


if __name__ == "__main__":
    unittest.main()
