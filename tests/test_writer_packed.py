"""End-to-end tests for the packed-mode backup writer.

Exercises ``Backup(use_packs=True)`` and verifies the resulting
backup is byte-identically restored by ``arq_reader``. The reader
already supports ``BlobLoc.isPacked=True`` (see test_reader_pack.py),
so this suite focuses on the writer's pack-emission contract:

- Pack files appear under ``treepacks/`` and ``blobpacks/`` with
  Arq-7-shaped names (UUID with first two hex chars as shard).
- ``standardobjects/`` is empty when packed mode is in use.
- Multiple packs flush at the configured size threshold.
- Round-trip restore matches the source byte-for-byte.
"""

from __future__ import annotations

import filecmp
import re
import secrets
import tempfile
import unittest
from pathlib import Path

from arq_reader import Restore
from arq_writer import build_backup
from arq_writer.pack_builder import PackBuilder
from arq_writer.types import BlobLoc
from arq_validator import constants as VC

# Arq 7 pack name = UUID with first 2 hex chars stripped (shard).
# 30 hex + 3 dashes + ".pack".
PACK_NAME_RE = re.compile(
    r"^[0-9A-F]{6}(-[0-9A-F]{4}){3}-[0-9A-F]{12}\.pack$",
)


def _build_source(td: Path) -> Path:
    src = td / "src"
    src.mkdir()
    (src / "a.txt").write_text("alpha\n")
    (src / "b.txt").write_text("beta\n" * 64)
    (src / "binary.dat").write_bytes(secrets.token_bytes(8192))
    sub = src / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("nested\n")
    (sub / "dup1.bin").write_bytes(b"shared payload " * 100)
    (sub / "dup2.bin").write_bytes(b"shared payload " * 100)
    deep = sub / "deeper" / "very" / "much"
    deep.mkdir(parents=True)
    (deep / "tiny.txt").write_text("z\n")
    (deep / "rand.bin").write_bytes(secrets.token_bytes(4096))
    return src


def _compare(a: Path, b: Path):
    rel_a = {str(p.relative_to(a)) for p in a.rglob("*") if p.is_file()}
    rel_b = {str(p.relative_to(b)) for p in b.rglob("*") if p.is_file()}
    missing = rel_a - rel_b
    extra = rel_b - rel_a
    mismatches = {
        r for r in (rel_a & rel_b)
        if not filecmp.cmp(a / r, b / r, shallow=False)
    }
    return missing, extra, mismatches


class PackBuilderUnitTests(unittest.TestCase):
    def test_path_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pb = PackBuilder(
                "12345678-1234-1234-1234-123456789012",
                "blobpacks",
                Path(td),
                max_pack_bytes=1_000_000,
            )
            loc = pb.add("a" * 64, b"\x00" * 100)
            self.assertTrue(loc.isPacked)
            # Path: /<cu>/blobpacks/<2-hex>/<rest>.pack
            parts = loc.relativePath.lstrip("/").split("/")
            self.assertEqual(parts[0], "12345678-1234-1234-1234-123456789012")
            self.assertEqual(parts[1], "blobpacks")
            self.assertEqual(len(parts[2]), 2)   # shard hex
            self.assertTrue(PACK_NAME_RE.match(parts[3]))

    def test_dedup_returns_same_loc(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pb = PackBuilder(
                "C" * 36, "blobpacks", Path(td),
                max_pack_bytes=1_000_000,
            )
            loc1 = pb.add("a" * 64, b"x" * 100)
            loc2 = pb.add("a" * 64, b"y" * 999)   # different bytes; ignored
            self.assertEqual(loc1, loc2)
            pb.close()
            packs = pb.packs_written
            # Only one ARQO accumulated.
            self.assertEqual(packs[0].blob_count, 1)
            self.assertEqual(len(packs[0].blob_ids), 1)

    def test_threshold_triggers_flush(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pb = PackBuilder(
                "C" * 36, "blobpacks", Path(td),
                max_pack_bytes=512,    # tight
            )
            loc1 = pb.add("a" * 64, b"\x00" * 300)
            loc2 = pb.add("b" * 64, b"\x00" * 300)   # 300+300 > 512 → flush
            pb.close()
            self.assertEqual(len(pb.packs_written), 2)
            # Different relativePaths.
            self.assertNotEqual(loc1.relativePath, loc2.relativePath)


class PackedBackupRoundTripTests(unittest.TestCase):
    def test_packed_round_trip_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _build_source(td)
            dest = td / "backup"
            out = td / "restored"

            res_w = build_backup(src, dest, "pw", use_packs=True)
            self.assertGreater(res_w.files_written, 0)

            r = Restore(dest, "pw")
            res_r = r.restore(folder_uuid=res_w.folder_uuid, dest=out)
            self.assertEqual(res_r.failures, [])
            missing, extra, mismatches = _compare(src, out)
            self.assertEqual(missing, set())
            self.assertEqual(extra, set())
            self.assertEqual(mismatches, set())

    def test_packed_creates_pack_dirs_not_standardobjects(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _build_source(td)
            dest = td / "backup"
            res_w = build_backup(src, dest, "pw", use_packs=True)
            cu_root = dest / res_w.computer_uuid

            # standardobjects must exist (init_plan creates it) but be empty.
            stdobj = cu_root / VC.STANDARDOBJECTS_DIR
            self.assertTrue(stdobj.is_dir())
            self.assertEqual(list(stdobj.iterdir()), [])

            # treepacks/ and blobpacks/ must contain pack files.
            tree_packs = list(
                (cu_root / VC.TREEPACKS_DIR).rglob("*.pack")
            )
            blob_packs = list(
                (cu_root / VC.BLOBPACKS_DIR).rglob("*.pack")
            )
            self.assertGreater(len(tree_packs), 0)
            self.assertGreater(len(blob_packs), 0)
            # Names match the validator's regex.
            for p in tree_packs + blob_packs:
                self.assertTrue(PACK_NAME_RE.match(p.name))

    def test_dedup_in_packed_mode(self) -> None:
        # Two files with identical content must share one ARQO inside
        # one pack, not duplicate it.
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = td / "src"
            src.mkdir()
            payload = b"payload bytes " * 100
            (src / "a.bin").write_bytes(payload)
            (src / "b.bin").write_bytes(payload)
            (src / "c.txt").write_text("different\n")

            dest = td / "backup"
            res_w = build_backup(src, dest, "pw", use_packs=True)
            r = Restore(dest, "pw")
            res_r = r.restore(folder_uuid=res_w.folder_uuid, dest=td / "out")
            self.assertEqual(res_r.failures, [])
            self.assertEqual(res_r.files_restored, 3)
            self.assertEqual(
                (td / "out" / "a.bin").read_bytes(),
                (td / "out" / "b.bin").read_bytes(),
            )

    def test_low_threshold_yields_multiple_packs(self) -> None:
        # With a tight max_pack_bytes, the writer must flush mid-walk
        # and emit several pack files; reader must still restore them
        # all transparently.
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = td / "src"
            src.mkdir()
            for i in range(8):
                (src / f"f{i}.bin").write_bytes(secrets.token_bytes(2048))
            dest = td / "backup"
            res_w = build_backup(
                src, dest, "pw",
                use_packs=True, max_pack_bytes=4096,
            )
            cu_root = dest / res_w.computer_uuid
            blob_packs = list((cu_root / VC.BLOBPACKS_DIR).rglob("*.pack"))
            self.assertGreaterEqual(len(blob_packs), 2)

            r = Restore(dest, "pw")
            res_r = r.restore(folder_uuid=res_w.folder_uuid, dest=td / "out")
            self.assertEqual(res_r.failures, [])
            for i in range(8):
                self.assertEqual(
                    (src / f"f{i}.bin").read_bytes(),
                    (td / "out" / f"f{i}.bin").read_bytes(),
                )

    def test_validator_passes_packed_backup(self) -> None:
        # The validator must accept packed output the same way it
        # accepts standalone-objects output.
        from arq_validator import LocalBackend, ValidationTier, validate
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _build_source(td)
            dest = td / "backup"
            build_backup(src, dest, "pw", use_packs=True)
            report = validate(
                LocalBackend(dest),
                tier=ValidationTier.DEEP,
                encryption_password="pw",
                sample_fraction=0,
            )
        self.assertIsNone(report.error)
        self.assertTrue(report.layout.layout_ok)
        self.assertTrue(report.backuprecord.keyset_decrypted)
        self.assertEqual(report.backuprecord.fail, 0)

    def test_packed_backup_layout_counts(self) -> None:
        # Validator's discovery should report blobpack/treepack
        # counts > 0 and standardobject_count == 0 for packed mode.
        from arq_validator import LocalBackend, ValidationTier, validate
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _build_source(td)
            dest = td / "backup"
            build_backup(src, dest, "pw", use_packs=True)
            report = validate(
                LocalBackend(dest),
                tier=ValidationTier.DRY_RUN,
            )
        self.assertGreater(report.layout.blobpack_count, 0)
        self.assertGreater(report.layout.treepack_count, 0)
        self.assertEqual(report.layout.standardobject_count, 0)


if __name__ == "__main__":
    unittest.main()
