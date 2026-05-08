"""Tests for the shape-fingerprint compatibility verification
helper."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arq_validator import (
    LocalBackend,
    compute_shape_fingerprint,
    diff_fingerprints,
)
from arq_writer import build_backup


def _make_tree(root: Path) -> None:
    (root / "subdir").mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"alpha\n")
    (root / "subdir" / "gamma.txt").write_bytes(b"gamma\n")


class FingerprintBasicsTests(unittest.TestCase):
    def test_fingerprint_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            fp = compute_shape_fingerprint(
                LocalBackend(dest),
                encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            self.assertEqual(fp["schema_version"], 1)
            self.assertEqual(len(fp["computers"]), 1)
            comp = fp["computers"][0]
            # UUID is redacted
            self.assertEqual(comp["uuid"], "REDACTED")
            # Sidecar schemas are dicts of {key: type-name}
            self.assertIn("backupName", comp["config_schema"])
            self.assertEqual(
                comp["config_schema"]["chunkerVersion"], "int",
            )
            # One folder, one record
            self.assertEqual(len(comp["folders"]), 1)
            folder = comp["folders"][0]
            self.assertEqual(len(folder["records"]), 1)
            rec = folder["records"][0]
            # Files surface in tree-walk order (sorted by rel_path)
            paths = [f["rel_path"] for f in rec["files"]]
            self.assertEqual(
                paths, ["alpha.txt", "subdir/gamma.txt"],
            )

    def test_two_runs_of_same_source_match(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest1 = tdp / "dest1"
            dest2 = tdp / "dest2"
            r1 = build_backup(src, dest1, encryption_password="pw")
            r2 = build_backup(src, dest2, encryption_password="pw")
            fp1 = compute_shape_fingerprint(
                LocalBackend(dest1),
                encryption_password="pw",
                computer_uuid=r1.computer_uuid,
            )
            fp2 = compute_shape_fingerprint(
                LocalBackend(dest2),
                encryption_password="pw",
                computer_uuid=r2.computer_uuid,
            )
            diff = diff_fingerprints(fp1, fp2)
            # Two runs of the same source must match modulo
            # creation_date in record metadata. The diff helper
            # treats record creation_date as part of the record's
            # opaque metadata, not as a structural diff.
            self.assertEqual(diff["summary"]["file_shape_diffs"], 0)
            self.assertEqual(diff["summary"]["chunk_pattern_diffs"], 0)
            self.assertEqual(diff["summary"]["missing_files_in_a"], 0)
            self.assertEqual(diff["summary"]["missing_files_in_b"], 0)


class FingerprintDiffTests(unittest.TestCase):
    def test_diff_detects_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            srcA = tdp / "srcA"
            srcA.mkdir()
            (srcA / "a.txt").write_bytes(b"a")
            (srcA / "b.txt").write_bytes(b"b")
            srcB = tdp / "srcB"
            srcB.mkdir()
            (srcB / "a.txt").write_bytes(b"a")
            destA = tdp / "destA"
            destB = tdp / "destB"
            rA = build_backup(srcA, destA, encryption_password="pw")
            rB = build_backup(srcB, destB, encryption_password="pw")
            fpA = compute_shape_fingerprint(
                LocalBackend(destA),
                encryption_password="pw",
                computer_uuid=rA.computer_uuid,
            )
            fpB = compute_shape_fingerprint(
                LocalBackend(destB),
                encryption_password="pw",
                computer_uuid=rB.computer_uuid,
            )
            diff = diff_fingerprints(fpA, fpB)
            self.assertEqual(diff["missing_files_in_b"], ["b.txt"])

    def test_diff_detects_chunk_pattern_mismatch(self) -> None:
        # Same file, different chunkers → different chunk_sizes.
        from arq_writer.chunker import ChunkerConfig

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            big = src / "big.bin"
            big.write_bytes(b"x" * (200 * 1024))
            dest1 = tdp / "dest1"
            dest2 = tdp / "dest2"
            r1 = build_backup(
                src, dest1, encryption_password="pw",
                chunker_config=ChunkerConfig(
                    window_size=64, boundary_bits=12,
                    min_chunk_size=4096, max_chunk_size=131072,
                ),
            )
            r2 = build_backup(
                src, dest2, encryption_password="pw",
                # No chunker → one blob per file
            )
            fp1 = compute_shape_fingerprint(
                LocalBackend(dest1),
                encryption_password="pw",
                computer_uuid=r1.computer_uuid,
            )
            fp2 = compute_shape_fingerprint(
                LocalBackend(dest2),
                encryption_password="pw",
                computer_uuid=r2.computer_uuid,
            )
            diff = diff_fingerprints(fp1, fp2)
            self.assertGreater(
                diff["summary"]["chunk_pattern_diffs"], 0,
            )
            # The mismatched file must be flagged.
            mismatched = {
                d["rel_path"] for d in diff["chunk_pattern_diffs"]
            }
            self.assertIn("big.bin", mismatched)

    def test_diff_match_field_for_identical(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            fp = compute_shape_fingerprint(
                LocalBackend(dest),
                encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            diff = diff_fingerprints(fp, fp)
            self.assertTrue(diff["match"])


class UnicodeFingerprintTests(unittest.TestCase):
    def test_unicode_paths_appear_verbatim(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "한글.txt").write_bytes(b"hi")
            (src / "🎵.mp3").write_bytes(b"music")
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            fp = compute_shape_fingerprint(
                LocalBackend(dest),
                encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            paths = {
                f["rel_path"]
                for f in fp["computers"][0]["folders"][0]["records"][0]["files"]
            }
            self.assertIn("한글.txt", paths)
            self.assertIn("🎵.mp3", paths)


if __name__ == "__main__":
    unittest.main()
