"""Binary serializers for Arq 7's ``Node`` / ``Tree`` / ``BlobLoc``.

The byte-level conventions come straight from the Arq 7 spec
("Data Format Documentation Conventions" section):

- ``[Bool]``: 1 byte, ``00`` or ``01``.
- ``[String]``: 1-byte ``isNotNull`` flag; if non-null, an 8-byte
  network-byte-order length followed by UTF-8 bytes.
- ``[UInt32]`` / ``[Int32]``: 4 bytes, network byte order.
- ``[UInt64]`` / ``[Int64]``: 8 bytes, network byte order.

Every field referenced by ``Node`` / ``Tree`` / ``BlobLoc`` follows the
same conventions, in the order documented in the spec. Because we're
emitting standalone-object backups, we still emit ``treeBlobLoc`` /
``dataBlobLoc`` records that reference paths under
``standardobjects/`` — the spec's BlobLoc structure doesn't care
whether the target is a pack or standalone, only what the bytes say.
"""

from __future__ import annotations

import struct
from typing import Optional

from .constants import (
    NODE_REPARSE_FIELDS_MIN_TREE_VERSION,
    NODE_V4_TRAILING_BLOCK_BYTES,
    TREE_VERSION,
    TREE_VERSION_V4_TRAILING_BLOCK,
)
from .types import BlobLoc, FileNode, Node, Tree, TreeNode


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def write_bool(value: bool) -> bytes:
    return b"\x01" if value else b"\x00"


def write_uint32(value: int) -> bytes:
    return struct.pack(">I", value & 0xFFFFFFFF)


def write_int32(value: int) -> bytes:
    return struct.pack(">i", value)


def write_uint64(value: int) -> bytes:
    return struct.pack(">Q", value & 0xFFFFFFFFFFFFFFFF)


def write_int64(value: int) -> bytes:
    return struct.pack(">q", value)


def write_string(value: Optional[str]) -> bytes:
    """Encode an optional UTF-8 string per Arq's [String] convention."""
    if value is None:
        return b"\x00"
    encoded = value.encode("utf-8")
    return b"\x01" + struct.pack(">Q", len(encoded)) + encoded


# ---------------------------------------------------------------------------
# BlobLoc
# ---------------------------------------------------------------------------


def write_blobloc(loc: BlobLoc) -> bytes:
    """Serialize one BlobLoc in Arq 7's on-disk binary layout.

    The actual layout (validated against Arq.app-produced trees on
    a real Hetzner Storage Box destination) carries ``isLargePack``
    immediately after ``isPacked``; this byte was missing from our
    earlier writer + reader pair. Round-trip stays correct because
    both ends were symmetric, but Arq.app refused to read what we
    emit. Adding this byte aligns us with Arq.app exactly.
    """
    out = bytearray()
    out += write_string(loc.blobIdentifier)
    out += write_bool(loc.isPacked)
    out += write_bool(loc.isLargePack)
    out += write_string(loc.relativePath)
    out += write_uint64(loc.offset)
    out += write_uint64(loc.length)
    out += write_bool(loc.stretchEncryptionKey)
    out += write_uint32(loc.compressionType)
    return bytes(out)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


def write_node(node: Node, *, tree_version: int = TREE_VERSION) -> bytes:
    """Serialize a ``Node`` (file or directory).

    ``tree_version`` controls whether the win_reparse_* fields are
    appended (Tree v2+ adds them, per the spec).
    """
    out = bytearray()
    is_tree = isinstance(node, TreeNode)
    out += write_bool(is_tree)
    if is_tree:
        assert isinstance(node, TreeNode)
        out += write_blobloc(node.treeBlobLoc)
    out += write_uint32(node.computerOSType)

    data_locs = (
        node.dataBlobLocs if isinstance(node, FileNode) else []
    )
    out += write_uint64(len(data_locs))
    for loc in data_locs:
        out += write_blobloc(loc)

    out += write_bool(node.aclBlobLoc is not None)
    if node.aclBlobLoc is not None:
        out += write_blobloc(node.aclBlobLoc)

    out += write_uint64(len(node.xattrsBlobLocs))
    for xattr in node.xattrsBlobLocs:
        out += write_blobloc(xattr)

    out += write_uint64(node.itemSize)
    out += write_uint64(node.containedFilesCount)
    out += write_int64(node.mtime_sec)
    out += write_int64(node.mtime_nsec)
    out += write_int64(node.ctime_sec)
    out += write_int64(node.ctime_nsec)
    out += write_int64(node.create_time_sec)
    out += write_int64(node.create_time_nsec)
    out += write_string(node.username)
    out += write_string(node.groupName)
    out += write_bool(node.deleted)
    out += write_int32(node.mac_st_dev)
    out += write_uint64(node.mac_st_ino)
    out += write_uint32(node.mac_st_mode)
    out += write_uint32(node.mac_st_nlink)
    out += write_uint32(node.mac_st_uid)
    out += write_uint32(node.mac_st_gid)
    out += write_int32(node.mac_st_rdev)
    out += write_int32(node.mac_st_flags)
    out += write_uint32(node.win_attrs)
    if tree_version >= NODE_REPARSE_FIELDS_MIN_TREE_VERSION:
        out += write_uint32(node.win_reparse_tag)
        out += write_bool(node.win_reparse_point_is_directory)
    if tree_version >= TREE_VERSION_V4_TRAILING_BLOCK:
        out += _v4_trailing_block(node)
    return bytes(out)


def _v4_trailing_block(node: Node) -> bytes:
    """Encode the 38-byte trailing block Tree v4 adds per Node.

    Empirical structure (from sampling 30 real-world nodes — see
    ``docs/REAL-DATA-DISCOVERIES.md`` §7 and
    ``arq_reader/parse.py:parse_node``):

      bytes  0..7  int64 BE  scanned-at sec   (the backup-pass time)
      bytes  8..15 int64 BE  scanned-at nsec
      bytes 16..23 int64 BE  0x00000000_01000000  (present-flag)
      bytes 24..37 14 zero bytes (reserved)

    The all-zero shape only appears for files freshly created in
    the latest pass; for anything we're emitting from a fresh walk
    those conditions essentially never hold, so we always write
    the structured form. The "scanned-at" timestamp is read from
    the writer's ``v4_scanned_at_sec`` / ``v4_scanned_at_nsec``
    attributes if set, else falls back to the node's create_time
    so the output is deterministic in tests.
    """
    sec = int(getattr(node, "v4_scanned_at_sec", 0) or 0)
    nsec = int(getattr(node, "v4_scanned_at_nsec", 0) or 0)
    if sec == 0 and nsec == 0:
        # Fall back to create_time to keep output deterministic
        # without forcing the caller to plumb a scanned-at value
        # through every Node construction.
        sec = int(getattr(node, "create_time_sec", 0) or 0)
        nsec = int(getattr(node, "create_time_nsec", 0) or 0)
    out = bytearray()
    out += write_int64(sec)
    out += write_int64(nsec)
    out += write_int64(0x00000000_01000000)   # present-flag
    out += b"\x00" * 14                        # reserved
    assert len(out) == NODE_V4_TRAILING_BLOCK_BYTES, (
        f"v4 trailing block must be exactly "
        f"{NODE_V4_TRAILING_BLOCK_BYTES} bytes, got {len(out)}"
    )
    return bytes(out)


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------


def write_tree(tree: Tree, *, version: int = TREE_VERSION) -> bytes:
    """Serialize a ``Tree`` (directory metadata + child Nodes by name).

    Tree binary layout (spec):
        [UInt32:version]
        [UInt64:childNodesByNameCount]
        repeated:
            [String:childName]
            [Node:childNode]
    """
    out = bytearray()
    out += write_uint32(version)
    out += write_uint64(len(tree.children))
    for child in tree.children:
        out += write_string(child.name)
        out += write_node(child.node, tree_version=version)
    return bytes(out)
