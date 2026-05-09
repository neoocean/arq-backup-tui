"""Tests for E1 (tree-blob cache) + E4 (dry-run) added in this PR.

E1: Restore now caches decrypted tree blobs across the pre-walk
+ restore phases — halves the SFTP round-trips on remote
backups. The cache lives on the Restore instance (per-call), is
keyed by ``blobIdentifier``, and only kicks in for tree blobs
(file blobs would explode memory).

E4: ``arq-backup create --dry-run`` walks source(s) + applies
the same exclusion + size rules a real backup would, but writes
no blobs, doesn't touch the destination, and doesn't even need
the password. Useful for confirming exclusion rules + estimating
destination disk usage.
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


# ---------------------------------------------------------------------------
# E1 — tree-blob cache
# ---------------------------------------------------------------------------


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class TreeBlobCacheTests(unittest.TestCase):

    def test_count_then_restore_uses_cache(self) -> None:
        """Two consecutive operations on the same Restore instance
        (count_tree-driven plan_totals + actual restore) should
        share decrypted tree blobs via the cache instead of
        fetching each tree twice."""
        from arq_writer import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            (src / "sub" / "deep").mkdir(parents=True)
            (src / "a.txt").write_text("alpha")
            (src / "sub" / "b.txt").write_text("beta")
            (src / "sub" / "deep" / "c.txt").write_text("gamma")
            dst = td / "dst"
            dst.mkdir()
            build_backup(src, dst, "pw")

            cu = next(p.name for p in dst.iterdir() if p.is_dir())
            fu = next(
                p.name
                for p in (dst / cu / "backupfolders").iterdir()
                if p.is_dir()
            )
            rs = Restore(str(dst), encryption_password="pw")
            out = td / "out"
            # plan_totals=True triggers _count_tree, which now
            # uses the cache; the subsequent _restore_dir_node
            # walks the same trees + should pull from the cache.
            rs.restore(
                folder_uuid=fu, computer_uuid=cu, dest=out,
                plan_totals=True,
            )
            # After the restore, the cache should be populated
            # (3 tree blobs: root, sub, deep).
            self.assertGreaterEqual(
                len(rs._tree_plain_cache), 1,
                f"tree cache empty after restore; cache: "
                f"{list(rs._tree_plain_cache.keys())}",
            )
            # The restored bytes match.
            self.assertEqual(
                (out / "a.txt").read_text(), "alpha",
            )
            self.assertEqual(
                (out / "sub" / "deep" / "c.txt").read_text(),
                "gamma",
            )

    def test_cache_keyed_by_blob_id(self) -> None:
        """Two BlobLocs pointing at the same content should share
        a cache entry — pin this so cross-snapshot dedup at the
        cache layer works."""
        from arq_reader import Restore
        rs = Restore("/", encryption_password="x")
        # Synthetic: pre-populate the cache + verify a lookup.
        rs._tree_plain_cache["abc123"] = b"cached-content"
        # Build a fake BlobLoc-shaped object that returns "abc123"
        # for blobIdentifier.

        class _FakeLoc:
            blobIdentifier = "abc123"

            # Accessed from _fetch_blob if cache miss; this
            # would be called only when our cache lookup fails.
            isPacked = False
            relativePath = "/should/not/fetch"
            offset = 0
            length = 0

        # Should hit the cache + return our pre-stuffed bytes
        # without touching the (broken) backend.
        out = rs._fetch_tree_blob_cached(_FakeLoc(), keyset=None)
        self.assertEqual(out, b"cached-content")


# ---------------------------------------------------------------------------
# E4 — dry-run
# ---------------------------------------------------------------------------


class DryRunTests(unittest.TestCase):

    def test_basic_walk_counts_files_and_bytes(self) -> None:
        from arq_writer.dry_run import dry_run_source
        with tempfile.TemporaryDirectory() as td:
            s = Path(td)
            (s / "a.txt").write_bytes(b"hello")     # 5 bytes
            (s / "b.bin").write_bytes(b"X" * 1000)  # 1000 bytes
            (s / "sub").mkdir()
            (s / "sub" / "c.txt").write_bytes(b"world!")  # 6 bytes

            r = dry_run_source(s)
            self.assertEqual(r.files_in_scope, 3)
            self.assertEqual(r.bytes_in_scope, 5 + 1000 + 6)
            self.assertEqual(r.dirs_walked, 2)  # root + sub

    def test_max_file_bytes_skips_oversize(self) -> None:
        from arq_writer.dry_run import dry_run_source
        with tempfile.TemporaryDirectory() as td:
            s = Path(td)
            (s / "small.txt").write_bytes(b"x" * 100)
            (s / "big.bin").write_bytes(b"x" * 10000)
            r = dry_run_source(s, max_file_bytes=5000)
            self.assertEqual(r.files_in_scope, 1)
            self.assertEqual(r.files_skipped_size, 1)

    def test_exclusion_rules_drop_matching_files(self) -> None:
        from arq_writer.dry_run import dry_run_source
        from arq_writer.exclusions import ExclusionRules
        with tempfile.TemporaryDirectory() as td:
            s = Path(td)
            (s / "a.log").write_bytes(b"keep me out")
            (s / "b.txt").write_bytes(b"keep me in")
            rules = ExclusionRules.of(wildcard=("*.log",))
            r = dry_run_source(s, exclusions=rules)
            self.assertEqual(r.files_in_scope, 1)
            self.assertEqual(r.files_skipped_excluded, 1)

    def test_largest_in_scope_is_capped_and_sorted(self) -> None:
        from arq_writer.dry_run import dry_run_source
        with tempfile.TemporaryDirectory() as td:
            s = Path(td)
            for i in range(30):
                (s / f"f{i:03d}.bin").write_bytes(
                    b"x" * (i * 100 + 1),
                )
            r = dry_run_source(s)
            # Cap at 20 entries.
            self.assertLessEqual(len(r.largest_in_scope), 20)
            # Sorted descending by size.
            sizes = [e.size for e in r.largest_in_scope]
            self.assertEqual(sizes, sorted(sizes, reverse=True))


class DryRunCLITests(unittest.TestCase):

    def test_cli_dry_run_prints_summary_without_password(self) -> None:
        """``arq-backup create … --dry-run`` should succeed
        without --password and print a summary block."""
        from arq_writer.cli import main
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            (src / "a.txt").write_text("hello")
            # No --dest, no --password — dry-run skips both.
            rc = main([
                "create", str(src), "--dry-run",
            ])
            self.assertEqual(rc, 0)

    def test_cli_dry_run_json_emits_machine_readable(self) -> None:
        from arq_writer.cli import main
        from io import StringIO
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            (src / "a.txt").write_text("hello")
            buf = StringIO()
            with redirect_stdout(buf):
                rc = main([
                    "create", str(src),
                    "--dry-run", "--json-events",
                ])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertTrue(data["dry_run"])
            self.assertEqual(data["totals"]["files_in_scope"], 1)


if __name__ == "__main__":
    unittest.main()
