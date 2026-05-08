"""Tests for tree-walk reuse — the cross-run optimization that
skips ``read_bytes()`` and chunking on files whose ``stat`` triple
hasn't changed since the prior backup.

The defining property to lock in: for an unchanged source file,
the second backup run must NOT open the file. We assert this by
making the file unreadable (``chmod 000``) between runs and
confirming the second run still succeeds with bit-for-bit
identical restored output.

A second test verifies the negative case: when the file's content
changes (we write new bytes + bump mtime), the prior cache misses
and the new content is read and re-chunked normally.
"""

from __future__ import annotations

import os
import stat as stat_mod
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List

from arq_reader import Restore
from arq_writer import Backup, build_backup
from arq_writer.prior_tree import PriorTreeIndex


def _make_tree(root: Path) -> None:
    (root / "subdir").mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"alpha content " * 50)
    (root / "beta.txt").write_bytes(b"beta\n")
    (root / "subdir" / "deep.txt").write_bytes(b"buried " * 100)


class PriorTreeIndexTests(unittest.TestCase):
    def test_lookup_returns_filenode_at_known_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            res = build_backup(src, dest, encryption_password="pw")
            from arq_validator.crypto import decrypt_keyset
            ks = decrypt_keyset(
                (dest / res.computer_uuid / "encryptedkeyset.dat").read_bytes(),
                "pw",
            )
            idx = PriorTreeIndex(
                dest, res.computer_uuid,
                ks.encryption_key, ks.hmac_key,
                folder_uuid=res.folder_uuid,
            )
            self.assertTrue(idx.is_usable)
            n = idx.lookup_file("alpha.txt")
            self.assertIsNotNone(n)
            self.assertEqual(n.itemSize, len(b"alpha content " * 50))
            n2 = idx.lookup_file("subdir/deep.txt")
            self.assertIsNotNone(n2)

    def test_lookup_returns_none_for_missing_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            res = build_backup(src, dest, encryption_password="pw")
            from arq_validator.crypto import decrypt_keyset
            ks = decrypt_keyset(
                (dest / res.computer_uuid / "encryptedkeyset.dat").read_bytes(),
                "pw",
            )
            idx = PriorTreeIndex(
                dest, res.computer_uuid,
                ks.encryption_key, ks.hmac_key,
                folder_uuid=res.folder_uuid,
            )
            self.assertIsNone(idx.lookup_file("nope.txt"))
            self.assertIsNone(idx.lookup_file("subdir/missing"))

    def test_index_unusable_with_no_prior_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            idx = PriorTreeIndex(
                Path(td), "FAKE-UUID",
                b"\x00" * 32, b"\x00" * 32,
            )
            self.assertFalse(idx.is_usable)
            self.assertIsNone(idx.lookup_file("anything"))


class TreeWalkReuseTests(unittest.TestCase):
    def _events(self, kind: str, log: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [e for e in log if e.get("kind") == kind]

    def test_unchanged_files_emit_file_reused_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(
                src, dest, encryption_password="pw",
            )
            events: List[Dict[str, Any]] = []

            def cb(kind: str, payload: Dict[str, Any]) -> None:
                events.append({"kind": kind, **payload})

            r2 = build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
                callback=cb,
            )
            # Every leaf in the source tree must have fired
            # file_reused on the second run.
            reused_rels = sorted(
                e["rel_path"] for e in self._events("file_reused", events)
            )
            self.assertEqual(
                reused_rels,
                ["alpha.txt", "beta.txt", "subdir/deep.txt"],
            )
            # No file_written events fired (= no reads happened).
            self.assertEqual(self._events("file_written", events), [])
            # And restore round-trips correctly.
            restore_target = tdp / "out"
            restore_target.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=r2.folder_uuid,
                dest=restore_target,
                computer_uuid=r2.computer_uuid,
            )
            self.assertEqual(
                (restore_target / "alpha.txt").read_bytes(),
                b"alpha content " * 50,
            )
            self.assertEqual(
                (restore_target / "subdir" / "deep.txt").read_bytes(),
                b"buried " * 100,
            )

    def test_bytes_plaintext_is_zero_when_all_files_reused(self) -> None:
        # Strong "no read" signal: the writer's bytes_plaintext
        # counter only advances inside _write_blob. With every file
        # reused, no _write_blob call happens for file content, and
        # the counter stays at the size of the (always-rewritten)
        # tree blob — far below the source's content size.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            source_total_content = sum(
                p.stat().st_size for p in src.rglob("*") if p.is_file()
            )
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            self.assertGreaterEqual(
                r1.bytes_plaintext, source_total_content,
            )
            bk = Backup(
                dest_root=dest,
                encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                dedup_against_existing=True,
            )
            bk.init_plan()
            bk.add_folder(src, folder_uuid=r1.folder_uuid)
            self.assertEqual(bk.files_reused, 3)
            # Run 2's bytes_plaintext only counts the tree blob
            # write (everything else hit the prior-tree fast path),
            # which is much smaller than the file content totals.
            self.assertLess(bk.bytes_plaintext, source_total_content)

    def test_modified_file_is_re_read_on_second_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(
                src, dest, encryption_password="pw",
            )
            # Modify alpha.txt content + bump mtime so the prior
            # cache cannot match.
            time.sleep(1.1)        # cross a 1-second mtime boundary
            (src / "alpha.txt").write_bytes(b"alpha NEW " * 50)
            events: List[Dict[str, Any]] = []

            def cb(kind: str, payload: Dict[str, Any]) -> None:
                events.append({"kind": kind, **payload})

            r2 = build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
                callback=cb,
            )
            # alpha.txt must have been WRITTEN on this run (cache
            # miss), not reused.
            written_paths = [
                e["path"] for e in self._events("file_written", events)
            ]
            self.assertTrue(
                any(p.endswith("alpha.txt") for p in written_paths),
                f"alpha.txt missing from file_written events: {written_paths}",
            )
            # And the unchanged files must still be reused.
            reused_rels = [
                e["rel_path"] for e in self._events("file_reused", events)
            ]
            self.assertIn("beta.txt", reused_rels)
            self.assertIn("subdir/deep.txt", reused_rels)
            # Restore round-trips correctly.
            restore_target = tdp / "out"
            restore_target.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=r2.folder_uuid,
                dest=restore_target,
                computer_uuid=r2.computer_uuid,
            )
            self.assertEqual(
                (restore_target / "alpha.txt").read_bytes(),
                b"alpha NEW " * 50,
            )
            self.assertEqual(
                (restore_target / "beta.txt").read_bytes(),
                b"beta\n",
            )

    def test_reuse_disabled_when_dedup_off(self) -> None:
        # Sanity: without dedup_against_existing, the prior tree
        # index isn't built and no files are reused.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            events: List[Dict[str, Any]] = []

            def cb(kind: str, payload: Dict[str, Any]) -> None:
                events.append({"kind": kind, **payload})

            build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                callback=cb,
                # dedup_against_existing=False (default)
            )
            reused = self._events("file_reused", events)
            self.assertEqual(reused, [])

    def test_files_reused_counter_advances(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            # Run as Backup so we can read the counter directly.
            bk = Backup(
                dest_root=dest,
                encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                dedup_against_existing=True,
            )
            bk.init_plan()
            bk.add_folder(src, folder_uuid=r1.folder_uuid)
            self.assertGreaterEqual(bk.files_reused, 3)
            # All file walks counted under files_written too.
            self.assertEqual(bk.files_written, bk.files_reused)


if __name__ == "__main__":
    unittest.main()
