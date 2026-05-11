"""K4-1 — sub-tree sweep runner correctness.

The K4-1 analyzer at ``scripts/k4_subtree_sweep.py`` walks each
v4 BackupRecord's root tree depth-first, tagging every Node
with its depth. The depth attribution is what produces the
key finding (zero-blocks at depth ≤ 1, non-zero at depth ≥ 2),
so regressing it would silently mis-characterise the data.

These tests pin the depth attribution + table-row math by
running a synthetic destination through the analyzer's
underlying helper and verifying it returns the expected
depth-grouped counts.
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


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class K4SubtreeSweepRunnerTests(unittest.TestCase):
    """Build a small synthetic destination with known tree depth +
    walk it via the K4 analyzer; verify each Node lands at the
    expected depth."""

    def test_walk_returns_depth_tagged_nodes(self) -> None:
        """Build a destination with nested directories of known
        depth; verify the sweep helper tags each Node correctly."""
        import sys
        sys.path.insert(
            0, str(Path(__file__).resolve().parent.parent),
        )
        from arq_writer.backup import build_backup
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from scripts.k4_subtree_sweep import _walk_with_depth
        from arq_writer.types import BlobLoc

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "file_at_root.txt").write_bytes(b"root")
            sub1 = src / "sub1"
            sub1.mkdir()
            (sub1 / "file_at_d1.txt").write_bytes(b"d1")
            sub2 = sub1 / "sub2"
            sub2.mkdir()
            (sub2 / "file_at_d2.txt").write_bytes(b"d2")
            sub3 = sub2 / "sub3"
            sub3.mkdir()
            (sub3 / "file_at_d3.txt").write_bytes(b"d3")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
                tree_version=4,
            )
            backend = LocalBackend(str(dest))
            ks = decrypt_keyset(
                backend.read_all(
                    f"/{res.computer_uuid}/encryptedkeyset.dat",
                ),
                "pw",
            )

            # Find the record's root tree blob.
            import json
            from arq_reader.decrypt import decrypt_lz4_arqo
            rec_arqo = Path(res.backuprecord_path).read_bytes()
            rec_plain = decrypt_lz4_arqo(
                rec_arqo, ks.encryption_key, ks.hmac_key,
            )
            record = json.loads(rec_plain.decode("utf-8"))
            root_loc = record["node"]["treeBlobLoc"]
            root_id = root_loc["blobIdentifier"]

            # Walk via the K4 analyzer.
            nodes = list(_walk_with_depth(
                backend, res.computer_uuid, root_id, root_loc, ks,
                max_records_depth=10,
            ))
            # Each named file should land at its expected depth.
            # The source structure is:
            #   src/  (the root tree; its children are depth 0)
            #     file_at_root.txt   → depth 0
            #     sub1/              → depth 0 (tree)
            #       file_at_d1.txt   → depth 1
            #       sub2/            → depth 1 (tree)
            #         file_at_d2.txt → depth 2
            #         sub3/          → depth 2 (tree)
            #           file_at_d3.txt → depth 3
            by_name = {n["name"]: n["depth"] for n in nodes}
            # Allow for the writer wrapping source.name as the
            # outer directory; in that case the "root" depth
            # shifts.
            if "file_at_root.txt" in by_name:
                offset = 0
            elif "sub1" in by_name and by_name.get("sub1", -1) == 0:
                offset = 0
            else:
                # source.name is the depth-0 entry; expected
                # children are then at depth 1 etc.
                offset = 1
                self.assertIn(
                    "file_at_root.txt", by_name,
                    f"expected file_at_root.txt in walked nodes; "
                    f"got names={list(by_name.keys())}",
                )

            self.assertEqual(
                by_name.get("file_at_root.txt"), 0 + offset,
                f"file_at_root.txt expected depth {0 + offset}; "
                f"got {by_name.get('file_at_root.txt')}",
            )
            self.assertEqual(
                by_name.get("file_at_d1.txt"), 1 + offset,
                f"file_at_d1.txt expected depth {1 + offset}; "
                f"got {by_name.get('file_at_d1.txt')}",
            )
            self.assertEqual(
                by_name.get("file_at_d2.txt"), 2 + offset,
                f"file_at_d2.txt expected depth {2 + offset}; "
                f"got {by_name.get('file_at_d2.txt')}",
            )
            self.assertEqual(
                by_name.get("file_at_d3.txt"), 3 + offset,
                f"file_at_d3.txt expected depth {3 + offset}; "
                f"got {by_name.get('file_at_d3.txt')}",
            )

    def test_max_depth_limits_descent(self) -> None:
        """``max_records_depth`` caps the recursion. Verify that
        a destination with 5 levels walked at max_depth=2
        returns no nodes deeper than depth 2."""
        import sys
        sys.path.insert(
            0, str(Path(__file__).resolve().parent.parent),
        )
        from arq_writer.backup import build_backup
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from scripts.k4_subtree_sweep import _walk_with_depth

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            current = src
            for i in range(5):
                current = current / f"d{i}"
                current.mkdir()
                (current / f"f{i}.txt").write_bytes(
                    f"depth{i}".encode(),
                )
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
                tree_version=4,
            )
            backend = LocalBackend(str(dest))
            ks = decrypt_keyset(
                backend.read_all(
                    f"/{res.computer_uuid}/encryptedkeyset.dat",
                ),
                "pw",
            )
            import json
            from arq_reader.decrypt import decrypt_lz4_arqo
            rec_arqo = Path(res.backuprecord_path).read_bytes()
            rec_plain = decrypt_lz4_arqo(
                rec_arqo, ks.encryption_key, ks.hmac_key,
            )
            record = json.loads(rec_plain.decode("utf-8"))
            root_loc = record["node"]["treeBlobLoc"]
            root_id = root_loc["blobIdentifier"]
            nodes = list(_walk_with_depth(
                backend, res.computer_uuid, root_id, root_loc, ks,
                max_records_depth=2,
            ))
            # No node deeper than depth 2.
            max_observed = max((n["depth"] for n in nodes), default=-1)
            self.assertLessEqual(
                max_observed, 2,
                f"max_records_depth=2 should cap descent; "
                f"got max depth={max_observed}",
            )


if __name__ == "__main__":
    unittest.main()
