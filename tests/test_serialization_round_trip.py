"""Strategy F: serialization-layer byte equivalence with Arq.app v8.

These regression tests pin the writer's serialize layer so a
``parse_X → write_X`` round-trip of any Arq.app v8-emitted blob
returns the exact input bytes. The full evidence (158 Tree v4
blobs + 18 BackupRecords + 100 xattr blobs against
``/Volumes/arqbackup1`` 2026-05-10, all 276/276 byte-identical)
lives in ``docs/COMPAT-VERIFICATION.md``; what's locked in here
is the **mechanism** that makes those round-trips work, against
synthetic samples that exercise the same code paths without
needing the operator's destination.
"""

from __future__ import annotations

import json
import unittest

from arq_writer.backuprecord import (
    parse_backuprecord, serialize_backuprecord,
)
from arq_writer.serialize import write_tree
from arq_writer.types import BlobLoc, FileNode, Tree, TreeChild, TreeNode
from arq_writer.xattrs import deserialize_xattrs, serialize_xattrs
from arq_reader.parse import parse_tree


class BackupRecordJsonByteEquivalenceTests(unittest.TestCase):
    """Arq.app's BackupRecord JSON encoding has two non-default
    quirks Python's ``json.dumps`` doesn't produce out of the
    box:

    1. **Compact separators** — ``,`` and ``:`` with no trailing
       space. Python defaults to ``', '`` / ``': '``.
    2. **Apple ``/`` escape** — forward slashes in string values
       come out as ``\\/`` (Apple's NSJSONSerialization
       convention). Python emits raw ``/``.

    Strategy F-2 verified 18/18 byte equivalence against
    ``/Volumes/arqbackup1``; these tests pin both quirks.
    """

    def test_compact_separators_no_whitespace(self) -> None:
        rec = {"a": 1, "b": "hello", "c": [1, 2]}
        out = serialize_backuprecord(rec, fmt="json")
        # No "key": value-with-space; no ", " between elements.
        self.assertNotIn(b": ", out)
        self.assertNotIn(b", ", out)
        # Round-trips via stdlib json.
        self.assertEqual(json.loads(out.decode("utf-8")), rec)

    def test_forward_slash_escaped_in_strings(self) -> None:
        # The path-shaped value in this record exercises the
        # ``\/`` escape on every ``/`` character.
        rec = {
            "relativePath": "/2DAC24D1-DA89-46C4-8B26-DE7A4D1DE019/treepacks/01",
        }
        out = serialize_backuprecord(rec, fmt="json")
        # All literal forward slashes in the value should be
        # escaped to ``\/``.
        self.assertNotIn(b'":"/', out)  # would mean unescaped
        self.assertIn(rb'":"\/2DAC24D1-DA89-46C4-8B26-DE7A4D1DE019\/treepacks\/01"', out)

    def test_round_trip_preserves_byte_layout(self) -> None:
        # Build a representative record (mirrors what Arq.app
        # emits for a small backup), serialize, parse, serialize
        # again — second emit must equal the first byte-for-byte.
        from arq_writer.backuprecord import build_backuprecord_dict

        n = FileNode(itemSize=0, mac_st_mode=0o100644)
        rec = build_backuprecord_dict(
            backup_folder_uuid="402790CC-33FA-4BEA-B1FA-186BC8A18007",
            backup_plan_uuid="2DAC24D1-DA89-46C4-8B26-DE7A4D1DE019",
            backup_plan_dict={"name": "p", "active": True},
            root_node=n,
            local_path="/Volumes/source/data",
            local_mount_point="/Volumes/source",
            relative_path="/treepacks",
            creation_date=1735189568.0,
            node_tree_version=4,
        )
        emitted = serialize_backuprecord(rec, fmt="json")
        re_parsed = parse_backuprecord(emitted)
        re_emitted = serialize_backuprecord(re_parsed, fmt="json")
        self.assertEqual(emitted, re_emitted)


class XattrBlobByteEquivalenceTests(unittest.TestCase):
    """Arq.app v8 emits xattr names in the order ``listxattr`` returned
    them — NOT alphabetically sorted. Strategy F-3 verified 100/100
    byte equivalence against ``/Volumes/arqbackup1``; this test pins
    the order-preservation contract.
    """

    def test_dict_insertion_order_is_preserved(self) -> None:
        # Order: provenance first, FinderInfo second.
        # An alphabetic sort would flip them (FinderInfo < provenance).
        xattrs = {
            "com.apple.provenance": b"\x01\x02\x00\x81)\xab\xf0\xb5\x15\x96\xfe",
            "com.apple.FinderInfo": b" " * 32,
        }
        blob = serialize_xattrs(xattrs)
        # Re-parse and confirm the on-disk order matches what we
        # passed in.
        parsed = deserialize_xattrs(blob)
        self.assertEqual(
            list(parsed.keys()),
            ["com.apple.provenance", "com.apple.FinderInfo"],
        )

    def test_round_trip_preserves_byte_layout(self) -> None:
        xattrs = {
            "com.apple.provenance": b"\x01\x02\x00",
            "com.apple.FinderInfo": b"x" * 32,
            "user.checksum": b"deadbeef",
        }
        blob = serialize_xattrs(xattrs)
        re_parsed = deserialize_xattrs(blob)
        re_emitted = serialize_xattrs(re_parsed)
        self.assertEqual(blob, re_emitted)

    def test_empty_xattrs_short_circuit(self) -> None:
        # No magic, no length prefix — caller can use ``b""`` as
        # a sentinel for "no xattr blob attached".
        self.assertEqual(serialize_xattrs({}), b"")


class TreeV4TrailingBlockPreservationTests(unittest.TestCase):
    """The Tree v4 38-byte trailing block has fields the project
    has only partly RE'd — bytes 12..15 carry a per-Node value
    we don't fully understand. The parser preserves the raw 38
    bytes verbatim so the writer can re-emit them on round-trip,
    making byte equivalence robust to whatever bytes 12..15
    encode (Strategy F-1: 158/158 byte-identical against
    ``/Volumes/arqbackup1``).
    """

    def _make_node_with_trailing(self, raw: bytes) -> FileNode:
        """A FileNode with an explicit Tree v4 trailing block."""
        return FileNode(itemSize=0, v4_trailing_block=raw)

    def test_preserves_raw_38_bytes_through_round_trip(self) -> None:
        # The exact 38-byte shape that broke F-1 pre-fix:
        # bytes 12..15 carry a non-zero ``0x0fac`` value, plus a
        # high-bit ``01`` at byte 16 that doesn't match Python's
        # natural int64 BE encoding of ``0x01000000``.
        raw = bytes.fromhex(
            "677d3440000000002c0da41f00000fac"  # bytes 0..15
            "0100000000000000"                  # bytes 16..23
            "0000000000000000000000000000"      # bytes 24..37 (14)
        )
        self.assertEqual(len(raw), 38)
        node = self._make_node_with_trailing(raw)
        from arq_writer.serialize import _v4_trailing_block
        self.assertEqual(_v4_trailing_block(node), raw)

    def test_empty_trailing_block_falls_back_to_synthesis(
        self,
    ) -> None:
        # A node from a fresh writer-side walk has no parsed
        # bytes, so the synthesis path runs. The synthesised form
        # has the documented structure (sec/nsec from create_time
        # + present-flag at byte 16 + 14 reserved zeros) even if
        # bytes 12..15 don't reproduce Arq.app's per-Node value.
        from arq_writer.serialize import _v4_trailing_block
        node = FileNode(
            itemSize=0,
            create_time_sec=0x12345678,
            create_time_nsec=0xABCDEF,
        )
        out = _v4_trailing_block(node)
        self.assertEqual(len(out), 38)
        # bytes 0..7 = create_time_sec int64 BE
        self.assertEqual(
            out[0:8],
            (0x12345678).to_bytes(8, "big", signed=True),
        )
        # bytes 8..15 = create_time_nsec int64 BE
        self.assertEqual(
            out[8:16],
            (0xABCDEF).to_bytes(8, "big", signed=True),
        )
        # bytes 16..23 = present-flag (0x00000000_01000000 BE)
        self.assertEqual(out[16:24], bytes.fromhex(
            "0000000001000000"
        ))
        # bytes 24..37 = 14 reserved zeros
        self.assertEqual(out[24:], b"\x00" * 14)

    def test_full_tree_round_trip_byte_identical(self) -> None:
        # End-to-end through write_tree + parse_tree: emit a
        # tiny Tree v4 with a non-default trailing block, then
        # parse it back and re-emit. Bytes must match.
        raw = bytes.fromhex(
            "0d79fa7300034399"       # sec / nsec extra bytes
            "00fef00000000000"
            "0100000000000000"
            "0000000000000000000000000000"
        )
        self.assertEqual(len(raw), 38)
        node = FileNode(
            itemSize=12,
            mac_st_mode=0o100644,
            v4_trailing_block=raw,
        )
        tree = Tree(
            children=[TreeChild(name="hello.txt", node=node)],
            version=4,
        )
        emitted = write_tree(tree, version=4)
        # Round-trip through parse_tree
        re_parsed = parse_tree(emitted)
        re_emitted = write_tree(re_parsed, version=4)
        self.assertEqual(emitted, re_emitted)
        # And the trailing block survived intact end-to-end.
        self.assertEqual(
            re_parsed.children[0].node.v4_trailing_block, raw,
        )


if __name__ == "__main__":
    unittest.main()
