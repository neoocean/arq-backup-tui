"""Tests for ``arq_writer.json_configs`` template emit.

These pin the JSON sidecar shape against the schema diff observed
on the operator's real Arq.app v8 destination
(``docs/COMPAT-VERIFICATION.md`` §2.7.1). Each missing or
divergent key surfaced there gets a focused test here so we don't
silently regress.
"""

from __future__ import annotations

import unittest

from arq_writer.json_configs import build_backupfolders_json


class BackupFoldersIndexTests(unittest.TestCase):
    """``backupfolders.json`` keys must match the Arq.app v8 set."""

    EXPECTED_KEYS = frozenset({
        "standardObjectDirs",
        "standardIAObjectDirs",
        "onezoneIAObjectDirs",
        "s3GlacierObjectDirs",
        "s3GlacierIRObjectDirs",
        "s3DeepArchiveObjectDirs",
    })

    def test_emits_all_six_storage_class_slots(self) -> None:
        out = build_backupfolders_json("CU-1234")
        self.assertEqual(set(out.keys()), self.EXPECTED_KEYS)

    def test_includes_s3GlacierIRObjectDirs(self) -> None:
        # Regression for the 2026-05-10 schema diff: this key was
        # missing from our writer's output but present (as []) in
        # Arq.app v8's emit. Pin it as an empty list — same shape
        # Arq.app uses for unused storage-class slots.
        out = build_backupfolders_json("CU-1234")
        self.assertIn("s3GlacierIRObjectDirs", out)
        self.assertEqual(out["s3GlacierIRObjectDirs"], [])

    def test_storage_class_slots_are_lists(self) -> None:
        out = build_backupfolders_json("CU-1234")
        for k in self.EXPECTED_KEYS:
            self.assertIsInstance(
                out[k], list, msg=f"{k} should be a list",
            )

    def test_standard_dir_threads_through_computer_uuid(self) -> None:
        # Sanity: the only slot writes use is standardObjectDirs.
        # Make sure the UUID is wired in and the others stay empty.
        out = build_backupfolders_json("CU-WIRED-IN")
        self.assertEqual(
            out["standardObjectDirs"],
            ["/CU-WIRED-IN/standardobjects"],
        )
        for k in self.EXPECTED_KEYS - {"standardObjectDirs"}:
            self.assertEqual(out[k], [], msg=k)


if __name__ == "__main__":
    unittest.main()
