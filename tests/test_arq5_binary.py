"""Round-trip tests for the Arq 5/6 Tree / Node / Commit / BlobKey
parsers.

We don't have a real Arq 5 backup to test against in this sandbox.
Instead, we synthesize spec-conformant byte streams (matching the
``arq_restore/repo/Tree.m`` / ``Node.m`` / ``Commit.m`` byte
sequences) and assert that our parser recovers the field values
those streams encode. This is the same strategy the validator and
the v0 reader use for synthetic Arq 7 fixtures.
"""

from __future__ import annotations

import struct
import unittest
from datetime import datetime, timezone

from arq_reader.arq5_binary import (
    COMPRESSION_GZIP,
    COMPRESSION_LZ4,
    COMPRESSION_NONE,
    Arq5ParseError,
    BinaryStream,
    BlobKey,
    parse_blobkey,
    parse_commit,
    parse_node,
    parse_tree,
)


# ---------------------------------------------------------------------------
# Encoder helpers — emit spec-conformant primitive binaries
# ---------------------------------------------------------------------------


def w_bool(v: bool) -> bytes:
    return b"\x01" if v else b"\x00"


def w_uint32(v: int) -> bytes:
    return struct.pack(">I", v)


def w_int32(v: int) -> bytes:
    return struct.pack(">i", v)


def w_uint64(v: int) -> bytes:
    return struct.pack(">Q", v)


def w_int64(v: int) -> bytes:
    return struct.pack(">q", v)


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


def w_blobkey(
    *, sha1, tree_version,
    stretch=False, storage_type=1,
    archive_id=None, archive_size=0, archive_uploaded=None,
):
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
# BinaryStream primitives
# ---------------------------------------------------------------------------


class PrimitiveTests(unittest.TestCase):
    def test_bool(self) -> None:
        s = BinaryStream(b"\x01\x00")
        self.assertTrue(s.read_bool())
        self.assertFalse(s.read_bool())

    def test_int_widths(self) -> None:
        b = (
            w_uint32(0xAABBCCDD)
            + w_int32(-1)
            + w_uint64(0x1122334455667788)
            + w_int64(-2)
        )
        s = BinaryStream(b)
        self.assertEqual(s.read_uint32(), 0xAABBCCDD)
        self.assertEqual(s.read_int32(), -1)
        self.assertEqual(s.read_uint64(), 0x1122334455667788)
        self.assertEqual(s.read_int64(), -2)

    def test_string_null_and_value(self) -> None:
        s = BinaryStream(w_string(None) + w_string("hello 안녕"))
        self.assertIsNone(s.read_string())
        self.assertEqual(s.read_string(), "hello 안녕")

    def test_date_roundtrip(self) -> None:
        dt = datetime(2024, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
        s = BinaryStream(w_date(dt) + w_date(None))
        got = s.read_date()
        self.assertEqual(got, dt)
        self.assertIsNone(s.read_date())

    def test_truncation_raises(self) -> None:
        s = BinaryStream(b"\x01\x00\x00\x00")
        with self.assertRaises(Arq5ParseError):
            s.read_uint64()


class BlobKeyTests(unittest.TestCase):
    def test_v22_full_blobkey(self) -> None:
        dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        b = w_blobkey(
            sha1="abcd1234" * 5,
            tree_version=22,
            stretch=True,
            storage_type=2,
            archive_id="arch-1",
            archive_size=999,
            archive_uploaded=dt,
        )
        s = BinaryStream(b)
        bk = parse_blobkey(s, 22, compression_type=COMPRESSION_LZ4)
        self.assertIsNotNone(bk)
        self.assertEqual(bk.sha1, "abcd1234" * 5)
        self.assertTrue(bk.stretchEncryptionKey)
        self.assertEqual(bk.storageType, 2)
        self.assertEqual(bk.archiveId, "arch-1")
        self.assertEqual(bk.archiveSize, 999)
        self.assertEqual(bk.archiveUploadedDate, dt)
        self.assertEqual(bk.compressionType, COMPRESSION_LZ4)

    def test_v12_minimal_blobkey_no_stretch(self) -> None:
        b = w_blobkey(sha1="aa" * 20, tree_version=12)
        s = BinaryStream(b)
        bk = parse_blobkey(s, 12)
        self.assertIsNotNone(bk)
        self.assertEqual(bk.sha1, "aa" * 20)
        self.assertFalse(bk.stretchEncryptionKey)
        self.assertEqual(bk.storageType, 1)
        self.assertIsNone(bk.archiveId)

    def test_null_blobkey(self) -> None:
        b = w_blobkey(sha1=None, tree_version=22)
        s = BinaryStream(b)
        bk = parse_blobkey(s, 22)
        self.assertIsNone(bk)


# ---------------------------------------------------------------------------
# Tree / Node — synthetic Tree v22 builder
# ---------------------------------------------------------------------------


def _build_tree_v22(
    *, nodes, mode=0o755, mtime_sec=0, mtime_nsec=0,
):
    """Build a minimal valid Tree v22 binary (no xattrs/acl, no
    missing nodes). ``nodes`` is a list of (name, node_bytes).
    """
    out = bytearray()
    out += b"TreeV022"
    # v19+ Int32 xattrs/acl compression types.
    out += w_int32(COMPRESSION_NONE)
    out += w_int32(COMPRESSION_NONE)
    # xattrs blob key (null), xattrs size.
    out += w_blobkey(sha1=None, tree_version=22)
    out += w_uint64(0)
    # acl blob key (null).
    out += w_blobkey(sha1=None, tree_version=22)
    # uid, gid, mode.
    out += w_int32(501) + w_int32(20) + w_int32(mode)
    # mtime sec/nsec.
    out += w_int64(mtime_sec) + w_int64(mtime_nsec)
    # flags, finderFlags, extendedFinderFlags.
    out += w_int64(0) + w_int32(0) + w_int32(0)
    # st_dev, st_ino, st_nlink, st_rdev.
    out += w_int32(0) + w_int32(0) + w_uint32(0) + w_int32(0)
    # ctime sec/nsec, st_blocks, st_blksize.
    out += w_int64(0) + w_int64(0) + w_int64(0) + w_uint32(4096)
    # createTime sec/nsec (v15+).
    out += w_int64(0) + w_int64(0)
    # missing_node_count (v18+) = 0.
    out += w_uint32(0)
    # nodes.
    out += w_uint32(len(nodes))
    for name, nbytes in nodes:
        out += w_string(name)
        out += nbytes
    return bytes(out)


def _build_node_v22(
    *, is_tree, data_blob_keys=(), mode=0o100644,
    uncompressed_size=0,
    mtime_sec=0, ctime_sec=0,
):
    """Build a minimal valid Node within a Tree v22."""
    out = bytearray()
    out += w_bool(is_tree)
    # treeContainsMissingItems (v18+).
    out += w_bool(False)
    # Compression types (v19+): data, xattrs, acl.
    out += w_int32(COMPRESSION_LZ4)
    out += w_int32(COMPRESSION_NONE)
    out += w_int32(COMPRESSION_NONE)
    # data blob keys.
    out += w_int32(len(data_blob_keys))
    for sha1 in data_blob_keys:
        out += w_blobkey(sha1=sha1, tree_version=22, stretch=True)
    out += w_uint64(uncompressed_size)
    # NB: thumbnail/preview removed in v18+.
    # xattrs blob key (null) + size.
    out += w_blobkey(sha1=None, tree_version=22)
    out += w_uint64(0)
    # acl blob key (null).
    out += w_blobkey(sha1=None, tree_version=22)
    # uid, gid, mode.
    out += w_int32(501) + w_int32(20) + w_int32(mode)
    # mtime sec/nsec, flags, finderFlags, extendedFinderFlags.
    out += w_int64(mtime_sec) + w_int64(0)
    out += w_int64(0) + w_int32(0) + w_int32(0)
    # finderFileType / finderFileCreator — null.
    out += w_string(None) + w_string(None)
    # isFileExtensionHidden.
    out += w_bool(False)
    # st_dev, st_ino, st_nlink, st_rdev.
    out += w_int32(0) + w_int32(0) + w_uint32(0) + w_int32(0)
    # ctime sec/nsec, createTime sec/nsec.
    out += w_int64(ctime_sec) + w_int64(0)
    out += w_int64(0) + w_int64(0)
    # st_blocks, st_blksize.
    out += w_int64(0) + w_uint32(4096)
    return bytes(out)


class TreeNodeTests(unittest.TestCase):
    def test_empty_v22_tree(self) -> None:
        body = _build_tree_v22(nodes=[])
        t = parse_tree(body)
        self.assertEqual(t.version, 22)
        self.assertEqual(len(t.nodes), 0)
        self.assertEqual(t.xattrsCompressionType, COMPRESSION_NONE)

    def test_tree_with_one_file_node(self) -> None:
        node = _build_node_v22(
            is_tree=False,
            data_blob_keys=["aa" * 20, "bb" * 20],
            uncompressed_size=12345,
            mode=0o100644,
            mtime_sec=1700000000,
        )
        body = _build_tree_v22(nodes=[("hello.txt", node)])
        t = parse_tree(body)
        self.assertIn("hello.txt", t.nodes)
        n = t.nodes["hello.txt"]
        self.assertFalse(n.isTree)
        self.assertEqual(len(n.dataBlobKeys), 2)
        self.assertEqual(n.dataBlobKeys[0].sha1, "aa" * 20)
        self.assertEqual(n.dataBlobKeys[0].compressionType, COMPRESSION_LZ4)
        self.assertEqual(n.uncompressedDataSize, 12345)
        self.assertEqual(n.mode, 0o100644)
        self.assertEqual(n.mtime_sec, 1700000000)

    def test_tree_with_subtree_node(self) -> None:
        sub = _build_node_v22(
            is_tree=True,
            data_blob_keys=["cc" * 20],
            uncompressed_size=0,
            mode=0o040755,
        )
        body = _build_tree_v22(nodes=[("sub", sub)])
        t = parse_tree(body)
        n = t.nodes["sub"]
        self.assertTrue(n.isTree)
        self.assertEqual(n.dataBlobKeys[0].sha1, "cc" * 20)

    def test_bad_header_rejected(self) -> None:
        with self.assertRaises(Arq5ParseError) as ctx:
            parse_tree(b"NOPE0022" + b"\x00" * 1000)
        self.assertIn("header", str(ctx.exception))

    def test_v13_rejected(self) -> None:
        with self.assertRaises(Arq5ParseError):
            parse_tree(b"TreeV013" + b"\x00" * 100)

    def test_invalid_short_input_rejected(self) -> None:
        with self.assertRaises(Arq5ParseError):
            parse_tree(b"Tre")

    def test_unicode_name(self) -> None:
        node = _build_node_v22(
            is_tree=False, data_blob_keys=["dd" * 20], uncompressed_size=1,
        )
        body = _build_tree_v22(nodes=[("유니코드.md", node)])
        t = parse_tree(body)
        self.assertIn("유니코드.md", t.nodes)


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


def _build_commit_v12(
    *, author="me", comment="", parent_sha1=None,
    tree_sha1="ee" * 20, location="file:///tmp/x",
):
    out = bytearray()
    out += b"CommitV012"
    out += w_string(author)
    out += w_string(comment)
    if parent_sha1 is None:
        out += w_uint64(0)
    else:
        out += w_uint64(1)
        out += w_string(parent_sha1)
        out += w_bool(True)               # stretched, v4+
    out += w_string(tree_sha1)
    out += w_bool(True)                   # tree stretched, v4+
    out += w_int32(COMPRESSION_LZ4)       # tree_compression_type, v10+
    out += w_string(location)
    # No merge_common_ancestor — v >= 8 here, range 3-7 only.
    out += w_date(datetime(2024, 6, 1, tzinfo=timezone.utc))
    out += w_uint64(0)                    # num_failed_files
    out += w_bool(False)                  # has_missing_nodes (v8+)
    out += w_bool(True)                   # is_complete (v9+)
    out += w_data(b"<plist></plist>")
    out += w_string("Arq 5.20.x")
    return bytes(out)


class CommitTests(unittest.TestCase):
    def test_v12_no_parent(self) -> None:
        body = _build_commit_v12()
        c = parse_commit(body)
        self.assertEqual(c.version, 12)
        self.assertEqual(c.author, "me")
        self.assertIsNone(c.parentCommitBlobKey)
        self.assertEqual(c.treeBlobKey.sha1, "ee" * 20)
        self.assertEqual(c.treeBlobKey.compressionType, COMPRESSION_LZ4)
        self.assertEqual(c.location, "file:///tmp/x")
        self.assertTrue(c.isComplete)
        self.assertFalse(c.hasMissingNodes)
        self.assertEqual(c.bucketXmlData, b"<plist></plist>")
        self.assertEqual(c.arqVersion, "Arq 5.20.x")

    def test_v12_with_parent(self) -> None:
        body = _build_commit_v12(parent_sha1="ff" * 20)
        c = parse_commit(body)
        self.assertEqual(c.parentCommitBlobKey.sha1, "ff" * 20)

    def test_bad_header_rejected(self) -> None:
        with self.assertRaises(Arq5ParseError):
            parse_commit(b"NoCommit01" + b"\x00" * 200)


if __name__ == "__main__":
    unittest.main()
