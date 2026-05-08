"""End-to-end tests against synthetic Arq backups.

Each test materializes a tiny Arq 7 tree on disk, hands it to the
high-level :func:`validate` entry point, and asserts on the result.
The test suite is the executable spec for the validator's behavior
in the absence of a real backup.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import List

from arq_validator import (
    Event,
    EventKind,
    LocalBackend,
    ValidationTier,
    validate,
)

from tests.fixtures import write_synthetic_backup


class RunnerHappyPathTests(unittest.TestCase):
    def test_dry_run_finds_layout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(Path(td), "pw")
            report = validate(
                LocalBackend(Path(td)),
                tier=ValidationTier.DRY_RUN,
            )
        self.assertIsNone(report.error)
        self.assertIsNotNone(report.layout)
        self.assertTrue(report.layout.layout_ok)
        self.assertIsNone(report.magic_check)

    def test_quick_runs_magic_check(self) -> None:
        events: List[Event] = []
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(
                Path(td), "pw",
                n_blobpacks=4, n_treepacks=2, n_standardobjects=6,
            )
            report = validate(
                LocalBackend(Path(td)),
                tier=ValidationTier.QUICK,
                sample_fraction=1.0,
                callback=events.append,
            )
        self.assertIsNotNone(report.magic_check)
        self.assertEqual(report.magic_check.fail, 0)
        self.assertGreater(report.magic_check.ok, 0)
        self.assertFalse(report.has_failures())
        # Lifecycle events should be present.
        kinds = {e.kind for e in events}
        self.assertIn(EventKind.RUN_STARTED, kinds)
        self.assertIn(EventKind.LAYOUT_DISCOVERED, kinds)
        self.assertIn(EventKind.RUN_FINISHED, kinds)

    def test_deep_decrypts_keyset_and_verifies_backuprecord(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(Path(td), "secret-password")
            report = validate(
                LocalBackend(Path(td)),
                tier=ValidationTier.DEEP,
                encryption_password="secret-password",
                sample_fraction=0,    # skip magic sweep for speed
            )
        self.assertIsNone(report.error)
        self.assertIsNotNone(report.backuprecord)
        self.assertTrue(report.backuprecord.keyset_decrypted)
        self.assertEqual(report.backuprecord.fail, 0)
        self.assertEqual(report.backuprecord.ok, 1)

    def test_audit_full_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(
                Path(td), "pw",
                n_blobpacks=2, n_treepacks=1, n_standardobjects=3,
            )
            report = validate(
                LocalBackend(Path(td)),
                tier=ValidationTier.AUDIT,
                encryption_password="pw",
                sample_fraction=0,
                audit_skip_larger_than=None,
            )
        self.assertIsNone(report.error)
        self.assertIsNotNone(report.audit)
        self.assertGreater(report.audit.files_total, 0)
        self.assertEqual(report.audit.files_fail, 0)
        self.assertEqual(report.audit.files_error, 0)
        self.assertFalse(report.has_failures())


class RunnerFailurePathTests(unittest.TestCase):
    def test_wrong_password(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(Path(td), "right-password")
            report = validate(
                LocalBackend(Path(td)),
                tier=ValidationTier.DEEP,
                encryption_password="WRONG",
                sample_fraction=0,
            )
        self.assertIsNotNone(report.backuprecord)
        self.assertFalse(report.backuprecord.keyset_decrypted)
        self.assertIn("HMAC", report.backuprecord.keyset_error or "")

    def test_corrupted_object_detected_in_audit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(
                Path(td), "pw",
                n_blobpacks=2,
                corrupt_first_blobpack=True,
            )
            report = validate(
                LocalBackend(Path(td)),
                tier=ValidationTier.AUDIT,
                encryption_password="pw",
                sample_fraction=0,
                audit_skip_larger_than=None,
            )
        self.assertIsNotNone(report.audit)
        self.assertGreaterEqual(report.audit.files_fail, 1)
        self.assertTrue(report.has_failures())

    def test_missing_password_for_deep(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            write_synthetic_backup(Path(td), "pw")
            report = validate(
                LocalBackend(Path(td)),
                tier=ValidationTier.DEEP,
                encryption_password=None,
            )
        self.assertIsNotNone(report.error)
        self.assertIn("encryption_password", report.error)

    def test_empty_root_layout_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            report = validate(
                LocalBackend(Path(td)),
                tier=ValidationTier.DRY_RUN,
            )
        self.assertIsNotNone(report.layout)
        self.assertFalse(report.layout.layout_ok)
        self.assertTrue(report.has_failures())


if __name__ == "__main__":
    unittest.main()
