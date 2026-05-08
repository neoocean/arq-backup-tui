"""Tests for the state-file IPC layer that lets CLI processes
publish progress for TUI monitoring.

Covers the producer side (:class:`~arq_tui.runs.RunWriter`),
consumer side (:func:`~arq_tui.runs.enumerate_runs`,
:func:`~arq_tui.runs.mark_stale`), and the JSON round-trip of
:class:`~arq_tui.runs.RunRecord`.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from arq_tui.runs import (
    DEFAULT_FLUSH_INTERVAL_SEC,
    RunKind,
    RunRecord,
    RunStatus,
    RunWriter,
    enumerate_runs,
    gc_finished_runs,
    is_pid_alive,
    mark_stale,
    new_run_id,
    run_writer_context,
    state_file_path,
)


class RunRecordRoundTripTests(unittest.TestCase):
    def test_default_record_round_trips_through_json(self) -> None:
        rec = RunRecord(
            run_id="X1",
            kind=RunKind.BACKUP.value,
            plan_id="P", plan_name="home",
            started_at=1.0, pid=42, host="h",
        )
        text = rec.to_json()
        # JSON parses cleanly
        parsed = json.loads(text)
        self.assertEqual(parsed["schema_version"], 1)
        self.assertEqual(parsed["run_id"], "X1")
        # And the reconstructor matches.
        rec2 = RunRecord.from_json(text)
        self.assertEqual(rec2.run_id, "X1")
        self.assertEqual(rec2.plan_name, "home")
        self.assertEqual(rec2.pid, 42)

    def test_unknown_schema_version_rejected(self) -> None:
        bad = json.dumps({"schema_version": 999, "run_id": "X"})
        with self.assertRaises(ValueError):
            RunRecord.from_json(bad)

    def test_progress_subdict_hydrates(self) -> None:
        rec = RunRecord(run_id="X")
        rec.progress.files_total = 100
        rec.progress.files_done = 50
        rec.progress.bytes_total = 1024
        rec.progress.bytes_done = 512
        rec.progress.current_path = "/x/y"
        rec.progress.throughput_bps = 2048.0
        rec.progress.eta_sec = 0.25
        rec2 = RunRecord.from_json(rec.to_json())
        self.assertEqual(rec2.progress.files_total, 100)
        self.assertEqual(rec2.progress.bytes_done, 512)
        self.assertEqual(rec2.progress.current_path, "/x/y")
        self.assertAlmostEqual(rec2.progress.throughput_bps, 2048.0)


class RunWriterLifecycleTests(unittest.TestCase):
    def test_writer_writes_starting_then_completed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            rec = RunRecord(
                run_id=new_run_id(),
                kind=RunKind.BACKUP.value,
            )
            writer = RunWriter(rec, state_dir=sd)
            with writer as rw:
                # Inside the with-block, status should be running.
                on_disk = RunRecord.from_json(
                    state_file_path(rec.run_id, state_dir=sd).read_text(),
                )
                self.assertEqual(
                    on_disk.status, RunStatus.RUNNING.value,
                )
                self.assertGreater(on_disk.started_at, 0)
                self.assertEqual(on_disk.pid, os.getpid())
                rw.event("file_written", path="/x", size=10)
                rw.event("file_written", path="/y", size=20)
                # Force a flush so the assertion sees the events.
                rw._force_flush()
                on_disk = RunRecord.from_json(
                    state_file_path(rec.run_id, state_dir=sd).read_text(),
                )
                self.assertEqual(on_disk.progress.files_done, 2)
                self.assertEqual(on_disk.progress.bytes_done, 30)
            # After the with-block, status should be completed.
            on_disk = RunRecord.from_json(
                state_file_path(rec.run_id, state_dir=sd).read_text(),
            )
            self.assertEqual(
                on_disk.status, RunStatus.COMPLETED.value,
            )
            self.assertIsNotNone(on_disk.finished_at)

    def test_writer_failed_on_exception(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            rec = RunRecord(run_id=new_run_id())
            writer = RunWriter(rec, state_dir=sd)
            with self.assertRaises(RuntimeError):
                with writer:
                    raise RuntimeError("boom")
            on_disk = RunRecord.from_json(
                state_file_path(rec.run_id, state_dir=sd).read_text(),
            )
            self.assertEqual(on_disk.status, RunStatus.FAILED.value)
            self.assertIn("boom", on_disk.error)

    def test_writer_cancelled_on_keyboard_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            rec = RunRecord(run_id=new_run_id())
            writer = RunWriter(rec, state_dir=sd)
            with self.assertRaises(KeyboardInterrupt):
                with writer:
                    raise KeyboardInterrupt()
            on_disk = RunRecord.from_json(
                state_file_path(rec.run_id, state_dir=sd).read_text(),
            )
            self.assertEqual(
                on_disk.status, RunStatus.CANCELLED.value,
            )

    def test_event_updates_progress_for_known_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            rec = RunRecord(run_id=new_run_id())
            with RunWriter(rec, state_dir=sd) as rw:
                rw.event(
                    "restore_planned",
                    total_files=100, total_bytes=1024,
                )
                rw.event("file_restored", path="/a", size=100)
                rw.event("file_restored", path="/b", size=200)
                self.assertEqual(rw.record.progress.files_total, 100)
                self.assertEqual(rw.record.progress.bytes_total, 1024)
                self.assertEqual(rw.record.progress.files_done, 2)
                self.assertEqual(rw.record.progress.bytes_done, 300)
                self.assertEqual(rw.record.progress.current_path, "/b")

    def test_events_tail_capped_at_max(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            rec = RunRecord(run_id=new_run_id())
            with RunWriter(rec, state_dir=sd) as rw:
                for i in range(200):
                    rw.event("file_written", path=f"/{i}", size=1)
                # events_tail must be capped at EVENTS_TAIL_MAX (50).
                self.assertLessEqual(len(rw.record.events_tail), 50)


class EnumerateRunsTests(unittest.TestCase):
    def test_enumerate_returns_records_sorted_by_started_at(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            for i in range(3):
                rec = RunRecord(
                    run_id=f"R{i}",
                    started_at=time.time() - (3 - i),
                )
                with RunWriter(rec, state_dir=sd):
                    pass
            recs = enumerate_runs(state_dir=sd)
            self.assertEqual(len(recs), 3)
            self.assertEqual(
                [r.run_id for r in recs], ["R0", "R1", "R2"],
            )

    def test_enumerate_skips_malformed_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            sd.mkdir(exist_ok=True)
            (sd / "garbage.json").write_text("{ not json")
            (sd / "wrong-schema.json").write_text(
                json.dumps({"schema_version": 99}),
            )
            # And one valid record:
            with RunWriter(RunRecord(run_id="OK"), state_dir=sd):
                pass
            recs = enumerate_runs(state_dir=sd)
            self.assertEqual([r.run_id for r in recs], ["OK"])

    def test_enumerate_returns_empty_when_dir_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td) / "does-not-exist"
            self.assertEqual(enumerate_runs(state_dir=sd), [])


class StaleAndGCTests(unittest.TestCase):
    def test_mark_stale_for_dead_pid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            # Build a record on disk that *claims* to be running
            # but with a PID that's certainly not alive (max int 32
            # bit minus a small offset is unlikely to be a real pid).
            rec = RunRecord(
                run_id="STALE-R",
                status=RunStatus.RUNNING.value,
                pid=2**31 - 5,
                started_at=time.time(),
            )
            sd.mkdir(parents=True, exist_ok=True)
            (sd / f"{rec.run_id}.json").write_text(rec.to_json())
            # Sanity: PID is dead.
            self.assertFalse(is_pid_alive(rec.pid))
            mark_stale(rec, state_dir=sd)
            on_disk = RunRecord.from_json(
                (sd / f"{rec.run_id}.json").read_text(),
            )
            self.assertEqual(on_disk.status, RunStatus.STALE.value)
            self.assertIsNotNone(on_disk.finished_at)

    def test_mark_stale_no_op_for_live_pid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            rec = RunRecord(
                run_id="LIVE-R",
                status=RunStatus.RUNNING.value,
                pid=os.getpid(),
                started_at=time.time(),
            )
            mark_stale(rec, state_dir=sd)
            self.assertEqual(
                rec.status, RunStatus.RUNNING.value,
            )

    def test_gc_removes_old_terminal_records(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            sd.mkdir(parents=True, exist_ok=True)
            # Old completed (should GC).
            old = RunRecord(
                run_id="OLD",
                status=RunStatus.COMPLETED.value,
                started_at=time.time() - 60 * 86400,
                finished_at=time.time() - 59 * 86400,
            )
            (sd / f"{old.run_id}.json").write_text(old.to_json())
            # Recent completed (should not).
            new = RunRecord(
                run_id="NEW",
                status=RunStatus.COMPLETED.value,
                started_at=time.time() - 60,
                finished_at=time.time() - 30,
            )
            (sd / f"{new.run_id}.json").write_text(new.to_json())
            # Running (should never GC).
            run = RunRecord(
                run_id="RUN",
                status=RunStatus.RUNNING.value,
                started_at=time.time() - 60 * 86400,
                pid=os.getpid(),
            )
            (sd / f"{run.run_id}.json").write_text(run.to_json())

            removed = gc_finished_runs(
                state_dir=sd, older_than_sec=30 * 86400,
            )
            self.assertEqual(removed, 1)
            # OLD is gone, NEW + RUN remain.
            ids = sorted(
                p.stem for p in sd.iterdir() if p.suffix == ".json"
            )
            self.assertEqual(ids, ["NEW", "RUN"])


class RunWriterContextTests(unittest.TestCase):
    def test_state_file_override_uses_provided_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "my-run.json"
            with run_writer_context(
                kind=RunKind.BACKUP,
                plan_id="P", plan_name="N",
                state_file=sf,
            ) as rw:
                rw.event("file_written", path="/x", size=1)
            self.assertTrue(sf.is_file())
            on_disk = RunRecord.from_json(sf.read_text())
            self.assertEqual(on_disk.run_id, "my-run")
            self.assertEqual(on_disk.plan_id, "P")
            self.assertEqual(on_disk.plan_name, "N")
            self.assertEqual(on_disk.kind, RunKind.BACKUP.value)

    def test_state_file_must_end_in_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "my-run.txt"
            with self.assertRaises(ValueError):
                with run_writer_context(
                    kind=RunKind.BACKUP, state_file=sf,
                ) as rw:
                    pass


if __name__ == "__main__":
    unittest.main()
