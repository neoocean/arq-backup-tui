"""Writer -> Reader round-trip tests.

These tests are the practical compatibility check we cannot run
against ``arq_restore`` in this sandbox. If our reader can faithfully
restore a backup our writer produces, every binary-format piece on
both sides agrees with the format the validator was written against
— the same format ``arq_restore`` parses.
"""

from __future__ import annotations

import filecmp
import os
import secrets
import shutil
import tempfile
import unittest
from pathlib import Path

from arq_reader import Restore
from arq_reader.decrypt import (
    DecryptError,
    decrypt_encrypted_object,
    decrypt_lz4_arqo,
)
from arq_reader.parse import BinaryReader, parse_blobloc, parse_node, parse_tree
from arq_writer import build_backup
from arq_writer.crypto_write import build_encrypted_object
from arq_writer.lz4_block import lz4_wrap
from arq_writer.serialize import (
    write_blobloc,
    write_node,
    write_tree,
)
from arq_writer.types import (
    BlobLoc,
    FileNode,
    Tree,
    TreeChild,
    TreeNode,
)


def _make_source(td: Path) -> Path:
    """Build a small but varied source tree for round-trip checks."""
    src = td / "src"
    src.mkdir()
    (src / "hello.txt").write_text("hello arq backup\n")
    (src / "binary.dat").write_bytes(bytes(range(256)) * 32)
    (src / "empty").write_bytes(b"")
    sub = src / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("# nested\n\ncontent here\n")
    (sub / "duplicate1.bin").write_bytes(b"shared bytes" * 100)
    (sub / "duplicate2.bin").write_bytes(b"shared bytes" * 100)
    deep = sub / "deeper" / "very" / "much"
    deep.mkdir(parents=True)
    (deep / "tiny.txt").write_text("z\n")
    (deep / "rand.bin").write_bytes(secrets.token_bytes(4096))
    return src


def _compare_trees(a: Path, b: Path) -> tuple[set, set, set]:
    """Returns (missing_in_b, extra_in_b, content_mismatches)."""
    rel_a = {
        str(p.relative_to(a))
        for p in a.rglob("*") if p.is_file()
    }
    rel_b = {
        str(p.relative_to(b))
        for p in b.rglob("*") if p.is_file()
    }
    missing = rel_a - rel_b
    extra = rel_b - rel_a
    mismatches = set()
    for r in rel_a & rel_b:
        if not filecmp.cmp(a / r, b / r, shallow=False):
            mismatches.add(r)
    return missing, extra, mismatches


class RoundTripTests(unittest.TestCase):
    def test_round_trip_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _make_source(td)
            dest = td / "backup"
            out = td / "restored"

            res_w = build_backup(src, dest, "secret-pw")
            self.assertGreater(res_w.files_written, 0)

            r = Restore(dest, "secret-pw")
            res_r = r.restore(folder_uuid=res_w.folder_uuid, dest=out)

            self.assertEqual(res_r.failures, [])
            self.assertGreater(res_r.files_restored, 0)
            missing, extra, mismatches = _compare_trees(src, out)
            self.assertEqual(missing, set(), f"missing: {missing}")
            self.assertEqual(extra, set(), f"extra: {extra}")
            self.assertEqual(mismatches, set(), f"mismatched: {mismatches}")

    def test_dedup_files_restore_correctly(self) -> None:
        # Both duplicate1.bin and duplicate2.bin reference the same
        # blob — the reader must reconstruct each file independently
        # and not collapse them into one.
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = td / "src"; src.mkdir()
            content = b"deduplicated payload " * 50
            (src / "a.bin").write_bytes(content)
            (src / "b.bin").write_bytes(content)
            (src / "c.txt").write_text("different\n")

            dest = td / "backup"; out = td / "restored"
            res_w = build_backup(src, dest, "pw")
            r = Restore(dest, "pw")
            res_r = r.restore(folder_uuid=res_w.folder_uuid, dest=out)

            self.assertEqual(res_r.files_restored, 3)
            self.assertEqual((out / "a.bin").read_bytes(), content)
            self.assertEqual((out / "b.bin").read_bytes(), content)
            self.assertEqual((out / "c.txt").read_text(), "different\n")

    def test_wrong_password_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _make_source(td)
            dest = td / "backup"
            res_w = build_backup(src, dest, "right-pw")

            r = Restore(dest, "WRONG-pw")
            from arq_validator.crypto import CryptoError
            with self.assertRaises(CryptoError):
                r.restore(folder_uuid=res_w.folder_uuid, dest=td / "out")

    def test_list_folders(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _make_source(td)
            dest = td / "backup"
            res_w = build_backup(src, dest, "pw")
            r = Restore(dest, "pw")
            folders = r.list_folders()
        self.assertEqual(len(folders), 1)
        self.assertEqual(folders[0][0], res_w.computer_uuid)
        self.assertEqual(folders[0][1], res_w.folder_uuid)

    def test_corrupted_blob_is_detected(self) -> None:
        # Flip a byte in one of the standalone object files; the
        # reader's HMAC verify must reject it.
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = _make_source(td)
            dest = td / "backup"
            res_w = build_backup(src, dest, "pw")
            stdobj = (
                dest / res_w.computer_uuid / "standardobjects"
            )
            for shard in stdobj.iterdir():
                for f in shard.iterdir():
                    data = bytearray(f.read_bytes())
                    data[100] ^= 0xFF
                    f.write_bytes(bytes(data))
                    break
                break
            r = Restore(dest, "pw")
            res_r = r.restore(folder_uuid=res_w.folder_uuid, dest=td / "out")
        self.assertGreaterEqual(len(res_r.failures), 1)
        first = res_r.failures[0]
        self.assertIn(first["kind"], ("file_fetch", "tree_fetch"))
        self.assertIn("HMAC", first["error"])


class FormatRoundTripTests(unittest.TestCase):
    """Direct encoder -> decoder round-trips at the format level."""

    def test_blobloc_round_trip(self) -> None:
        loc = BlobLoc(
            blobIdentifier="a" * 64, isPacked=False,
            relativePath="/x/y", offset=10, length=99,
            stretchEncryptionKey=True, compressionType=2,
        )
        b = write_blobloc(loc)
        r = BinaryReader(b)
        parsed = parse_blobloc(r)
        self.assertEqual(parsed, loc)
        self.assertEqual(r.remaining(), 0)

    def test_file_node_round_trip(self) -> None:
        n = FileNode(
            dataBlobLocs=[
                BlobLoc(blobIdentifier="x" * 64, relativePath="/p1", length=1),
                BlobLoc(blobIdentifier="y" * 64, relativePath="/p2", length=2),
            ],
            itemSize=42,
            mtime_sec=1700000000,
            mtime_nsec=12345,
            mac_st_mode=0o100644,
            mac_st_uid=501,
            mac_st_gid=20,
            username="user",
            groupName="staff",
        )
        b = write_node(n)
        parsed = parse_node(BinaryReader(b), tree_version=3)
        self.assertEqual(parsed, n)

    def test_tree_node_round_trip(self) -> None:
        loc = BlobLoc(blobIdentifier="z" * 64, relativePath="/t", length=10)
        n = TreeNode(treeBlobLoc=loc, mtime_sec=42)
        b = write_node(n)
        parsed = parse_node(BinaryReader(b), tree_version=3)
        self.assertEqual(parsed, n)

    def test_tree_round_trip(self) -> None:
        loc = BlobLoc(
            blobIdentifier="0" * 64, relativePath="/q", length=1,
        )
        t = Tree(children=[
            TreeChild(name="a.txt", node=FileNode(
                dataBlobLocs=[loc], itemSize=1,
            )),
            TreeChild(name="sub", node=TreeNode(treeBlobLoc=loc)),
            TreeChild(name="유니코드.md", node=FileNode(
                dataBlobLocs=[loc], itemSize=2,
            )),
        ])
        b = write_tree(t)
        parsed = parse_tree(b)
        self.assertEqual(len(parsed.children), 3)
        for orig, got in zip(t.children, parsed.children):
            self.assertEqual(orig.name, got.name)


class ArqoDecryptTests(unittest.TestCase):
    def test_decrypt_round_trip(self) -> None:
        enc = secrets.token_bytes(32)
        mac = secrets.token_bytes(32)
        plaintext = b"hello world. encrypted payload." * 7
        arqo = build_encrypted_object(plaintext, enc, mac)
        recovered = decrypt_encrypted_object(arqo, enc, mac)
        self.assertEqual(recovered, plaintext)

    def test_decrypt_lz4_round_trip(self) -> None:
        enc = secrets.token_bytes(32)
        mac = secrets.token_bytes(32)
        plaintext = b"compress me " * 200
        arqo = build_encrypted_object(lz4_wrap(plaintext), enc, mac)
        recovered = decrypt_lz4_arqo(arqo, enc, mac)
        self.assertEqual(recovered, plaintext)

    def test_wrong_hmac_key_rejected(self) -> None:
        enc = secrets.token_bytes(32)
        mac = secrets.token_bytes(32)
        arqo = build_encrypted_object(b"hi", enc, mac)
        with self.assertRaises(DecryptError):
            decrypt_encrypted_object(
                arqo, enc, secrets.token_bytes(32),
            )

    def test_truncated_input_rejected(self) -> None:
        with self.assertRaises(DecryptError):
            decrypt_encrypted_object(
                b"ARQO" + b"\x00" * 50,
                secrets.token_bytes(32), secrets.token_bytes(32),
            )

    def test_wrong_magic_rejected(self) -> None:
        with self.assertRaises(DecryptError):
            decrypt_encrypted_object(
                b"NOPE" + b"\x00" * 200,
                secrets.token_bytes(32), secrets.token_bytes(32),
            )


if __name__ == "__main__":
    unittest.main()
