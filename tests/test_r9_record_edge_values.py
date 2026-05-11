"""R9 — BackupRecord top-level edge-value pins.

Round 7 R5 confirmed key-name parity (20/20 top-level keys
match real Arq.app v8). R9 locks the typical VALUE set our
writer emits for the edge-defaulted fields against what real
Arq.app v8 emits — so a future writer change that silently
flips a default would surface here rather than only in
operator-eyeball-the-record reviews.

Sampled 2026-05-11 against ``/Volumes/arqbackup1`` v3 record
``7191376.backuprecord`` (the most common v3 default shape):

  archived: False
  copiedFromCommit: False
  copiedFromSnapshot: False
  backupRecordErrors: []
  isComplete: True
  storageClass: 'STANDARD'
  computerOSType: 1  (macOS)
  diskIdentifier: 'ROOT'
  volumeName: 'Macintosh HD'

(``volumeName`` is operator-set; pins reflect the default our
writer emits when no override is passed.)
"""

from __future__ import annotations

import unittest


class R9_RecordEdgeValueDefaultsTests(unittest.TestCase):
    """``build_backuprecord_dict`` defaults match real Arq.app
    v8's per-record emit values for the edge fields R7-R5 did
    not lock at the value level."""

    def _build(self, **overrides):
        from arq_writer.backuprecord import (
            build_backuprecord_dict,
        )
        from arq_writer.types import TreeNode, BlobLoc
        kwargs = dict(
            backup_folder_uuid="00000000-0000-0000-0000-000000000000",
            backup_plan_uuid="11111111-1111-1111-1111-111111111111",
            backup_plan_dict={},
            root_node=TreeNode(
                treeBlobLoc=BlobLoc(blobIdentifier="aa" * 32),
            ),
            local_path="/x",
            local_mount_point="/",
            volume_name="Macintosh HD",
            disk_identifier="ROOT",
        )
        kwargs.update(overrides)
        return build_backuprecord_dict(**kwargs)

    def test_archived_default_is_false(self) -> None:
        rec = self._build()
        self.assertEqual(rec["archived"], False)
        self.assertIs(type(rec["archived"]), bool)

    def test_copied_from_commit_default_is_false(self) -> None:
        rec = self._build()
        self.assertEqual(rec["copiedFromCommit"], False)
        self.assertIs(type(rec["copiedFromCommit"]), bool)

    def test_copied_from_snapshot_default_is_false(self) -> None:
        rec = self._build()
        self.assertEqual(rec["copiedFromSnapshot"], False)
        self.assertIs(type(rec["copiedFromSnapshot"]), bool)

    def test_backup_record_errors_default_is_empty_list(
        self,
    ) -> None:
        rec = self._build()
        self.assertEqual(rec["backupRecordErrors"], [])
        self.assertIs(type(rec["backupRecordErrors"]), list)

    def test_is_complete_default_is_true(self) -> None:
        """Fresh backup is complete by default; the
        is_complete=False emit path is exercised separately
        (E5-new)."""
        rec = self._build()
        self.assertEqual(rec["isComplete"], True)
        self.assertIs(type(rec["isComplete"]), bool)

    def test_storage_class_default_is_STANDARD(self) -> None:
        rec = self._build()
        self.assertEqual(rec["storageClass"], "STANDARD")
        self.assertIs(type(rec["storageClass"]), str)

    def test_computer_os_type_default_is_macos(self) -> None:
        """Arq.app v8 encodes macOS as ``computerOSType=1``."""
        rec = self._build()
        self.assertEqual(rec["computerOSType"], 1)
        self.assertIs(type(rec["computerOSType"]), int)

    def test_disk_identifier_round_trip(self) -> None:
        rec = self._build(disk_identifier="ROOT")
        self.assertEqual(rec["diskIdentifier"], "ROOT")
        # Operator can override.
        rec2 = self._build(disk_identifier="custom-disk")
        self.assertEqual(rec2["diskIdentifier"], "custom-disk")

    def test_volume_name_round_trip(self) -> None:
        rec = self._build(volume_name="Macintosh HD")
        self.assertEqual(rec["volumeName"], "Macintosh HD")
        rec2 = self._build(volume_name="External SSD")
        self.assertEqual(rec2["volumeName"], "External SSD")

    def test_backup_record_errors_round_trip(self) -> None:
        """Operator-supplied errors round-trip as a list of dicts."""
        rec = self._build(backup_record_errors=[
            {"localPath": "/x/y", "errorMessage": "EACCES",
             "pathIsDirectory": False},
        ])
        self.assertEqual(len(rec["backupRecordErrors"]), 1)
        self.assertEqual(
            rec["backupRecordErrors"][0]["localPath"], "/x/y",
        )
        self.assertEqual(
            rec["backupRecordErrors"][0]["errorMessage"], "EACCES",
        )

    def test_v4_record_adds_nodeTreeVersion(self) -> None:
        """When tree_version=4 is used, the record's
        ``nodeTreeVersion: 4`` lands at the top level — pinning
        this against accidental removal in a refactor."""
        rec = self._build(node_tree_version=4)
        self.assertEqual(rec["nodeTreeVersion"], 4)
        self.assertEqual(rec["version"], 101)

    def test_v3_record_omits_nodeTreeVersion(self) -> None:
        """v3 records (the v100 default) have NO nodeTreeVersion
        key — real Arq.app v8 emit pattern."""
        rec = self._build()
        self.assertEqual(rec["version"], 100)
        self.assertNotIn("nodeTreeVersion", rec)

    def test_top_level_key_count_after_round_9(self) -> None:
        """20 keys for v4 record / 19 for v3. Matches real
        Arq.app v8 sample (R5 schema parity)."""
        v3 = self._build()
        self.assertEqual(len(v3), 19)
        v4 = self._build(node_tree_version=4)
        self.assertEqual(len(v4), 20)
        # And the extra key is exactly nodeTreeVersion.
        self.assertEqual(
            set(v4.keys()) - set(v3.keys()),
            {"nodeTreeVersion"},
        )


if __name__ == "__main__":
    unittest.main()
