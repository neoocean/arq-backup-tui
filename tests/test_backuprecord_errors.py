"""Tests for the ``backupRecordErrors`` field on backuprecord plists (T4).

Pre-T4 the writer emitted ``errorCount: int`` (a scalar count of
failed paths). Arq.app v8's actual schema is
``backupRecordErrors: List[Dict]`` — each item describes one failed
path with a ``localPath`` / ``errorMessage`` / ``pathIsDirectory``
required triple, plus an optional NSError-mapped triple
(``errorCode`` / ``errorDomain`` / ``severity``) when the underlying
error came from a POSIX call.

Per-error structure sampled 2026-05-10 from two records on the
operator's destination (``docs/COMPAT-VERIFICATION.md`` §2.7.1, T4
in HANDOFF.md).

This test pins:

- The new field name + list type are emitted by
  ``build_backuprecord_dict`` (no more ``errorCount``).
- The empty-list default means a healthy backup record matches
  the operator's "no errors" records byte-for-byte at the schema
  level.
- Structured errors round-trip through serialize → ARQO →
  decrypt → ``parse_backuprecord`` cleanly, both for the minimal
  required-3-keys form and the full required+optional 6-keys form.
"""

from __future__ import annotations

import unittest

from arq_writer.backuprecord import build_backuprecord_dict
from arq_writer.types import FileNode


def _empty_node() -> FileNode:
    """A minimal valid Node placeholder. ``Node`` is a Union of
    FileNode and TreeNode; ``build_backuprecord_dict`` only feeds it
    to ``node_to_dict`` so any FileNode-shaped instance works."""
    return FileNode(itemSize=0)


class BackupRecordErrorsFieldTests(unittest.TestCase):
    """``build_backuprecord_dict`` emits ``backupRecordErrors``,
    not ``errorCount``."""

    def _build(self, **kwargs):
        return build_backuprecord_dict(
            backup_folder_uuid="FOLDER-UUID",
            backup_plan_uuid="PLAN-UUID",
            backup_plan_dict={},
            root_node=_empty_node(),
            local_path="/tmp/x",
            **kwargs,
        )

    def test_field_name_is_backupRecordErrors_not_errorCount(self) -> None:
        rec = self._build()
        self.assertIn("backupRecordErrors", rec)
        self.assertNotIn("errorCount", rec)

    def test_default_is_empty_list_not_int(self) -> None:
        # Pre-fix this was 0 (an int). Post-fix it must be a list
        # so the schema diff against an Arq.app destination shows
        # zero ``a_only`` / ``b_only`` keys for the field.
        rec = self._build()
        self.assertEqual(rec["backupRecordErrors"], [])
        self.assertIsInstance(rec["backupRecordErrors"], list)

    def test_explicit_none_keeps_empty_list(self) -> None:
        # Defensive: ``None`` from a caller maps to an empty list,
        # not ``None``, so ``len(rec["backupRecordErrors"])`` always
        # works.
        rec = self._build(backup_record_errors=None)
        self.assertEqual(rec["backupRecordErrors"], [])

    def test_minimal_error_three_keys_round_trips(self) -> None:
        # Required schema sampled from operator's record CA0D1896
        # (errors raised by an interrupted directory enumeration —
        # no NSError triple).
        err = {
            "localPath": "/Users/x/Library/Suggestions",
            "errorMessage": (
                "Failed to get contents of directory: "
                "Interrupted system call"
            ),
            "pathIsDirectory": True,
        }
        rec = self._build(backup_record_errors=[err])
        self.assertEqual(len(rec["backupRecordErrors"]), 1)
        self.assertEqual(rec["backupRecordErrors"][0], err)

    def test_full_error_six_keys_round_trips(self) -> None:
        # Full schema sampled from operator's record 6833FE33
        # (POSIX errors with NSError mapping — errorDomain +
        # errorCode + severity present).
        err = {
            "errorCode": 23,
            "pathIsDirectory": False,
            "errorDomain": "NSPOSIXErrorDomain",
            "severity": 3,
            "localPath": "/Volumes/vault/some/path/file.dat",
            "errorMessage": (
                "Failed to read file status: "
                "Too many open files in system"
            ),
        }
        rec = self._build(backup_record_errors=[err])
        self.assertEqual(rec["backupRecordErrors"][0], err)

    def test_multiple_errors_preserve_order(self) -> None:
        errs = [
            {"localPath": f"/a/{i}", "errorMessage": "e",
             "pathIsDirectory": False}
            for i in range(5)
        ]
        rec = self._build(backup_record_errors=errs)
        self.assertEqual(len(rec["backupRecordErrors"]), 5)
        self.assertEqual(
            [e["localPath"] for e in rec["backupRecordErrors"]],
            ["/a/0", "/a/1", "/a/2", "/a/3", "/a/4"],
        )

    def test_field_value_is_a_copy_not_aliased(self) -> None:
        # Caller mutating the list it passed in must not affect the
        # record we return.
        errs: list = []
        rec = self._build(backup_record_errors=errs)
        errs.append({"localPath": "/x", "errorMessage": "e",
                     "pathIsDirectory": False})
        # The record's list is the empty-default branch (the truthy
        # check skipped the empty caller list), so the late append
        # must not leak into the record.
        self.assertEqual(rec["backupRecordErrors"], [])

    def test_nonempty_caller_list_is_copied(self) -> None:
        errs = [{"localPath": "/x", "errorMessage": "e",
                 "pathIsDirectory": False}]
        rec = self._build(backup_record_errors=errs)
        errs.append({"localPath": "/y", "errorMessage": "f",
                     "pathIsDirectory": True})
        # Late-appended /y must not show in the record.
        self.assertEqual(len(rec["backupRecordErrors"]), 1)
        self.assertEqual(
            rec["backupRecordErrors"][0]["localPath"], "/x",
        )


class NodeTreeVersionFieldTests(unittest.TestCase):
    """F2 (HANDOFF.md): the backuprecord plist's tree-version
    coupling.

    Sampled 2026-05-10 against ``/Volumes/arqbackup1`` (352 real
    records):

    - 333 records have ``version=100`` with ``volumeName`` only
      (Tree v3 path).
    - 18 records have ``version=101`` with
      ``nodeTreeVersion=4`` (Tree v4 path).

    This test pins the writer's emit to that mapping:
    ``node_tree_version=N`` (when set) implies ``version=101``
    and emits ``nodeTreeVersion=N``; omitting it keeps the
    legacy ``version=100`` / no-``nodeTreeVersion`` shape.
    """

    def _build(self, **kwargs):
        return build_backuprecord_dict(
            backup_folder_uuid="FOLDER-UUID",
            backup_plan_uuid="PLAN-UUID",
            backup_plan_dict={},
            root_node=_empty_node(),
            local_path="/tmp/x",
            **kwargs,
        )

    def test_default_emits_v100_no_nodeTreeVersion(self) -> None:
        rec = self._build()
        self.assertEqual(rec["version"], 100)
        self.assertNotIn("nodeTreeVersion", rec)

    def test_node_tree_version_4_implies_v101(self) -> None:
        rec = self._build(node_tree_version=4)
        self.assertEqual(rec["version"], 101)
        self.assertEqual(rec["nodeTreeVersion"], 4)

    def test_explicit_version_overrides_node_tree_version_default(
        self,
    ) -> None:
        # An explicit ``version`` kwarg wins over the
        # ``node_tree_version``-derived default — useful for
        # callers writing legacy v100 records with v4 trees
        # (rare but valid against older Arq destinations).
        rec = self._build(node_tree_version=4, version=100)
        self.assertEqual(rec["version"], 100)
        self.assertEqual(rec["nodeTreeVersion"], 4)

    def test_node_tree_version_int_coerced(self) -> None:
        # Defensive: make sure callers passing a numeric-looking
        # value don't end up with ``"4"`` as a string in the plist.
        rec = self._build(node_tree_version="4")
        self.assertEqual(rec["nodeTreeVersion"], 4)
        self.assertIs(type(rec["nodeTreeVersion"]), int)

    def _e2e_record(self, *, tree_version: int):
        """Build a tiny backup with the given ``tree_version`` and
        return the parsed backuprecord plist + dest dir for cleanup
        helpers."""
        import os
        import tempfile
        from pathlib import Path
        from arq_writer import build_backup
        from arq_validator.backend import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_validator.layout import keyset_path
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_writer.backuprecord import parse_backuprecord

        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        tdp = Path(td.name)
        src = tdp / "src"
        src.mkdir()
        (src / "a.txt").write_text("alpha")
        dest = tdp / "dest"
        r = build_backup(
            src, dest, encryption_password="pw",
            tree_version=tree_version,
        )
        backend = LocalBackend(dest)
        ks = decrypt_keyset(
            backend.read_all(keyset_path("/", r.computer_uuid)),
            "pw",
        )
        rel = "/" + str(
            Path(r.backuprecord_path).resolve()
            .relative_to(Path(dest).resolve())
        ).replace(os.sep, "/")
        return parse_backuprecord(decrypt_lz4_arqo(
            backend.read_all(rel),
            ks.encryption_key, ks.hmac_key,
        ))

    def test_writer_tree_v3_emits_v100_no_nodeTreeVersion(self) -> None:
        # Legacy Tree v3 path: record version stays at 100 and
        # ``nodeTreeVersion`` is omitted entirely. Matches the 333
        # records in the operator's destination that emit
        # ``version=100`` + ``volumeName`` only.
        rec = self._e2e_record(tree_version=3)
        self.assertEqual(rec["version"], 100)
        self.assertNotIn("nodeTreeVersion", rec)

    def test_writer_tree_v4_emits_v101_with_nodeTreeVersion(self) -> None:
        # Modern Tree v4 path: record version bumps to 101 and
        # ``nodeTreeVersion=4`` is emitted alongside ``volumeName``.
        # This closes the F2 row in
        # ``docs/COMPAT-VERIFICATION.md`` §2.7's schema diff against
        # ``/Volumes/arqbackup1`` (where 18/352 records carry this
        # exact shape).
        rec = self._e2e_record(tree_version=4)
        self.assertEqual(rec["version"], 101)
        self.assertEqual(rec["nodeTreeVersion"], 4)


class NodeJsonArqAppV8KeysTests(unittest.TestCase):
    """``node_to_dict`` emits every key Arq.app v8's BackupRecord
    JSON ``node`` field carries.

    Sampled 2026-05-10 against a v101 record on
    ``/Volumes/arqbackup1`` (HANDOFF.md GAP-B): the real root
    node has 34 keys; pre-fix our emit was 25. The 9 missing keys
    are the post-fix focus:

    - ``addedTime_sec`` / ``addedTime_nsec``
    - ``documentID`` / ``hasDocumentID``
    - ``holes`` / ``isSparse`` / ``sparseLogicalSize``
    - ``reparseTag`` / ``reparsePointIsDirectory``
    """

    # Keys common to TreeNode and FileNode JSON shapes.
    # Updated 2026-05-11 (V4 fix): D2 had ``aclBlobLoc`` always
    # emitted (null when no ACL, BlobLoc dict otherwise). V4
    # found that arq_restore's parser crashes on ``aclBlobLoc:
    # null``, and re-sampling real Arq.app v8 v4 records
    # confirmed Arq.app OMITS the key entirely. The current emit
    # rule omits when null; the key is conditional, not shared.
    SHARED_KEYS = frozenset({
        "isTree", "computerOSType", "containedFilesCount", "itemSize",
        "modificationTime_sec", "modificationTime_nsec",
        "changeTime_sec", "changeTime_nsec",
        "creationTime_sec", "creationTime_nsec",
        "deleted",
        "userName", "groupName",
        "mac_st_dev", "mac_st_ino", "mac_st_mode", "mac_st_nlink",
        "mac_st_uid", "mac_st_gid", "mac_st_rdev", "mac_st_flags",
        "winAttrs",
        "dataBlobLocs", "xattrsBlobLocs",
        # Added in GAP-α (the original 9-key gap close):
        "addedTime_sec", "addedTime_nsec",
        "documentID", "hasDocumentID",
        "holes", "isSparse", "sparseLogicalSize",
        "reparseTag", "reparsePointIsDirectory",
    })
    # FileNode JSON has 33 keys (shared set), TreeNode JSON has
    # 34 (plus ``treeBlobLoc``). When the node carries an ACL,
    # ``aclBlobLoc`` is added on top (FileNode: 34 keys, TreeNode:
    # 35) — pinned by the ACL-emit test below. Sampled count
    # matches Arq.app v8's BackupRecord root node JSON
    # (re-sampled 2026-05-11 against ``/Volumes/arqbackup1`` v4
    # record post-V4 fix).
    TREE_NODE_KEYS = SHARED_KEYS | {"treeBlobLoc"}

    def _file_node_dict(self):
        from arq_writer.backuprecord import node_to_dict
        n = FileNode(
            itemSize=42, mtime_sec=100, ctime_sec=200,
            create_time_sec=50, create_time_nsec=12345,
        )
        return node_to_dict(n)

    def _tree_node_dict(self):
        from arq_writer.backuprecord import node_to_dict
        from arq_writer.types import BlobLoc, TreeNode
        n = TreeNode(
            treeBlobLoc=BlobLoc(blobIdentifier="x"),
            itemSize=0,
        )
        return node_to_dict(n)

    def test_file_node_emits_shared_keys(self) -> None:
        d = self._file_node_dict()
        self.assertEqual(set(d.keys()), self.SHARED_KEYS)

    def test_tree_node_emits_thirty_five_keys(self) -> None:
        # Tree node without ACL emits 34 keys (was 35 pre-V4 fix
        # when aclBlobLoc was always emitted). With ACL the node
        # emits 35 — covered by test_acl_blob_loc_emitted_when_present.
        d = self._tree_node_dict()
        self.assertEqual(set(d.keys()), self.TREE_NODE_KEYS)
        self.assertEqual(len(d), 34)

    def test_acl_blob_loc_emitted_when_present(self) -> None:
        """V4 fix: ``aclBlobLoc`` is conditional. Tree node with
        ACL emits 35 keys; without ACL it emits 34 (key absent)."""
        from arq_writer.backuprecord import node_to_dict
        from arq_writer.types import BlobLoc, TreeNode
        loc = BlobLoc(blobIdentifier="acl" * 21 + "x")
        node = TreeNode(
            treeBlobLoc=BlobLoc(blobIdentifier="tree"),
            aclBlobLoc=loc,
        )
        d = node_to_dict(node)
        self.assertIn("aclBlobLoc", d)
        self.assertEqual(d["aclBlobLoc"]["blobIdentifier"],
                         "acl" * 21 + "x")
        self.assertEqual(
            set(d.keys()),
            self.TREE_NODE_KEYS | {"aclBlobLoc"},
        )
        self.assertEqual(len(d), 35)

    def test_addedTime_maps_to_create_time(self) -> None:
        # Best-effort proxy until the writer tracks per-entry
        # add-time separately. Keeps non-zero values for fresh
        # entries, which is what Arq.app's emit shows.
        d = self._file_node_dict()
        self.assertEqual(d["addedTime_sec"], 50)
        self.assertEqual(d["addedTime_nsec"], 12345)

    def test_documentID_defaults(self) -> None:
        # Real records show this exact pair for every non-document
        # file: documentID=0 with hasDocumentID=True.
        d = self._file_node_dict()
        self.assertEqual(d["documentID"], 0)
        self.assertEqual(d["hasDocumentID"], True)
        self.assertIs(type(d["documentID"]), int)
        self.assertIs(type(d["hasDocumentID"]), bool)

    def test_sparse_defaults_for_dense_files(self) -> None:
        # Writer doesn't probe sparseness yet (separate enhancement);
        # the defaults must match what Arq.app emits for ordinary
        # dense files, which is what the operator's records show.
        d = self._file_node_dict()
        self.assertEqual(d["isSparse"], False)
        self.assertEqual(d["sparseLogicalSize"], 0)
        self.assertEqual(d["holes"], [])
        self.assertIs(type(d["holes"]), list)

    def test_reparse_keys_renamed_from_win_prefix(self) -> None:
        # Tree v4 binary keeps these as ``win_reparse_*``; the
        # JSON path drops the ``win_`` prefix to match Arq.app.
        from arq_writer.backuprecord import node_to_dict
        n = FileNode(
            itemSize=0,
            win_reparse_tag=0xA000_000C,  # IO_REPARSE_TAG_SYMLINK
            win_reparse_point_is_directory=True,
        )
        d = node_to_dict(n)
        self.assertEqual(d["reparseTag"], 0xA000_000C)
        self.assertEqual(d["reparsePointIsDirectory"], True)
        # And the JSON shape doesn't keep the legacy ``win_*`` names.
        self.assertNotIn("win_reparse_tag", d)
        self.assertNotIn("win_reparse_point_is_directory", d)

    def test_winAttrs_kept_camelCase(self) -> None:
        # The pre-existing ``win_attrs`` → ``winAttrs`` rename
        # already matched Arq.app and shouldn't regress.
        from arq_writer.backuprecord import node_to_dict
        n = FileNode(itemSize=0, win_attrs=0x20)  # FILE_ATTRIBUTE_ARCHIVE
        d = node_to_dict(n)
        self.assertEqual(d["winAttrs"], 0x20)
        self.assertNotIn("win_attrs", d)


if __name__ == "__main__":
    unittest.main()
