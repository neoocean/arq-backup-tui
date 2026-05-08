"""Tests for the four library APIs the TUI depends on:

- ``Restore.list_records`` — record history per folder
- ``Restore.restore(backuprecord_path=...)`` — historical restore
- ``Restore.restore(paths=[...])`` — path-filtered restore
- ``Backup.cancel`` — graceful cancel raises BackupCancelled

Plus a Unicode-safe-paths regression: the path-filter compares
byte-for-byte against UTF-8 child names from the Tree, so
non-ASCII paths must round-trip transparently.
"""

from __future__ import annotations

import threading
import time
import tempfile
import unittest
from pathlib import Path

from arq_reader import Restore
from arq_reader.restore import _PathFilter, RecordInfo
from arq_writer import Backup, build_backup
from arq_writer.backup import BackupCancelled


def _make_tree(root: Path) -> None:
    (root / "subdir").mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"alpha\n")
    (root / "beta.txt").write_bytes(b"beta\n")
    (root / "subdir" / "gamma.txt").write_bytes(b"gamma\n")


class ListRecordsTests(unittest.TestCase):
    def test_returns_one_record_per_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            time.sleep(1.1)
            r2 = build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                # Reuse keyset so both records decrypt under the
                # same master keys.
                dedup_against_existing=True,
            )
            rs = Restore(dest, encryption_password="pw")
            records = rs.list_records(
                folder_uuid=r1.folder_uuid,
                computer_uuid=r1.computer_uuid,
            )
            self.assertEqual(len(records), 2)
            for r in records:
                self.assertIsInstance(r, RecordInfo)
                self.assertEqual(r.folder_uuid, r1.folder_uuid)
                self.assertEqual(r.computer_uuid, r1.computer_uuid)
                self.assertGreater(r.creation_date, 0)
            # Sorted oldest-first.
            self.assertLess(
                records[0].creation_date, records[1].creation_date,
            )

    def test_empty_folder_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            rs = Restore(dest, encryption_password="pw")
            records = rs.list_records(
                folder_uuid="00000000-0000-0000-0000-000000000000",
                computer_uuid=r1.computer_uuid,
            )
            self.assertEqual(records, [])


class HistoricalRestoreTests(unittest.TestCase):
    def test_restore_specific_record_path(self) -> None:
        # Two backups; the SECOND modifies alpha.txt. Restoring
        # the FIRST record must recover the original alpha.txt.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            time.sleep(1.1)
            (src / "alpha.txt").write_bytes(b"alpha NEW\n")
            r2 = build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
            )
            rs = Restore(dest, encryption_password="pw")
            recs = rs.list_records(
                folder_uuid=r1.folder_uuid,
                computer_uuid=r1.computer_uuid,
            )
            old_path = recs[0].relative_path
            out = tdp / "out"
            out.mkdir()
            rs.restore(
                folder_uuid=r1.folder_uuid,
                computer_uuid=r1.computer_uuid,
                dest=out,
                backuprecord_path=old_path,
            )
            self.assertEqual(
                (out / "alpha.txt").read_bytes(), b"alpha\n",
            )


class PathFilterUnitTests(unittest.TestCase):
    def test_empty_filter_matches_everything(self) -> None:
        f = _PathFilter.from_paths([])
        self.assertTrue(f.matches("anything"))
        self.assertTrue(f.descend(""))
        self.assertTrue(f.descend("a/b"))

    def test_exact_path_match(self) -> None:
        f = _PathFilter.from_paths(["docs/notes.md"])
        self.assertTrue(f.matches("docs/notes.md"))
        self.assertFalse(f.matches("docs/other.md"))
        self.assertFalse(f.matches("notes.md"))

    def test_directory_prefix_match(self) -> None:
        f = _PathFilter.from_paths(["docs"])
        self.assertTrue(f.matches("docs/notes.md"))
        self.assertTrue(f.matches("docs/sub/file.txt"))
        self.assertFalse(f.matches("photos/x.jpg"))

    def test_descend_skips_unrelated_subtrees(self) -> None:
        f = _PathFilter.from_paths(["docs/sub"])
        self.assertTrue(f.descend(""))
        self.assertTrue(f.descend("docs"))
        self.assertTrue(f.descend("docs/sub"))
        self.assertTrue(f.descend("docs/sub/deep"))
        self.assertFalse(f.descend("photos"))
        self.assertFalse(f.descend("docs/other"))

    def test_unicode_paths_round_trip(self) -> None:
        # The filter compares byte-for-byte against UTF-8 names; a
        # Korean / Japanese / emoji path should match exactly.
        f = _PathFilter.from_paths([
            "문서/이력서.txt",
            "写真/family.jpg",
            "🎵/song.mp3",
        ])
        self.assertTrue(f.matches("문서/이력서.txt"))
        self.assertTrue(f.matches("写真/family.jpg"))
        self.assertTrue(f.matches("🎵/song.mp3"))
        self.assertFalse(f.matches("문서/other.txt"))
        # Descend must allow walking into a subtree that contains a
        # filtered path even when the directory name itself is non-
        # ASCII.
        self.assertTrue(f.descend("문서"))
        self.assertTrue(f.descend("写真"))


class PathFilteredRestoreTests(unittest.TestCase):
    def test_paths_filter_restores_only_matches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            rs = Restore(dest, encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            rs.restore(
                folder_uuid=r1.folder_uuid,
                computer_uuid=r1.computer_uuid,
                dest=out,
                paths=["alpha.txt", "subdir/gamma.txt"],
            )
            self.assertEqual(
                (out / "alpha.txt").read_bytes(), b"alpha\n",
            )
            self.assertEqual(
                (out / "subdir" / "gamma.txt").read_bytes(),
                b"gamma\n",
            )
            self.assertFalse((out / "beta.txt").exists())

    def test_paths_filter_with_unicode_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "한글폴더").mkdir()
            (src / "한글폴더" / "메모.txt").write_bytes("내용".encode("utf-8"))
            (src / "ascii.txt").write_bytes(b"plain")
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            rs = Restore(dest, encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            rs.restore(
                folder_uuid=r1.folder_uuid,
                computer_uuid=r1.computer_uuid,
                dest=out,
                paths=["한글폴더"],
            )
            self.assertEqual(
                (out / "한글폴더" / "메모.txt").read_bytes(),
                "내용".encode("utf-8"),
            )
            self.assertFalse((out / "ascii.txt").exists())

    def test_unfiltered_default_restores_everything(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            rs = Restore(dest, encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            rs.restore(
                folder_uuid=r1.folder_uuid,
                computer_uuid=r1.computer_uuid,
                dest=out,
            )
            for rel in ("alpha.txt", "beta.txt", "subdir/gamma.txt"):
                self.assertTrue((out / rel).exists())


class BackupCancelTests(unittest.TestCase):
    def test_cancel_raises_backupcancelled(self) -> None:
        # Build a source big enough that cancellation has time to
        # land mid-walk (~50 files). Cancel from a sibling thread.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            for i in range(50):
                (src / f"f{i:03d}.bin").write_bytes(b"x" * (4 * 1024))
            dest = tdp / "dest"
            dest.mkdir()
            bk = Backup(
                dest_root=dest, encryption_password="pw",
            )

            def trigger_cancel() -> None:
                # Yield to the writer briefly so the walk is well
                # underway, then cancel.
                time.sleep(0.05)
                bk.cancel()

            t = threading.Thread(target=trigger_cancel, daemon=True)
            t.start()
            bk.init_plan()
            with self.assertRaises(BackupCancelled):
                bk.add_folder(src, folder_name="root")
            t.join(timeout=2)

    def test_cancel_before_walk_is_immediate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"a")
            dest = tdp / "dest"
            dest.mkdir()
            bk = Backup(
                dest_root=dest, encryption_password="pw",
            )
            bk.init_plan()
            bk.cancel()
            with self.assertRaises(BackupCancelled):
                bk.add_folder(src, folder_name="root")


if __name__ == "__main__":
    unittest.main()
