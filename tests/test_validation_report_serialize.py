"""Tests for ValidationReport JSON serialization round-trip.

Operator request 2026-05-12 (priority #3 of the validator
improvements menu): ValidationReport had a ``to_dict()`` helper
but no ``to_json`` / ``from_dict`` / ``from_json`` — meaning the
report could not round-trip cleanly for archive / TUI re-display
/ external-monitor integration.

This CL adds the missing methods.  These tests pin:

  - to_json() produces parseable JSON
  - from_json() reconstructs a ValidationReport whose tier-
    blocks are typed dataclasses (not plain dicts)
  - Round-trip (report → json → report → json) is stable
  - None tier blocks survive round-trip (operator ran
    --tier quick → audit block is None → still None after
    re-parse)
  - Forward compat: unknown extra keys in the JSON are
    silently dropped by from_dict
  - Backward compat: missing fields fall back to the
    dataclass defaults
"""

from __future__ import annotations

import json
import unittest


class ValidationReportRoundTripTests(unittest.TestCase):

    def _make_full_report(self):
        from arq_validator.runner import ValidationReport
        from arq_validator.tiers import (
            LayoutResult, MagicCheckResult,
            BackupRecordResult, ObjectAuditResult,
        )
        r = ValidationReport(
            tier="audit", started_at=100.0, finished_at=234.5,
            root="/", backend_kind="LocalBackend",
            error=None,
        )
        r.layout = LayoutResult(
            layout_ok=True,
            computer_uuids=["ABC-123", "DEF-456"],
            blobpack_count=256, treepack_count=16,
            largeblobpack_count=2,
            standardobject_count=42,
            backup_folder_count=3,
            missing_keyset_for=[],
        )
        r.magic_check = MagicCheckResult(
            total=10, ok=10, fail=0,
            failures=[], sample_fraction=0.05,
        )
        r.backuprecord = BackupRecordResult(
            keyset_decrypted=True, keyset_error=None,
            total=3, ok=3, fail=0, failures=[],
        )
        r.audit = ObjectAuditResult(
            files_total=100, files_ok=98,
            files_fail=1, files_error=1,
            files_skipped=0,
            files_skipped_by_ledger=0,
            inner_arqos_total=120, inner_arqos_ok=119,
            inner_arqos_fail=1,
            bytes_read=12345678,
            failures=[
                {"computer": "ABC-123", "kind": "blobpacks",
                 "shard": "0a", "file_name": "broken.pack",
                 "error": "1/1 inner ARQO HMAC mismatch"},
                {"computer": "ABC-123", "kind": "treepacks",
                 "shard": "1f", "file_name": "missing.pack",
                 "error": "stat: FileNotFoundError"},
            ],
            aborted_reason=None,
            started_at=100.0,
            planned_files=100,
        )
        return r

    # ----- to_json basic output -----

    def test_to_json_default_indent_pretty(self) -> None:
        # to_json() default is indent=2 → multi-line, pretty.
        r = self._make_full_report()
        out = r.to_json()
        self.assertIn("\n", out)
        # Parseable.
        parsed = json.loads(out)
        self.assertEqual(parsed["tier"], "audit")

    def test_to_json_compact(self) -> None:
        # indent=None → compact single-line form.
        r = self._make_full_report()
        out = r.to_json(indent=None)
        self.assertNotIn("\n", out)
        self.assertEqual(json.loads(out)["tier"], "audit")

    def test_to_json_drops_none_tier_blocks(self) -> None:
        # to_dict (which to_json calls) filters None top-
        # level keys.  Operator running --tier quick has
        # audit=None; it shouldn't appear in JSON.
        from arq_validator.runner import ValidationReport
        r = ValidationReport(tier="quick", root="/",
                              backend_kind="LocalBackend")
        out = json.loads(r.to_json())
        self.assertNotIn("layout", out)
        self.assertNotIn("audit", out)
        self.assertEqual(out["tier"], "quick")

    # ----- from_json + from_dict reconstruction -----

    def test_from_json_round_trip(self) -> None:
        # Full report → JSON → report → fields equal.
        from arq_validator.runner import ValidationReport
        from arq_validator.tiers import (
            LayoutResult, MagicCheckResult,
            BackupRecordResult, ObjectAuditResult,
        )
        r = self._make_full_report()
        out = r.to_json()
        r2 = ValidationReport.from_json(out)
        self.assertEqual(r2.tier, r.tier)
        self.assertEqual(r2.started_at, r.started_at)
        self.assertEqual(r2.finished_at, r.finished_at)
        # Tier blocks are properly typed (not plain dicts).
        self.assertIsInstance(r2.layout, LayoutResult)
        self.assertIsInstance(r2.magic_check, MagicCheckResult)
        self.assertIsInstance(
            r2.backuprecord, BackupRecordResult)
        self.assertIsInstance(r2.audit, ObjectAuditResult)
        # Nested fields preserved.
        self.assertEqual(
            r2.layout.computer_uuids,
            r.layout.computer_uuids)
        self.assertEqual(
            r2.audit.failures, r.audit.failures)
        self.assertEqual(
            r2.audit.planned_files, r.audit.planned_files)

    def test_from_json_double_trip_idempotent(self) -> None:
        # report → json → report → json → should match the
        # first json byte-for-byte.
        r = self._make_full_report()
        out1 = r.to_json()
        r2 = type(r).from_json(out1)
        out2 = r2.to_json()
        self.assertEqual(out1, out2)

    def test_from_dict_none_tier_blocks_preserved(self) -> None:
        # Operator ran --tier quick → audit/backuprecord None.
        # Round-trip preserves the None signal.
        from arq_validator.runner import ValidationReport
        r = ValidationReport(tier="quick", root="/",
                              backend_kind="LocalBackend")
        out = r.to_json()
        r2 = ValidationReport.from_json(out)
        self.assertIsNone(r2.layout)
        self.assertIsNone(r2.audit)
        self.assertIsNone(r2.backuprecord)
        self.assertIsNone(r2.magic_check)

    # ----- forward / backward compat -----

    def test_from_dict_ignores_unknown_top_level_keys(
            self) -> None:
        # A newer version's to_dict might add fields; older
        # from_dict must not crash.
        from arq_validator.runner import ValidationReport
        d = {
            "tier": "quick",
            "root": "/",
            "backend_kind": "LocalBackend",
            "future_field_x": 42,
            "future_field_y": "hello",
        }
        r = ValidationReport.from_dict(d)
        self.assertEqual(r.tier, "quick")

    def test_from_dict_ignores_unknown_nested_keys(
            self) -> None:
        # Same forward-compat at the tier-block level.
        from arq_validator.runner import ValidationReport
        d = {
            "tier": "deep", "root": "/",
            "backend_kind": "LocalBackend",
            "layout": {
                "layout_ok": True,
                "computer_uuids": ["X-1"],
                "future_nested_key": "ignored",
            },
        }
        r = ValidationReport.from_dict(d)
        self.assertEqual(r.layout.layout_ok, True)
        self.assertEqual(
            r.layout.computer_uuids, ["X-1"])

    def test_from_dict_missing_fields_use_defaults(
            self) -> None:
        # Older JSON missing a newer field should not crash
        # — the dataclass default kicks in.  E.g.,
        # planned_files was added with the ETA-CL; old JSON
        # without that key should still parse.
        from arq_validator.runner import ValidationReport
        d = {
            "tier": "audit", "root": "/",
            "backend_kind": "LocalBackend",
            "audit": {
                "files_total": 5, "files_ok": 5,
                # planned_files / started_at absent
            },
        }
        r = ValidationReport.from_dict(d)
        self.assertEqual(r.audit.files_total, 5)
        # Defaults applied for absent fields.
        self.assertEqual(r.audit.planned_files, 0)
        self.assertEqual(r.audit.started_at, 0.0)

    # ----- behavioral helpers preserved through round-trip -----

    def test_has_failures_after_round_trip(self) -> None:
        # Behavioral methods (has_failures, elapsed_sec) must
        # still work on the reconstructed object.
        from arq_validator.runner import ValidationReport
        r = self._make_full_report()
        r2 = ValidationReport.from_json(r.to_json())
        self.assertTrue(r.has_failures())
        self.assertTrue(r2.has_failures())
        self.assertAlmostEqual(
            r2.elapsed_sec, r.elapsed_sec)


class DataclassFromDictHelperTests(unittest.TestCase):
    """Direct tests for the internal _dataclass_from_dict
    helper (used by from_dict for each tier block).
    """

    def test_unknown_keys_dropped(self) -> None:
        from arq_validator.runner import _dataclass_from_dict
        from arq_validator.tiers import LayoutResult
        out = _dataclass_from_dict(LayoutResult, {
            "layout_ok": True,
            "computer_uuids": ["A"],
            "totally_made_up_field": 999,
        })
        self.assertEqual(out.layout_ok, True)
        self.assertEqual(out.computer_uuids, ["A"])

    def test_missing_keys_use_dataclass_defaults(
            self) -> None:
        from arq_validator.runner import _dataclass_from_dict
        from arq_validator.tiers import ObjectAuditResult
        out = _dataclass_from_dict(ObjectAuditResult, {
            "files_total": 10, "files_ok": 8,
        })
        self.assertEqual(out.files_total, 10)
        self.assertEqual(out.files_ok, 8)
        # Defaults for absent fields.
        self.assertEqual(out.files_fail, 0)
        self.assertEqual(out.failures, [])
        self.assertEqual(out.bytes_read, 0)


if __name__ == "__main__":
    unittest.main()
