"""Round-trip tests for binary serializers — writer encodes, reader
parses. The validator's ``verify_encrypted_object_hmac`` already
covers HMAC verification end-to-end via the integration test;
this file focuses on the format-level primitives.
"""

from __future__ import annotations

import secrets
import struct
import unittest

from arq_writer.crypto_write import (
    aes_256_cbc_encrypt,
    build_encrypted_keyset,
    build_encrypted_object,
    compute_blob_id,
)
from arq_writer.serialize import (
    write_blobloc,
    write_bool,
    write_int32,
    write_int64,
    write_node,
    write_string,
    write_tree,
    write_uint32,
    write_uint64,
)
from arq_writer.types import BlobLoc, FileNode, Tree, TreeChild, TreeNode

from arq_validator.crypto import (
    Keyset,
    decrypt_keyset,
    verify_encrypted_object_hmac,
)


class PrimitiveTests(unittest.TestCase):
    def test_bool(self) -> None:
        self.assertEqual(write_bool(True), b"\x01")
        self.assertEqual(write_bool(False), b"\x00")

    def test_uint_int(self) -> None:
        self.assertEqual(write_uint32(0x01020304), b"\x01\x02\x03\x04")
        self.assertEqual(write_int32(-1), b"\xff\xff\xff\xff")
        self.assertEqual(write_uint64(1), b"\x00" * 7 + b"\x01")
        self.assertEqual(write_int64(-2), b"\xff" * 7 + b"\xfe")

    def test_string_null(self) -> None:
        self.assertEqual(write_string(None), b"\x00")

    def test_string_nonempty(self) -> None:
        s = "héllo"
        b = s.encode("utf-8")
        self.assertEqual(
            write_string(s),
            b"\x01" + struct.pack(">Q", len(b)) + b,
        )


class BloblocAndNodeTests(unittest.TestCase):
    def test_blobloc_round_trip_through_validator_constants(self) -> None:
        # We only round-trip the binary shape (no parser on the read
        # side — the validator only HMACs ARQOs, not the inner
        # binary). Assert the byte layout matches the spec ordering.
        loc = BlobLoc(
            blobIdentifier="a" * 64,
            isPacked=False,
            relativePath="/x",
            offset=10,
            length=20,
            stretchEncryptionKey=True,
            compressionType=2,
        )
        b = write_blobloc(loc)
        # First byte of blobIdentifier string is isNotNull=01.
        self.assertEqual(b[0], 0x01)
        # u64 length (8 bytes)
        self.assertEqual(struct.unpack(">Q", b[1:9])[0], 64)
        # Then 64 'a' bytes
        self.assertEqual(b[9:73], b"a" * 64)

    def test_file_node_serializes(self) -> None:
        n = FileNode(
            dataBlobLocs=[BlobLoc(
                blobIdentifier="b" * 64,
                relativePath="/y", length=42,
            )],
            itemSize=42, mtime_sec=1000,
        )
        b = write_node(n)
        # First byte: isTree = 0
        self.assertEqual(b[0], 0x00)
        self.assertGreater(len(b), 100)

    def test_tree_node_serializes(self) -> None:
        loc = BlobLoc(
            blobIdentifier="c" * 64,
            relativePath="/z", length=100,
        )
        n = TreeNode(treeBlobLoc=loc)
        b = write_node(n)
        self.assertEqual(b[0], 0x01)   # isTree = 1

    def test_tree_serializes(self) -> None:
        loc = BlobLoc(blobIdentifier="d" * 64,
                      relativePath="/q", length=1)
        t = Tree(children=[
            TreeChild(name="file1", node=FileNode(
                dataBlobLocs=[loc],
                itemSize=1,
            )),
            TreeChild(name="dir1", node=TreeNode(treeBlobLoc=loc)),
        ])
        b = write_tree(t)
        # u32 version, then u64 count=2
        self.assertEqual(struct.unpack(">I", b[:4])[0], 3)
        self.assertEqual(struct.unpack(">Q", b[4:12])[0], 2)


class CryptoWriteRoundTripTests(unittest.TestCase):
    def test_keyset_round_trips_through_validator(self) -> None:
        password = "correct horse battery staple"
        enc = secrets.token_bytes(32)
        mac = secrets.token_bytes(32)
        salt = secrets.token_bytes(32)
        blob = build_encrypted_keyset(password, enc, mac, salt)
        keyset = decrypt_keyset(blob, password)
        self.assertEqual(keyset.encryption_key, enc)
        self.assertEqual(keyset.hmac_key, mac)
        self.assertEqual(keyset.blob_id_salt, salt)

    def test_arqo_round_trips_through_validator(self) -> None:
        enc = secrets.token_bytes(32)
        mac = secrets.token_bytes(32)
        plaintext = b"hello arq " * 10
        arqo = build_encrypted_object(plaintext, enc, mac)
        ok, _, _ = verify_encrypted_object_hmac(arqo, mac)
        self.assertTrue(ok)

    def test_arqo_wrong_hmac_key_fails_verify(self) -> None:
        enc = secrets.token_bytes(32)
        mac = secrets.token_bytes(32)
        arqo = build_encrypted_object(b"x", enc, mac)
        ok, _, _ = verify_encrypted_object_hmac(arqo, secrets.token_bytes(32))
        self.assertFalse(ok)

    def test_blob_id_deterministic_and_unique(self) -> None:
        salt = b"\x00" * 32
        a = compute_blob_id(salt, b"hello")
        b = compute_blob_id(salt, b"hello")
        c = compute_blob_id(salt, b"world")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 64)

    def test_aes_encrypt_round_trips_via_decrypt(self) -> None:
        from arq_validator.crypto import aes_256_cbc_decrypt
        key = secrets.token_bytes(32)
        iv = secrets.token_bytes(16)
        plaintext = b"hello world. encrypt me." * 7
        ct = aes_256_cbc_encrypt(key, iv, plaintext)
        pt = aes_256_cbc_decrypt(key, iv, ct)
        self.assertEqual(pt, plaintext)


if __name__ == "__main__":
    unittest.main()
