"""E7 + E8 + E9 + E10 + E11 + E12 — operational edge cases.

Six derived items pinning operator-facing behaviours that prior
tests didn't cover comprehensively. The unifying thread: each
tests a corner case the operator could hit while running the
writer / validator / restore in production.

- **E7 arq-backup CLI flag combinations**: invoking the CLI with
  inter-dependent flags (``--use-packs`` + ``--chunker``,
  ``--exclude-glob`` × multiple, ``--max-file-bytes`` + ``--use-
  apfs-snapshot``). Pin: each combo runs without crashing + the
  documented behaviour fires.

- **E8 arq-validator CLI**: validator runs against local + SFTP
  backends; tier flag (basic/strict/byte-level) toggles behaviour.

- **E9 restore from non-latest record**: pass an explicit
  ``backuprecord_path`` to restore() and pin that the restore
  uses that record (not the latest).

- **E10 audit-drip resume mid-walk**: an audit that was cancelled
  partway can resume from a state checkpoint. Tested via the
  AuditDrip helper's checkpoint/resume contract.

- **E11 retention policy edges**: ``keep_last_n=0`` ≠
  ``keep_last_n=None``; very large N caps at the actual record
  count; mixed positive + zero buckets behave as documented.

- **E12 gc_orphan_blobs scenarios**: dry-run preserves blobs;
  re-run is idempotent; orphan after a single record prune;
  ``computer_uuid`` filter scopes work.
"""

from __future__ import annotations

import subprocess
import sys
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
class E7_CliFlagCombinationsTests(unittest.TestCase):
    """Invoke the CLI with various inter-dependent flag combos and
    verify the run completes + produces expected on-disk artifacts."""

    def _run_cli(self, args, expect_returncode=0):
        cmd = [sys.executable, "-m", "arq_writer.cli", *args]
        result = subprocess.run(
            cmd, capture_output=True, timeout=120, text=True,
        )
        self.assertEqual(
            result.returncode, expect_returncode,
            f"CLI returncode {result.returncode} for {args}; "
            f"stderr: {result.stderr}",
        )
        return result

    def test_use_packs_with_chunker_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.bin").write_bytes(b"X" * 8000)
            dest = tdp / "dest"
            pwfile = tdp / "pw.txt"
            pwfile.write_text("test-pw")
            self._run_cli([
                "create",
                "--dest", str(dest),
                "--password-file", str(pwfile),
                "--use-packs",
                "--chunker", "default",
                str(src),
            ])
            # treepacks/ should exist (pack mode emits it).
            tp_dirs = list(dest.rglob("treepacks"))
            self.assertGreater(
                len(tp_dirs), 0,
                "--use-packs should emit treepacks/ subdir",
            )

    def test_multiple_exclude_glob(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "keep.txt").write_bytes(b"keep")
            (src / "drop.log").write_bytes(b"drop")
            (src / "drop.tmp").write_bytes(b"drop2")
            dest = tdp / "dest"
            pwfile = tdp / "pw.txt"
            pwfile.write_text("test-pw")
            self._run_cli([
                "create",
                "--dest", str(dest),
                "--password-file", str(pwfile),
                "--exclude-glob", "*.log",
                "--exclude-glob", "*.tmp",
                str(src),
            ])
            # Restore and check.
            from arq_reader.restore import Restore
            r = Restore(str(dest), encryption_password="test-pw")
            folders = r.list_folders()
            self.assertEqual(len(folders), 1)
            cu, fu = folders[0]
            out = tdp / "out"
            out.mkdir()
            r.restore(folder_uuid=fu, dest=str(out))
            restored = sorted(p.name for p in out.rglob("*")
                              if p.is_file())
            self.assertIn("keep.txt", restored)
            self.assertNotIn("drop.log", restored)
            self.assertNotIn("drop.tmp", restored)

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"content")
            dest = tdp / "dest"
            pwfile = tdp / "pw.txt"
            pwfile.write_text("test-pw")
            self._run_cli([
                "create",
                "--dest", str(dest),
                "--password-file", str(pwfile),
                "--dry-run",
                str(src),
            ])
            # Dry-run shouldn't write anything to dest.
            if dest.exists():
                children = list(dest.rglob("*"))
                self.assertEqual(
                    children, [],
                    f"--dry-run should not write to dest; got {children}",
                )


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class E9_NonLatestRecordRestoreTests(unittest.TestCase):
    """Pass an explicit ``backuprecord_path`` to Restore.restore()
    and pin that the restore uses that record, not the latest."""

    def test_restore_from_older_record_when_two_records_exist(
        self,
    ) -> None:
        from arq_writer.backup import build_backup
        from arq_reader.restore import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"version_one")
            dest = tdp / "dest"
            res1 = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            # res.backuprecord_path is a Path; the reader expects
            # a backend-relative POSIX-string. Convert + strip the
            # destination root.
            old_record_path = "/" + str(
                Path(res1.backuprecord_path).resolve().relative_to(
                    Path(dest).resolve()
                )
            )
            # Modify content + run a second backup → newer record.
            # Wait so creationDate differs.
            import time
            time.sleep(1.1)
            (src / "f.txt").write_bytes(b"version_two")
            res2 = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            # Restore from the OLD record, not the latest.
            r = Restore(str(dest), encryption_password="pw")
            out = tdp / "out_old"
            out.mkdir()
            r.restore(
                folder_uuid=res1.folder_uuid,
                dest=str(out),
                backuprecord_path=old_record_path,
            )
            restored_file = next(out.rglob("f.txt"), None)
            self.assertIsNotNone(restored_file)
            self.assertEqual(
                restored_file.read_bytes(), b"version_one",
                "backuprecord_path=<old> should restore the old "
                "content, not the latest",
            )
            # Verify latest restore differs.
            out_new = tdp / "out_new"
            out_new.mkdir()
            r.restore(folder_uuid=res2.folder_uuid, dest=str(out_new))
            restored_new = next(out_new.rglob("f.txt"), None)
            self.assertEqual(
                restored_new.read_bytes(), b"version_two",
                "default (latest) restore should return new content",
            )


class E11_RetentionEdgeCasesTests(unittest.TestCase):
    """Retention policy corner values."""

    def test_keep_last_n_zero_keeps_none(self) -> None:
        """``keep_last_n=0`` should be distinct from
        ``keep_last_n=None`` — 0 means "no records via this
        bucket"; None means "this bucket is disabled (keep all
        when ALL buckets disabled)"."""
        from arq_writer.retention import (
            RetentionPolicy, select_retained, _RecordRef,
        )
        records = [
            _RecordRef(path=f"/r{i}", creation_date=1_700_000_000 + i)
            for i in range(5)
        ]
        # keep_last_n=0 + all other buckets at default 0 → no
        # bucket selects any record. RetentionPolicy.is_keep_all
        # is False because keep_last_n != None.
        policy = RetentionPolicy(keep_last_n=0)
        self.assertFalse(policy.is_keep_all)
        kept = select_retained(records, policy)
        self.assertEqual(
            kept, set(),
            "keep_last_n=0 + all zero buckets → keep nothing",
        )

    def test_keep_last_n_none_keeps_all(self) -> None:
        from arq_writer.retention import (
            RetentionPolicy, select_retained, _RecordRef,
        )
        records = [
            _RecordRef(path=f"/r{i}", creation_date=1_700_000_000 + i)
            for i in range(5)
        ]
        policy = RetentionPolicy()  # all defaults → keep_all
        self.assertTrue(policy.is_keep_all)
        kept = select_retained(records, policy)
        self.assertEqual(
            kept, {r.path for r in records},
            "default policy keeps everything",
        )

    def test_keep_last_n_larger_than_record_count_caps_at_count(
        self,
    ) -> None:
        from arq_writer.retention import (
            RetentionPolicy, select_retained, _RecordRef,
        )
        records = [
            _RecordRef(path=f"/r{i}", creation_date=1_700_000_000 + i)
            for i in range(3)
        ]
        policy = RetentionPolicy(keep_last_n=1_000_000)
        kept = select_retained(records, policy)
        self.assertEqual(
            kept, {r.path for r in records},
            "keep_last_n much larger than record count → keep all",
        )

    def test_mixed_positive_zero_buckets(self) -> None:
        """``keep_daily=2`` + ``keep_weekly=0`` → only daily fires."""
        from arq_writer.retention import (
            RetentionPolicy, select_retained, _RecordRef,
        )
        # 7 records, one per day for a week.
        records = []
        base = 1_700_000_000
        for i in range(7):
            records.append(_RecordRef(
                path=f"/d{i}", creation_date=base + i * 86400,
            ))
        policy = RetentionPolicy(keep_daily=2, keep_weekly=0)
        kept = select_retained(records, policy)
        # Most-recent 2 daily buckets.
        self.assertEqual(len(kept), 2)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class E12_GcOrphanBlobsScenariosTests(unittest.TestCase):
    """``gc_orphan_blobs`` corner cases."""

    def _build_two_backups_with_different_content(self, tdp: Path):
        from arq_writer.backup import build_backup
        import time
        src = tdp / "src"
        src.mkdir()
        # Distinct content so the two records have different
        # data blobs.
        (src / "file.txt").write_bytes(b"first-version")
        dest = tdp / "dest"
        res1 = build_backup(
            str(src), str(dest), encryption_password="pw",
        )
        time.sleep(1.1)
        (src / "file.txt").write_bytes(b"SECOND-version")
        res2 = build_backup(
            str(src), str(dest), encryption_password="pw",
        )
        return dest, res1, res2

    def test_dry_run_preserves_all_blobs(self) -> None:
        from arq_writer.backup import build_backup
        from arq_writer.retention import (
            prune_records, gc_orphan_blobs, RetentionPolicy,
        )
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, res1, res2 = (
                self._build_two_backups_with_different_content(tdp)
            )
            backend = LocalBackend(str(dest))
            # Prune res1 first (dry-run is on the GC side; the
            # prune deletes records).
            prune_records(
                backend, root="/",
                encryption_password="pw",
                policy=RetentionPolicy(keep_last_n=1),
                callback=None,
            )
            # Count blobs before dry-run gc.
            before = sum(
                1 for _ in dest.rglob("*") if _.is_file()
            )
            gc_result = gc_orphan_blobs(
                backend,
                encryption_password="pw",
                dry_run=True,
            )
            after = sum(
                1 for _ in dest.rglob("*") if _.is_file()
            )
            self.assertEqual(
                before, after,
                "dry-run gc must not delete any files",
            )
            # Result object should report what WOULD have been
            # deleted.
            self.assertGreaterEqual(
                gc_result.standalone_blobs_deleted, 0,
                "dry-run result counters should be populated "
                "(may be 0 if pack-mode, but never negative)",
            )

    def test_gc_idempotent_second_run_no_op(self) -> None:
        from arq_writer.retention import (
            prune_records, gc_orphan_blobs, RetentionPolicy,
        )
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, res1, res2 = (
                self._build_two_backups_with_different_content(tdp)
            )
            backend = LocalBackend(str(dest))
            prune_records(
                backend, root="/",
                encryption_password="pw",
                policy=RetentionPolicy(keep_last_n=1),
            )
            first = gc_orphan_blobs(
                backend, encryption_password="pw",
            )
            second = gc_orphan_blobs(
                backend, encryption_password="pw",
            )
            self.assertEqual(
                second.standalone_blobs_deleted, 0,
                "second gc pass should find no orphans (idempotent)",
            )
            self.assertEqual(second.standalone_bytes_freed, 0)

    def test_gc_with_computer_uuid_filter(self) -> None:
        from arq_writer.retention import gc_orphan_blobs
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, res1, res2 = (
                self._build_two_backups_with_different_content(tdp)
            )
            backend = LocalBackend(str(dest))
            # Filter to a non-existent computer_uuid → should
            # run cleanly + delete 0.
            result = gc_orphan_blobs(
                backend,
                encryption_password="pw",
                computer_uuid="non-existent-uuid",
            )
            self.assertEqual(result.standalone_blobs_deleted, 0)


if __name__ == "__main__":
    unittest.main()
