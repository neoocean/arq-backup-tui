"""Pins the empirical structure of Tree v4's 38-byte trailing block.

Background — see ``scripts/probe_tree_v4_block.py`` and the
``parse_node`` comment in ``arq_reader/parse.py``. We sampled 30
Nodes from a real Hetzner destination and found two shapes:

1. **All zero** — appears for files freshly created in the latest
   backup pass (mtime ≈ ctime ≈ create_sec).
2. **Structured** — bytes 0..7 = int64 BE "scanned-at" sec,
   bytes 8..15 = int64 BE nsec, bytes 16..23 = constant
   ``0x00000000_01000000`` (present-flag), bytes 24..37 = 14 zero
   bytes reserved.

These tests don't decode the block — the reader treats it as
opaque to stay forward-compatible — but they DO assert the parser
correctly consumes both shapes without misaligning subsequent
children. A regression here would break Tree v4 destinations on
the operator's Hetzner box.
"""

from __future__ import annotations

import struct
import unittest

from arq_reader.parse import BinaryReader, parse_node


# Tree v4's pre-trailing portion is the same as v3 (the v4 delta
# is purely the trailing 38 bytes). To exercise just the trailing
# logic we hand-build a minimal Node binary and append both shapes
# of the trailing block, then confirm the parser stops exactly
# at the end of the block (no over- or under-read).
def _build_minimal_v4_file_node_bytes(*, trailing: bytes) -> bytes:
    """Hand-build the binary form of a Tree v4 FileNode + trailing
    block. Mirrors ``arq_writer.serialize.write_file_node`` in
    field order; we don't import the writer because the trailing
    block isn't (yet) a writer concept."""
    out = bytearray()
    # is_tree=False
    out.append(0)
    # computer_os = 1 (macOS)
    out += struct.pack(">I", 1)
    # data_count = 0
    out += struct.pack(">Q", 0)
    # acl_present = False
    out.append(0)
    # xattr_count = 0
    out += struct.pack(">Q", 0)
    # item_size, contained
    out += struct.pack(">QQ", 1024, 0)
    # mtime/ctime/create (sec, nsec) × 3
    out += struct.pack(">qqqqqq", 1, 0, 2, 0, 3, 0)
    # username, groupName — both [String] (1-byte isNotNull + 8-byte
    # length + bytes). Empty string = isNotNull=0x01 + length=0.
    for _ in range(2):
        out.append(1)
        out += struct.pack(">Q", 0)
    # deleted = False
    out.append(0)
    # mac_st_dev (int32) + mac_st_ino (uint64)
    out += struct.pack(">iQ", 0, 0)
    # mac_st_mode (uint32) + mac_st_nlink (uint32) + mac_st_uid + gid
    out += struct.pack(">IIII", 0o100644, 1, 501, 20)
    # mac_st_rdev (int32) + mac_st_flags (int32)
    out += struct.pack(">ii", 0, 0)
    # win_attrs (uint32)
    out += struct.pack(">I", 0)
    # win_reparse_tag + win_reparse_is_dir (Tree v2+)
    out += struct.pack(">I", 0)
    out.append(0)
    # Trailing 38-byte block — this is the v4 addition.
    assert len(trailing) == 38
    out += trailing
    return bytes(out)


class TreeV4TrailingBlockShapesTests(unittest.TestCase):
    """The parser must consume the 38-byte block whether it's
    all-zero or has the observed (timestamp, nsec, present-flag,
    reserved) shape — and either way leave ``pos`` at end-of-buffer
    so the next child reads from the right offset."""

    def test_all_zero_trailing_block_is_consumed(self) -> None:
        node_bytes = _build_minimal_v4_file_node_bytes(
            trailing=b"\x00" * 38,
        )
        r = BinaryReader(node_bytes)
        node = parse_node(r, tree_version=4)
        # Parser must reach exactly EOF — no leftover bytes.
        self.assertEqual(r.remaining(), 0)
        # Structural sanity: all the writer-side metadata round-tripped.
        self.assertEqual(node.itemSize, 1024)
        self.assertEqual(node.mac_st_uid, 501)

    def test_structured_trailing_block_is_consumed(self) -> None:
        # Same shape as the most common observed pattern from real
        # operator data: int64 BE timestamp + int64 BE nsec +
        # constant flag + reserved zeros.
        # Timestamp 0x6880CB0D corresponds to one of the actual
        # samples we captured (~2025-07-23 18:33:17 UTC).
        scanned_at_sec = struct.pack(">q", 0x6880CB0D)
        scanned_at_nsec = struct.pack(">q", 0x296095D7)
        present_flag = struct.pack(">q", 0x0000_0000_0100_0000)
        reserved = b"\x00" * 14
        trailing = scanned_at_sec + scanned_at_nsec + present_flag + reserved
        self.assertEqual(len(trailing), 38)
        node_bytes = _build_minimal_v4_file_node_bytes(trailing=trailing)
        r = BinaryReader(node_bytes)
        node = parse_node(r, tree_version=4)
        self.assertEqual(r.remaining(), 0)
        self.assertEqual(node.itemSize, 1024)

    def test_v3_node_does_not_consume_trailing_block(self) -> None:
        """Sanity: passing tree_version=3 must NOT consume 38
        trailing bytes — that would corrupt v3 destinations
        which never emit the block."""
        node_bytes = _build_minimal_v4_file_node_bytes(
            trailing=b"\x00" * 38,
        )
        # Strip the last 38 bytes so v3-shaped binary has no
        # trailing block at all.
        v3_bytes = node_bytes[:-38]
        r = BinaryReader(v3_bytes)
        node = parse_node(r, tree_version=3)
        self.assertEqual(r.remaining(), 0)
        self.assertEqual(node.itemSize, 1024)


class TreeV4TrailingBlockParsingTwoChildrenTest(unittest.TestCase):
    """The real-world failure mode the trailing-block fix originally
    plugged was the parser misaligning between two consecutive Nodes
    in a v4 Tree. Build a 2-Node Tree binary and confirm both Nodes
    parse back."""

    def test_two_consecutive_v4_nodes_align_correctly(self) -> None:
        from arq_reader.parse import parse_tree
        # Tree binary header: version (uint32) + count (uint64)
        out = bytearray()
        out += struct.pack(">IQ", 4, 2)
        # Node #1: name + minimal v4 FileNode
        for name, trailing in (
            (b"first.txt", b"\x00" * 38),
            (b"second.txt",
             struct.pack(">q", 0x6880CB0D)
             + struct.pack(">q", 0x296095D7)
             + struct.pack(">q", 0x01000000)
             + b"\x00" * 14),
        ):
            # name string: isNotNull=1 + length(uint64 BE) + bytes
            out.append(1)
            out += struct.pack(">Q", len(name))
            out += name
            out += _build_minimal_v4_file_node_bytes(trailing=trailing)

        tree = parse_tree(bytes(out))
        self.assertEqual(tree.version, 4)
        self.assertEqual(len(tree.children), 2)
        self.assertEqual(tree.children[0].name, "first.txt")
        self.assertEqual(tree.children[1].name, "second.txt")


if __name__ == "__main__":
    unittest.main()
