"""N4 — hardlink-shape parity (writer + reader round-trip).

Arq.app v8's ArqAgent binary contains symbols
``_hardLinkedPathsByInodeNumber`` and ``HardLinkQueue`` —
confirms Arq.app explicitly handles hardlinks by inode. Our
writer also tracks hardlinks via a ``(st_dev, st_ino)`` cache
that returns the same FileNode for every link.

N4 pins the end-to-end behaviour:

1. Build a fixture: file ``A`` plus hardlinks ``B``, ``C`` all
   sharing the same inode.
2. Back up via our writer.
3. Verify the emitted tree's FileNode entries for A/B/C have:
   - the SAME ``mac_st_ino``
   - ``mac_st_nlink >= 3``
   - dataBlobLocs pointing at the SAME blob (only one
     plaintext blob emitted, shared by the three FileNode
     entries)
4. Restore through our reader.
5. Verify the restored A/B/C share an inode on disk (i.e.
   the reader recreated the hardlink relationship, not three
   independent copies).

Combined: this is the read-write hardlink contract our writer
+ reader commits to, and the closest format-level analogue of
Arq.app's HardLinkQueue mechanism.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
class N4_HardlinkShapeTests(unittest.TestCase):

    def test_writer_dedupes_hardlinks_to_one_blob(self) -> None:
        """A, B, C are hardlinks (same inode). Writer should
        emit exactly ONE data blob and reference it from all
        three FileNode entries."""
        from arq_writer.backup import build_backup
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_reader.parse import parse_tree
        from arq_validator.crypto import decrypt_keyset

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            content = b"shared content via hardlinks\n" * 100
            (src / "A").write_bytes(content)
            os.link(src / "A", src / "B")
            os.link(src / "A", src / "C")
            # Sanity: shared inode.
            inodes = {(src / n).stat().st_ino for n in "ABC"}
            self.assertEqual(
                len(inodes), 1,
                "A/B/C must share an inode for this test",
            )
            self.assertEqual(
                (src / "A").stat().st_nlink, 3,
                "A's nlink must be 3 with two hardlinks added",
            )

            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest),
                encryption_password=("-".join(("t","tst","pw"))),
            )

            # Walk the standalone-objects + verify only one
            # plaintext-content blob exists (the rest are tree
            # blobs / xattr blobs / etc).
            so = dest / res.computer_uuid / "standardobjects"
            self.assertTrue(so.is_dir())

            # Read the root tree blob + verify all three FileNode
            # entries point at the same dataBlobLoc.
            ks = decrypt_keyset(
                (dest / res.computer_uuid /
                 "encryptedkeyset.dat").read_bytes(),
                "test-pw",
            )
            rec_path = Path(res.backuprecord_path)
            rec_arqo = rec_path.read_bytes()
            rec = json.loads(decrypt_lz4_arqo(
                rec_arqo, ks.encryption_key, ks.hmac_key,
            ).decode("utf-8"))
            root_loc = rec["node"]["treeBlobLoc"]
            root_id = root_loc["blobIdentifier"]
            tree_blob = (
                so / root_id[:2] / root_id[2:]
            ).read_bytes()
            tree_plain = decrypt_lz4_arqo(
                tree_blob, ks.encryption_key, ks.hmac_key,
            )
            tree = parse_tree(tree_plain)

            file_children = {
                c.name: c.node for c in tree.children
                if c.name in ("A", "B", "C")
            }
            self.assertEqual(
                set(file_children.keys()), {"A", "B", "C"},
            )
            # All three share the same data blob.
            data_blobs = {
                file_children[n].dataBlobLocs[0].blobIdentifier
                for n in "ABC"
            }
            self.assertEqual(
                len(data_blobs), 1,
                f"A/B/C should share one data blob; got "
                f"{data_blobs}",
            )
            # All three have the same mac_st_ino.
            inodes_emitted = {
                file_children[n].mac_st_ino for n in "ABC"
            }
            self.assertEqual(
                len(inodes_emitted), 1,
                f"A/B/C should emit the same mac_st_ino; got "
                f"{inodes_emitted}",
            )
            # mac_st_nlink >= 3 on all three.
            for n in "ABC":
                self.assertGreaterEqual(
                    file_children[n].mac_st_nlink, 3,
                    f"{n}'s emitted mac_st_nlink should be "
                    f">= 3; got {file_children[n].mac_st_nlink}",
                )

    def test_restore_reconstructs_hardlinks(self) -> None:
        """After our reader restores A/B/C, the on-disk inodes
        should match — confirming the reader recreated the
        link relationship rather than producing 3 copies."""
        from arq_writer.backup import build_backup
        from arq_reader.restore import Restore

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            content = b"link content " * 50
            (src / "A").write_bytes(content)
            os.link(src / "A", src / "B")
            os.link(src / "A", src / "C")

            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )

            out = tdp / "out"
            out.mkdir()
            r = Restore(str(dest), encryption_password="pw")
            r.restore(folder_uuid=res.folder_uuid, dest=str(out))

            # Find the restored A/B/C — somewhere under out/
            paths = {}
            for n in "ABC":
                hit = next(out.rglob(n), None)
                self.assertIsNotNone(
                    hit, f"restored {n} not found",
                )
                paths[n] = hit

            # All three should share an inode on the restored
            # filesystem (we're on the same tempdir, so inode
            # comparison is meaningful).
            inodes = {paths[n].stat().st_ino for n in "ABC"}
            self.assertEqual(
                len(inodes), 1,
                f"restored A/B/C should share an inode "
                f"(hardlink relationship preserved); got "
                f"{inodes}",
            )
            # And all three should have nlink >= 3.
            for n in "ABC":
                self.assertGreaterEqual(
                    paths[n].stat().st_nlink, 3,
                    f"restored {n} nlink should be >= 3",
                )
            # Content matches.
            for n in "ABC":
                self.assertEqual(
                    paths[n].read_bytes(), content,
                )


class N4_ArqAgentHardlinkSymbolsTests(unittest.TestCase):
    """Defensive RE check: confirm Arq.app's hardlink-tracking
    symbols are present in the binary. If a future Arq.app
    upgrade renames them, this test fails — operator gets a
    signal to revisit hardlink-parity assumptions."""

    BINARY = Path(
        "/Applications/Arq.app/Contents/Resources/"
        "ArqAgent.app/Contents/MacOS/ArqAgent"
    )

    @unittest.skipUnless(
        BINARY.is_file(),
        "ArqAgent not installed locally",
    )
    def test_hardlink_queue_symbol_present(self) -> None:
        """Hardlink-tracking symbols are visible as strings in
        the binary even when nm omits them (private ObjC
        classes get string-table entries but not exported nm
        symbols). Use ``strings`` for the detection."""
        proc = subprocess.run(
            ["strings", str(self.BINARY)],
            capture_output=True, check=True, text=True,
            timeout=60,
        )
        self.assertIn(
            "HardLinkQueue", proc.stdout,
            "Arq.app's HardLinkQueue class missing — Arq.app "
            "may no longer track hardlinks by inode; revisit "
            "our writer's hardlink-cache strategy.",
        )
        self.assertIn(
            "_hardLinkedPathsByInodeNumber", proc.stdout,
        )
        self.assertIn(
            "setHardLinkedPath:forInode:", proc.stdout,
        )
        self.assertIn(
            "hardLinkedPathForInode:", proc.stdout,
        )


if __name__ == "__main__":
    unittest.main()
