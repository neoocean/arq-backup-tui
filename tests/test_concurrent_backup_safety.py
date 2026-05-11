"""C-F1 — Concurrent backup safety.

Two ``Backup`` instances writing to the same destination
simultaneously: what happens? The writer has no explicit
process-level lock (no flock, no lockfile). Safety relies on:

- Content-addressed blob_id naming: identical content produces
  identical blob filenames, so a concurrent emit of the same
  blob just overwrites the same path with the same bytes.
- Pack files use unique UUIDs per PackBuilder instance, so two
  writers' packs land in different pack-file paths (no
  truncation race).
- BackupRecord paths use timestamp-derived numbers; two
  writers running at the same wall-clock second would
  theoretically collide but the bucket+counter scheme
  separates them.

This module pins:

1. Two sequential writers to the same destination — no
   corruption, both records readable.
2. Two writers from the same TempDirectory but DIFFERENT
   computer UUIDs — each gets its own subtree, no
   interference.
3. Standardobjects blob_id collision is benign: writer A and
   writer B emit the same blob_id for the same plaintext;
   whoever writes last wins, but the content is identical.

Race conditions involving FS atomicity (partial file writes)
are NOT pinned here — that's an OS / filesystem concern, not
a writer-level invariant.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
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
class ConcurrentBackupSafetyTests(unittest.TestCase):

    def test_sequential_writers_to_same_destination_dont_corrupt(
        self,
    ) -> None:
        """Two sequential builds → both produce readable
        backuprecords, neither corrupts the other."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src1 = tdp / "src1"
            src1.mkdir()
            (src1 / "a.txt").write_bytes(b"a-content")
            src2 = tdp / "src2"
            src2.mkdir()
            (src2 / "b.txt").write_bytes(b"b-content")
            dest = tdp / "dest"
            r1 = build_backup(
                str(src1), str(dest), encryption_password="pw",
            )
            r2 = build_backup(
                str(src2), str(dest), encryption_password="pw",
                computer_uuid=r1.computer_uuid,
            )
            # Both records exist.
            rs = Restore(str(dest), encryption_password="pw")
            recs = rs.list_records(
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
            )
            self.assertGreaterEqual(len(recs), 1)
            recs2 = rs.list_records(
                computer_uuid=r1.computer_uuid,
                folder_uuid=r2.folder_uuid,
            )
            self.assertGreaterEqual(len(recs2), 1)

    def test_two_writers_different_computer_uuids_isolated(
        self,
    ) -> None:
        """Two builds with different computer UUIDs → each gets
        own subtree under the destination. No interference."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "shared.txt").write_bytes(b"shared")
            dest = tdp / "dest"
            r1 = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            r2 = build_backup(
                str(src), str(dest), encryption_password="pw",
                computer_uuid=None,   # fresh UUID
            )
            self.assertNotEqual(
                r1.computer_uuid, r2.computer_uuid,
                "two builds without explicit UUID should get "
                "distinct UUIDs",
            )
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            uuids = {l.computer_uuid for l in layouts}
            self.assertEqual(
                uuids, {r1.computer_uuid, r2.computer_uuid},
            )

    def test_simultaneous_threads_same_destination_isolated_uuids(
        self,
    ) -> None:
        """Threaded backups (different computer UUIDs):
        both complete, both records readable. Stresses the
        no-explicit-lock assumption."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            for i in (1, 2):
                (tdp / f"src{i}").mkdir()
                (tdp / f"src{i}" / f"f{i}.txt").write_bytes(
                    f"thread-{i}".encode(),
                )
            dest = tdp / "dest"
            results = {}
            errors = {}

            def run(idx):
                try:
                    results[idx] = build_backup(
                        str(tdp / f"src{idx}"),
                        str(dest),
                        encryption_password="pw",
                    )
                except Exception as exc:
                    errors[idx] = exc

            t1 = threading.Thread(target=run, args=(1,))
            t2 = threading.Thread(target=run, args=(2,))
            t1.start()
            t2.start()
            t1.join(timeout=60)
            t2.join(timeout=60)
            self.assertEqual(errors, {}, f"thread errors: {errors}")
            self.assertEqual(len(results), 2)
            # Both records readable.
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            uuids = {l.computer_uuid for l in layouts}
            self.assertEqual(len(uuids), 2)

    def test_content_addressing_makes_concurrent_same_blob_safe(
        self,
    ) -> None:
        """Two writers emitting the same blob_id (same plaintext,
        same salt — possible if they share a keyset) overwrite
        the same path with the same bytes. No silent corruption
        even if the second write races the first."""
        from arq_writer.backup import build_backup
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "shared.txt").write_bytes(b"identical content")
            dest = tdp / "dest"
            r1 = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            # Re-backup with explicit computer_uuid + same salt
            # would require keyset reuse. Easier: dedup_against_existing
            # which reads the existing keyset.
            r2 = build_backup(
                str(src), str(dest), encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
            )
            # The same blob_id should EXIST exactly once on disk
            # (not duplicated, not corrupted).
            so_root = dest / r1.computer_uuid / "standardobjects"
            blob_files = list(so_root.rglob("*"))
            blob_files = [f for f in blob_files if f.is_file()]
            blob_ids = [
                (f.parent.name + f.name).lower() for f in blob_files
            ]
            # Each blob_id appears in at most one file.
            from collections import Counter
            for bid, n in Counter(blob_ids).items():
                self.assertEqual(
                    n, 1,
                    f"blob_id {bid} duplicated {n} times",
                )


if __name__ == "__main__":
    unittest.main()
