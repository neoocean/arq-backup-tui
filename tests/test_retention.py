"""Retention policy + pruning + blob GC tests."""

from __future__ import annotations

import datetime
import sys
import time
import tempfile
import unittest
from pathlib import Path
from typing import List

from arq_validator import LocalBackend
from arq_writer import (
    Backup,
    RetentionPolicy,
    apply_retention,
    build_backup,
    gc_orphan_blobs,
    prune_records,
)
from arq_writer.retention import _RecordRef, select_retained


# ---------------------------------------------------------------------------
# select_retained — pure-logic policy tests (no I/O)
# ---------------------------------------------------------------------------


def _refs(*epochs: int) -> List[_RecordRef]:
    return [
        _RecordRef(path=f"/r/{i}", creation_date=cd)
        for i, cd in enumerate(epochs)
    ]


class SelectRetainedPolicyTests(unittest.TestCase):
    def test_empty_records(self) -> None:
        self.assertEqual(
            select_retained([], RetentionPolicy(keep_last_n=5)),
            set(),
        )

    def test_keep_all_default(self) -> None:
        records = _refs(100, 200, 300)
        keep = select_retained(records, RetentionPolicy())
        self.assertEqual(keep, {r.path for r in records})

    def test_keep_last_n(self) -> None:
        # 5 records, keep_last_n=2 → newest 2 only.
        records = _refs(100, 200, 300, 400, 500)
        keep = select_retained(
            records, RetentionPolicy(keep_last_n=2),
        )
        # Newest 2 are creation_date 500, 400 → indices 4, 3 → /r/4, /r/3.
        self.assertEqual(keep, {"/r/3", "/r/4"})

    def test_keep_daily(self) -> None:
        # 4 records on 4 different days; keep_daily=2 → newest 2 days.
        day1 = int(datetime.datetime(2026, 5, 1, 12, 0,
                                     tzinfo=datetime.timezone.utc).timestamp())
        day2 = int(datetime.datetime(2026, 5, 2, 12, 0,
                                     tzinfo=datetime.timezone.utc).timestamp())
        day3 = int(datetime.datetime(2026, 5, 3, 12, 0,
                                     tzinfo=datetime.timezone.utc).timestamp())
        day4 = int(datetime.datetime(2026, 5, 4, 12, 0,
                                     tzinfo=datetime.timezone.utc).timestamp())
        records = _refs(day1, day2, day3, day4)
        keep = select_retained(
            records, RetentionPolicy(keep_daily=2),
        )
        # Two newest days = day4 (/r/3) + day3 (/r/2)
        self.assertEqual(keep, {"/r/2", "/r/3"})

    def test_buckets_or_together(self) -> None:
        # keep_last_n=1 + keep_daily=2 → union, not intersection.
        day1 = int(datetime.datetime(2026, 5, 1, 12, 0,
                                     tzinfo=datetime.timezone.utc).timestamp())
        day2 = int(datetime.datetime(2026, 5, 2, 12, 0,
                                     tzinfo=datetime.timezone.utc).timestamp())
        day3 = int(datetime.datetime(2026, 5, 3, 12, 0,
                                     tzinfo=datetime.timezone.utc).timestamp())
        records = _refs(day1, day2, day3)
        keep = select_retained(
            records, RetentionPolicy(keep_last_n=1, keep_daily=2),
        )
        # last_n=1 keeps day3; daily=2 keeps day3 + day2 → union
        self.assertEqual(keep, {"/r/1", "/r/2"})

    def test_hourly_within_same_day(self) -> None:
        # Three records same day, different hours; keep_hourly=2.
        h1 = int(datetime.datetime(2026, 5, 1, 10, 0,
                                   tzinfo=datetime.timezone.utc).timestamp())
        h2 = int(datetime.datetime(2026, 5, 1, 11, 0,
                                   tzinfo=datetime.timezone.utc).timestamp())
        h3 = int(datetime.datetime(2026, 5, 1, 12, 0,
                                   tzinfo=datetime.timezone.utc).timestamp())
        records = _refs(h1, h2, h3)
        keep = select_retained(
            records, RetentionPolicy(keep_hourly=2),
        )
        # Newest 2 hours = h3 (/r/2), h2 (/r/1)
        self.assertEqual(keep, {"/r/1", "/r/2"})


# ---------------------------------------------------------------------------
# Integration: real backups → prune → GC → restore still works
# ---------------------------------------------------------------------------


def _make_tree(root: Path) -> None:
    (root / "a.txt").write_bytes(b"alpha\n")
    (root / "b.txt").write_bytes(b"beta\n")


class PruneAndRestoreTests(unittest.TestCase):
    def test_prune_records_only_keeps_specified(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            time.sleep(1.1)
            build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
            )
            time.sleep(1.1)
            build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
            )
            backend = LocalBackend(dest)
            res = prune_records(
                backend,
                encryption_password="pw",
                policy=RetentionPolicy(keep_last_n=1),
                computer_uuid=r1.computer_uuid,
            )
            self.assertEqual(len(res.deleted), 2)
            self.assertEqual(len(res.retained), 1)
            # The deleted files are gone from disk.
            for p in res.deleted:
                self.assertFalse(
                    (dest / p.lstrip("/")).exists(),
                )

    def test_dry_run_doesnt_delete(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            time.sleep(1.1)
            build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
            )
            backend = LocalBackend(dest)
            res = prune_records(
                backend,
                encryption_password="pw",
                policy=RetentionPolicy(keep_last_n=1),
                computer_uuid=r1.computer_uuid,
                dry_run=True,
            )
            self.assertEqual(len(res.deleted), 1)
            # Files still exist on disk after dry-run.
            for p in res.deleted:
                self.assertTrue(
                    (dest / p.lstrip("/")).exists(),
                )

    def test_gc_orphan_blobs_after_prune_with_changing_content(self) -> None:
        # Run two backups with different content on each → blobs from
        # the first run become orphan after pruning record 1.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"alpha v1\n")
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            time.sleep(1.1)
            # Change content → new blobs
            (src / "a.txt").write_bytes(b"alpha v2 changed\n")
            build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
            )
            backend = LocalBackend(dest)
            # Count blobs before
            so_root = dest / r1.computer_uuid / "standardobjects"
            before = sum(
                1 for shard in so_root.iterdir()
                if shard.is_dir()
                for _ in shard.iterdir()
            )
            res = apply_retention(
                backend,
                encryption_password="pw",
                policy=RetentionPolicy(keep_last_n=1),
                computer_uuid=r1.computer_uuid,
            )
            # 1 record pruned
            self.assertEqual(len(res.prune.deleted), 1)
            # GC should have deleted the blobs unique to record 1
            # (the content of a.txt v1 + the v1 tree blob)
            self.assertGreater(res.gc.standalone_blobs_deleted, 0)
            after = sum(
                1 for shard in so_root.iterdir()
                if shard.is_dir()
                for _ in shard.iterdir()
            )
            self.assertEqual(
                after,
                before - res.gc.standalone_blobs_deleted,
            )

    def test_restore_still_works_after_prune_and_gc(self) -> None:
        from arq_reader import Restore

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"v1\n")
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            time.sleep(1.1)
            (src / "a.txt").write_bytes(b"v2 latest\n")
            r2 = build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
            )
            backend = LocalBackend(dest)
            apply_retention(
                backend,
                encryption_password="pw",
                policy=RetentionPolicy(keep_last_n=1),
                computer_uuid=r1.computer_uuid,
            )
            # Restore the surviving (latest) backup → must yield v2
            target = tdp / "out"
            target.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=r2.folder_uuid,
                computer_uuid=r2.computer_uuid,
                dest=target,
            )
            self.assertEqual(
                (target / "a.txt").read_bytes(),
                b"v2 latest\n",
            )

    def test_keep_all_default_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            time.sleep(1.1)
            build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
            )
            backend = LocalBackend(dest)
            res = prune_records(
                backend,
                encryption_password="pw",
                policy=RetentionPolicy(),
                computer_uuid=r1.computer_uuid,
            )
            self.assertEqual(res.deleted, [])
            self.assertEqual(len(res.retained), 2)


class PackedModeRetentionTests(unittest.TestCase):
    @unittest.skipIf(
        sys.platform == "darwin",
        # macOS Sequoia ≥ auto-attaches ``com.apple.provenance`` to
        # every kernel-observed write with the SAME value across all
        # files on the machine. Two consequences for this test:
        #
        # 1. The xattr blob (one shared blob_id) lives in run-1's
        #    pack. Run 2 dedups against it (correct), so v2's
        #    record still references run-1's pack via the xattr.
        # 2. ``xattr -c`` / ``xattr -d com.apple.provenance`` are
        #    no-ops on Sequoia — the attribute is kernel-protected.
        #
        # ⇒ Pack-1 cannot be GC'd while v2's record exists, so
        #    the "GC deletes ≥1 pack" assertion can't hold on
        #    macOS. Linux CI (Python 3.9 + 3.11 + 3.12) covers the
        #    intended path. Listed under HANDOFF.md "Known
        #    landmines"; tracked as L2.
        "com.apple.provenance shared across files prevents per-run "
        "pack GC on macOS Sequoia; covered by Linux CI matrix.",
    )
    def test_pack_files_collected_correctly(self) -> None:
        # packed mode: pack files survive only if some referenced
        # BlobLoc points into them.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"v1\n")
            dest = tdp / "dest"
            r1 = build_backup(
                src, dest, encryption_password="pw", use_packs=True,
            )
            time.sleep(1.1)
            (src / "a.txt").write_bytes(b"v2 changed\n")
            build_backup(
                src, dest, encryption_password="pw", use_packs=True,
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
            )
            backend = LocalBackend(dest)
            blobpacks_dir = (
                dest / r1.computer_uuid / "blobpacks"
            )
            packs_before = list(blobpacks_dir.rglob("*.pack"))
            res = apply_retention(
                backend,
                encryption_password="pw",
                policy=RetentionPolicy(keep_last_n=1),
                computer_uuid=r1.computer_uuid,
            )
            packs_after = list(blobpacks_dir.rglob("*.pack"))
            # GC should have deleted at least one pack (the one
            # holding v1 of a.txt) since no surviving record
            # references it.
            self.assertLess(len(packs_after), len(packs_before))


class CallbackEventsTests(unittest.TestCase):
    def test_callback_emits_record_deleted_and_blob_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"v1\n")
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            time.sleep(1.1)
            (src / "a.txt").write_bytes(b"v2\n")
            build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
            )
            events = []

            def cb(kind, payload):
                events.append((kind, payload))

            apply_retention(
                LocalBackend(dest),
                encryption_password="pw",
                policy=RetentionPolicy(keep_last_n=1),
                computer_uuid=r1.computer_uuid,
                callback=cb,
            )
            kinds = [k for k, _ in events]
            self.assertIn("record_deleted", kinds)
            self.assertIn("blob_deleted", kinds)


if __name__ == "__main__":
    unittest.main()
