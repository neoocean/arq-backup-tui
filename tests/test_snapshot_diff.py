"""Tests for the snapshot-diff API.

The diff walks two backuprecords' trees in parallel + reports
added / removed / modified / type_changed entries. These tests
build a sequence of small backups against the same destination,
then diff pairs of consecutive records to confirm each kind of
change is detected.

The destination is reused across runs so the writer's
dedup-against-existing kicks in — that's the realistic
deployment shape (incremental nightly backups against the same
SFTP mirror).
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
class SnapshotDiffTests(unittest.TestCase):

    def _run_backup(
        self, src, dst, password="pw",
        *, computer_uuid=None, folder_uuid=None,
    ):
        """Run a backup, pinning computer_uuid + folder_uuid so
        subsequent calls accumulate records under the SAME folder
        (which is what diff_snapshots needs to find a pair to
        compare). Returns ``(computer_uuid, folder_uuid)`` so the
        caller can chain."""
        from arq_writer import Backup
        bk = Backup(
            dest_root=dst, encryption_password=password,
            backup_name="diff-test", use_packs=False,
            dedup_against_existing=True,
            computer_uuid=computer_uuid,
        )
        bk.init_plan()
        bk.add_folder(src, folder_uuid=folder_uuid)
        return (
            bk.computer_uuid,
            bk._folder_plans[-1]["backupFolderUUID"],
        )

    def _records_for(self, dst, cu):
        """Return absolute server-side record paths sorted oldest
        first."""
        from arq_validator.backend import LocalBackend
        from arq_validator.layout import (
            list_backuprecords,
        )
        from arq_validator import discover_layout
        backend = LocalBackend(dst)
        lay = next(
            lt for lt in discover_layout(
                backend, "/", enumerate_objects=False,
            ) if lt.computer_uuid == cu
        )
        out = []
        for fu in lay.backup_folder_uuids:
            out.extend(list_backuprecords(
                backend, "/", cu, fu,
            ))
        return sorted(out), backend

    def _diff(self, dst, password="pw"):
        from arq_reader import Restore
        from arq_reader.snapshot_diff import diff_snapshots
        # Find any existing computer-uuid in the dest.
        cu = next(
            p.name for p in dst.iterdir()
            if p.is_dir() and len(p.name) > 30
        )
        recs, backend = self._records_for(dst, cu)
        self.assertGreaterEqual(len(recs), 2)
        rs = Restore(str(dst), encryption_password=password,
                     backend=backend)
        return diff_snapshots(
            rs,
            record_path_a=recs[-2],
            record_path_b=recs[-1],
            computer_uuid=cu,
        )

    def _two_run_diff(self, src, dst, *, mutate_after_first):
        """Run backup, mutate source per ``mutate_after_first()``,
        run backup again, return diff. Pins UUIDs across runs."""
        cu, fu = self._run_backup(src, dst)
        time.sleep(1.1)
        mutate_after_first()
        self._run_backup(src, dst, computer_uuid=cu, folder_uuid=fu)
        return self._diff(dst)

    def test_no_changes_yields_empty_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            (src / "stable.txt").write_text("hello\n")
            dst = td / "dst"
            dst.mkdir()
            r = self._two_run_diff(
                src, dst, mutate_after_first=lambda: None,
            )
            self.assertEqual(
                r.entries, [],
                f"expected no diff entries, got {r.entries!r}",
            )

    def test_added_file_detected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            (src / "a.txt").write_text("alpha")
            dst = td / "dst"
            dst.mkdir()
            r = self._two_run_diff(
                src, dst,
                mutate_after_first=lambda: (
                    src / "b.txt").write_text("beta"),
            )
            added = [e for e in r.entries if e.kind == "added"]
            self.assertEqual(
                len(added), 1, f"got {r.entries!r}",
            )
            self.assertEqual(added[0].rel_path, "b.txt")

    def test_removed_file_detected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            (src / "a.txt").write_text("alpha")
            (src / "doomed.txt").write_text("bye")
            dst = td / "dst"
            dst.mkdir()
            r = self._two_run_diff(
                src, dst,
                mutate_after_first=lambda: (
                    src / "doomed.txt").unlink(),
            )
            removed = [e for e in r.entries if e.kind == "removed"]
            self.assertEqual(len(removed), 1)
            self.assertEqual(removed[0].rel_path, "doomed.txt")

    def test_modified_file_detected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            f = src / "edit-me.txt"
            f.write_text("v1")
            dst = td / "dst"
            dst.mkdir()
            r = self._two_run_diff(
                src, dst,
                mutate_after_first=lambda: f.write_text("v2"),
            )
            mod = [e for e in r.entries if e.kind == "modified"]
            self.assertEqual(len(mod), 1)
            self.assertEqual(mod[0].rel_path, "edit-me.txt")

    def test_diff_recurses_into_subdirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            (src / "sub").mkdir(parents=True)
            (src / "sub" / "deep.txt").write_text("v1")
            dst = td / "dst"
            dst.mkdir()

            def _mutate():
                (src / "sub" / "deep.txt").write_text("v2")
                (src / "sub" / "newcomer.txt").write_text("hello")

            r = self._two_run_diff(
                src, dst, mutate_after_first=_mutate,
            )
            paths = {e.rel_path for e in r.entries}
            self.assertIn("sub/deep.txt", paths)
            self.assertIn("sub/newcomer.txt", paths)


if __name__ == "__main__":
    unittest.main()
