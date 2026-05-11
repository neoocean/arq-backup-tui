"""C9 — stretchEncryptionKey per-blob flag handling.

Per Arq 7 spec, each BlobLoc carries a ``stretchEncryptionKey``
bool. When True (the writer's default + what Arq.app v8 emits),
the per-blob session key is PBKDF2-stretched before AES-CBC.
When False, the raw bytes are used directly.

The writer emits True universally; the reader should handle
both — operators with legacy Arq 5/6 destinations may
encounter False entries.

This module pins:

1. Writer emits ``stretchEncryptionKey: True`` on every BlobLoc
   (binary + JSON forms)
2. Reader correctly round-trips True
3. BlobLoc dataclass default is True
4. parse/write round-trip preserves the flag through binary
   tree
"""

from __future__ import annotations

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


class StretchEncryptionKeyFlagTests(unittest.TestCase):

    def test_default_blobloc_has_stretch_true(self) -> None:
        from arq_writer.types import BlobLoc
        loc = BlobLoc(blobIdentifier="x" * 64)
        self.assertTrue(loc.stretchEncryptionKey)

    def test_blobloc_to_dict_preserves_flag(self) -> None:
        from arq_writer.backuprecord import blobloc_to_dict
        from arq_writer.types import BlobLoc
        loc = BlobLoc(
            blobIdentifier="x" * 64,
            stretchEncryptionKey=True,
        )
        d = blobloc_to_dict(loc)
        self.assertIn("stretchEncryptionKey", d)
        self.assertIs(d["stretchEncryptionKey"], True)
        # Boolean type (not 1/0).
        self.assertIsInstance(
            d["stretchEncryptionKey"], bool,
        )

    def test_blobloc_to_dict_with_false_flag(self) -> None:
        from arq_writer.backuprecord import blobloc_to_dict
        from arq_writer.types import BlobLoc
        loc = BlobLoc(
            blobIdentifier="x" * 64,
            stretchEncryptionKey=False,
        )
        d = blobloc_to_dict(loc)
        self.assertIs(d["stretchEncryptionKey"], False)

    def test_binary_tree_preserves_flag(self) -> None:
        """parse → write round-trip on binary Tree preserves
        stretchEncryptionKey per-BlobLoc."""
        from arq_writer.serialize import write_tree
        from arq_writer.types import BlobLoc, FileNode, Tree, TreeChild
        from arq_reader.parse import parse_tree

        # FileNode with two data blobs: one stretch=True, one False.
        loc_true = BlobLoc(
            blobIdentifier="t" * 64, stretchEncryptionKey=True,
        )
        loc_false = BlobLoc(
            blobIdentifier="f" * 64, stretchEncryptionKey=False,
        )
        node = FileNode(
            dataBlobLocs=[loc_true, loc_false],
            itemSize=1024,
            mac_st_mode=0o100644,
        )
        tree = Tree(children=[TreeChild(name="f.bin", node=node)])
        blob = write_tree(tree)
        parsed = parse_tree(blob)
        parsed_node = parsed.children[0].node
        self.assertEqual(
            len(parsed_node.dataBlobLocs), 2,
        )
        self.assertTrue(
            parsed_node.dataBlobLocs[0].stretchEncryptionKey,
        )
        self.assertFalse(
            parsed_node.dataBlobLocs[1].stretchEncryptionKey,
        )

    @unittest.skipUnless(_has_openssl(), "openssl CLI required")
    def test_real_backup_emits_stretch_true(self) -> None:
        """End-to-end: every BlobLoc in a real backup has
        stretchEncryptionKey=True."""
        from arq_writer.backup import build_backup
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_reader.parse import parse_tree
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"x")
            (src / "b.txt").write_bytes(b"y")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
                use_packs=True,
            )
            b = LocalBackend(str(dest))
            ks = decrypt_keyset(
                b.read_all(
                    f"/{res.computer_uuid}/encryptedkeyset.dat",
                ),
                "pw",
            )
            # Walk treepacks and inspect every BlobLoc inside the
            # root tree.
            for pack_path in (
                dest / res.computer_uuid / "treepacks"
            ).rglob("*.pack"):
                arqo = pack_path.read_bytes()
                # Tree pack files concatenate ARQOs; find each.
                # For simplicity: decrypt first ARQO + parse tree.
                from arq_reader.pack import reconstruct_index
                entries = reconstruct_index(arqo)
                for entry in entries:
                    arqo_bytes = arqo[
                        entry.offset:entry.offset + entry.length
                    ]
                    try:
                        plain = decrypt_lz4_arqo(
                            arqo_bytes, ks.encryption_key,
                            ks.hmac_key,
                        )
                        tree = parse_tree(plain)
                    except Exception:
                        continue
                    for child in tree.children:
                        node = child.node
                        for loc in getattr(
                            node, "dataBlobLocs", []
                        ):
                            self.assertTrue(
                                loc.stretchEncryptionKey,
                                f"BlobLoc emitted without "
                                f"stretchEncryptionKey=True",
                            )


if __name__ == "__main__":
    unittest.main()
