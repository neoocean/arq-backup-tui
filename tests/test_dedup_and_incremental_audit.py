"""Tests for E2 (cross-snapshot dedup) + E3 (incremental
audit ledger).
"""

from __future__ import annotations

import json
import os
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


# ---------------------------------------------------------------------------
# E2 — dedup ratio measurement
# ---------------------------------------------------------------------------


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class DedupMeasurementTests(unittest.TestCase):

    def _two_snapshots(self, td, *, mutate):
        from arq_writer import Backup
        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_text("alpha")
        (src / "b.txt").write_text("beta")
        dst = td / "dst"
        dst.mkdir()
        bk1 = Backup(
            dest_root=dst, encryption_password="pw",
            dedup_against_existing=True,
        )
        bk1.init_plan()
        bk1.add_folder(src, folder_uuid="F1")
        cu = bk1.computer_uuid
        time.sleep(1.1)
        mutate(src)
        bk2 = Backup(
            dest_root=dst, encryption_password="pw",
            dedup_against_existing=True,
            computer_uuid=cu,
        )
        bk2.init_plan()
        bk2.add_folder(src, folder_uuid="F1")
        return dst, cu

    def _records_for(self, dst, cu):
        from arq_validator.backend import LocalBackend
        from arq_validator.layout import list_backuprecords
        backend = LocalBackend(dst)
        return list_backuprecords(backend, "/", cu, "F1"), backend

    def test_no_change_yields_high_dedup(self) -> None:
        from arq_reader import Restore
        from arq_reader.snapshot_diff import measure_dedup_ratio
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, cu = self._two_snapshots(
                td, mutate=lambda src: None,
            )
            recs, backend = self._records_for(dst, cu)
            self.assertGreaterEqual(len(recs), 2)
            rs = Restore(
                str(dst), encryption_password="pw",
                backend=backend,
            )
            r = measure_dedup_ratio(
                rs, record_path_a=recs[0], record_path_b=recs[-1],
                computer_uuid=cu,
            )
            # Every blob in B should already be in A.
            self.assertEqual(
                r.shared_blob_count, r.b_blob_count,
            )
            self.assertEqual(r.shared_ratio, 1.0)

    def test_one_added_file_lowers_ratio(self) -> None:
        from arq_reader import Restore
        from arq_reader.snapshot_diff import measure_dedup_ratio
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, cu = self._two_snapshots(
                td, mutate=lambda src: (
                    src / "c.txt"
                ).write_text("gamma-new"),
            )
            recs, backend = self._records_for(dst, cu)
            rs = Restore(
                str(dst), encryption_password="pw",
                backend=backend,
            )
            r = measure_dedup_ratio(
                rs, record_path_a=recs[0], record_path_b=recs[-1],
                computer_uuid=cu,
            )
            # Some blobs shared, some new in B (the added file
            # AND the new tree blob that includes it).
            self.assertGreater(r.shared_blob_count, 0)
            self.assertLess(r.shared_ratio, 1.0)
            self.assertGreater(r.b_unique_count, 0)


# ---------------------------------------------------------------------------
# E3 — incremental audit ledger
# ---------------------------------------------------------------------------


class AuditLedgerTests(unittest.TestCase):

    def test_empty_ledger_contains_nothing(self) -> None:
        from arq_validator.incremental_audit import AuditLedger
        l = AuditLedger()
        self.assertFalse(l.contains("abc"))
        self.assertEqual(l.size, 0)

    def test_record_then_contains_returns_true(self) -> None:
        from arq_validator.incremental_audit import AuditLedger
        l = AuditLedger()
        l.record("abc")
        self.assertTrue(l.contains("abc"))
        self.assertFalse(l.contains("xyz"))

    def test_round_trip_through_disk(self) -> None:
        from arq_validator.incremental_audit import (
            AuditLedger, load_ledger, save_ledger,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.json"
            l = AuditLedger(target="cu-1")
            l.record("blob-a", when=100.0)
            l.record("blob-b", when=200.0)
            l.sweep_count = 3
            l.last_sweep_finished_at = 999.0
            save_ledger(l, path)
            loaded = load_ledger(path, target="cu-1")
            self.assertEqual(loaded.target, "cu-1")
            self.assertEqual(loaded.size, 2)
            self.assertEqual(loaded.sweep_count, 3)
            self.assertEqual(
                loaded.last_sweep_finished_at, 999.0,
            )
            self.assertTrue(loaded.contains("blob-a"))
            self.assertTrue(loaded.contains("blob-b"))

    def test_corrupt_ledger_yields_fresh_one(self) -> None:
        """A corrupt JSON shouldn't break the validator —
        worst case the operator re-audits everything."""
        from arq_validator.incremental_audit import load_ledger
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "ledger.json"
            path.write_text("{not json", encoding="utf-8")
            l = load_ledger(path, target="cu-1")
            self.assertEqual(l.size, 0)
            self.assertEqual(l.target, "cu-1")

    def test_prune_drops_stale_entries(self) -> None:
        from arq_validator.incremental_audit import (
            AuditLedger, prune_older_than,
        )
        l = AuditLedger()
        l.record("fresh", when=1000.0)
        l.record("stale-1", when=500.0)
        l.record("stale-2", when=400.0)
        # now=1100; cutoff = 1100 - 200 = 900; stale-1 (500)
        # + stale-2 (400) drop, fresh (1000) survives.
        n = prune_older_than(l, age_sec=200, now=1100.0)
        self.assertEqual(n, 2)
        self.assertEqual(l.size, 1)
        self.assertTrue(l.contains("fresh"))

    def test_forget_removes_specific_entry(self) -> None:
        from arq_validator.incremental_audit import AuditLedger
        l = AuditLedger()
        l.record("a")
        l.record("b")
        l.forget("a")
        self.assertFalse(l.contains("a"))
        self.assertTrue(l.contains("b"))
        # forget on missing entry is a no-op (not an error).
        l.forget("nonexistent")

    def test_merge_keeps_later_timestamp(self) -> None:
        from arq_validator.incremental_audit import (
            AuditLedger, merge_ledgers,
        )
        a = AuditLedger(target="t")
        a.record("shared", when=100.0)
        a.record("only-a", when=50.0)
        b = AuditLedger(target="t")
        b.record("shared", when=200.0)
        b.record("only-b", when=300.0)
        merged = merge_ledgers(a, b)
        self.assertEqual(merged.size, 3)
        self.assertEqual(
            merged.blob_last_ok["shared"], 200.0,
        )

    def test_default_ledger_path_is_per_target(self) -> None:
        from arq_validator.incremental_audit import (
            ledger_path_for,
        )
        p1 = ledger_path_for("cu-A")
        p2 = ledger_path_for("cu-B")
        self.assertNotEqual(p1, p2)
        self.assertTrue(str(p1).endswith("cu-A.json"))


if __name__ == "__main__":
    unittest.main()
