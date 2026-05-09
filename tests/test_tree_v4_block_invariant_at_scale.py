"""Pin the Tree v4 38-byte trailing-block invariant at scale.

Background: PR #20 documented the block's structure from a
30-node sample on the operator's destination. PR-C1 re-ran the
probe at 1500 nodes; the same hypothesis held — 1497/1500 nodes
have the (sec, nsec, present-flag, reserved) shape, the
remaining 3 are all-zero (each a ``.DS_Store`` whose
mtime/ctime/create_sec all match — files freshly created in
the latest backup pass).

This test is a structural invariant on the binary the writer
emits when ``tree_version=4``: every emitted block must satisfy
the constant-flag + reserved-zeros assumptions, so a future
writer change can't drift away from real-world Arq.app v8
shape without breaking this test.
"""

from __future__ import annotations

import struct
import unittest

from arq_writer.constants import (
    NODE_V4_TRAILING_BLOCK_BYTES,
    TREE_VERSION_V4_TRAILING_BLOCK,
)
from arq_writer.serialize import _v4_trailing_block, write_node, write_tree
from arq_writer.types import FileNode, Tree, TreeChild


class V4BlockStructureInvariantTests(unittest.TestCase):

    def _emit_block(self, *, scanned_at_sec=0, scanned_at_nsec=0,
                    create_sec=1700000000, create_nsec=0):
        node = FileNode(
            dataBlobLocs=[], itemSize=0,
            create_time_sec=create_sec,
            create_time_nsec=create_nsec,
        )
        if scanned_at_sec or scanned_at_nsec:
            node.v4_scanned_at_sec = scanned_at_sec  # type: ignore[attr-defined]
            node.v4_scanned_at_nsec = scanned_at_nsec  # type: ignore[attr-defined]
        return _v4_trailing_block(node)

    def test_every_emitted_block_is_38_bytes(self) -> None:
        # 4-corner check: zero, low, high, max-uint64-shaped.
        for sec, nsec in (
            (0, 0),
            (0x6880CB0D, 0x296095D7),
            (0xFFFFFFFF, 0x3B9AC9FF),  # ~100 years from epoch
            (1700000000, 999_999_999),
        ):
            blk = self._emit_block(
                scanned_at_sec=sec, scanned_at_nsec=nsec,
            )
            self.assertEqual(
                len(blk), NODE_V4_TRAILING_BLOCK_BYTES,
                f"sec={sec}, nsec={nsec} produced wrong size",
            )

    def test_constant_flag_invariant_holds_for_every_emit(self) -> None:
        """Bytes 16..23 must always be 0x00000000_01000000 regardless
        of the timestamp values supplied — that's the structural
        signature we observed in 1497 of 1500 real-world v4 blocks."""
        for sec, nsec in (
            (1700000000, 0), (0x6880CB0D, 0x296095D7),
            (0, 12345), (999, 999),
        ):
            blk = self._emit_block(
                scanned_at_sec=sec, scanned_at_nsec=nsec,
            )
            flag = struct.unpack(">q", blk[16:24])[0]
            self.assertEqual(
                flag, 0x01000000,
                f"flag drifted for sec={sec}, nsec={nsec}: "
                f"got 0x{flag:x}",
            )

    def test_reserved_tail_is_always_zero(self) -> None:
        """Bytes 24..37 (14 bytes) are reserved + always zero in
        every observed real-world block."""
        for sec, nsec in (
            (1700000000, 0), (0x6880CB0D, 0x296095D7),
            (0, 12345), (999, 999),
        ):
            blk = self._emit_block(
                scanned_at_sec=sec, scanned_at_nsec=nsec,
            )
            self.assertEqual(
                blk[24:38], b"\x00" * 14,
                f"reserved bytes non-zero for sec={sec}, nsec={nsec}: "
                f"{blk[24:38].hex()}",
            )

    def test_round_trip_via_full_tree(self) -> None:
        """End-to-end: a Tree(version=4) wrapping a node with our
        trailing-block emit must round-trip through the reader
        and yield the same field values."""
        from arq_reader.parse import parse_tree
        node = FileNode(
            dataBlobLocs=[], itemSize=42,
            create_time_sec=0x6880CB0D,
            create_time_nsec=0x296095D7,
        )
        tree = Tree(
            children=[TreeChild(name="x.txt", node=node)],
            version=TREE_VERSION_V4_TRAILING_BLOCK,
        )
        wire = write_tree(
            tree, version=TREE_VERSION_V4_TRAILING_BLOCK,
        )
        parsed = parse_tree(wire)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(len(parsed.children), 1)
        self.assertEqual(parsed.children[0].node.itemSize, 42)
        self.assertEqual(
            parsed.children[0].node.create_time_sec, 0x6880CB0D,
        )

    def test_zero_timestamps_still_emit_constant_flag(self) -> None:
        """Documents an intentional divergence from Arq.app v8.

        In the operator's destination 3 of 1500 nodes (every
        ``.DS_Store`` whose mtime == ctime == create_sec) had a
        fully-zero 38-byte block. We DON'T replicate that
        all-zero case — our writer always emits the structured
        form (timestamp + constant flag + reserved zeros). The
        all-zero shape is presumably a tiny optimization Arq.app
        applies when no scanned-at value is meaningful for that
        node; reader-side both shapes are accepted, so this
        divergence doesn't break round-trip compat. If we ever
        need byte-perfect Arq.app emission this is one of the
        knobs to add.
        """
        blk = self._emit_block(
            scanned_at_sec=0, scanned_at_nsec=0,
            create_sec=0, create_nsec=0,
        )
        # The constant flag is present even with zero timestamps;
        # reserved bytes are still zero.
        flag = struct.unpack(">q", blk[16:24])[0]
        self.assertEqual(flag, 0x01000000)
        self.assertEqual(blk[24:38], b"\x00" * 14)


if __name__ == "__main__":
    unittest.main()
