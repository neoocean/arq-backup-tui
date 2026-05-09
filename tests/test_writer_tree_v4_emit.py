"""Tests for the writer's Tree v4 emit path.

Background — the writer historically emitted Tree v3 only.
Reading v4 was added in PR #20 (the operator's destination is
Arq.app v8 which writes v4); now the writer can also emit v4
when called with ``tree_version=4``. The default stays v3 so
existing destinations + tests aren't perturbed.

These tests pin:

- The 38-byte trailing block has the exact (sec, nsec, flag,
  reserved) shape we observed in real data.
- A v4 round-trip parses back to a Node with the same on-disk
  metadata as a v3 round-trip would (i.e. v4 is purely additive;
  the trailing block doesn't disturb the existing fields).
- The default ``write_tree`` / ``write_node`` still produce v3
  bytes — no accidental opt-in.
"""

from __future__ import annotations

import struct
import unittest

from arq_writer.constants import (
    NODE_V4_TRAILING_BLOCK_BYTES,
    TREE_VERSION,
    TREE_VERSION_V4_TRAILING_BLOCK,
)
from arq_writer.serialize import write_node, write_tree, _v4_trailing_block
from arq_writer.types import (
    BlobLoc, FileNode, Tree, TreeChild, TreeNode,
)


def _file_node(*, sec: int = 1700000000) -> FileNode:
    """Build a minimal FileNode with concrete create_time, so the
    fallback path in :func:`_v4_trailing_block` produces a non-zero
    timestamp the test can assert on."""
    return FileNode(
        dataBlobLocs=[],
        itemSize=42,
        create_time_sec=sec,
        create_time_nsec=123,
    )


class V4TrailingBlockShapeTests(unittest.TestCase):
    """Pin the trailing-block byte layout we discovered in
    real-world Arq.app v8 data."""

    def test_size_is_exactly_38_bytes(self) -> None:
        block = _v4_trailing_block(_file_node())
        self.assertEqual(len(block), NODE_V4_TRAILING_BLOCK_BYTES)
        self.assertEqual(NODE_V4_TRAILING_BLOCK_BYTES, 38)

    def test_layout_matches_observed_structure(self) -> None:
        node = _file_node(sec=0x6880CB0D)
        node.v4_scanned_at_nsec = 0x296095D7  # type: ignore[attr-defined]
        node.v4_scanned_at_sec = 0x6880CB0D   # type: ignore[attr-defined]
        block = _v4_trailing_block(node)
        # bytes 0..7  = int64 BE 0x000000006880CB0D
        sec_bytes = struct.unpack(">q", block[0:8])[0]
        self.assertEqual(sec_bytes, 0x6880CB0D)
        # bytes 8..15 = int64 BE 0x00000000296095D7
        nsec_bytes = struct.unpack(">q", block[8:16])[0]
        self.assertEqual(nsec_bytes, 0x296095D7)
        # bytes 16..23 = int64 BE 0x0000000001000000 (present-flag)
        flag_bytes = struct.unpack(">q", block[16:24])[0]
        self.assertEqual(flag_bytes, 0x01000000)
        # bytes 24..37 = 14 zero bytes
        self.assertEqual(block[24:38], b"\x00" * 14)

    def test_create_time_fallback_when_scanned_at_unset(self) -> None:
        node = _file_node(sec=1700000000)
        block = _v4_trailing_block(node)
        sec_bytes = struct.unpack(">q", block[0:8])[0]
        # Fallback is create_time_sec when v4_scanned_at_sec is 0.
        self.assertEqual(sec_bytes, 1700000000)


class V3DefaultStaysV3Tests(unittest.TestCase):
    """No accidental opt-in: write_node / write_tree default
    must still produce v3 bytes (no trailing 38-byte block)."""

    def test_default_write_node_omits_trailing_block(self) -> None:
        # Compare lengths: a v4 emit is exactly 38 bytes longer than
        # a v3 emit of the same Node.
        node = _file_node()
        v3 = write_node(node)  # default tree_version
        v4 = write_node(
            node, tree_version=TREE_VERSION_V4_TRAILING_BLOCK,
        )
        self.assertEqual(len(v4), len(v3) + 38)
        # Bytes match for the v3 prefix.
        self.assertEqual(v4[: len(v3)], v3)

    def test_default_tree_version_is_3(self) -> None:
        # Spec sanity. If we ever bump TREE_VERSION the writer's
        # round-trip semantics need a separate change; this test
        # alerts before silent flips.
        self.assertEqual(TREE_VERSION, 3)


class V4RoundTripTests(unittest.TestCase):
    """A v4-emitted Tree must parse back through the reader and
    yield the same node metadata."""

    def test_v4_tree_round_trips_through_reader(self) -> None:
        from arq_reader.parse import parse_tree
        node = _file_node(sec=1700000123)
        tree = Tree(
            children=[TreeChild(name="alpha.txt", node=node)],
            version=TREE_VERSION_V4_TRAILING_BLOCK,
        )
        wire = write_tree(
            tree, version=TREE_VERSION_V4_TRAILING_BLOCK,
        )
        parsed = parse_tree(wire)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(len(parsed.children), 1)
        c = parsed.children[0]
        self.assertEqual(c.name, "alpha.txt")
        self.assertEqual(c.node.itemSize, 42)
        self.assertEqual(c.node.create_time_sec, 1700000123)


if __name__ == "__main__":
    unittest.main()
