"""E3-new — APFS clone (``cp -c``) handling.

APFS supports clonefile() — a copy operation that shares
underlying disk storage with the source but allocates a new
inode. From the backup walker's perspective:

- Two cloned files share underlying disk blocks
- They have SEPARATE inodes (unlike hardlinks)
- Each appears as a distinct entry with its own metadata

The walker should:

1. Walk both files as distinct entries (NOT hardlink-dedup,
   which keys on shared inode).
2. Rely on content-addressed dedup to fold their bytes into
   a single blob on the destination side.
3. Restore each clone as a regular file (the restore-side
   doesn't preserve the clone relationship — APFS clone is a
   storage optimisation, not user-visible state).

This module pins those properties:

- Two clones restore byte-identical to source
- Their on-destination data blob_id is the same (content
  addressing folds them)
- Each gets its own FileNode (no inode-sharing collapse)
"""

from __future__ import annotations

import os
import platform
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


def _make_apfs_clone(src: Path, dst: Path) -> bool:
    """Create an APFS clone of ``src`` at ``dst`` via ``cp -c``.
    Returns True on success. Skips when not on macOS or when the
    FS doesn't support clonefile()."""
    if platform.system() != "Darwin":
        return False
    try:
        r = subprocess.run(
            ["cp", "-c", str(src), str(dst)],
            capture_output=True, text=True,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
@unittest.skipUnless(
    platform.system() == "Darwin",
    "APFS clone (cp -c) only on macOS",
)
class ApfsCloneHandlingTests(unittest.TestCase):

    def test_apfs_clone_pair_restores_byte_identical(self) -> None:
        """Both clones restore byte-identical to source."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            content = b"shared APFS clone content\n" * 200
            original = src / "original.bin"
            clone = src / "clone.bin"
            original.write_bytes(content)
            if not _make_apfs_clone(original, clone):
                self.skipTest(
                    "this filesystem doesn't support APFS clone",
                )
            # Verify the clone shares an inode (no, that's
            # hardlinks) — clones have DIFFERENT inodes. Sanity-
            # check the test setup.
            self.assertNotEqual(
                original.stat().st_ino,
                clone.stat().st_ino,
                "APFS clones should have distinct inodes, not "
                "shared (that would be hardlinks)",
            )
            dest = tdp / "dest"
            build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            self.assertEqual(
                (out / "original.bin").read_bytes(), content,
            )
            self.assertEqual(
                (out / "clone.bin").read_bytes(), content,
            )
            # Restored files have their own inodes (no clone
            # preservation — APFS clone is a storage optimisation,
            # not user-visible state).
            self.assertNotEqual(
                (out / "original.bin").stat().st_ino,
                (out / "clone.bin").stat().st_ino,
            )

    def test_apfs_clone_pair_dedups_to_one_data_blob(self) -> None:
        """Content-addressed dedup folds both clones' data into
        a single blob on the destination."""
        from arq_writer.backup import build_backup
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            content = b"clone dedup test " * 100
            (src / "a.bin").write_bytes(content)
            if not _make_apfs_clone(src / "a.bin", src / "b.bin"):
                self.skipTest("APFS clone unavailable")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
                use_packs=True,
            )
            # The data blob (computed by compute_blob_id over
            # identical plaintexts) should appear in
            # res.blob_ids exactly once (the writer's blob_id
            # cache catches the second emit). Tree + xattr blobs
            # may inflate the count.
            from collections import Counter
            counts = Counter(res.blob_ids)
            # No blob_id appears more than once in the emit list
            # (the cache prevents duplicate emits).
            for bid, n in counts.items():
                self.assertEqual(
                    n, 1,
                    f"blob_id {bid} appears {n} times in emit "
                    f"list — writer cache failed to fold clone",
                )

    def test_apfs_clone_gives_each_file_its_own_node(self) -> None:
        """Two clones produce two distinct FileNodes in the tree
        — they share storage but the tree records them
        separately (unlike hardlinks which share a FileNode via
        the writer's inode cache)."""
        from arq_writer.backup import build_backup
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_reader.parse import parse_tree
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            content = b"per-file FileNode test " * 50
            (src / "x.bin").write_bytes(content)
            if not _make_apfs_clone(src / "x.bin", src / "y.bin"):
                self.skipTest("APFS clone unavailable")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            # Walk the tree blob to count children.
            b = LocalBackend(str(dest))
            ks = decrypt_keyset(
                b.read_all(
                    f"/{res.computer_uuid}/encryptedkeyset.dat",
                ),
                "pw",
            )
            # Find the root tree from the backuprecord.
            import json
            rec_arqo = b.read_all(
                "/" + str(
                    res.backuprecord_path.relative_to(
                        res.dest_root
                    )
                )
            )
            rec = json.loads(
                decrypt_lz4_arqo(
                    rec_arqo, ks.encryption_key, ks.hmac_key,
                ).decode("utf-8")
            )
            tloc = rec["node"]["treeBlobLoc"]
            tree_raw = b.read_all(
                f"/{res.computer_uuid}/standardobjects/"
                f"{tloc['blobIdentifier'][:2]}/"
                f"{tloc['blobIdentifier'][2:]}"
            )
            tree = parse_tree(
                decrypt_lz4_arqo(
                    tree_raw, ks.encryption_key, ks.hmac_key,
                )
            )
            names = {c.name for c in tree.children}
            self.assertIn("x.bin", names)
            self.assertIn("y.bin", names)
            self.assertEqual(
                len(tree.children), 2,
                "expected 2 FileNodes (one per clone), got "
                f"{len(tree.children)}",
            )


if __name__ == "__main__":
    unittest.main()
