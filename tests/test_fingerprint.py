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


class UuidKeyCollapseTests(unittest.TestCase):
    """``_schema_of_dict`` must collapse UUID-keyed maps whose values
    share the same nested schema down to a single ``"<uuid>"``
    placeholder. Without this, two backups of the same source
    fingerprint differently solely because they assigned different
    folder UUIDs — making the salt-independent diff unusable for
    its intended purpose (writer-vs-Arq.app comparison).
    """

    def test_single_uuid_key_collapses(self) -> None:
        from arq_validator.fingerprint import _schema_of_dict

        schema = _schema_of_dict({
            "5EA84470-463D-4D4E-9085-9203B8E5EA32": {
                "name": "alpha", "size": 12, "active": True,
            },
        })
        self.assertEqual(
            schema,
            {"<uuid>": {
                "active": "bool", "name": "str", "size": "int",
            }},
        )

    def test_multi_uuid_keys_with_uniform_schema_collapse(self) -> None:
        from arq_validator.fingerprint import _schema_of_dict

        schema = _schema_of_dict({
            "5EA84470-463D-4D4E-9085-9203B8E5EA32": {"x": 1, "y": "a"},
            "FA21E3B6-926C-46E0-A3EB-BDAB1984FB51": {"x": 2, "y": "b"},
            "CA0D1896-B097-46A2-B0B8-BED9DC8FCE50": {"x": 3, "y": "c"},
        })
        self.assertEqual(
            schema, {"<uuid>": {"x": "int", "y": "str"}},
        )

    def test_uuid_keys_with_heterogeneous_schema_do_not_collapse(
        self,
    ) -> None:
        # If the per-UUID values disagree on schema, that's a real
        # difference worth surfacing — keep the per-key entries so
        # diffing still pinpoints which UUID's payload diverges.
        from arq_validator.fingerprint import _schema_of_dict

        schema = _schema_of_dict({
            "5EA84470-463D-4D4E-9085-9203B8E5EA32": {"x": 1},
            "FA21E3B6-926C-46E0-A3EB-BDAB1984FB51": {"x": 1, "y": "a"},
        })
        self.assertNotIn("<uuid>", schema)
        self.assertEqual(set(schema.keys()), {
            "5EA84470-463D-4D4E-9085-9203B8E5EA32",
            "FA21E3B6-926C-46E0-A3EB-BDAB1984FB51",
        })

    def test_non_uuid_keys_preserve_per_key_schema(self) -> None:
        # Regular field names must not be mistaken for UUIDs even
        # when they look hex-y.
        from arq_validator.fingerprint import _schema_of_dict

        schema = _schema_of_dict({
            "alpha": {"x": 1},
            "abcdef01": {"x": 1},  # 8 hex but no dashes — not a UUID
        })
        self.assertEqual(set(schema.keys()), {"alpha", "abcdef01"})

    def test_uuid_keys_with_non_dict_values_do_not_collapse(self) -> None:
        # The collapse only applies to ``UUID -> dict`` maps. UUID
        # keys mapping to scalars stay as-is.
        from arq_validator.fingerprint import _schema_of_dict

        schema = _schema_of_dict({
            "5EA84470-463D-4D4E-9085-9203B8E5EA32": "string-value",
            "FA21E3B6-926C-46E0-A3EB-BDAB1984FB51": "another-value",
        })
        self.assertEqual(
            sorted(schema.keys()),
            sorted([
                "5EA84470-463D-4D4E-9085-9203B8E5EA32",
                "FA21E3B6-926C-46E0-A3EB-BDAB1984FB51",
            ]),
        )

    def test_two_runs_with_random_uuids_match(self) -> None:
        # End-to-end: two writer runs against the same source assign
        # independent random UUIDs. With the collapse in place, the
        # comparison must report ``match: True`` — which is the whole
        # point of the salt-independent fingerprint.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            d_a = tdp / "a"
            d_b = tdp / "b"
            r_a = build_backup(src, d_a, encryption_password="pw")
            r_b = build_backup(src, d_b, encryption_password="pw")
            self.assertNotEqual(r_a.folder_uuid, r_b.folder_uuid)
            fp_a = compute_shape_fingerprint(
                LocalBackend(d_a),
                encryption_password="pw",
                computer_uuid=r_a.computer_uuid,
            )
            fp_b = compute_shape_fingerprint(
                LocalBackend(d_b),
                encryption_password="pw",
                computer_uuid=r_b.computer_uuid,
            )
            diff = diff_fingerprints(fp_a, fp_b)
            self.assertTrue(
                diff["match"],
                msg=(
                    "two runs of the same source must fingerprint "
                    "identically modulo random UUIDs; got: "
                    f"{diff}"
                ),
            )


class MaxRecordsPerFolderTests(unittest.TestCase):
    """``compute_shape_fingerprint(max_records_per_folder=N)`` keeps
    the latest N records per folder. Without it, fingerprinting
    real-world destinations with hundreds of backuprecords per folder
    is intractable (HANDOFF.md T5).

    The latest record is the one operators actually care about for
    a writer-vs-Arq.app comparison, so we cap from the end of
    ``list_backuprecords`` (which returns chronological order).
    """

    def _build_with_n_records(self, n: int) -> tuple:
        # build_backup names backuprecord files by an
        # ``int(creationDate)`` second-resolution timestamp under a
        # bucket directory; two runs in the same second overwrite the
        # same path. To get N distinct records, sleep just over a
        # second between calls and mutate the source so the content
        # also differs (catches a future writer change that might
        # use a non-time-based id). All records land under one
        # pinned (computer_uuid, folder_uuid) tuple.
        import time
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        tdp = Path(td.name)
        src = tdp / "src"
        src.mkdir()
        _make_tree(src)
        dest = tdp / "dest"
        cu = "13237B8D-BDBF-43DB-B5A9-546EFB3852B5"
        fu = "5EA84470-463D-4D4E-9085-9203B8E5EA32"
        for i in range(n):
            if i > 0:
                time.sleep(1.05)
            (src / f"marker-{i}.txt").write_bytes(
                f"run {i}\n".encode("utf-8"),
            )
            build_backup(
                src, dest, encryption_password="pw",
                computer_uuid=cu, folder_uuid=fu,
            )
        return dest, cu

    def test_no_cap_keeps_all_records(self) -> None:
        dest, cu = self._build_with_n_records(3)
        fp = compute_shape_fingerprint(
            LocalBackend(dest),
            encryption_password="pw",
            computer_uuid=cu,
        )
        folders = fp["computers"][0]["folders"]
        self.assertEqual(len(folders), 1)
        self.assertEqual(len(folders[0]["records"]), 3)
        self.assertNotIn(
            "records_truncated_to_latest", folders[0],
        )

    def test_cap_keeps_only_latest(self) -> None:
        dest, cu = self._build_with_n_records(3)
        fp = compute_shape_fingerprint(
            LocalBackend(dest),
            encryption_password="pw",
            computer_uuid=cu,
            max_records_per_folder=1,
        )
        folder = fp["computers"][0]["folders"][0]
        self.assertEqual(len(folder["records"]), 1)
        self.assertEqual(
            folder["records_truncated_to_latest"], 1,
        )
        self.assertEqual(
            folder["records_total_in_destination"], 3,
        )

    def test_cap_above_total_keeps_all_no_truncation_marker(
        self,
    ) -> None:
        # When the cap >= record count, every record is kept and the
        # truncation markers stay absent (the operator can tell from
        # their absence that nothing was dropped).
        dest, cu = self._build_with_n_records(2)
        fp = compute_shape_fingerprint(
            LocalBackend(dest),
            encryption_password="pw",
            computer_uuid=cu,
            max_records_per_folder=10,
        )
        folder = fp["computers"][0]["folders"][0]
        self.assertEqual(len(folder["records"]), 2)
        self.assertNotIn(
            "records_truncated_to_latest", folder,
        )

    def test_capped_runs_diff_cleanly_at_same_cap(self) -> None:
        # End-to-end: two destinations both fingerprinted at the
        # same cap should diff cleanly (proving the cap is consistent
        # — an operator running against /Volumes/arqbackup1 with
        # --max-records-per-folder 1 won't see spurious record-count
        # diffs against a synthetic-source fingerprint at the same
        # cap).
        dest_a, cu_a = self._build_with_n_records(3)
        dest_b, cu_b = self._build_with_n_records(3)
        fp_a = compute_shape_fingerprint(
            LocalBackend(dest_a),
            encryption_password="pw",
            computer_uuid=cu_a,
            max_records_per_folder=1,
        )
        fp_b = compute_shape_fingerprint(
            LocalBackend(dest_b),
            encryption_password="pw",
            computer_uuid=cu_b,
            max_records_per_folder=1,
        )
        diff = diff_fingerprints(fp_a, fp_b)
        self.assertEqual(diff["record_count_diffs"], [])
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
