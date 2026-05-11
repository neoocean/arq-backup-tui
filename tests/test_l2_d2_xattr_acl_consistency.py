"""A보완-2 — L2 + D2 consistency through full round-trip.

L2 (PR #67) fixed ``reuse_file_node_for`` to carry
``xattrsBlobLocs`` + ``aclBlobLoc`` through tree-walk reuse.
D2 (PR #86) added ``aclBlobLoc`` to JSON node emit. A보완-1
(this branch series) closed the reader-side JSON consumption gap.

This module is the **end-to-end consistency pin**: a FileNode
that carries both xattr + ACL references survives BOTH:

1. The binary Tree round-trip (parse_tree → write_tree)
2. The BackupRecord JSON round-trip (serialize → parse)
3. The dedup_against_existing tree-walk reuse path

All three layers must agree on the BlobLoc payload — silently
dropping the field in any one of them is a real corruption
risk (restored file loses xattr / ACL).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


class L2D2RoundTripConsistencyTests(unittest.TestCase):
    """Pure serialize/parse — no I/O — verifying the three layers
    agree on xattr + ACL BlobLoc payloads."""

    def _file_node_with_xattr_and_acl(self):
        from arq_writer.types import BlobLoc, FileNode
        xattr_loc = BlobLoc(
            blobIdentifier="x" * 64,
            isPacked=False,
            relativePath="",
            offset=0,
            length=100,
            stretchEncryptionKey=True,
            compressionType=2,
        )
        acl_loc = BlobLoc(
            blobIdentifier="a" * 64,
            isPacked=False,
            relativePath="",
            offset=0,
            length=50,
            stretchEncryptionKey=True,
            compressionType=2,
        )
        return FileNode(
            itemSize=42,
            mac_st_mode=0o100644,
            xattrsBlobLocs=[xattr_loc],
            aclBlobLoc=acl_loc,
        )

    def test_binary_tree_preserves_xattr_acl_refs(self) -> None:
        """parse_tree(write_tree(tree)) preserves both fields."""
        from arq_writer.serialize import write_tree
        from arq_writer.types import Tree, TreeChild
        from arq_reader.parse import parse_tree
        tree = Tree(children=[
            TreeChild(
                name="f.txt", node=self._file_node_with_xattr_and_acl(),
            ),
        ])
        blob = write_tree(tree)
        parsed = parse_tree(blob)
        n = parsed.children[0].node
        self.assertIsNotNone(n.aclBlobLoc)
        self.assertEqual(n.aclBlobLoc.blobIdentifier, "a" * 64)
        self.assertEqual(len(n.xattrsBlobLocs), 1)
        self.assertEqual(
            n.xattrsBlobLocs[0].blobIdentifier, "x" * 64,
        )

    def test_json_backuprecord_preserves_xattr_acl_refs(self) -> None:
        """node_to_dict + parse_backuprecord JSON round-trip."""
        from arq_writer.backuprecord import (
            build_backuprecord_dict, parse_backuprecord,
            serialize_backuprecord,
        )
        rec = build_backuprecord_dict(
            backup_folder_uuid="F", backup_plan_uuid="P",
            backup_plan_dict={},
            root_node=self._file_node_with_xattr_and_acl(),
            local_path="/x",
        )
        # Serialize → parse → check.
        json_bytes = serialize_backuprecord(rec, fmt="json")
        re_parsed = parse_backuprecord(json_bytes)
        node = re_parsed["node"]
        self.assertIsInstance(node["aclBlobLoc"], dict)
        self.assertEqual(
            node["aclBlobLoc"]["blobIdentifier"], "a" * 64,
        )
        self.assertEqual(len(node["xattrsBlobLocs"]), 1)
        self.assertEqual(
            node["xattrsBlobLocs"][0]["blobIdentifier"], "x" * 64,
        )

    def test_reuse_file_node_for_carries_xattr_and_acl(self) -> None:
        """L2's fix: prior FileNode's xattr + ACL refs flow into
        the reused FileNode."""
        import os
        from arq_writer.prior_tree import reuse_file_node_for

        prior = self._file_node_with_xattr_and_acl()
        # Synthetic stat result that matches prior's shape.
        class _Stat:
            st_size = 42
            st_mtime = 1_700_000_000.0
            st_ctime = 1_700_000_000.0
            st_mode = 0o100644
            st_uid = 501
            st_gid = 20
            st_ino = 12345
            st_nlink = 1
            st_dev = 0
        reused = reuse_file_node_for(_Stat(), prior)
        self.assertEqual(
            reused.aclBlobLoc.blobIdentifier, "a" * 64,
            "L2 fix: reused FileNode must carry ACL ref",
        )
        self.assertEqual(len(reused.xattrsBlobLocs), 1)
        self.assertEqual(
            reused.xattrsBlobLocs[0].blobIdentifier, "x" * 64,
            "L2 fix: reused FileNode must carry xattr refs",
        )


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class L2D2EndToEndConsistencyTests(unittest.TestCase):
    """Full end-to-end: build → dedup re-run → restore. Verifies
    xattr + ACL references survive every layer."""

    def test_end_to_end_xattr_acl_survives_dedup_rerun(self) -> None:
        """Two-pass backup with dedup_against_existing — restored
        files maintain their xattrs (no silent loss through the
        reuse path)."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"content")
            dest = tdp / "dest"
            # First pass.
            r1 = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            # Second pass (dedup against existing).
            build_backup(
                str(src), str(dest), encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                folder_uuid=r1.folder_uuid,
                dedup_against_existing=True,
            )
            # Restore.
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            self.assertEqual(
                (out / "f.txt").read_bytes(), b"content",
                "content survived dedup rerun + restore",
            )


if __name__ == "__main__":
    unittest.main()
