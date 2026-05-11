"""E6-new — Windows-specific Node fields zero/null pattern on macOS source.

Arq.app v8 is cross-platform: the Node binary + JSON shape
carries Windows-only fields (``win_attrs``, ``win_reparse_tag``,
``reparseTag``, ``reparsePointIsDirectory``, ``winAttrs``).
On a macOS source they must emit as zero/False — matching the
exact pattern Arq.app uses, so a Windows reader walking a
macOS-emitted destination sees the expected ``no reparse, no
attributes`` shape.

This module pins the convention:

- Tree v3 / v4 binary Node carries ``win_attrs=0``,
  ``win_reparse_tag=0``, ``win_reparse_point_is_directory=False``
  on macOS source files.
- BackupRecord JSON node carries ``winAttrs=0``, ``reparseTag=0``,
  ``reparsePointIsDirectory=False``.
- Empty (zero) values round-trip through parse → write byte-
  identically, so subsequent emits don't drift.

These fields exist for Windows operators emitting backups that
macOS would also read. Their values are MEANINGFUL on Windows
(NTFS reparse points, attribute flags) but always-zero on macOS.
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


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class WindowsFieldsZeroPatternTests(unittest.TestCase):

    def test_node_json_emits_zero_for_winAttrs_on_macos_source(
        self,
    ) -> None:
        """BackupRecord plist's node JSON shape carries
        ``winAttrs`` / ``reparseTag`` / ``reparsePointIsDirectory``.
        On a macOS-walked file, all three must be zero/False."""
        from arq_writer.backuprecord import node_to_dict
        from arq_writer.types import FileNode

        # Default FileNode has zero values for all win_* fields.
        node = FileNode(itemSize=100, mac_st_mode=0o100644)
        d = node_to_dict(node)
        self.assertEqual(
            d["winAttrs"], 0,
            f"winAttrs should be 0 on macOS source; got {d['winAttrs']}",
        )
        self.assertEqual(d["reparseTag"], 0)
        self.assertFalse(d["reparsePointIsDirectory"])

    def test_full_backup_emits_zero_windows_fields(self) -> None:
        """End-to-end: build a real backup, decrypt the
        BackupRecord, verify every node carries
        zero/False Windows fields."""
        from arq_writer.backup import build_backup
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_reader.decrypt import decrypt_lz4_arqo
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"alpha")
            (src / "b.txt").write_bytes(b"bravo")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            b = LocalBackend(str(dest))
            ks = decrypt_keyset(
                b.read_all(
                    f"/{res.computer_uuid}/encryptedkeyset.dat",
                ),
                "pw",
            )
            rec_path = str(
                res.backuprecord_path.relative_to(res.dest_root)
            )
            arqo = b.read_all("/" + rec_path)
            plain = decrypt_lz4_arqo(
                arqo, ks.encryption_key, ks.hmac_key,
            )
            rec = json.loads(plain.decode("utf-8"))
            node = rec["node"]
            # The root node of a macOS-source backup must have
            # all-zero Windows fields.
            self.assertEqual(node["winAttrs"], 0)
            self.assertEqual(node["reparseTag"], 0)
            self.assertFalse(node["reparsePointIsDirectory"])

    def test_tree_binary_round_trip_preserves_zero_windows_fields(
        self,
    ) -> None:
        """Tree v3 + v4 binary Node serialise preserves zero
        Windows fields through parse → write."""
        from arq_writer.serialize import write_tree
        from arq_writer.types import FileNode, Tree, TreeChild
        from arq_reader.parse import parse_tree

        node = FileNode(itemSize=42, mac_st_mode=0o100644)
        self.assertEqual(node.win_attrs, 0)
        self.assertEqual(node.win_reparse_tag, 0)
        self.assertFalse(node.win_reparse_point_is_directory)
        tree = Tree(children=[
            TreeChild(name="f.txt", node=node),
        ])
        for version in (3, 4):
            blob = write_tree(tree, version=version)
            parsed = parse_tree(blob)
            child_node = parsed.children[0].node
            self.assertEqual(child_node.win_attrs, 0)
            self.assertEqual(child_node.win_reparse_tag, 0)
            self.assertFalse(
                child_node.win_reparse_point_is_directory,
            )
            # Re-emit matches.
            re_emit = write_tree(parsed, version=version)
            self.assertEqual(re_emit, blob)


if __name__ == "__main__":
    unittest.main()
