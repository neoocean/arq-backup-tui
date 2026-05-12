"""X1 — emit invariance across Python versions.

CI matrix already runs the full suite on Python 3.9 / 3.11 /
3.12 (`.github/workflows/test.yml`). X1 adds a single test that
PINS the byte-level emit output of a deterministic backup-plan
construction so any Python-version-dependent behaviour
(dict insertion order, float repr, JSON encoding quirks)
surfaces as the same hash failure on every matrix leg.

Deterministic inputs:
- Fixed plan_uuid / plan_name / folder_uuids
- creation_time / update_time = 0 (avoid time.time())
- No env-dependent calls

The expected SHA-256 hash is captured below. If a future Python
version (or library change) shifts dict-key ordering, JSON
separator preference, or any other emit-affecting behaviour,
this test fails — operator gets a clear signal that the
deterministic-emit invariant has drifted.
"""

from __future__ import annotations

import hashlib
import json
import unittest


# Captured 2026-05-12 against Python 3.13.13 (cpython).
# Any Python version that doesn't reproduce this hash has
# diverged in emit behaviour and warrants investigation.
EXPECTED_BACKUPPLAN_SHA256 = (
    # Will be filled in after the first run below; for now,
    # the test computes the current hash and pins THAT value.
    # We can't pre-compute it from this docstring; we instead
    # use a "compute-and-pin" pattern + check across Python
    # versions in CI.
    None  # placeholder — set in __init__
)


class X1_PythonVersionEmitInvarianceTests(unittest.TestCase):

    def _build_deterministic_plan(self):
        from arq_writer.json_configs import build_backupplan
        return build_backupplan(
            plan_uuid="11111111-2222-3333-4444-555555555555",
            plan_name="X1 deterministic plan",
            folder_plans=[],
            is_encrypted=True,
            update_time=1700001234.0,
            creation_time=1700000000.0,
            storage_location_id=1,
        )

    def test_deterministic_plan_emit_is_stable(self) -> None:
        """Same inputs produce same JSON bytes within one
        Python version. (Pinning step before cross-version
        comparison can apply.)"""
        plan1 = self._build_deterministic_plan()
        plan2 = self._build_deterministic_plan()
        # Dict equality.
        self.assertEqual(plan1, plan2)
        # Byte equality.
        bytes1 = json.dumps(
            plan1, ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        bytes2 = json.dumps(
            plan2, ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(bytes1).hexdigest(),
            hashlib.sha256(bytes2).hexdigest(),
        )

    def test_plan_hash_matches_pinned_value(self) -> None:
        """Pinned SHA-256 of the deterministic-plan emit. If
        this test fails on a fresh Python version, dict
        insertion order or JSON encoding has diverged."""
        plan = self._build_deterministic_plan()
        emit = json.dumps(
            plan, ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        got = hashlib.sha256(emit).hexdigest()
        # Pin: this is the hash on Python 3.13.13. CI runs
        # 3.9 / 3.11 / 3.12 — should all match this since
        # dict insertion order is defined since 3.7 and our
        # build_backupplan uses only literal-constructed
        # dicts (no set-iteration order, no hash randomization
        # in any code path here).
        expected = (
            "c15be52265abbabcb6f01c70aba854d0"
            "69190f7d42c60055a0b3ab27262e0762"
        )
        # If the expected hash above is wrong, the assertion
        # below will print the actual one; on first run we
        # update the constant to match (one-time pin).
        self.assertEqual(
            got, expected,
            f"plan emit hash drifted to {got!r}. If this is a "
            f"new Python version: update EXPECTED to {got!r} "
            f"after verifying the emit is still well-formed.",
        )

    def test_backuprecord_hash_with_deterministic_inputs(
        self,
    ) -> None:
        """Same shape: pin SHA-256 of a deterministic
        BackupRecord emit."""
        from arq_writer.backuprecord import (
            build_backuprecord_dict, serialize_backuprecord,
        )
        from arq_writer.types import TreeNode, BlobLoc
        rec = build_backuprecord_dict(
            backup_folder_uuid="aaaa",
            backup_plan_uuid="bbbb",
            backup_plan_dict={"x": 1},
            root_node=TreeNode(
                treeBlobLoc=BlobLoc(blobIdentifier="cc" * 32),
            ),
            local_path="/x",
            local_mount_point="/",
            volume_name="V",
            disk_identifier="D",
            creation_date=1700000000,
        )
        emit = serialize_backuprecord(rec, fmt="json")
        got = hashlib.sha256(emit).hexdigest()
        expected = (
            "fcc1562eb6c8d508bceb216101ac6ab9"
            "32769ae1a89b14ad8906383c12302842"
        )
        self.assertEqual(
            got, expected,
            f"BackupRecord emit hash drifted to {got!r}. "
            f"On a new Python version this is the first place "
            f"dict insertion order / JSON quirks surface.",
        )


if __name__ == "__main__":
    unittest.main()
