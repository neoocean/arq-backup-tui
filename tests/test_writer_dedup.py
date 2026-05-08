"""Cross-run deduplication tests for the writer.

Three behaviors to lock in:

1. Without ``dedup_against_existing``, a second ``build_backup``
   against the same destination treats it like a fresh run: blob_id
   salt + keys are regenerated, every blob is "new" from the
   writer's perspective, and the keyset gets overwritten.
2. With ``dedup_against_existing=True``, the second run reuses the
   existing keyset (so blob_ids match), and the writer skips
   re-encrypting + re-writing every standalone object whose content
   hasn't changed.
3. Restore from the second-run backuprecord must still round-trip
   the source tree byte-for-byte — dedup must not break correctness.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from arq_reader import Restore
from arq_writer import build_backup
from arq_writer.dedup import (
    find_latest_backuprecord,
    seed_from_backuprecord,
    seed_from_standardobjects,
)


def _write_tree(root: Path) -> None:
    """Materialize a small fixed source tree under ``root``."""
    (root / "subdir").mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"hello dedup\n" * 64)
    (root / "beta.txt").write_bytes(b"different content\n")
    (root / "subdir" / "gamma.txt").write_bytes(b"deep file\n" * 32)


class SeedFromStandardobjectsTests(unittest.TestCase):
    def test_seed_picks_up_every_blob(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _write_tree(src)
            dest = tdp / "dest"
            res = build_backup(
                src, dest, encryption_password="pw",
            )
            cache = {}
            added = seed_from_standardobjects(
                dest, res.computer_uuid, cache,
            )
            # Every blob the run wrote must be discoverable by scan.
            self.assertEqual(set(cache.keys()), set(res.blob_ids))
            self.assertEqual(added, len(res.blob_ids))

    def test_seed_no_duplicates_when_re_run(self) -> None:
        # Calling seed twice doesn't double-count.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _write_tree(src)
            dest = tdp / "dest"
            res = build_backup(
                src, dest, encryption_password="pw",
            )
            cache = {}
            added1 = seed_from_standardobjects(
                dest, res.computer_uuid, cache,
            )
            added2 = seed_from_standardobjects(
                dest, res.computer_uuid, cache,
            )
            self.assertEqual(added2, 0)
            self.assertEqual(added1, len(cache))

    def test_seed_on_empty_destination(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = {}
            added = seed_from_standardobjects(
                Path(td), "FAKE-UUID", cache,
            )
            self.assertEqual(added, 0)
            self.assertEqual(cache, {})


class SeedFromBackuprecordTests(unittest.TestCase):
    def test_walks_top_level_node_bloblocs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _write_tree(src)
            dest = tdp / "dest"
            # Run a packed backup so the BlobLocs encode (relativePath,
            # offset, length) tuples that wouldn't survive a plain
            # standardobjects scan.
            res = build_backup(
                src, dest, encryption_password="pw", use_packs=True,
            )
            rec_path = find_latest_backuprecord(dest, res.computer_uuid)
            self.assertIsNotNone(rec_path)
            # Need the keys to decrypt the record. Cheapest path is
            # to load them from the destination's keyset.
            from arq_validator.crypto import decrypt_keyset
            keyset_blob = (
                dest / res.computer_uuid / "encryptedkeyset.dat"
            ).read_bytes()
            ks = decrypt_keyset(keyset_blob, "pw")
            cache = {}
            added = seed_from_backuprecord(
                rec_path, cache,
                encryption_key=ks.encryption_key,
                hmac_key=ks.hmac_key,
            )
            self.assertGreater(added, 0)
            # Each entry must look like a valid BlobLoc.
            for bid, loc in cache.items():
                self.assertEqual(loc.blobIdentifier, bid)
                self.assertGreater(loc.length, 0)


class DedupAgainstExistingTests(unittest.TestCase):
    def test_second_run_reuses_keyset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _write_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(
                src, dest, encryption_password="pw",
            )
            # Capture the first run's keyset bytes — they must NOT
            # change on the second run when dedup is enabled.
            keyset_path = (
                dest / r1.computer_uuid / "encryptedkeyset.dat"
            )
            blob1 = keyset_path.read_bytes()
            r2 = build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                dedup_against_existing=True,
            )
            blob2 = keyset_path.read_bytes()
            self.assertEqual(blob1, blob2)
            # Run 2's freshly-written blob_ids list (cache misses
            # only) must be a STRICT subset of run 1's: every blob
            # that was already on disk should have hit the cache.
            for bid in r2.blob_ids:
                self.assertIn(bid, set(r1.blob_ids))
            self.assertLess(len(r2.blob_ids), len(r1.blob_ids))

    def test_second_run_skips_rewrites(self) -> None:
        # Capture standardobjects file mtimes after run 1, run a
        # second backup with dedup on, and confirm none of those
        # files were modified (writer skipped them).
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _write_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(
                src, dest, encryption_password="pw",
            )
            # Snapshot mtimes.
            so_root = dest / r1.computer_uuid / "standardobjects"
            mtimes_before = {}
            for shard in os.scandir(so_root):
                for f in os.scandir(shard.path):
                    mtimes_before[f.path] = f.stat().st_mtime_ns
            # Wait a tick so any rewrite would shift mtime.
            os.utime(
                next(iter(mtimes_before.keys())),
                ns=(0, 0),
            )
            r2 = build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                dedup_against_existing=True,
            )
            # Re-snapshot. All standardobjects should be untouched
            # by run 2 (run 2 should have hit the dedup cache for
            # every blob).
            mtimes_after = {}
            for shard in os.scandir(so_root):
                for f in os.scandir(shard.path):
                    mtimes_after[f.path] = f.stat().st_mtime_ns
            # Same set of files.
            self.assertEqual(
                set(mtimes_before.keys()), set(mtimes_after.keys()),
            )
            # Specifically: the file we forced to (0,0) must still
            # be (0,0) — no rewrite happened.
            self.assertEqual(
                mtimes_after[next(iter(mtimes_before.keys()))],
                0,
            )

    def test_second_run_restore_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _write_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(
                src, dest, encryption_password="pw",
            )
            r2 = build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                dedup_against_existing=True,
            )
            # Restore using the destination's most recent
            # backuprecord (= run 2's). Output must match the source.
            restore_target = tdp / "restored"
            restore_target.mkdir()
            rs = Restore(dest, encryption_password="pw")
            rs.restore(
                folder_uuid=r2.folder_uuid,
                dest=restore_target,
                computer_uuid=r2.computer_uuid,
            )
            for rel in ("alpha.txt", "beta.txt", "subdir/gamma.txt"):
                self.assertEqual(
                    (src / rel).read_bytes(),
                    (restore_target / rel).read_bytes(),
                    f"{rel} round-trip mismatch",
                )

    def test_dedup_off_regenerates_keyset(self) -> None:
        # Sanity: without the flag, the keyset IS rewritten (so the
        # baseline behavior of older callers is unchanged).
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _write_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(
                src, dest, encryption_password="pw",
            )
            keyset_path = (
                dest / r1.computer_uuid / "encryptedkeyset.dat"
            )
            blob1 = keyset_path.read_bytes()
            build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                # dedup_against_existing=False (default)
            )
            blob2 = keyset_path.read_bytes()
            # Random IV/salt → bytes must differ even though plaintext
            # is the same plan.
            self.assertNotEqual(blob1, blob2)


if __name__ == "__main__":
    unittest.main()
