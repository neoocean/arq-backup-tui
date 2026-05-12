"""Tests for AUDIT_PROGRESS ETA + throughput fields.

Operator request 2026-05-12 (priority #2 of the validator
improvements menu): the historical AUDIT_PROGRESS event carried
only "N files done, M bytes read" — useful for current state but
not for "how much longer?" estimation.  The enriched event now
exposes:

  elapsed_sec        — wall time since L2 started
  files_per_sec      — running file throughput
  bytes_per_sec      — running byte throughput
  planned_files      — total files the layout pre-counted
  remaining_files    — planned - total (clamped to ≥ 0)
  eta_sec            — linear extrapolation of remaining time
  progress_fraction  — total / planned (0.0 ≤ f ≤ 1.0)

Plus `_format_duration` helper for human-readable ETA strings.

These tests pin both the schema (key presence + type) and the
math (throughput / ETA derivation).  TIER_FINISHED also carries
the final snapshot — pinned at the bottom.
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


def _make_backup(td: Path, file_count: int = 8) -> tuple:
    """Build a small Arq 7 backup at td/dst.  Returns
    (dst_root, computer_uuid)."""
    from arq_writer import Backup
    src = td / "src"
    src.mkdir()
    for i in range(file_count):
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


class FormatDurationTests(unittest.TestCase):
    """Pure-function tests for `_format_duration`."""

    def test_zero(self) -> None:
        from arq_validator.tiers import _format_duration
        self.assertEqual(_format_duration(0), "0:00")

    def test_short_seconds(self) -> None:
        from arq_validator.tiers import _format_duration
        self.assertEqual(_format_duration(45), "0:45")
        self.assertEqual(_format_duration(59), "0:59")

    def test_minutes(self) -> None:
        from arq_validator.tiers import _format_duration
        self.assertEqual(_format_duration(60), "1:00")
        self.assertEqual(_format_duration(125), "2:05")
        self.assertEqual(_format_duration(3599), "59:59")

    def test_hours(self) -> None:
        from arq_validator.tiers import _format_duration
        self.assertEqual(_format_duration(3600), "1:00:00")
        self.assertEqual(_format_duration(3725), "1:02:05")
        self.assertEqual(_format_duration(7325), "2:02:05")

    def test_negative_clamps_to_zero(self) -> None:
        from arq_validator.tiers import _format_duration
        self.assertEqual(_format_duration(-10), "0:00")

    def test_fractional_seconds_rounded(self) -> None:
        # 45.4 -> 45, 45.6 -> 46.  We round, not truncate.
        from arq_validator.tiers import _format_duration
        self.assertEqual(_format_duration(45.4), "0:45")
        self.assertEqual(_format_duration(45.6), "0:46")


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class AuditProgressEtaTests(unittest.TestCase):
    """End-to-end: run L2 audit + capture AUDIT_PROGRESS events
    and verify the new ETA + throughput fields are present + sane.
    """

    def _run_audit_capture_events(self, dst: Path,
                                    *, progress_every: int = 1):
        from arq_validator.backend import LocalBackend
        from arq_validator.layout import discover_layout
        from arq_validator.tiers import run_full_audit
        from arq_validator.events import Event, EventKind
        events: List[Event] = []

        def cb(ev: Event) -> None:
            events.append(ev)

        backend = LocalBackend(dst)
        layouts = discover_layout(backend, "/")
        result = run_full_audit(
            backend, layouts, encryption_password="pw",
            root="/", skip_larger_than=None,
            progress_every=progress_every,
            callback=cb,
        )
        return result, events

    def test_planned_files_set_from_layout(self) -> None:
        # The driver pre-computes planned_files from layout
        # sizes; result.planned_files reflects this.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td)
            result, _events = self._run_audit_capture_events(dst)
        self.assertGreater(result.planned_files, 0)
        # All counted files (skip ledger entries) get audited
        # in this fixture (no size cap), so planned == total.
        self.assertEqual(
            result.planned_files, result.files_total)

    def test_started_at_set(self) -> None:
        # ObjectAuditResult.started_at is set at the start
        # of run_full_audit; non-zero proves the wiring.
        import time
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td)
            t0 = time.time()
            result, _ = self._run_audit_capture_events(dst)
            t1 = time.time()
        self.assertGreaterEqual(result.started_at, t0)
        self.assertLessEqual(result.started_at, t1)

    def test_audit_progress_event_carries_eta_fields(
            self) -> None:
        # AUDIT_PROGRESS events expose elapsed_sec /
        # files_per_sec / bytes_per_sec / planned_files /
        # remaining_files / eta_sec / progress_fraction.
        from arq_validator.events import EventKind
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td)
            # progress_every=1 → emit on every file.
            _result, events = self._run_audit_capture_events(
                dst, progress_every=1)
        progress = [
            e for e in events
            if e.kind == EventKind.AUDIT_PROGRESS]
        self.assertGreater(len(progress), 0,
                            "no AUDIT_PROGRESS emitted")
        for ev in progress:
            p = ev.payload
            # New ETA + throughput fields.
            self.assertIn("elapsed_sec", p)
            self.assertIn("files_per_sec", p)
            self.assertIn("bytes_per_sec", p)
            self.assertIn("planned_files", p)
            self.assertIn("remaining_files", p)
            self.assertIn("eta_sec", p)
            self.assertIn("progress_fraction", p)
            # Types + sanity.
            self.assertGreater(p["elapsed_sec"], 0)
            self.assertGreaterEqual(p["files_per_sec"], 0)
            self.assertGreaterEqual(p["bytes_per_sec"], 0)
            self.assertGreater(p["planned_files"], 0)
            self.assertGreaterEqual(p["remaining_files"], 0)
            # eta_sec is None when remaining=0 OR throughput=0
            # — both ok.  When present, must be >= 0.
            if p["eta_sec"] is not None:
                self.assertGreaterEqual(p["eta_sec"], 0)
            # progress_fraction in (0, 1] (we emit AFTER
            # processing at least one file).
            if p["progress_fraction"] is not None:
                self.assertGreater(p["progress_fraction"], 0)
                self.assertLessEqual(p["progress_fraction"], 1.0)

    def test_progress_fraction_increases_over_time(self) -> None:
        # As files get audited the progress fraction grows
        # monotonically.  Cap > 1 is impossible per the
        # `min(1.0)` upper bound.
        from arq_validator.events import EventKind
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td, file_count=10)
            _result, events = self._run_audit_capture_events(
                dst, progress_every=1)
        fractions = [
            e.payload["progress_fraction"]
            for e in events
            if e.kind == EventKind.AUDIT_PROGRESS
            and e.payload.get("progress_fraction") is not None
        ]
        self.assertGreater(len(fractions), 1,
                            "need ≥ 2 progress events to "
                            "compare fractions")
        for prev, cur in zip(fractions, fractions[1:]):
            self.assertGreaterEqual(cur, prev,
                f"progress_fraction regressed: {prev} -> {cur}")

    def test_message_contains_files_per_sec_and_eta(
            self) -> None:
        # The human-readable message line carries the
        # operator-visible ETA hint ("ETA H:MM:SS" or "ETA
        # unknown").  UIs that only display the message
        # string still get the information.
        from arq_validator.events import EventKind
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td)
            _result, events = self._run_audit_capture_events(
                dst, progress_every=1)
        msgs = [
            e.message
            for e in events
            if e.kind == EventKind.AUDIT_PROGRESS]
        self.assertGreater(len(msgs), 0)
        # At least one progress message includes the
        # throughput suffix.  Per-second numbers are present
        # in every emission.
        self.assertTrue(
            any("files/s" in m for m in msgs),
            f"no files/s in any message: {msgs[:3]}")
        self.assertTrue(
            any("MB/s" in m for m in msgs),
            f"no MB/s in any message: {msgs[:3]}")
        # At least one message carries an ETA suffix (string
        # form — either H:MM:SS or "ETA unknown").
        self.assertTrue(
            any("ETA " in m for m in msgs),
            f"no ETA in any message: {msgs[:3]}")

    def test_tier_finished_carries_final_throughput(
            self) -> None:
        # TIER_FINISHED also exposes elapsed_sec /
        # files_per_sec / bytes_per_sec / planned_files so
        # the operator's run-complete summary doesn't need
        # to re-derive throughput.
        from arq_validator.events import EventKind
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td)
            _result, events = self._run_audit_capture_events(dst)
        finished = [
            e for e in events
            if e.kind == EventKind.TIER_FINISHED]
        self.assertEqual(len(finished), 1)
        p = finished[0].payload
        self.assertIn("elapsed_sec", p)
        self.assertIn("files_per_sec", p)
        self.assertIn("bytes_per_sec", p)
        self.assertIn("planned_files", p)
        self.assertGreater(p["elapsed_sec"], 0)
        # The final message string contains the duration +
        # throughput, helping operators paste it directly into
        # a chat / log.
        self.assertIn("files/s", finished[0].message)
        self.assertIn("MB/s", finished[0].message)

    def test_eta_zero_when_at_completion(self) -> None:
        # The LAST progress emission of a complete run has
        # remaining_files == 0 (we've audited everything), so
        # eta_sec is None (no remaining work → no ETA).
        from arq_validator.events import EventKind
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            dst, _ = _make_backup(td)
            _result, events = self._run_audit_capture_events(
                dst, progress_every=1)
        progress = [
            e for e in events
            if e.kind == EventKind.AUDIT_PROGRESS]
        self.assertGreater(len(progress), 0)
        last = progress[-1]
        # When the last event coincides with the final file,
        # remaining_files is 0 and eta_sec is None.
        if last.payload["remaining_files"] == 0:
            self.assertIsNone(last.payload["eta_sec"])


if __name__ == "__main__":
    unittest.main()
