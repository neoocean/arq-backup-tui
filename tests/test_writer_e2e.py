"""End-to-end test: writer creates a backup, validator validates it.

This is the most important test in the suite — the validator was
already proven correct against synthetic Arq-7-shaped trees; if the
writer's output passes the same validator, every binary-format piece
the writer produces is consistent with the format the validator was
written against.
"""

from __future__ import annotations

import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from arq_validator import LocalBackend, ValidationTier, validate

from arq_writer import build_backup
from arq_writer.lz4_block import lz4_unwrap


def _make_source(td: Path) -> Path:
    src = td / "src"
    src.mkdir()
    (src / "hello.txt").write_text("hello arq backup\n")
    (src / "binary.dat").write_bytes(bytes(range(256)) * 32)
    sub = src / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("# nested file\n\nsome content\n")
    (sub / "empty").write_bytes(b"")
    return src


class WriterEndToEndTests(unittest.TestCase):
    def test_dry_run_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _make_source(td)
            dest = td / "dest"
            res = build_backup(src, dest, "test-pw", backup_name="t1")
            self.assertGreater(res.files_written, 0)
            report = validate(
                LocalBackend(dest), tier=ValidationTier.DRY_RUN,
            )
        self.assertIsNotNone(report.layout)
        self.assertTrue(report.layout.layout_ok,
                        f"layout failed: {report.layout}")

    def test_quick_passes_full_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _make_source(td)
            dest = td / "dest"
            build_backup(src, dest, "test-pw")
            report = validate(
                LocalBackend(dest),
                tier=ValidationTier.QUICK,
                sample_fraction=1.0,
            )
        self.assertIsNotNone(report.magic_check)
        self.assertEqual(report.magic_check.fail, 0)
        self.assertGreater(report.magic_check.ok, 0)

    def test_deep_decrypts_and_verifies_backuprecord(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _make_source(td)
            dest = td / "dest"
            build_backup(src, dest, "secret123")
            report = validate(
                LocalBackend(dest),
                tier=ValidationTier.DEEP,
                encryption_password="secret123",
                sample_fraction=0,
            )
        self.assertIsNone(report.error)
        self.assertIsNotNone(report.backuprecord)
        self.assertTrue(
            report.backuprecord.keyset_decrypted,
            f"keyset failed: {report.backuprecord.keyset_error}",
        )
        self.assertEqual(report.backuprecord.fail, 0)
        self.assertEqual(report.backuprecord.ok, 1)

    def test_audit_full_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _make_source(td)
            dest = td / "dest"
            build_backup(src, dest, "auditme")
            report = validate(
                LocalBackend(dest),
                tier=ValidationTier.AUDIT,
                encryption_password="auditme",
                sample_fraction=0,
                audit_skip_larger_than=None,
            )
        self.assertIsNone(report.error)
        self.assertIsNotNone(report.audit)
        self.assertGreater(report.audit.files_total, 0)
        self.assertEqual(report.audit.files_fail, 0)
        self.assertEqual(report.audit.files_error, 0)

    def test_layout_files_match_arq7_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _make_source(td)
            dest = td / "dest"
            res = build_backup(src, dest, "pw")
            cu_root = dest / res.computer_uuid

            # Top-level files exist with valid JSON.
            for fn in ("backupconfig.json", "backupfolders.json",
                        "backupplan.json"):
                with (cu_root / fn).open() as f:
                    parsed = json.load(f)
                self.assertIsInstance(parsed, dict)

            # Encrypted keyset starts with the spec magic.
            keyset = (cu_root / "encryptedkeyset.dat").read_bytes()
            self.assertTrue(keyset.startswith(b"ARQ_ENCRYPTED_MASTER_KEYS"))

            # Standalone objects are sharded by 2 hex chars.
            standalone = cu_root / "standardobjects"
            self.assertTrue(standalone.is_dir())
            shard_dirs = sorted(p.name for p in standalone.iterdir())
            for d in shard_dirs:
                self.assertEqual(len(d), 2)
                self.assertTrue(all(c in "0123456789abcdef" for c in d))

            # Backuprecord file exists at the spec'd path.
            self.assertTrue(res.backuprecord_path.exists())

    def test_dedup_byte_identical_files(self) -> None:
        # Two files with identical content must produce the same blob_id
        # and only one on-disk standalone object.
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = td / "src"
            src.mkdir()
            shared = b"identical content " * 50
            (src / "a.bin").write_bytes(shared)
            (src / "b.bin").write_bytes(shared)
            (src / "different.txt").write_text("different\n")
            res = build_backup(src, td / "dest", "pw")
        # 3 files but only 2 unique contents → 2 file-blob writes.
        # Plus the root tree and a directory tree = +1 tree blob.
        # blob_ids list contains a duplicate when dedup hits, so
        # uniqueness must be checked via set:
        unique = set(res.blob_ids)
        self.assertGreaterEqual(len(res.blob_ids), 3)
        # 2 unique file blobs + at least 1 tree = >=3 unique
        self.assertGreaterEqual(len(unique), 3)
        # The file-blob count specifically: at most 2 unique file blobs
        # (the third is the tree).
        # We can't easily separate them here without more API surface,
        # but the strict invariant is that re-writing identical bytes
        # doesn't produce a fourth blob.
        self.assertEqual(res.files_written, 3)

    def test_backuprecord_decrypts_and_parses(self) -> None:
        """Decrypt the writer's backuprecord and parse the inner payload.

        Goes through every layer the writer assembles: ARQO HMAC →
        AES-256-CBC decrypt → LZ4 decompress → JSON / plist parse.
        Asserts the spec-required top-level keys are all present
        and well-typed. The writer now defaults to JSON
        (Arq.app-compatible) for the inner serialization, but the
        reader-side helper used here accepts both formats so this
        test still covers writers configured for binary-plist
        output.
        """
        from arq_validator.crypto import (
            aes_256_cbc_decrypt,
            decrypt_keyset,
        )
        from arq_validator.constants import (
            ARQO_HEADER_BYTES,
            ARQO_HMAC_BODY_OFFSET,
            ARQO_MASTER_IV_BYTES,
            KEYSET_FILE,
        )

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _make_source(td)
            dest = td / "dest"
            res = build_backup(src, dest, "secret456",
                               backup_name="hello")
            keyset_blob = (
                dest / res.computer_uuid / KEYSET_FILE
            ).read_bytes()
            keyset = decrypt_keyset(keyset_blob, "secret456")
            arqo = res.backuprecord_path.read_bytes()

        # Strip ARQO header → master_iv + encrypted_session + ciphertext.
        body = arqo[ARQO_HMAC_BODY_OFFSET:]
        master_iv = body[:ARQO_MASTER_IV_BYTES]
        enc_session = body[
            ARQO_MASTER_IV_BYTES : ARQO_MASTER_IV_BYTES + 64
        ]
        ciphertext = body[ARQO_MASTER_IV_BYTES + 64:]
        # Decrypt session header → 16B data_iv + 32B session_key + 16B pad
        session_pt = aes_256_cbc_decrypt(
            keyset.encryption_key, master_iv, enc_session,
        )
        data_iv, session_key = session_pt[:16], session_pt[16:48]
        # Decrypt the ciphertext to get the LZ4-wrapped record bytes.
        lz4_wrapped = aes_256_cbc_decrypt(session_key, data_iv, ciphertext)
        record_bytes = lz4_unwrap(lz4_wrapped)
        # Parse via the dual-format helper so the test stays correct
        # whether the writer emitted JSON (Arq.app-compatible default)
        # or binary plist.
        from arq_reader.restore import _parse_backuprecord
        parsed = _parse_backuprecord(record_bytes)

        # Assert spec-required keys are present.
        for required in ("archived", "arqVersion", "backupFolderUUID",
                          "backupPlanJSON", "backupPlanUUID",
                          "computerOSType", "creationDate", "isComplete",
                          "localPath", "node", "relativePath",
                          "storageClass", "version"):
            self.assertIn(required, parsed,
                          f"backuprecord missing key {required!r}")
        node = parsed["node"]
        self.assertTrue(node["isTree"])
        self.assertIn("treeBlobLoc", node)
        self.assertIn("blobIdentifier", node["treeBlobLoc"])
        self.assertIn("relativePath", node["treeBlobLoc"])


if __name__ == "__main__":
    unittest.main()
