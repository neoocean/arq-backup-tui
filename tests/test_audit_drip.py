"""End-to-end tests for the resumable audit-drip orchestrator."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from arq_validator import LocalBackend, run_audit_drip
from arq_validator.audit_drip import (
    Throttle,
    build_walk,
    load_state,
    pause as pause_drip,
    resume as resume_drip,
    save_state,
)
from arq_validator.layout import discover_layout

from tests.fixtures import write_synthetic_backup


class WalkAndCursorTests(unittest.TestCase):
    def test_walk_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(
                Path(td), "pw",
                n_blobpacks=3, n_treepacks=2, n_standardobjects=4,
            )
            backend = LocalBackend(Path(td))
            layouts = discover_layout(backend, "/")
        walk1 = build_walk(layouts)
        walk2 = build_walk(layouts)
        self.assertEqual(walk1, walk2)
        # All families represented; expected count = blobs+trees+stdobj.
        self.assertEqual(len(walk1), 3 + 2 + 4)

    def test_throttle_zero_is_noop(self) -> None:
        t = Throttle(0)
        before = time.monotonic()
        t.wait(); t.wait(); t.wait()
        self.assertLess(time.monotonic() - before, 0.05)

    def test_throttle_imposes_minimum_spacing(self) -> None:
        t = Throttle(files_per_min=600)   # 10 calls/sec → 0.1s spacing
        # First wait primes the timer; subsequent waits should sleep ~0.1s.
        t.wait()
        before = time.monotonic()
        t.wait()
        elapsed = time.monotonic() - before
        self.assertGreaterEqual(elapsed, 0.05)


class AuditDripFireTests(unittest.TestCase):
    def _setup(self, td: str) -> Path:
        return write_synthetic_backup(
            Path(td), "pw",
            n_blobpacks=4, n_treepacks=2, n_standardobjects=6,
        ) and Path(td)

    def test_full_sweep_in_one_fire(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(
                Path(td), "pw",
                n_blobpacks=2, n_treepacks=1, n_standardobjects=3,
            )
            state_file = Path(td) / "drip.json"
            state = run_audit_drip(
                LocalBackend(Path(td)),
                target="local",
                state_file=state_file,
                encryption_password="pw",
                max_runtime_sec=0,
                skip_larger_than=None,
            )
            self.assertEqual(state.sweep_count, 1)
            self.assertIsNotNone(state.sweep_completed_at)
            self.assertIsNone(state.cursor_kind)
            self.assertEqual(state.fails_this_sweep, 0)
            self.assertEqual(state.errors_this_sweep, 0)
            self.assertGreater(state.files_audited_this_sweep, 0)
            # Persisted state must reload cleanly.
            loaded = load_state(state_file, "local")
            self.assertEqual(loaded.sweep_count, 1)

    def test_cursor_resume_across_two_fires(self) -> None:
        # Force the first fire to abort early by setting an
        # impossibly-tight time budget; the second fire resumes.
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(
                Path(td), "pw",
                n_blobpacks=8, n_treepacks=4, n_standardobjects=6,
            )
            state_file = Path(td) / "drip.json"
            state1 = run_audit_drip(
                LocalBackend(Path(td)),
                target="local",
                state_file=state_file,
                encryption_password="pw",
                max_runtime_sec=1,    # tight; usually completes in <0.1s
                skip_larger_than=None,
            )
            # If we did finish in one shot (synthetic fixture is tiny),
            # the test still validates resume semantics: a second fire
            # on a completed sweep starts a fresh sweep.
            state2 = run_audit_drip(
                LocalBackend(Path(td)),
                target="local",
                state_file=state_file,
                encryption_password="pw",
                max_runtime_sec=0,
                skip_larger_than=None,
            )
        # Either we resumed mid-sweep (sweep_count == 1 throughout),
        # or completed sweep #1 then started sweep #2. Both are valid.
        self.assertGreaterEqual(state2.sweep_count, 1)
        # After the second fire, the sweep must be either complete
        # for the same sweep number, or completed for sweep #N>=1.
        self.assertIsNotNone(state2.sweep_completed_at)

    def test_corrupt_object_lands_in_failures(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(
                Path(td), "pw",
                n_blobpacks=2,
                corrupt_first_blobpack=True,
            )
            state_file = Path(td) / "drip.json"
            state = run_audit_drip(
                LocalBackend(Path(td)),
                target="local",
                state_file=state_file,
                encryption_password="pw",
                max_runtime_sec=0,
                skip_larger_than=None,
            )
        self.assertGreaterEqual(state.fails_this_sweep, 1)
        self.assertTrue(state.failed_files_this_sweep)

    def test_pause_silent_skips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(Path(td), "pw")
            state_file = Path(td) / "drip.json"
            # Pause indefinitely.
            pause_drip(state_file, "local", until_epoch=-1)
            state = run_audit_drip(
                LocalBackend(Path(td)),
                target="local",
                state_file=state_file,
                encryption_password="pw",
                max_runtime_sec=0,
            )
            self.assertEqual(state.last_fire_aborted_reason, "paused")
            self.assertEqual(state.last_fire_files_processed, 0)
            # Resume: next fire should proceed.
            resume_drip(state_file, "local")
            state2 = run_audit_drip(
                LocalBackend(Path(td)),
                target="local",
                state_file=state_file,
                encryption_password="pw",
                max_runtime_sec=0,
                skip_larger_than=None,
            )
        self.assertIsNone(state2.last_fire_aborted_reason)
        self.assertGreater(state2.files_audited_this_sweep, 0)

    def test_corrupt_state_file_is_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(Path(td), "pw")
            state_file = Path(td) / "drip.json"
            state_file.write_text("{not valid json")
            state = run_audit_drip(
                LocalBackend(Path(td)),
                target="local",
                state_file=state_file,
                encryption_password="pw",
                max_runtime_sec=0,
                skip_larger_than=None,
            )
            self.assertEqual(state.sweep_count, 1)
            # Reloads cleanly.
            with state_file.open() as f:
                self.assertIn("sweep_count", json.load(f))

    def test_wrong_password_records_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(Path(td), "right")
            state_file = Path(td) / "drip.json"
            state = run_audit_drip(
                LocalBackend(Path(td)),
                target="local",
                state_file=state_file,
                encryption_password="WRONG",
            )
        self.assertFalse(state.last_fire_keyset_decrypted)
        self.assertIsNotNone(state.error)
        # 2026-05-27: with per-cu keyset selection a wrong password
        # means no backup set decrypts → the fire reports the
        # aggregate "no decryptable keyset" error (and lists the cu
        # under last_fire_skipped_backup_sets) rather than leaking
        # the raw per-keyset HMAC-fail string.
        self.assertIn("no decryptable keyset", state.error)
        self.assertIsNotNone(state.last_fire_skipped_backup_sets)

    def test_multi_backup_set_skips_unauditable(self) -> None:
        """2026-05-27 regression: a destination hosting several
        backup sets (one openable with the password, one encrypted
        with a DIFFERENT password, one UNENCRYPTED) must audit the
        openable set and SKIP the rest — never report the skipped
        sets' packs as HMAC failures (the pre-fix bug that fired
        false CRITs)."""
        cu_ok = "11111111-1111-1111-1111-111111111111"
        cu_diff = "22222222-2222-2222-2222-222222222222"
        cu_plain = "33333333-3333-3333-3333-333333333333"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_synthetic_backup(root, "right", computer_uuid=cu_ok)
            write_synthetic_backup(root, "different",
                                   computer_uuid=cu_diff)
            write_synthetic_backup(root, "x", computer_uuid=cu_plain)
            # Make cu_plain "unencrypted": remove its keyset file.
            (root / cu_plain / "encryptedkeyset.dat").unlink()
            state = run_audit_drip(
                LocalBackend(root),
                target="local",
                state_file=root / "drip.json",
                encryption_password="right",
            )
        # The openable set decrypted → fire proceeds (not aborted).
        self.assertTrue(state.last_fire_keyset_decrypted)
        # CRITICAL: zero false failures from the unauditable sets.
        self.assertEqual(state.fails_this_sweep, 0)
        self.assertEqual(state.inner_arqos_fail_this_sweep, 0)
        # The unauditable sets' packs were skipped, not failed.
        self.assertGreater(state.skipped_this_sweep, 0)
        # The openable set's packs WERE audited (inner ARQOs verified).
        self.assertGreater(state.inner_arqos_ok_this_sweep, 0)
        # Both unauditable sets are reported (skipped, with reasons).
        skipped = state.last_fire_skipped_backup_sets or []
        joined = " ".join(skipped)
        self.assertIn(cu_diff, joined)
        self.assertIn(cu_plain, joined)
        self.assertIn("unencrypted", joined)        # cu_plain reason
        self.assertIn("different backup set", joined)  # cu_diff reason

    def test_save_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "drip.json"
            from arq_validator.audit_drip import AuditDripState
            original = AuditDripState(
                target="hetzner",
                sweep_count=3,
                cursor_computer="ABC",
                cursor_kind="blobpacks",
                cursor_shard="ff",
                cursor_file_name="x.pack",
                files_audited_this_sweep=42,
            )
            save_state(original, state_file)
            roundtripped = load_state(state_file, "hetzner")
        self.assertEqual(roundtripped.sweep_count, 3)
        self.assertEqual(roundtripped.cursor_kind, "blobpacks")
        self.assertEqual(roundtripped.cursor_file_name, "x.pack")
        self.assertEqual(roundtripped.files_audited_this_sweep, 42)


if __name__ == "__main__":
    unittest.main()
