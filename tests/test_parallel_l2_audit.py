"""Tests for parallel L2 audit (``audit_concurrency`` parameter).

The L2 tier in :mod:`arq_validator.tiers` historically ran every
per-file HMAC verification on the calling thread.  For a populated
LocalBackend mirror (tens of GB of small blob/tree packs) the
HMAC step is CPU-bound and serial — running it across multiple
worker threads is the obvious speedup.

Operator request 2026-05-12 locked the parallel-L2 design as the
top priority improvement; see DESIGN.md §parallel-l2 for the
contract.  These tests pin:

  - audit_concurrency=1 still produces the historical serial path
    (no behavior change for existing callers).
  - audit_concurrency=4 produces the SAME counts + failures as
    the serial path (parallel ≡ serial under the merge-delta
    contract).
  - The safety clamp downgrades to 1 when the backend declares
    ``supports_concurrent_reads = False`` (sole SFTP-safety
    invariant) and emits a LOG event so the operator sees the
    downgrade.
  - Ledger contains() / record() interactions are safe under
    concurrent workers (a parallel run grows the ledger by the
    same count as a serial run).

Skips on hosts without OpenSSL since the writer + validator both
need it for AES-256-CBC.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import List
from unittest.mock import patch


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _make_backup(td: Path, file_count: int = 6) -> tuple:
    """Build a small Arq 7 backup at ``td/dst`` with
    ``file_count`` source files.  Returns ``(dst_root,
    computer_uuid)``.

    Each file gets distinct binary content so the writer
    can't dedup blobs across them — we want multiple object
    files in the resulting backup so parallel audit has
    actual work to spread across threads.
    """
    from arq_writer import Backup
    src = td / "src"
    src.mkdir()
    for i in range(file_count):
        # 64KB random-ish content per file → forces individual
        # blobs.
        (src / f"f{i:02d}.bin").write_bytes(
            (bytes(range(256)) * 256) * (i + 1))
    dst = td / "dst"
    dst.mkdir()
    bk = Backup(
        dest_root=dst, encryption_password="pw",
        dedup_against_existing=False,
    )
    bk.init_plan()
    bk.add_folder(src, folder_uuid="F1")
    return dst, bk.computer_uuid


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class ParallelL2AuditTests(unittest.TestCase):
    """Parallel ≡ serial outcome equivalence + LocalBackend
    thread-safety.
    """

    def _audit(self, dst: Path, *, concurrency: int):
        from arq_validator.backend import LocalBackend
        from arq_validator.layout import discover_layout
        from arq_validator.tiers import run_full_audit
        backend = LocalBackend(dst)
        layouts = discover_layout(backend, "/")
        # skip_larger_than=None: audit every object file
        # regardless of size.  The default AUDIT_DEFAULT_SKIP
        # _LARGER_THAN=256K would skip the larger files in
        # our test fixture, masking the corruption-detection
        # test.
        return run_full_audit(
            backend, layouts, encryption_password="pw",
            root="/", audit_concurrency=concurrency,
            skip_larger_than=None,
        )

    def test_concurrency_1_is_default_serial(self) -> None:
        # Default behavior unchanged: audit_concurrency=1 runs
        # the historical serial path.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td)
            result = self._audit(dst, concurrency=1)
        self.assertGreater(result.files_total, 0)
        self.assertEqual(result.files_fail, 0)
        self.assertEqual(result.files_error, 0)
        self.assertIsNone(result.aborted_reason)

    def test_parallel_yields_same_counts_as_serial(self) -> None:
        # The parallel driver merges deltas under a lock; the
        # final counters MUST match the serial run for the same
        # input.  This is the headline correctness check.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td, file_count=8)
            serial = self._audit(dst, concurrency=1)
            parallel = self._audit(dst, concurrency=4)
        self.assertEqual(
            parallel.files_total, serial.files_total)
        self.assertEqual(
            parallel.files_ok, serial.files_ok)
        self.assertEqual(
            parallel.files_fail, serial.files_fail)
        self.assertEqual(
            parallel.files_error, serial.files_error)
        self.assertEqual(
            parallel.files_skipped, serial.files_skipped)
        self.assertEqual(
            parallel.bytes_read, serial.bytes_read)
        self.assertEqual(
            parallel.inner_arqos_ok, serial.inner_arqos_ok)
        self.assertEqual(
            parallel.inner_arqos_fail, serial.inner_arqos_fail)

    def test_parallel_failures_set_matches_serial(self) -> None:
        # Inject a single-byte corruption into one object file
        # then verify both serial + parallel detect it +
        # report the SAME failure.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td)
            # Find an object file under any of the object
            # families and corrupt one byte at a small offset
            # (after the ARQO magic so the magic-check still
            # passes — we want the HMAC mismatch path).  Small
            # backups land everything under standardobjects/;
            # bigger backups also produce blobpacks/treepacks
            # but we don't need those here.
            obj_dirs = ("standardobjects", "blobpacks",
                         "treepacks", "largeblobpacks")
            object_files = []
            for sub in obj_dirs:
                object_files.extend(
                    p for p in dst.rglob(f"{sub}/*/*")
                    if p.is_file())
            self.assertTrue(
                object_files, "no object files written")
            # Pick the LARGEST object file so the mutation
            # lands inside the HMAC-protected payload region
            # of one of its inner ARQO containers.  Single-byte
            # flips at random offsets sometimes land in
            # length-prefix fields whose corruption produces
            # parse-failure rather than HMAC mismatch; flipping
            # a 64-byte run guarantees we hit HMAC-protected
            # bytes whatever the offset semantic.
            object_files.sort(
                key=lambda p: p.stat().st_size, reverse=True)
            target = object_files[0]
            data = bytearray(target.read_bytes())
            # 64-byte flip centered on the file middle.  Far
            # enough from header (offset 0) + trailing HMAC
            # (last 32 bytes) that we're solidly inside payload
            # for any reasonable ARQO container layout.
            mid = len(data) // 2
            for i in range(64):
                data[mid + i] ^= 0xFF
            target.write_bytes(bytes(data))
            serial = self._audit(dst, concurrency=1)
            parallel = self._audit(dst, concurrency=4)
        self.assertGreater(
            serial.files_fail + serial.files_error, 0,
            "serial mode missed the corruption")
        # Failure counts identical.
        self.assertEqual(
            parallel.files_fail, serial.files_fail)
        self.assertEqual(
            parallel.files_error, serial.files_error)
        # Failure sets contain the same file_names (order may
        # differ in parallel mode; compare by set).
        serial_files = {
            f["file_name"] for f in serial.failures
            if "file_name" in f}
        parallel_files = {
            f["file_name"] for f in parallel.failures
            if "file_name" in f}
        self.assertEqual(parallel_files, serial_files)

    def test_concurrency_clamped_for_non_concurrent_backend(
            self) -> None:
        # A backend that declares
        # ``supports_concurrent_reads = False`` (SftpBackend's
        # value) must auto-downgrade to concurrency=1.  We
        # synthesize a wrapper backend that delegates to
        # LocalBackend but advertises False, so we can run
        # locally without an SFTP server.
        from arq_validator.backend import LocalBackend

        class NonConcurrentLocal:
            supports_concurrent_reads = False

            def __init__(self, inner):
                self._inner = inner
            # Forward read-side methods only — that's all the
            # validator uses.
            def list_dir(self, p):
                return self._inner.list_dir(p)
            def stat_size(self, p):
                return self._inner.stat_size(p)
            def read_range(self, p, o, l):
                return self._inner.read_range(p, o, l)
            def read_all(self, p):
                return self._inner.read_all(p)
            def exists(self, p):
                return self._inner.exists(p)
            def is_dir(self, p):
                return self._inner.is_dir(p)

        from arq_validator.layout import discover_layout
        from arq_validator.tiers import run_full_audit
        from arq_validator.events import Event, EventKind

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td)
            backend = NonConcurrentLocal(LocalBackend(dst))
            layouts = discover_layout(backend, "/")
            log_events: List[Event] = []

            def cb(event: Event) -> None:
                if event.kind == EventKind.LOG:
                    log_events.append(event)

            result = run_full_audit(
                backend, layouts, encryption_password="pw",
                root="/", audit_concurrency=8, callback=cb,
                skip_larger_than=None,
            )
        self.assertGreater(result.files_total, 0)
        self.assertEqual(result.files_fail, 0)
        # The clamp LOG event must mention the downgrade.
        self.assertTrue(
            any("clamping to 1" in e.message
                or "clamped_to" in e.payload.get("clamped_to", "")
                for e in log_events),
            "expected a clamp-to-1 LOG event when backend "
            f"declares supports_concurrent_reads=False; "
            f"got: {[e.message for e in log_events]}")

    def test_ledger_records_same_count_under_concurrency(
            self) -> None:
        # The ledger must observe the same number of records
        # under parallel mode as serial.  Race-free across
        # workers because the driver merges deltas under a
        # lock; this test pins the contract end-to-end.
        from arq_validator.incremental_audit import AuditLedger

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td)
            from arq_validator.backend import LocalBackend
            from arq_validator.layout import discover_layout
            from arq_validator.tiers import run_full_audit
            backend = LocalBackend(dst)
            layouts = discover_layout(backend, "/")

            ledger_serial = AuditLedger()
            run_full_audit(
                backend, layouts, encryption_password="pw",
                root="/", audit_concurrency=1,
                skip_larger_than=None,
                ledger=ledger_serial)

            ledger_parallel = AuditLedger()
            run_full_audit(
                backend, layouts, encryption_password="pw",
                root="/", audit_concurrency=4,
                skip_larger_than=None,
                ledger=ledger_parallel)
        self.assertEqual(
            ledger_serial.size, ledger_parallel.size,
            f"ledger size diverged: "
            f"serial={ledger_serial.size} "
            f"parallel={ledger_parallel.size}")
        self.assertGreater(ledger_serial.size, 0)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class ParallelL2AuditBoundsTests(unittest.TestCase):
    """Defensive bounds on ``audit_concurrency``."""

    def _audit(self, dst: Path, *, concurrency):
        from arq_validator.backend import LocalBackend
        from arq_validator.layout import discover_layout
        from arq_validator.tiers import run_full_audit
        backend = LocalBackend(dst)
        layouts = discover_layout(backend, "/")
        return run_full_audit(
            backend, layouts, encryption_password="pw",
            root="/", audit_concurrency=concurrency,
            skip_larger_than=None,
        )

    def test_zero_or_negative_clamps_to_one(self) -> None:
        # max(1, min(64, int(n))) — operator typo (0 or
        # negative) silently falls back to sequential.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td)
            for n in (0, -1, -100):
                result = self._audit(dst, concurrency=n)
                self.assertGreater(result.files_total, 0)
                self.assertEqual(result.files_fail, 0)

    def test_excessive_value_clamps_to_sixty_four(self) -> None:
        # Operator-passed 1000 → clamped to 64 (defensive cap
        # against fd-table exhaustion + thread-pool overhead
        # on extreme requests).  Behavior is identical to a
        # smaller value for a small backup; this test pins
        # that the clamp doesn't break the run.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td)
            result = self._audit(dst, concurrency=1000)
        self.assertGreater(result.files_total, 0)
        self.assertEqual(result.files_fail, 0)


class AuditDeltaMergeTests(unittest.TestCase):
    """Pure-Python tests for the delta-merge helpers — no
    OpenSSL needed since we synthesize deltas directly.
    """

    def test_merge_accumulates_all_fields(self) -> None:
        from arq_validator.tiers import (
            _AuditDelta, _merge_audit_delta,
        )
        from arq_validator.tiers import ObjectAuditResult
        r = ObjectAuditResult()
        d = _AuditDelta(
            files_total=1, files_ok=1, bytes_read=100,
            inner_arqos_total=1, inner_arqos_ok=1,
            ledger_record_file_name="f1.pack")
        _merge_audit_delta(r, d)
        self.assertEqual(r.files_total, 1)
        self.assertEqual(r.files_ok, 1)
        self.assertEqual(r.bytes_read, 100)
        self.assertEqual(r.inner_arqos_ok, 1)
        # ledger_record_file_name is consumed by the driver,
        # not merged into result.
        self.assertEqual(r.failures, [])

    def test_merge_repeated_fields_accumulate(self) -> None:
        from arq_validator.tiers import (
            _AuditDelta, _merge_audit_delta,
        )
        from arq_validator.tiers import ObjectAuditResult
        r = ObjectAuditResult()
        for i in range(5):
            _merge_audit_delta(r, _AuditDelta(
                files_total=1, files_ok=1, bytes_read=10))
        self.assertEqual(r.files_total, 5)
        self.assertEqual(r.files_ok, 5)
        self.assertEqual(r.bytes_read, 50)

    def test_merge_failures_extend(self) -> None:
        from arq_validator.tiers import (
            _AuditDelta, _merge_audit_delta,
        )
        from arq_validator.tiers import ObjectAuditResult
        r = ObjectAuditResult()
        _merge_audit_delta(r, _AuditDelta(
            files_total=1, files_fail=1,
            failures=[{"file_name": "x.pack",
                       "error": "hmac mismatch"}]))
        _merge_audit_delta(r, _AuditDelta(
            files_total=1, files_error=1,
            failures=[{"file_name": "y.pack",
                       "error": "stat: ENOENT"}]))
        self.assertEqual(len(r.failures), 2)
        self.assertEqual(r.files_fail, 1)
        self.assertEqual(r.files_error, 1)


class RunFullAuditMultiSetKeysetTests(unittest.TestCase):
    """2026-05-27: run_full_audit decrypts a keyset PER
    computer-UUID and SKIPS (not aborts) backup sets that are
    unencrypted or open with a different password — parity with
    the audit-drip per-cu keyset fix.  Guards a destination that
    hosts several backup sets with different / no encryption."""

    def _run(self, root: Path, password: str):
        from arq_validator.backend import LocalBackend
        from arq_validator.layout import discover_layout
        from arq_validator.tiers import run_full_audit
        backend = LocalBackend(root)
        layouts = discover_layout(backend, "/")
        return run_full_audit(
            backend, layouts, encryption_password=password,
            root="/", skip_larger_than=None)

    def test_skips_unauditable_audits_openable(self) -> None:
        from tests.fixtures import write_synthetic_backup
        cu_ok = "11111111-1111-1111-1111-111111111111"
        cu_diff = "22222222-2222-2222-2222-222222222222"
        cu_plain = "33333333-3333-3333-3333-333333333333"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_synthetic_backup(root, "right", computer_uuid=cu_ok)
            write_synthetic_backup(root, "different",
                                   computer_uuid=cu_diff)
            write_synthetic_backup(root, "x", computer_uuid=cu_plain)
            (root / cu_plain / "encryptedkeyset.dat").unlink()
            result = self._run(root, "right")
        # openable set audited; NO false failures from the others.
        self.assertIsNone(result.aborted_reason)
        self.assertGreater(result.files_ok, 0)
        self.assertEqual(result.files_fail, 0)
        # the two unauditable sets are recorded as skipped.
        joined = " ".join(result.skipped_backup_sets)
        self.assertIn(cu_diff, joined)
        self.assertIn(cu_plain, joined)
        self.assertIn("unencrypted", joined)
        self.assertIn("different backup set", joined)

    def test_all_unauditable_still_aborts(self) -> None:
        # The genuine "wrong password, nothing auditable" case must
        # still abort with keyset_failed (preserved via the new
        # per-cu path).
        from tests.fixtures import write_synthetic_backup
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_synthetic_backup(root, "right")
            result = self._run(root, "WRONG")
        self.assertEqual(result.aborted_reason, "keyset_failed")
        self.assertTrue(result.skipped_backup_sets)


if __name__ == "__main__":
    unittest.main()
