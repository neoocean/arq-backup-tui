"""Tests for L3 graph-consistency tier (`run_graph_check`).

Operator request 2026-05-12 (priority #4 of the validator
improvements menu): the existing L0-L2 tiers don't follow the
per-record blob graph end-to-end across ALL records.  L1b
verifies the *latest* backuprecord's own HMAC; L2 walks every
EncryptedObject file in isolation.  Neither catches:

  - A tree node that references a blob whose on-disk file
    no longer exists (partial-GC or accidental delete).
  - Bit-rot on an older record's referenced blob that L1b
    skipped because the record isn't the latest.

L3 fills the gap by iterating every backuprecord in every
folder and delegating per-record graph walks to
``record_validator.validate_record`` (which already exists).
The new ``run_graph_check`` aggregates results, dedupes
failures by (blob_id, kind), and exposes progress events
mirroring L2's ETA-aware payload.

These tests pin:

  - GraphCheckResult is properly populated on a clean backup
    (records_ok == records_checked, zero missing/hmac fails).
  - Missing-blob corruption is detected (an actual reference
    pointing at a deleted file).
  - HMAC bit-rot is detected when the on-disk file is
    flipped in place.
  - ValidationTier.GRAPH chains through to L3 from
    ``validate()`` (the smoke test of the tier-enum wiring).
  - AUDIT_PROGRESS events from L3 carry the ETA + throughput
    fields (records/sec, bytes/sec, eta_sec, etc.).
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import List


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _make_backup_with_records(td: Path, *, snapshots: int = 2):
    """Build an Arq 7 backup at ``td/dst`` with N snapshots.

    Each snapshot mutates one source file so we get distinct
    records pointing at distinct trees (necessary for L3 to
    have multiple records to walk).  Returns (dst_root,
    computer_uuid).
    """
    from arq_writer import Backup
    src = td / "src"
    src.mkdir()
    (src / "a.txt").write_text("alpha-v1\n")
    (src / "b.bin").write_bytes(b"\x10\x20\x30" * 100)
    dst = td / "dst"
    dst.mkdir()
    bk0 = Backup(
        dest_root=dst, encryption_password="pw",
        dedup_against_existing=False,
    )
    bk0.init_plan()
    bk0.add_folder(
        src,
        folder_uuid="11111111-1111-4111-8111-111111111111")
    cu = bk0.computer_uuid
    for i in range(1, snapshots):
        # Tiny mutation per snapshot so a new record is needed.
        (src / "a.txt").write_text(f"alpha-v{i + 1}\n")
        bk = Backup(
            dest_root=dst, encryption_password="pw",
            dedup_against_existing=True,
            computer_uuid=cu,
        )
        bk.init_plan()
        bk.add_folder(
            src,
            folder_uuid="11111111-1111-4111-8111-111111111111")
    return dst, cu


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class GraphCheckHappyPathTests(unittest.TestCase):

    def _graph(self, dst: Path):
        from arq_validator.backend import LocalBackend
        from arq_validator.layout import discover_layout
        from arq_validator.tiers import run_graph_check
        backend = LocalBackend(dst)
        layouts = discover_layout(backend, "/")
        return run_graph_check(
            backend, layouts, encryption_password="pw",
            root="/",
        )

    def test_clean_backup_all_records_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup_with_records(td, snapshots=2)
            result = self._graph(dst)
        self.assertGreater(result.records_checked, 0)
        self.assertEqual(
            result.records_ok, result.records_checked)
        self.assertEqual(result.records_fail, 0)
        self.assertEqual(result.blobs_missing, 0)
        self.assertEqual(result.blobs_hmac_fail, 0)
        self.assertEqual(result.failures, [])
        self.assertIsNone(result.aborted_reason)

    def test_planned_records_set_from_layout(self) -> None:
        # planned_records is computed up-front; equals the
        # actual records walked in an unbounded run.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup_with_records(td, snapshots=3)
            result = self._graph(dst)
        self.assertGreaterEqual(result.planned_records, 1)
        self.assertEqual(
            result.records_checked, result.planned_records)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class GraphCheckDetectsCorruptionTests(unittest.TestCase):

    def _graph(self, dst: Path, **kw):
        from arq_validator.backend import LocalBackend
        from arq_validator.layout import discover_layout
        from arq_validator.tiers import run_graph_check
        backend = LocalBackend(dst)
        layouts = discover_layout(backend, "/")
        return run_graph_check(
            backend, layouts, encryption_password="pw",
            root="/", **kw,
        )

    def test_missing_blob_detected(self) -> None:
        # Delete one of the on-disk object files that a tree
        # references.  L3 should surface it as kind="missing"
        # (the tree node points at a path that no longer
        # exists).
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup_with_records(td, snapshots=1)
            # Pick a standardobjects file (the data blobs the
            # tree node references; deleting a tree pack
            # would also fail decode).
            object_files = []
            for sub in ("standardobjects",):
                object_files.extend(
                    p for p in dst.rglob(f"{sub}/*/*")
                    if p.is_file() and p.stat().st_size > 50)
            self.assertTrue(object_files)
            # Sort by size; delete the smallest so we
            # minimize cascading impacts (avoid blowing
            # away the root tree).
            object_files.sort(key=lambda p: p.stat().st_size)
            target = object_files[0]
            target.unlink()
            result = self._graph(dst)
        self.assertGreater(
            result.blobs_missing + result.blobs_hmac_fail
            + result.blobs_decode_fail, 0,
            f"L3 missed the deletion: {result.failures}")

    def test_hmac_bit_rot_detected(self) -> None:
        # Flip 64 bytes in the middle of a referenced blob
        # file.  L3 walks via validate_record which fetches +
        # HMAC-verifies each blob; corruption should land as
        # kind="hmac" (or potentially "decode" if the flip
        # destroys structural fields).
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup_with_records(td, snapshots=1)
            object_files = []
            for sub in ("standardobjects",):
                object_files.extend(
                    p for p in dst.rglob(f"{sub}/*/*")
                    if p.is_file() and p.stat().st_size > 200)
            self.assertTrue(object_files)
            # Pick the LARGEST so the flip lands in HMAC-
            # protected payload (not in length-prefix
            # header).
            object_files.sort(
                key=lambda p: p.stat().st_size, reverse=True)
            target = object_files[0]
            data = bytearray(target.read_bytes())
            mid = len(data) // 2
            for i in range(64):
                data[mid + i] ^= 0xFF
            target.write_bytes(bytes(data))
            result = self._graph(dst)
        # Any of the failure-kind counters bumping = success.
        total_fail = (
            result.blobs_missing
            + result.blobs_hmac_fail
            + result.blobs_decode_fail)
        self.assertGreater(
            total_fail, 0,
            f"L3 missed the bit-rot: {result.failures}")


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class GraphTierIntegrationTests(unittest.TestCase):

    def test_graph_tier_via_validate(self) -> None:
        # ValidationTier.GRAPH chains all five layers (L0 →
        # L1a → L1b → L2 → L3).  Smoke test: pick the GRAPH
        # tier from validate() and assert all five tier blocks
        # are populated on the report.
        from arq_validator.backend import LocalBackend
        from arq_validator.runner import (
            validate, ValidationTier,
        )
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup_with_records(td, snapshots=1)
            report = validate(
                LocalBackend(dst),
                tier=ValidationTier.GRAPH,
                encryption_password="pw",
                audit_skip_larger_than=None,
            )
        self.assertIsNotNone(report.layout)
        self.assertIsNotNone(report.magic_check)
        self.assertIsNotNone(report.backuprecord)
        self.assertIsNotNone(report.audit)
        self.assertIsNotNone(report.graph)
        self.assertFalse(report.has_failures(),
                          f"clean backup has_failures: "
                          f"{report.error} / {report.graph.failures}")

    def test_audit_tier_does_not_run_graph(self) -> None:
        # Sanity check the wiring: ValidationTier.AUDIT runs
        # L2 but does NOT run L3.
        from arq_validator.backend import LocalBackend
        from arq_validator.runner import (
            validate, ValidationTier,
        )
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup_with_records(td, snapshots=1)
            report = validate(
                LocalBackend(dst),
                tier=ValidationTier.AUDIT,
                encryption_password="pw",
                audit_skip_larger_than=None,
            )
        self.assertIsNotNone(report.audit)
        self.assertIsNone(report.graph)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class GraphCheckProgressEventTests(unittest.TestCase):

    def test_progress_event_payload_has_eta_fields(self) -> None:
        # L3 emits AUDIT_PROGRESS per record with elapsed_sec,
        # records_per_sec, bytes_per_sec, eta_sec, etc.
        # Mirrors L2's enriched payload for UI consistency.
        from arq_validator.backend import LocalBackend
        from arq_validator.layout import discover_layout
        from arq_validator.tiers import run_graph_check
        from arq_validator.events import Event, EventKind
        events: List[Event] = []
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup_with_records(td, snapshots=2)

            def cb(ev: Event) -> None:
                events.append(ev)

            backend = LocalBackend(dst)
            layouts = discover_layout(backend, "/")
            run_graph_check(
                backend, layouts, encryption_password="pw",
                root="/", callback=cb,
            )
        progress = [
            e for e in events
            if e.kind == EventKind.AUDIT_PROGRESS
            and e.payload.get("tier") == "L3"]
        self.assertGreater(len(progress), 0,
                            "no L3 AUDIT_PROGRESS emitted")
        p = progress[-1].payload
        # ETA + throughput fields present.
        for key in ("elapsed_sec", "records_per_sec",
                    "bytes_per_sec", "planned_records",
                    "remaining_records"):
            self.assertIn(key, p, f"missing key: {key}")


class GraphCheckResultSerializationTests(unittest.TestCase):
    """L3's GraphCheckResult round-trips through to_dict /
    from_dict like the other tier results.  Pinned because the
    Priority #3 CL added JSON serialize support for the
    existing 4 tier blocks; L3 is the 5th and must compose."""

    def test_round_trip(self) -> None:
        from arq_validator.runner import ValidationReport
        from arq_validator.tiers import GraphCheckResult
        r = ValidationReport(
            tier="graph", root="/",
            backend_kind="LocalBackend",
        )
        r.graph = GraphCheckResult(
            records_checked=5, records_ok=5,
            records_fail=0, blob_walks_total=42,
            blobs_unique=12, bytes_fetched=1000,
            planned_records=5,
        )
        out = r.to_json()
        r2 = ValidationReport.from_json(out)
        self.assertIsInstance(r2.graph, GraphCheckResult)
        self.assertEqual(r2.graph.records_checked, 5)
        self.assertEqual(r2.graph.blob_walks_total, 42)
        self.assertEqual(r2.graph.planned_records, 5)


if __name__ == "__main__":
    unittest.main()
