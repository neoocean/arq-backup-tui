"""End-to-end Arq 5 / Arq 6 restore tests.

We don't have a real Arq 5 backup in the sandbox, so the tests build
a synthetic Arq 5/6 destination with our own primitives:

- ``encryptionv3.dat`` via :func:`arq5_keyset.build_arq5_keyset_blob`
- ``bucketdata/<folder>/refs/heads/master`` containing ``<sha1>Y``
- objects under ``objects/<2-hex>/<remaining-38-hex>``
- each object is the writer-side ARQO of an LZ4-wrapped plaintext

For a leaf Tree we:
  1. Build a binary Tree v22 (re-using the test_arq5_binary helpers
     adapted into a writer-style helper here).
  2. Wrap it as ARQO-encrypted (via build_encrypted_object) +
     LZ4-wrapped, store under its plaintext SHA-1.

For the root Commit we:
  1. Build a binary Commit v12 referencing the tree's SHA-1.
  2. Store it the same way.
  3. Write the master ref pointing at the commit's SHA-1.

Restore reads the destination and reconstructs the source tree.
Tests assert byte-identical match.
"""

from __future__ import annotations

import hashlib
import secrets
import struct
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from arq_reader.arq5_keyset import (
    arq5_compute_blob_sha1,
    build_arq5_keyset_blob,
    decrypt_arq5_keyset,
)
from arq_reader.arq5_restore import Arq5Restore
from arq_validator.crypto import CryptoError
from arq_writer.crypto_write import build_encrypted_object
from arq_writer.lz4_block import lz4_wrap


# ---------------------------------------------------------------------------
# Primitive encoders (same as test_arq5_binary)
# ---------------------------------------------------------------------------


def w_bool(v): return b"\x01" if v else b"\x00"
def w_uint32(v): return struct.pack(">I", v)
def w_int32(v): return struct.pack(">i", v)
def w_uint64(v): return struct.pack(">Q", v)
def w_int64(v): return struct.pack(">q", v)


def w_string(s):
    if s is None:
        return b"\x00"
    body = s.encode("utf-8")
    return b"\x01" + w_uint64(len(body)) + body


def w_date(dt):
    if dt is None:
        return b"\x00"
    millis = int(dt.timestamp() * 1000)
    return b"\x01" + w_int64(millis)


def w_data(b: bytes) -> bytes:
    return w_uint64(len(b)) + b


def w_blobkey(*, sha1, tree_version, stretch=False, storage_type=1,
              archive_id=None, archive_size=0, archive_uploaded=None):
    out = w_string(sha1)
    if tree_version >= 14:
        out += w_bool(stretch)
    if tree_version >= 17:
        out += w_uint32(storage_type)
        out += w_string(archive_id)
        out += w_uint64(archive_size)
        out += w_date(archive_uploaded)
    return out


# ---------------------------------------------------------------------------
# Tree v22 / Node v22 builders (encoder side of test_arq5_binary's
# parser, extracted here to avoid cross-test imports).
# ---------------------------------------------------------------------------


def build_tree_v22(*, nodes, mode=0o755):
    out = bytearray()
    out += b"TreeV022"
    out += w_int32(0) + w_int32(0)        # xattrs/acl compression types (none)
    out += w_blobkey(sha1=None, tree_version=22)
    out += w_uint64(0)
    out += w_blobkey(sha1=None, tree_version=22)
    out += w_int32(501) + w_int32(20) + w_int32(mode)
    out += w_int64(0) + w_int64(0)
    out += w_int64(0) + w_int32(0) + w_int32(0)
    out += w_int32(0) + w_int32(0) + w_uint32(0) + w_int32(0)
    out += w_int64(0) + w_int64(0) + w_int64(0) + w_uint32(4096)
    out += w_int64(0) + w_int64(0)        # createTime
    out += w_uint32(0)                    # missing nodes
    out += w_uint32(len(nodes))
    for name, nbytes in nodes:
        out += w_string(name)
        out += nbytes
    return bytes(out)


def build_node_v22(*, is_tree, data_blob_keys, mode, uncompressed_size=0):
    out = bytearray()
    out += w_bool(is_tree)
    out += w_bool(False)                  # treeContainsMissingItems
    # data, xattrs, acl compression (LZ4 for data so ARQOs can be LZ4-wrapped).
    out += w_int32(2) + w_int32(0) + w_int32(0)
    out += w_int32(len(data_blob_keys))
    for sha1 in data_blob_keys:
        out += w_blobkey(sha1=sha1, tree_version=22, stretch=True)
    out += w_uint64(uncompressed_size)
    # xattrs blob key (null) + size, acl blob key (null).
    out += w_blobkey(sha1=None, tree_version=22)
    out += w_uint64(0)
    out += w_blobkey(sha1=None, tree_version=22)
    out += w_int32(501) + w_int32(20) + w_int32(mode)
    out += w_int64(0) + w_int64(0)
    out += w_int64(0) + w_int32(0) + w_int32(0)
    out += w_string(None) + w_string(None)
    out += w_bool(False)
    out += w_int32(0) + w_int32(0) + w_uint32(0) + w_int32(0)
    out += w_int64(0) + w_int64(0) + w_int64(0) + w_int64(0)
    out += w_int64(0) + w_uint32(4096)
    return bytes(out)


def build_commit_v12(*, tree_sha1, tree_compression):
    out = bytearray()
    out += b"CommitV012"
    out += w_string("test")
    out += w_string("")
    out += w_uint64(0)                    # no parent
    out += w_string(tree_sha1)
    out += w_bool(True)                   # tree stretched
    out += w_int32(tree_compression)      # tree compression
    out += w_string("file:///x")
    out += w_date(datetime(2024, 1, 1, tzinfo=timezone.utc))
    out += w_uint64(0)                    # num_failed_files
    out += w_bool(False)                  # has_missing_nodes
    out += w_bool(True)                   # is_complete
    out += w_data(b"<plist></plist>")
    out += w_string("Arq 5.20.x")
    return bytes(out)


# ---------------------------------------------------------------------------
# Synthetic Arq 5 destination builder
# ---------------------------------------------------------------------------


def write_object(
    dest: Path, computer: str, plaintext: bytes,
    enc_key: bytes, hmac_key: bytes, blob_id_salt: bytes,
    *, compress: bool = True,
) -> str:
    """Write one Arq 5 blob: optionally LZ4-wrap, ARQO-wrap, store at
    ``objects/<2-hex>/<rest-38-hex>``. Returns the SHA-1 (the
    plaintext blob ID using the v3 salt).
    """
    sha1 = arq5_compute_blob_sha1(plaintext, blob_id_salt)
    body = lz4_wrap(plaintext) if compress else plaintext
    arqo = build_encrypted_object(body, enc_key, hmac_key)
    p = dest / computer / "objects" / sha1[:2] / sha1[2:]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(arqo)
    return sha1


def build_synthetic_arq5(
    dest: Path, password: str, source_files: dict,
    *,
    computer: str = "ABCDEFAB-1234-5678-9012-ABCDEFABCDEF",
    folder: str = "11111111-2222-3333-4444-555555555555",
):
    """Materialize an Arq 5/6 destination with a single backup folder
    containing ``source_files`` (a dict of relative-path → bytes).

    Returns ``(computer_uuid, folder_uuid, commit_sha1)`` so tests
    can call ``Arq5Restore.restore`` with the right identifiers.
    """
    enc_key = secrets.token_bytes(32)
    hmac_key = secrets.token_bytes(32)
    blob_id_salt = secrets.token_bytes(32)
    cu_root = dest / computer
    cu_root.mkdir(parents=True, exist_ok=True)
    keyset_blob = build_arq5_keyset_blob(
        password, enc_key, hmac_key, blob_id_salt,
    )
    (cu_root / "encryptionv3.dat").write_bytes(keyset_blob)

    # Arrange files into a flat tree (no nested dirs in this builder).
    nodes = []
    for rel, content in source_files.items():
        sha1 = write_object(
            dest, computer, content, enc_key, hmac_key, blob_id_salt,
        )
        node_bytes = build_node_v22(
            is_tree=False, data_blob_keys=[sha1],
            mode=0o100644, uncompressed_size=len(content),
        )
        nodes.append((rel, node_bytes))

    tree_bytes = build_tree_v22(nodes=nodes, mode=0o755)
    tree_sha1 = write_object(
        dest, computer, tree_bytes, enc_key, hmac_key, blob_id_salt,
    )
    commit_bytes = build_commit_v12(
        tree_sha1=tree_sha1, tree_compression=2,
    )
    commit_sha1 = write_object(
        dest, computer, commit_bytes, enc_key, hmac_key, blob_id_salt,
    )

    ref_path = (
        cu_root / "bucketdata" / folder / "refs" / "heads" / "master"
    )
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text(commit_sha1 + "Y")

    return computer, folder, commit_sha1


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class KeysetRoundTripTests(unittest.TestCase):
    def test_v3_round_trip(self) -> None:
        enc_key = secrets.token_bytes(32)
        hmac_key = secrets.token_bytes(32)
        blob_id_salt = secrets.token_bytes(32)
        blob = build_arq5_keyset_blob(
            "secret-pw", enc_key, hmac_key, blob_id_salt,
        )
        ks = decrypt_arq5_keyset(blob, "secret-pw", "any-uuid")
        self.assertEqual(ks.encryption_version, 3)
        self.assertEqual(ks.encryption_key, enc_key)
        self.assertEqual(ks.hmac_key, hmac_key)
        self.assertEqual(ks.blob_id_salt, blob_id_salt)

    def test_v2_round_trip(self) -> None:
        enc_key = secrets.token_bytes(32)
        hmac_key = secrets.token_bytes(32)
        blob = build_arq5_keyset_blob("pw", enc_key, hmac_key, None)
        ks = decrypt_arq5_keyset(
            blob, "pw", "TEST-COMPUTER-UUID",
        )
        self.assertEqual(ks.encryption_version, 2)
        # v2 salt is the computer UUID padded.
        self.assertTrue(
            ks.blob_id_salt.startswith(b"TEST-COMPUTER-UUID")
        )

    def test_wrong_password_rejected(self) -> None:
        blob = build_arq5_keyset_blob(
            "right",
            secrets.token_bytes(32),
            secrets.token_bytes(32),
            secrets.token_bytes(32),
        )
        with self.assertRaises(CryptoError) as ctx:
            decrypt_arq5_keyset(blob, "WRONG", "any")
        self.assertIn("HMAC", str(ctx.exception))

    def test_bad_header_rejected(self) -> None:
        blob = bytearray(build_arq5_keyset_blob(
            "pw",
            secrets.token_bytes(32),
            secrets.token_bytes(32),
            secrets.token_bytes(32),
        ))
        blob[0:4] = b"NOPE"
        with self.assertRaises(CryptoError):
            decrypt_arq5_keyset(bytes(blob), "pw", "any")


class Arq5RestoreTests(unittest.TestCase):
    def test_round_trip_simple_files(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            dest = td / "backup"
            files = {
                "a.txt": b"alpha\n",
                "b.bin": b"beta payload " * 100,
                "empty": b"",
                "유니코드.md": "유니코드 콘텐츠\n".encode("utf-8"),
            }
            cu, folder, _ = build_synthetic_arq5(dest, "pw", files)

            r = Arq5Restore(dest, "pw")
            out = td / "restored"
            res = r.restore(
                computer_uuid=cu, folder_uuid=folder, dest=out,
            )
            self.assertEqual(res.failures, [])
            self.assertEqual(res.files_restored, 4)
            for rel, content in files.items():
                self.assertEqual(
                    (out / rel).read_bytes(), content,
                    f"mismatch for {rel}",
                )

    def test_list_computers_and_folders(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            cu, folder, _ = build_synthetic_arq5(
                td / "backup", "pw",
                {"x.txt": b"hi"},
            )
            r = Arq5Restore(td / "backup", "pw")
            self.assertEqual(r.list_computers(), [cu])
            self.assertEqual(r.list_folders(cu), [folder])

    def test_wrong_password(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            cu, folder, _ = build_synthetic_arq5(
                td / "backup", "right-pw",
                {"x.txt": b"hi"},
            )
            r = Arq5Restore(td / "backup", "WRONG")
            with self.assertRaises(CryptoError):
                r.restore(
                    computer_uuid=cu, folder_uuid=folder,
                    dest=td / "out",
                )

    def test_missing_blob_lands_in_failures(self) -> None:
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            cu, folder, _ = build_synthetic_arq5(
                td / "backup", "pw",
                {"x.txt": b"abc"},
            )
            # Delete the data object for "x.txt" while keeping the
            # tree blob intact.
            objs_dir = td / "backup" / cu / "objects"
            entries = sorted(p for p in objs_dir.rglob("*") if p.is_file())
            # Three blobs (file content, tree, commit). Delete the
            # smallest — that's the file content.
            entries.sort(key=lambda p: p.stat().st_size)
            entries[0].unlink()
            r = Arq5Restore(td / "backup", "pw")
            res = r.restore(
                computer_uuid=cu, folder_uuid=folder,
                dest=td / "out",
            )
            self.assertGreaterEqual(len(res.failures), 1)


class BlobLookupTests(unittest.TestCase):
    def test_object_paths_known_layouts(self) -> None:
        from arq_reader.arq5_keyset import arq5_object_paths
        paths = arq5_object_paths("CU", "abcd1234" + "0" * 32)
        # First two are the modern sharded paths.
        self.assertIn("/CU/objects/ab/cd1234" + "0" * 32, paths)
        self.assertIn("/CU/objects2/ab/cd1234" + "0" * 32, paths)
        # Legacy unsharded path is also tried.
        self.assertIn("/CU/objects/abcd1234" + "0" * 32, paths)


if __name__ == "__main__":
    unittest.main()
